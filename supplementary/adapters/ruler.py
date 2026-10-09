"""读取预生成 RULER，直接调用冻结官方规则，不执行联网评分入口。"""
import ast
import importlib.util
import json
from pathlib import Path
import re
import time

from common import ROOT, freeze, pick_indices, read_jsonl, validate_context, append_jsonl


def official_rules(source):
    import yaml
    source = Path(source)
    constants = source / "scripts/eval/synthetic/constants.py"
    specification = importlib.util.spec_from_file_location("fab_ruler_official_constants", constants)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    tasks = yaml.safe_load((source / "scripts/synthetic.yaml").read_text(encoding="utf-8"))
    # 只提取官方清洗函数，不执行 evaluate.py 的 argparse / nltk.download / NeMo imports。
    tree = ast.parse((source / "scripts/eval/evaluate.py").read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "postprocess_pred")
    namespace = {"re": re}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source / "scripts/eval/evaluate.py"), "exec"), namespace)
    return module.TASKS, tasks, namespace["postprocess_pred"]


def find_source(root, length, task):
    root = Path(root)
    candidates = []
    for folder in (root / str(length), root / f"{length // 1024}k"):
        if folder.is_dir():
            candidates += list(folder.glob(f"**/{task}.jsonl"))
            for split in ("validation", "test", "data"):
                candidates += list(folder.glob(f"**/{task}/{split}.jsonl"))
    candidates = sorted(set(candidates))
    if len(candidates) != 1:
        raise FileNotFoundError(f"需要唯一的 RULER {length}/{task} 文件，找到 {candidates}；支持 <长度>/<任务>.jsonl 或 <长度>/<任务>/validation.jsonl")
    return candidates[0]


def prepare(job):
    import yaml
    manifest = yaml.safe_load(Path(job["manifest"]).read_text(encoding="utf-8"))
    for key in ("ruler_commit", "tokenizer", "lengths", "seed", "tasks"):
        if key not in manifest:
            raise ValueError(f"RULER 生成记录缺少 {key}: {job['manifest']}")
    if job["task"] not in manifest["tasks"] or job["length"] not in manifest["lengths"]:
        raise ValueError("所选任务/长度不在预生成 manifest 中")
    base_tasks, tasks, cleaner = official_rules(job["source_dir"])
    task = tasks[job["task"]]
    metric = base_tasks[task["task"]]["metric_fn"]
    path = find_source(job["data_dir"], job["length"], job["task"])
    rows = read_jsonl(path)
    for number, row in enumerate(rows, 1):
        if not isinstance(row.get("input"), str) or not row.get("outputs") or not all(isinstance(x, str) for x in row["outputs"]):
            raise ValueError(f"RULER input / outputs 字段错误：{path}:{number}")
    indices = pick_indices(len(rows), job["samples"], job["seed"], job["key"])
    selected = [rows[index] for index in indices]
    selected_units = len(selected)
    folder = ROOT / "samples/ruler" / str(job["length"]) / job["task"] / f"n{selected_units}_seed{job['seed']}"
    snapshot = freeze(folder / "sample.json", {"source": str(path.relative_to(Path(job["data_dir"]))), "generation_manifest": manifest,
                                             "source_rows": indices, "records": selected})
    lengths = [validate_context(job, [{"role": "user", "content": row["input"]}]) for row in selected]
    job["selected_units"] = selected_units
    job["selection_path"] = str(snapshot)
    return selected, metric, cleaner, task, lengths


def register(job, records, metric, cleaner, task, lengths):
    from evalscope.api.benchmark import BenchmarkMeta, DefaultDataAdapter
    from evalscope.api.dataset import DatasetDict, Sample, build_dataset_from_records
    from evalscope.api.metric import Score
    from evalscope.api.registry import register_benchmark

    @register_benchmark(BenchmarkMeta(
        name="fab_ruler", pretty_name="RULER", dataset_id=job["selection_path"],
        description="读取预生成 RULER v1 数据，复用冻结官方任务与规则评分。",
        subset_list=[job["key"].replace("/", "_")], metric_list=["acc"],
        few_shot_num=0, prompt_template="{question}",
    ))
    class RulerAdapter(DefaultDataAdapter):
        def load(self):
            return DatasetDict({self.subset_list[0]: build_dataset_from_records(
                records=records, sample_fields=self.record_to_sample, name=self.subset_list[0],
                location=job["selection_path"], limit=None, repeats=1, shuffle=False, seed=job["seed"],
            )}), None

        def record_to_sample(self, record):
            return Sample(input=record["input"], target=json.dumps(record["outputs"], ensure_ascii=False),
                          metadata={"ruler_index": record.get("index"), "outputs": record["outputs"],
                                    "task": job["task"], "length": job["length"],
                                    "source": job["selection_path"]})

        def _on_inference(self, model, sample):
            started = time.monotonic()
            record = {"model": job["model"], "key": job["key"], "generation": job["generation_config"]}
            try:
                result = super()._on_inference(model, sample)
                record.update(usage=result.usage.model_dump() if result.usage else None,
                              finish_reason=result.choices[0].stop_reason if result.choices else None,
                              error=result.error)
                return result
            except Exception as exc:
                record["error"] = str(exc)
                raise
            finally:
                record["elapsed_seconds"] = time.monotonic() - started
                append_jsonl(Path(job["output"]) / "api_calls.jsonl", record)

        def match_score(self, original_prediction, filtered_prediction, reference, task_state):
            cleaned = cleaner(filtered_prediction, task)
            value = metric([cleaned], [json.loads(reference)]) / 100
            return Score(value={"acc": value}, prediction=original_prediction,
                         extracted_prediction=cleaned, metadata={"empty": not bool(cleaned)})

    return "fab_ruler"


def export_official_summary(job, metric, cleaner, task):
    from common import save_json
    groups = []
    for path in (Path(job["output"]) / "predictions").rglob("*.jsonl"):
        for row in read_jsonl(path):
            state = row.get("task_state", {})
            choices = row.get("model_output", state.get("output", {}))["choices"]
            prediction = choices[0]["message"].get("content") or ""
            metadata = row.get("metadata", state.get("sample", {}).get("metadata", {}))
            groups.append((cleaner(prediction, task), metadata["outputs"]))
    if groups:
        save_json(Path(job["output"]) / "ruler_official_score.json", {
            "task": job["task"], "length": job["length"], "count": len(groups),
            "score_percent": metric([x[0] for x in groups], [x[1] for x in groups]),
            "empty": sum(not row[0] for row in groups),
            "ruler_source": job["source_dir"],
        })
