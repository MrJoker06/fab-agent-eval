"""共享参数和 StratifiedSampler；各 eval 脚本独立定义 TaskConfig。"""
import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
import json
import math
import os
from pathlib import Path
import random
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent / "laptop_lab/datasets/evalscope_full"
SPLITS = {"math_500": "test", "mmlu_pro": "test", "ifeval": "train", "cmmlu": "test", "ceval": "val"}
# 规划假设，不是实测：正式比较前请替换为三个模型中最慢的秒/题。
SECONDS = {"math_500": 180, "mmlu_pro": 90, "ifeval": 15, "cmmlu": 45, "ceval": 45}
for key in ("HF_HUB_OFFLINE", "HF_DATASETS_OFFLINE", "TRANSFORMERS_OFFLINE"):
    os.environ[key] = "1"
os.environ.setdefault("HF_DATASETS_CACHE", str(ROOT.parent / "laptop_lab/.d"))


def arguments(name, max_tokens, few_shot_num=0):
    parser = argparse.ArgumentParser(description=f"{name} 分层抽样评测")
    parser.add_argument("--job-config", type=Path, help="由 run_all.ps1 生成的完整单项配置")
    parser.add_argument("--model", help="单独执行时填已启动服务的模型别名")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--api-key", default="EMPTY")
    parser.add_argument("--data-dir", type=Path, default=SOURCE / name)
    parser.add_argument("--samples", type=int, help="总题数，覆盖预算自动计算")
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--models", type=int, default=3)
    parser.add_argument("--projects", type=int, default=5)
    parser.add_argument("--seconds-per-sample", type=float, default=SECONDS[name])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-tokens", type=int, default=max_tokens)
    parser.add_argument("--few-shot", type=int, default=few_shot_num)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--min-p", type=float, default=0.0)
    parser.add_argument("--presence-penalty", type=float, default=0.0)
    parser.add_argument("--repetition-penalty", type=float, default=1.0)
    parser.add_argument("--thinking", choices=["on", "off"], default="on")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--non-stream", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="冻结样本、打印 TaskConfig，不请求模型")
    mode.add_argument("--check-data", action="store_true", help="冻结样本、检查本地加载，不请求模型")
    args = parser.parse_args()
    args.profile = "manual"
    args.dataset_args = {}
    args.api_url = f"http://127.0.0.1:{args.port}/v1"
    args.generation_config = {
        "temperature": args.temperature, "top_p": args.top_p, "top_k": args.top_k,
        "presence_penalty": args.presence_penalty, "repetition_penalty": args.repetition_penalty,
        "max_tokens": args.max_tokens, "n": 1, "seed": args.seed,
        "stream": not args.non_stream, "timeout": 1800, "retries": 0,
        "extra_body": {"min_p": args.min_p, "chat_template_kwargs": {"enable_thinking": args.thinking == "on"}},
    }
    job = {}
    if args.job_config:
        job = json.loads(args.job_config.read_text(encoding="utf-8-sig"))
        if job["benchmark"] != name:
            parser.error("job-config 的数据集与当前脚本不符")
        for key in ("model", "api_url", "api_key", "samples", "seed", "hours", "models",
                    "projects", "seconds_per_sample", "batch_size", "generation_config", "profile", "dataset_args"):
            if key in job:
                setattr(args, key, job[key])
        args.data_dir = Path(job["data_dir"])
        args.few_shot = job["few_shot_num"]
        args.max_tokens = args.generation_config["max_tokens"]
    if not args.model:
        parser.error("请指定 --model 或 --job-config")
    if (not math.isfinite(args.hours) or args.hours <= 0 or args.models < 1 or args.projects < 1
            or not math.isfinite(args.seconds_per_sample) or args.seconds_per_sample <= 0):
        parser.error("预算、模型数、项目数和每题耗时必须为正")
    args.task_seconds = job.get("task_seconds", args.hours * 3600 * 0.8 / args.models / args.projects)
    args.samples = args.samples if args.samples is not None else math.floor(args.task_seconds / args.seconds_per_sample)
    if args.samples < 1 or args.max_tokens < 1 or args.batch_size < 1:
        parser.error("题数、输出预算和并发数必须为正")
    model_folder = args.model.replace("/", "_").replace("\\", "_")
    stamp = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y%m%d_%H%M%S_%f")
    args.output = Path(job["output"]) if job else ROOT / "outputs" / "manual" / model_folder / name / stamp
    return args


