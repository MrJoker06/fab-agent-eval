"""单项 RULER 补充入口，由 run_supplementary.ps1 提供 job 配置。"""
from pathlib import Path
from common import ROOT, arguments, execute, save_json

job, check_data = arguments()
from evalscope import TaskConfig
from adapters.ruler import prepare, register, export_official_summary

records, metric, cleaner, task, lengths = prepare(job)
benchmark = register(job, records, metric, cleaner, task, lengths)
config = TaskConfig(
    model=job["model"], api_url=job["api_url"], api_key=job["api_key"], eval_type="openai_api",
    datasets=[benchmark], dataset_args={benchmark: {"dataset_id": job["selection_path"],
                                                 "subset_list": [job["key"].replace("/", "_")]}},
    dataset_dir=str(ROOT / ".cache/ruler/evalscope"), limit=None,
    generation_config={key: value for key, value in job["generation_config"].items() if key != "max_retries"},
    model_args={"max_retries": 0}, eval_batch_size=job["batch_size"],
    judge={"strategy": "rule"}, seed=job["seed"], work_dir=job["output"], no_timestamp=True,
)
save_json(Path(job["output"]) / "selection.json", {
    "selection_path": job["selection_path"], "units": job["selected_units"],
    "input_tokens": lengths, "ctx_size": job["ctx_size"], "model_requests": 0 if check_data else None,
})
execute(config, job, check_data)
if not check_data:
    export_official_summary(job, metric, cleaner, task)
