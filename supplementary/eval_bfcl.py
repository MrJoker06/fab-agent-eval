"""BFCL 完整离线补充入口：官方数据、固定抽样、原生 AgentAdapter。"""
from pathlib import Path
from common import ROOT, arguments, execute, save_json, validate_context

job, check_data = arguments()
if job["batch_size"] != 1:
    raise ValueError("当前 BFCL 状态与前置任务基线要求 batch_size=1")
from adapters.bfcl import configure_embedding
configure_embedding(job)

from evalscope import TaskConfig
from adapters.bfcl import prepare, install_handler, register
entries, truth, selected = prepare(job)
install_handler(job)
benchmark = register(job, entries, truth)
config = TaskConfig(
    model=job["model"], api_url=job["api_url"], api_key=job["api_key"], eval_type="openai_api",
    datasets=[benchmark], dataset_args={benchmark: {
        "subset_list": [job["category"]], "extra_params": {
            "is_fc_model": job["category"] != "format_sensitivity",
            "underscore_to_dot": True, "SERPAPI_API_KEY": None,
        }
    }},
    dataset_dir=str(ROOT / ".cache/bfcl/evalscope"), limit=None, eval_batch_size=1,
    generation_config={key: value for key, value in job["generation_config"].items() if key != "max_retries"},
    model_args={"max_retries": 0}, judge={"strategy": "rule"}, seed=job["seed"],
    work_dir=job["output"], no_timestamp=True,
)
# 无请求预检查也确认本地 tokenizer 存在；工具和完整历史在真实请求前逐次核对。
validate_context(job, [{"role": "user", "content": "BFCL offline preflight"}])
save_json(Path(job["output"]) / "selection.json", {
    "selection_path": job["selection_path"], "sample_unit": job["sample_unit"],
    "selected_units": selected, "scored_cases": sum("prereq" not in row["id"] for row in entries),
    "prereq_cases": sum("prereq" in row["id"] for row in entries),
})
execute(config, job, check_data)
