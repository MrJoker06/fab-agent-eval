"""补充测试公共代码，不导入或修改必要测试 common.py。"""
import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from importlib.metadata import version
from functools import lru_cache
import json
import os
from pathlib import Path
import random
import time
from threading import Lock
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
_audit_lock = Lock()


def append_jsonl(path, value):
    with _audit_lock:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def merge(*items):
    result = {}
    for item in items:
        for key, value in item.items():
            result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


def arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-config", type=Path, required=True)
    parser.add_argument("--check-data", action="store_true")
    args = parser.parse_args()
    job = json.loads(args.job_config.read_text(encoding="utf-8-sig"))
    cache = ROOT / ".cache" / job["benchmark"]
    for key in ("HF_HUB_OFFLINE", "HF_DATASETS_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY"):
        os.environ[key] = "1"
    for key, subdir in (("HF_HOME", "huggingface"), ("HF_HUB_CACHE", "huggingface/hub"),
                        ("HF_DATASETS_CACHE", "huggingface/datasets"), ("EVALSCOPE_CACHE", "evalscope"),
                        ("MODELSCOPE_CACHE", "modelscope")):
        os.environ[key] = str(cache / subdir)
    os.environ["BFCL_PROJECT_ROOT"] = str(Path(job["output"]) / "bfcl_runtime")
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
    if urlparse(job["api_url"]).hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("补充评测仅允许 localhost 模型服务")
    if version("evalscope") != "1.12.0":
        raise ValueError("补充环境需要 EvalScope 1.12.0")
    return job, args.check_data


def pick_indices(size, count, seed, name):
    """层内调用真正的 StratifiedSampler；保留源顺序。"""
    from evalscope.api.dataset import MemoryDataset, Sample
    from evalscope.collections import CollectionSchema, DatasetInfo, StratifiedSampler

    @dataclass
    class LocalPool(DatasetInfo):
        items: list = field(default_factory=list, repr=False)
        def get_data(self):
            return {"default": MemoryDataset(self.items, name=name)}

    if count < 1 or size < 1:
        raise ValueError(f"{name}: 抽样数量与来源数量必须为正")
    items = [Sample(input="", metadata={"row": i}) for i in range(size)]
    state = random.getstate()
    random.seed(seed)
    try:
        schema = CollectionSchema(name=name, datasets=[LocalPool(name=name, items=items)])
        selected = StratifiedSampler(schema).sample(min(count, size))
    finally:
        random.setstate(state)
    return sorted(row["prompt"]["metadata"]["row"] for row in selected)


def freeze(path, value):
    path = Path(path)
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != value:
            raise ValueError(f"固定样本来源已变化，请换 seed 或指定新的样本目录：{path}")
    else:
        save_json(path, value)
    return path


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8-sig").split("\n") if line.strip()]


@lru_cache(maxsize=4)
def local_tokenizer(path):
    from transformers import AutoTokenizer
    if not Path(path).is_dir():
        raise FileNotFoundError(f"完整本地 tokenizer 目录不存在：{path}")
    return AutoTokenizer.from_pretrained(path, local_files_only=True)


def validate_context(job, messages, tools=None):
    tokenizer = local_tokenizer(job["tokenizer"])
    options = job["generation_config"].get("extra_body", {}).get("chat_template_kwargs", {})
    count = len(tokenizer.apply_chat_template(messages, tools=tools, tokenize=True,
                                             add_generation_prompt=True, **options))
    if count + job["generation_config"]["max_tokens"] > job["ctx_size"]:
        raise ValueError(f"上下文不足：实际输入 {count} + 输出 {job['generation_config']['max_tokens']} > ctx {job['ctx_size']}")
    return count


def execute(config, job, check_data):
    from evalscope import run_task
    from evalscope.api.registry import get_benchmark
    started = time.monotonic()
    if check_data:
        adapter = get_benchmark(config.datasets[0], config)
        data = adapter.load_dataset()
        counts = {name: len(rows) for name, rows in data.items()}
        save_json(Path(job["output"]) / "data_check.json", {"loaded": counts, "total": sum(counts.values()), "model_requests": 0})
        print(json.dumps({"loaded": counts, "total": sum(counts.values()), "model_requests": 0}, ensure_ascii=False))
        return
    completed = False
    try:
        run_task(task_cfg=config)
        completed = True
    finally:
        elapsed = time.monotonic() - started
        output = Path(job["output"])
        calls = read_jsonl(output / "api_calls.jsonl") if (output / "api_calls.jsonl").exists() else []
        failures = []
        for file in (output / "predictions").rglob("*.jsonl"):
            for row in read_jsonl(file):
                state = row.get("task_state", {})
                model_output = row.get("model_output", state.get("output", {}))
                choices = model_output.get("choices", [])
                text = choices[0].get("message", {}).get("content", "") if choices else ""
                try:
                    result = json.loads(text)
                except (ValueError, TypeError):
                    result = None
                if row.get("error") or model_output.get("error") or (job["benchmark"] == "bfcl" and isinstance(result, dict) and "error_message" in result):
                    failures.append({"file": str(file), "sample_id": row.get("sample_id", row.get("index"))})
        request_errors = [{"api_error": row["error"]} for row in calls if row.get("error")]
        summary = {"completed": completed, "elapsed_seconds": elapsed, "requests": len(calls),
                   "truncated_requests": sum(row.get("finish_reason") in ("length", "max_tokens", "model_length") for row in calls),
                   "failed_samples": failures, "sample_unit": job["sample_unit"],
                   "request_errors": request_errors,
                   "seconds_per_unit_with_margin": elapsed / max(1, job["selected_units"]) * 1.25,
                   "budget_seconds": job["budget_seconds"], "hard_deadline_enforced": False}
        save_json(output / "budget_estimate.json", summary)
    if failures or request_errors:
        raise RuntimeError(f"异常预测 {len(failures)} 条、异常请求 {len(request_errors)} 次；查看预测与 budget_estimate.json")
