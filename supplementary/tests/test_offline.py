"""离线回归：预算、官方评分、状态依赖、参数转发和原生报告。

需要固定 EvalScope/BFCL 环境；集成检查使用显式本地材料和模拟 localhost API。
"""
import argparse
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import time
import unittest

PARSER = argparse.ArgumentParser()
PARSER.add_argument("--ruler-source", required=True)
PARSER.add_argument("--tokenizer", required=True)
PARSER.add_argument("--embedding", required=True)
PARSER.add_argument("--tests", nargs="+", default=[])
OPTIONS = PARSER.parse_args()
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for KEY in ("HF_HUB_OFFLINE", "HF_DATASETS_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY"):
    os.environ[KEY] = "1"
from common import pick_indices, read_jsonl
from run_suite import resolve_plan
from adapters.ruler import official_rules
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
MODEL = CONFIG["models"][0]["name"]


def options(**changes):
    args = argparse.Namespace(hours=6, profile="official", models=None, datasets=["ruler", "bfcl"],
                              lengths=None, ruler_tasks=None, bfcl_categories=None, samples=0,
                              smoke=False, run_dir="test-plan", python=sys.executable)
    for key, value in changes.items():
        setattr(args, key, value)
    return args


class MockAPI(BaseHTTPRequestHandler):
    requests = []
    response_call = None
    fail = False

    def log_message(self, *args):
        pass

    def respond(self, code, value):
        data = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/v1/models":
            self.respond(200, {"data": [{"id": MODEL}]})
        else:
            self.respond(200, {"status": "ok"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        if self.fail:
            self.respond(500, {"error": {"message": "deliberate mock failure", "type": "server_error"}})
            return
        message = {"role": "assistant", "content": "CODE"}
        reason = "stop"
        if body.get("tools") and self.response_call:
            function, arguments = self.response_call
            name = next((row["function"]["name"] for row in body["tools"] if row["function"]["name"] == function.replace(".", "_")), function)
            message.update(content=None, tool_calls=[{"id": "call_1", "type": "function",
                                                      "function": {"name": name, "arguments": json.dumps(arguments)}}])
            reason = "tool_calls"
        self.respond(200, {"id": "mock", "object": "chat.completion", "created": 0, "model": MODEL,
                          "choices": [{"index": 0, "message": message, "finish_reason": reason}],
                          "usage": {"prompt_tokens": 42, "completion_tokens": 10, "total_tokens": 52}})


class OfflineTests(unittest.TestCase):
    def test_budget_and_filtering(self):
        plan = resolve_plan(deepcopy(CONFIG), options())
        self.assertEqual(len(plan["models"]), 3)
        self.assertEqual(len(plan["models"][0]["jobs"]), 47)
        self.assertTrue(all(value == 0 for value in plan["coverage_shortfall_seconds_per_model"].values()))
        subset = resolve_plan(deepcopy(CONFIG), options(models=[MODEL], datasets=["bfcl"], bfcl_categories=["simple_python"]))
        original = next(job for job in plan["models"][0]["jobs"] if job["key"] == "bfcl/simple_python")
        self.assertEqual(subset["models"][0]["jobs"][0]["samples"], original["samples"])
        self.assertEqual(plan["hours"], 6)
        tiny = resolve_plan(deepcopy(CONFIG), options(hours=0.1))
        self.assertTrue(any(value > 0 for value in tiny["coverage_shortfall_seconds_per_model"].values()))

    def test_web_search_rejected(self):
        config = deepcopy(CONFIG)
        config["bfcl"]["categories"].append("web_search_base")
        with self.assertRaises(ValueError):
            resolve_plan(config, options())

    def test_stratified_sampler_reproducible(self):
        first = pick_indices(100, 9, 42, "test")
        self.assertEqual(first, pick_indices(100, 9, 42, "test"))
        self.assertEqual(len(first), len(set(first)))
        self.assertNotEqual(first, list(range(9)))

    def test_official_ruler_metrics(self):
        base, tasks, cleaner = official_rules(OPTIONS.ruler_source)
        self.assertEqual(len(tasks), 13)
        for name, task in tasks.items():
            rule = base[task["task"]]["metric_fn"]
            self.assertEqual(rule([""], [["ABC", "DEF"]]), 0)
            self.assertEqual(rule(["ABC DEF"], [["ABC", "DEF"]]), 100)
            self.assertEqual(rule(["ABC"], [["ABC", "DEF"]]), 100 if task["task"] == "qa" else 50)
        self.assertEqual(cleaner("\x01 ABC \x02", {}), "ABC")

    def make_job(self, name, **overrides):
        plan = resolve_plan(deepcopy(CONFIG), options(models=[MODEL], samples=1))
        job = deepcopy(next(row for row in plan["models"][0]["jobs"] if row["benchmark"] == name))
        job["tokenizer"] = str(Path(OPTIONS.tokenizer).resolve())
        job["source_dir"] = str(Path(OPTIONS.ruler_source).resolve())
        job["embedding_path"] = str(Path(OPTIONS.embedding).resolve())
        job.update(overrides)
        return job

    def run_worker(self, job, check=False):
        file = Path(job["output"]).parent / (Path(job["output"]).name + ".json")
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(job), encoding="utf-8")
        command = [sys.executable, "-u", str(ROOT / f"eval_{job['benchmark']}.py"), "--job-config", str(file)]
        if check:
            command.append("--check-data")
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}, timeout=240)
        if result.returncode:
            print(result.stdout[-3000:])
            print(result.stderr[-5000:])
        return result

    def test_real_bfcl_all_offline_categories(self):
        with tempfile.TemporaryDirectory(prefix="bfcl-offline-") as temp:
            for category in CONFIG["bfcl"]["categories"]:
                print(f"CHECK DATA {category}", flush=True)
                with self.subTest(category=category):
                    job = self.make_job("bfcl", category=category, key=f"bfcl/{category}",
                                        output=str(Path(temp) / category))
                    result = self.run_worker(job, check=True)
                    self.assertEqual(result.returncode, 0)
                    selection = json.loads((Path(job["output"]) / "selection.json").read_text(encoding="utf-8"))
                    self.assertGreater(selection["scored_cases"], 0)
                    snapshot = json.loads(Path(selection["selection_path"]).read_text(encoding="utf-8"))
                    ids = {entry["id"] for entry in snapshot["entries"]}
                    self.assertTrue(all(set(entry.get("depends_on", [])) <= ids for entry in snapshot["entries"]))
                    if category.startswith("memory_"):
                        self.assertGreater(selection["prereq_cases"], 0)
                    if category == "format_sensitivity":
                        self.assertEqual(selection["scored_cases"], 26)

    def test_mock_native_bfcl_and_ruler_and_failure(self):
        MockAPI.requests = []
        MockAPI.response_call = None
        MockAPI.fail = False
        server = ThreadingHTTPServer(("127.0.0.1", 0), MockAPI)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="supplementary-native-") as temp:
                temp = Path(temp)
                from bfcl_eval.utils import load_dataset_entry, load_ground_truth_entry
                rows = load_dataset_entry("simple_python", include_prereq=False, include_language_specific_hint=False)
                picked = rows[pick_indices(len(rows), 1, 42, "simple_python")[0]]
                truth = next(row for row in load_ground_truth_entry("simple_python") if row["id"] == picked["id"])["ground_truth"]
                expected = truth[0] if isinstance(truth, list) else truth
                name, arguments = next(iter(expected.items()))
                MockAPI.response_call = (name, {key: value[0] if isinstance(value, list) else value for key, value in arguments.items()})
                api = f"http://127.0.0.1:{server.server_port}/v1"
                bfcl = self.make_job("bfcl", category="simple_python", key="bfcl/simple_python", api_url=api, output=str(temp / "bfcl"))
                result = self.run_worker(bfcl)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(len(MockAPI.requests), 1)
                request = MockAPI.requests[0]
                for key, value in {"max_tokens": 2048, "seed": 42, "top_k": 20, "min_p": 0, "temperature": 0.7, "top_p": 0.8}.items():
                    self.assertEqual(request[key], value)
                self.assertFalse(request["chat_template_kwargs"]["enable_thinking"])
                self.assertTrue(list((temp / "bfcl/reports").rglob("*.html")))
                review = read_jsonl(next((temp / "bfcl/reviews").rglob("*.jsonl")))[0]
                self.assertIn("acc", json.dumps(review))
                report = json.loads(next((temp / "bfcl/reports").rglob("fab_bfcl.json")).read_text(encoding="utf-8"))
                self.assertEqual(report["metrics"][0]["score"], 1)
                self.assertNotIn("OVERALL", json.dumps(report))

                # 明确为合成数据，只验证官方评分和报告流程，不作为长上下文成绩。
                data = temp / "ruler_generated"
                (data / "16k").mkdir(parents=True)
                (data / "16k/niah_single_1.jsonl").write_text(json.dumps({"index": 0, "input": "Return CODE", "outputs": ["CODE", "OTHER"], "length": 16384}) + "\n", encoding="utf-8")
                manifest = data / "generation_manifest.yaml"
                manifest.write_text("ruler_commit: fixture\ntokenizer: fixture\nseed: 42\nlengths: [16384]\ntasks: [niah_single_1]\n", encoding="utf-8")
                ruler = self.make_job("ruler", data_dir=str(data), manifest=str(manifest), api_url=api,
                                      output=str(temp / "ruler"), seed=time.time_ns() % (2**31))
                result = self.run_worker(ruler)
                self.assertEqual(result.returncode, 0)
                summary = json.loads((temp / "ruler/ruler_official_score.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["score_percent"], 50)
                self.assertTrue(list((temp / "ruler/reports").rglob("*.html")))
                self.assertEqual(len(MockAPI.requests), 2)

                MockAPI.fail = True
                failed = deepcopy(bfcl)
                failed["output"] = str(temp / "bfcl_failed")
                result = self.run_worker(failed)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(MockAPI.requests), 3, "不得自动重试请求")
                audit = read_jsonl(temp / "bfcl_failed/api_calls.jsonl")
                self.assertTrue(audit[0].get("error"))
                failed_ruler = deepcopy(ruler)
                failed_ruler["output"] = str(temp / "ruler_failed")
                result = self.run_worker(failed_ruler)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(MockAPI.requests), 4, "RULER 也不得自动重试")
                budget = json.loads((temp / "ruler_failed/budget_estimate.json").read_text(encoding="utf-8"))
                self.assertFalse(budget["completed"])
                self.assertEqual(budget["requests"], 1)
                self.assertTrue(budget["request_errors"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_mock_format_and_memory(self):
        MockAPI.requests = []
        MockAPI.response_call = None
        MockAPI.fail = False
        server = ThreadingHTTPServer(("127.0.0.1", 0), MockAPI)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="bfcl-stateful-") as temp:
                for category in ("format_sensitivity", "memory_kv"):
                    job = self.make_job("bfcl", category=category, key=f"bfcl/{category}",
                                        api_url=f"http://127.0.0.1:{server.server_port}/v1",
                                        output=str(Path(temp) / category))
                    result = self.run_worker(job)
                    self.assertEqual(result.returncode, 0)
                    self.assertTrue(list((Path(job["output"]) / "reports").rglob("*.html")))
                    if category == "format_sensitivity":
                        self.assertEqual(len(MockAPI.requests), 26)
                    else:
                        self.assertTrue(list(Path(job["output"]).rglob("*_final.json")))
                        self.assertGreater(len(MockAPI.requests), 26)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *OPTIONS.tests], verbosity=2)
