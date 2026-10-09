"""BFCL 本地官方数据、成组抽样和仅在补充进程生效的请求适配。"""
from copy import deepcopy
from importlib.metadata import version
import json
from pathlib import Path
import time

from common import ROOT, freeze, pick_indices, validate_context, append_jsonl


def configure_embedding(job):
    """在 memory_vector 首次 import 前把官方默认模型名称映射到完整本地目录。"""
    if job["category"] != "memory_vector":
        return
    path = Path(job["embedding_path"])
    if not (path / "modules.json").is_file():
        raise FileNotFoundError(f"MiniLM 完整本地目录缺失：{path}")
    import sentence_transformers
    original = sentence_transformers.SentenceTransformer

    def local_encoder(model_name, *args, **kwargs):
        if model_name in ("all-MiniLM-L6-v2", "sentence-transformers/all-MiniLM-L6-v2"):
            model_name = str(path)
            kwargs.update(local_files_only=True, device="cpu")
        return original(model_name, *args, **kwargs)

    sentence_transformers.SentenceTransformer = local_encoder
    # 预检查实际加载，确认没有依赖隐式 Hub 缓存。
    from bfcl_eval.eval_checker.multi_turn_eval.func_source_code.memory_vector import ENCODER
    if ENCODER.get_sentence_embedding_dimension() != 384:
        raise ValueError("all-MiniLM-L6-v2 应输出 384 维向量")


def prepare(job):
    if version("bfcl-eval") != "2025.10.27.1":
        raise ValueError("需要 bfcl-eval==2025.10.27.1")
    from bfcl_eval.utils import load_dataset_entry, load_ground_truth_entry, is_memory_prereq, is_relevance_or_irrelevance
    category = job["category"]
    if category.startswith("web_search"):
        raise ValueError("完全离线方案不执行 Web Search")
    entries = load_dataset_entry(category, include_prereq=True, include_language_specific_hint=False)
    mains = [row for row in entries if not is_memory_prereq(row["id"])]
    if category.startswith("memory_"):
        units = sorted({row["scenario"] for row in mains})
        selected = [units[i] for i in pick_indices(len(units), job["samples"], job["seed"], category)]
        retained = [row for row in entries if row["scenario"] in selected]
    elif category == "format_sensitivity":
        units = sorted({row["id"].split(":")[-1] for row in mains})
        selected = [units[i] for i in pick_indices(len(units), job["samples"], job["seed"], category)]
        retained = [row for row in entries if row["id"].split(":")[-1] in selected]
        for unit in selected:
            variations = {row["id"].split(":")[1] for row in retained if row["id"].split(":")[-1] == unit}
            if len(variations) != 26:
                raise ValueError(f"格式题 {unit} 缺少 26 个配对变体")
    else:
        units = [row["id"] for row in mains]
        selected = [units[i] for i in pick_indices(len(units), job["samples"], job["seed"], category)]
        retained = [row for row in entries if row["id"] in selected]
    ids = {row["id"] for row in retained}
    for row in retained:
        missing = set(row.get("depends_on", [])) - ids
        if missing:
            raise ValueError(f"BFCL 前置依赖缺失：{row['id']} -> {sorted(missing)}")
    truth = [] if is_relevance_or_irrelevance(category) else load_ground_truth_entry(category)
    from evalscope.benchmarks.bfcl.v4.utils import prepare_ground_truth_map
    truth_map = prepare_ground_truth_map(category, truth)
    for row in retained:
        key = row["id"].split(":")[-1] if category == "format_sensitivity" else row["id"]
        if not is_memory_prereq(row["id"]) and not is_relevance_or_irrelevance(category) and key not in truth_map:
            raise ValueError(f"参考答案缺失：{row['id']}")
    folder = ROOT / "samples/bfcl" / category / f"n{len(selected)}_seed{job['seed']}"
    snapshot = freeze(folder / "sample.json", {
        "bfcl_version": version("bfcl-eval"), "category": category, "selected_units": selected,
        "entries": retained, "ground_truth": truth,
    })
    job["selected_units"], job["selection_path"] = len(selected), str(snapshot)
    return retained, truth, selected