def prepare_sample(name, args):
    from evalscope.api.dataset import MemoryDataset, Sample
    from evalscope.collections import CollectionSchema, DatasetInfo, StratifiedSampler
    split = SPLITS[name]
    files = sorted(args.data_dir.glob(f"*/{split}.jsonl"))
    if not files:
        raise FileNotFoundError(f"请准备本地导出数据：{args.data_dir}/<subset>/{split}.jsonl")
    rows, groups = [], defaultdict(list)
    for path in files:
        for line in path.read_text(encoding="utf-8-sig").split("\n"):
            if not line.strip():
                continue
            record = json.loads(line)
            if name == "math_500":
                group = f"Level {record['level']}"
            elif name in {"mmlu_pro", "cmmlu"}:
                group = record["category"].lower()
            elif name == "ifeval":
                group = (record.get("instruction_id_list") or ["unknown"])[0]
            else:
                group = path.parent.name
            index = len(rows)
            rows.append((path.parent.name, record))
            groups[group].append(Sample(input="", metadata={"row": index}))
    count = min(args.samples, len(rows))
    # 题数足够时每层至少一题，其余按容量比例和最大余数法分配。
    base = int(count >= len(groups))
    capacity = {key: len(values) - base for key, values in groups.items()}
    remaining = count - base * len(groups)
    total = sum(capacity.values())
    quotas = {key: base + (remaining * size // total if total else 0) for key, size in capacity.items()}
    order = sorted(capacity, key=lambda key: (-(remaining * capacity[key] % total) if total else 0, key))
    for key in order[:count - sum(quotas.values())]:
        quotas[key] += 1

    @dataclass
    class LocalStratum(DatasetInfo):
        samples: list = field(default_factory=list, repr=False)
        subset: str = ""

        def get_data(self):
            return {self.subset: MemoryDataset(self.samples, name=name)}

    state = random.getstate()
    random.seed(args.seed)
    selected = []
    try:
        for key, values in groups.items():
            if quotas[key]:
                schema = CollectionSchema(name=name, datasets=[
                    LocalStratum(name=name, samples=values, subset=key)
                ])
                selected.extend(StratifiedSampler(schema).sample(quotas[key]))
    finally:
        random.setstate(state)
    indices = sorted(entry["prompt"]["metadata"]["row"] for entry in selected)
    target = ROOT / "samples" / name / f"n{count}_seed{args.seed}"
    payloads = defaultdict(list)
    for index in indices:
        subset, record = rows[index]
        payloads[f"{subset}/{split}.jsonl"].append(record)
    if name == "mmlu_pro" or (args.few_shot > 0 and name in {"cmmlu", "ceval"}):
        example_split = "validation" if name == "mmlu_pro" else "dev"
        for path in args.data_dir.glob(f"*/{example_split}.jsonl"):
            payloads[f"{path.parent.name}/{example_split}.jsonl"] = [
                json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()
            ]
    configs = defaultdict(list)
    for relative, records in payloads.items():
        path = target / relative
        text = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
        if path.exists() and path.read_text(encoding="utf-8") != text:
            raise ValueError(f"已有固定样本不同，请换 seed：{path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(text, encoding="utf-8")
        subset, file_name = relative.split("/")
        configs[subset].append((Path(file_name).stem, relative))
    yaml = "---\nconfigs:\n"
    for subset, splits in configs.items():
        yaml += f"- config_name: {subset}\n  data_files:\n"
        for split_name, relative in splits:
            yaml += f"  - split: {split_name}\n    path: {relative}\n"
    (target / "README.md").write_text(yaml + "---\n", encoding="utf-8")
    selection = {"benchmark": name, "seed": args.seed, "source_count": len(rows), "sample_count": count,
                 "source_strata": {key: len(value) for key, value in groups.items()},
                 "sample_strata": quotas, "source_row_indices": indices}
    (target / "selection.json").write_text(json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8")
    args.samples = count
    args.sample_directory = target
    subsets = ["default"] if name == "ifeval" else sorted(key for key, amount in quotas.items() if amount)
    print(f"{name}: {count}/{len(rows)} 题；样本 {target}")
    print("分层题数:", {key: amount for key, amount in quotas.items() if amount})
    print(f"整轮 {args.hours:g}h / {args.models} 模型 / {args.projects} 项目，预留 20%；本项预算 {args.task_seconds / 3600:.2f}h")
    return target, subsets


def execute(config, args):
    from evalscope import run_task
    if args.dry_run:
        print(config.model_dump_json(indent=2))
        return
    if args.check_data:
        from evalscope.api.registry import get_benchmark
        data = get_benchmark(config.datasets[0], config).load_dataset()
        counts = {key: len(value) for key, value in data.items()}
        print(json.dumps({"loaded": counts, "total": sum(counts.values()), "model_requests": 0}, ensure_ascii=False))
        assert sum(counts.values()) == args.samples
        return
    started = time.monotonic()
    run_task(task_cfg=config)
    elapsed = time.monotonic() - started
    seconds = elapsed / args.samples * 1.25
    result = {"profile": args.profile, "suite_hours": args.hours, "models": args.models, "projects": args.projects,
              "samples": args.samples, "elapsed_seconds": elapsed, "estimated_seconds_per_sample": seconds,
              "recommended_samples": min(json.loads((args.sample_directory / "selection.json").read_text(encoding="utf-8"))["source_count"], math.floor(args.task_seconds / seconds)),
              "hard_deadline_enforced": False}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "budget_estimate.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (args.output / "selection.json").write_text((args.sample_directory / "selection.json").read_text(encoding="utf-8"), encoding="utf-8")
    print(f"RESULT {args.output}")
    print(f"保守估计 {seconds:.1f} 秒/题；预算建议 {result['recommended_samples']} 题。")