def install_handler(job):
    import httpx
    from bfcl_eval.model_handler.api_inference.openai_completion import OpenAICompletionsHandler
    original_client = OpenAICompletionsHandler._build_client_kwargs
    generation = job["generation_config"]
    output = Path(job["output"])
    output.mkdir(parents=True, exist_ok=True)

    def client_kwargs(self):
        options = original_client(self)
        options.update(max_retries=0, timeout=generation.get("timeout", 1800),
                       http_client=httpx.Client(trust_env=False))
        return options

    def generate(self, **kwargs):
        # 不调用带 RateLimitError 自动重试的装饰器，失败只记一次。
        for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "max_tokens", "seed", "n", "stop", "reasoning_effort"):
            if key in generation:
                kwargs[key] = generation[key]
        kwargs["extra_body"] = generation.get("extra_body", {})
        kwargs["stream"] = False
        messages = [row.model_dump(exclude_none=True) if hasattr(row, "model_dump") else row for row in kwargs["messages"]]
        input_tokens = validate_context(job, messages, kwargs.get("tools"))
        started = time.monotonic()
        record = {"model": self.model_name, "category": job["category"], "input_tokens_checked": input_tokens,
                  "generation": {key: value for key, value in kwargs.items() if key not in ("messages", "tools")}}
        try:
            response = self.client.chat.completions.create(**kwargs)
            record["usage"] = response.usage.model_dump() if response.usage else None
            record["finish_reason"] = response.choices[0].finish_reason
            return response, time.monotonic() - started
        except Exception as exc:
            record["error"] = str(exc)
            raise
        finally:
            record["elapsed_seconds"] = time.monotonic() - started
            append_jsonl(output / "api_calls.jsonl", record)

    OpenAICompletionsHandler._build_client_kwargs = client_kwargs
    OpenAICompletionsHandler.generate_with_backoff = generate


def register(job, entries, truth):
    from evalscope.api.benchmark import BenchmarkMeta
    from evalscope.api.dataset import DatasetDict
    from evalscope.api.metric import Score
    from evalscope.api.registry import register_benchmark
    from evalscope.benchmarks.bfcl.v4.bfcl_v4_adapter import BFCLV4Adapter
    from evalscope.benchmarks.bfcl.v4 import utils
    category = job["category"]

    @register_benchmark(BenchmarkMeta(
        name="fab_bfcl", pretty_name="BFCL-v4 offline", dataset_id=job["selection_path"],
        description="固定 BFCL V4 官方数据、工具流程与评分的离线抽样评测。",
        subset_list=[category], metric_list=["acc"], few_shot_num=0,
        extra_params={
            "is_fc_model": {"type": "bool", "value": category != "format_sensitivity", "description": "FC mode"},
            "underscore_to_dot": {"type": "bool", "value": True, "description": "官方函数名处理"},
            "SERPAPI_API_KEY": {"type": "str | null", "value": None, "description": "不执行联网类别"},
        },
    ))
    class OfflineBFCLAdapter(BFCLV4Adapter):
        def _on_generate_report_end(self, report, output_dir, **kwargs):
            # 父类会添加加权 OVERALL；离线子集只保留实际类别成绩。
            pass

        def load(self):
            # 修复冻结版本格式变体的参考答案 ID 映射，调用官方评分时保持变体格式。
            original_map = utils.prepare_ground_truth_map
            if category == "format_sensitivity":
                def mapped(cat, rows):
                    result = original_map(cat, rows)
                    return {row["id"]: result[row["id"].split(":")[-1]] for row in entries}
                utils.prepare_ground_truth_map = mapped
            try:
                dataset = self._create_dataset_for_category(category, deepcopy(entries), deepcopy(truth))
            finally:
                utils.prepare_ground_truth_map = original_map
            return DatasetDict({category: dataset}), None

        def _prereq_inference(self):
            if self.prereq_finished:
                return
            from bfcl_eval.utils import get_directory_structure_by_id
            # 保留官方数据顺序、全部依赖，串行执行；前置失败不被原生 helper 吞掉。
            for entry in self.prereq_entries:
                result, _ = self.handler.inference(deepcopy(entry), include_input_log=False, exclude_state_log=False)
                folder = self.model_result_dir / get_directory_structure_by_id(entry["id"]) / "memory_snapshot/prereq_checkpoints"
                if not (folder / (entry["id"] + ".json")).is_file():
                    raise RuntimeError(f"Memory 前置快照未生成：{entry['id']}")
            self.prereq_finished = True

        def match_score(self, original_prediction, filtered_prediction, reference, task_state):
            if category != "format_sensitivity":
                return super().match_score(original_prediction, filtered_prediction, reference, task_state)
            self._init_handler()
            from bfcl_eval.constants.enums import Language, ReturnFormat
            from bfcl_eval.eval_checker.eval_runner import _evaluate_single_ast_entry
            from bfcl_eval.model_handler.utils import parse_prompt_variation_params
            prompt = task_state.metadata
            fmt, tag, _, _, _ = parse_prompt_variation_params(prompt["id"].split(":")[1])
            model = utils.DUMMY_MODEL_UNDERSCORE_TO_DOT
            result = _evaluate_single_ast_entry(self.handler, prompt["id"], json.loads(filtered_prediction),
                                                prompt["ground_truth"], prompt, model, category,
                                                language=Language.PYTHON, return_format=ReturnFormat(fmt),
                                                has_tool_call_tag=tag)
            return Score(value={"acc": int(bool(result["valid"]))}, prediction=original_prediction,
                         extracted_prediction=filtered_prediction, metadata=result)

    return "fab_bfcl"
