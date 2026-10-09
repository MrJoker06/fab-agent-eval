"""标准库验证补充服务生命周期、失败清理；只启动临时模拟服务。"""
import argparse
import json
from pathlib import Path
import socket
import sys
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from run_suite import execute_plan, metric_summary


class LifecycleTest(unittest.TestCase):
    def test_combined_keeps_layers_independent(self):
        with tempfile.TemporaryDirectory(prefix="combined-entry-") as temp:
            root = Path(temp)
            (root / "run_combined.ps1").write_text((ROOT.parent / "run_combined.ps1").read_text(encoding="utf-8"), encoding="utf-8")
            parameters = 'param($Profile, $Hours, [string[]]$Model, [string[]]$Datasets, $Config, $PythonExe, [switch]$DryRun, [switch]$CheckData)\n'
            for name, code in (("run_all.ps1", 0), ("run_supplementary.ps1", 7)):
                (root / name).write_text(parameters + '@{profile=$Profile; hours=$Hours; models=$Model; datasets=$Datasets} | ConvertTo-Json -Depth 10 | Set-Content -Encoding utf8 -LiteralPath "$PSScriptRoot/' + name + '.json"\n' + f'exit {code}\n', encoding="utf-8")
            (root / "invoke.ps1").write_text('& "$PSScriptRoot/run_combined.ps1" -RequiredHours 24 -SupplementaryHours 6 -Model @("one", "two") -RequiredDatasets @("math_500", "cmmlu")\nexit $LASTEXITCODE\n', encoding="utf-8")
            result = subprocess.run(["pwsh", "-NoProfile", "-File", str(root / "invoke.ps1")], capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 1)
            folder = next((root / "supplementary/runs").glob("combined_*"))
            status = json.loads((folder / "combined_status.json").read_text(encoding="utf-8-sig"))
            self.assertEqual([row["exit_code"] for row in status["layers"]], [0, 7])
            self.assertEqual(status["layers"][0]["status"], "completed")
            self.assertEqual(status["planned_total_hours"], 30)
            required = json.loads((root / "run_all.ps1.json").read_text(encoding="utf-8-sig"))
            self.assertEqual(required["hours"], 24)
            self.assertEqual(required["models"], ["one", "two"])
            self.assertEqual(required["datasets"], ["math_500", "cmmlu"])

    def test_metric_summary_keeps_missing_tasks_visible(self):
        with tempfile.TemporaryDirectory(prefix="supplementary-summary-") as temp:
            root = Path(temp)
            plan = {"models": [{"name": "model", "jobs": [
                {"benchmark": "ruler", "length": 16384, "task": name} for name in ("one", "two", "three")
            ]}]}
            status = {"jobs": []}
            for task, score in (("one", 100), ("two", 0)):
                folder = root / task
                folder.mkdir()
                (folder / "ruler_official_score.json").write_text(json.dumps({
                    "task": task, "length": 16384, "score_percent": score, "count": 1,
                }))
                status["jobs"].append({"model": "model", "key": f"ruler/16384/{task}", "exit_code": 0, "output": str(folder)})
            summary = metric_summary(plan, status)
            self.assertEqual(summary["ruler"][0]["mean_of_completed_tasks_percent"], 50)
            self.assertEqual(summary["ruler"][0]["missing_tasks"], ["three"])
            self.assertIsNone(summary["official_bfcl_overall"])

    def test_restart_failure_cleanup(self):
        with tempfile.TemporaryDirectory(prefix="supplementary-lifecycle-") as temp:
            temp = Path(temp)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            helper = temp / "mock server.py"
            events = temp / "starts.jsonl"
            helper.write_text(
                "import argparse,json\nfrom http.server import BaseHTTPRequestHandler,ThreadingHTTPServer\n"
                "p=argparse.ArgumentParser();p.add_argument('--model');p.add_argument('--port',type=int);p.add_argument('--ctx');p.add_argument('--events');a=p.parse_args()\n"
                "with open(a.events,'a') as f:f.write(json.dumps({'model':a.model,'ctx':a.ctx})+'\\n')\n"
                "class H(BaseHTTPRequestHandler):\n"
                " def log_message(self,*args):pass\n"
                " def do_GET(self):\n"
                "  value={'data':[{'id':a.model}]} if self.path=='/v1/models' else {'status':'ok'}\n"
                "  b=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)\n"
                "ThreadingHTTPServer.allow_reuse_address=True\n"
                "ThreadingHTTPServer(('127.0.0.1',a.port),H).serve_forever()\n", encoding="utf-8")
            worker = temp / "mock worker.py"
            worker.write_text(
                "import argparse,json\np=argparse.ArgumentParser();p.add_argument('--job-config');p.add_argument('--check-data',action='store_true');a=p.parse_args()\n"
                "j=json.load(open(a.job_config,encoding='utf-8'));raise SystemExit(7 if not a.check_data and j['key']=='bfcl/failure' else 0)\n", encoding="utf-8")
            plan = {"profile": "official", "hours": 6, "run_dir": str(temp / "run"),
                    "python": sys.executable, "coverage_shortfall_seconds_per_model": {"bfcl": 0}, "models": []}
            for model in ("model_one", "model_two"):
                jobs = []
                for key, ctx in (("bfcl/success", "1024"), ("bfcl/failure" if model == "model_one" else "bfcl/second", "2048")):
                    jobs.append({"model": model, "key": key, "benchmark": "bfcl", "samples": 1,
                                 "sample_unit": "single", "ctx_size": int(ctx), "script": str(worker),
                                 "output": str(temp / "outputs" / model / key.replace("/", "_")),
                                 "api_url": f"http://127.0.0.1:{port}/v1", "ready_timeout": 10,
                                 "command": [sys.executable, "-u", str(helper), "--model", model,
                                             "--port", str(port), "--ctx", ctx, "--events", str(events)]})
                plan["models"].append({"name": model, "jobs": jobs})
            args = argparse.Namespace(datasets=["bfcl"], dry_run=False, check_data=False, existing_server=False)
            self.assertEqual(execute_plan(plan, args), 1)
            status = json.loads((temp / "run/suite_status.json").read_text(encoding="utf-8"))
            self.assertEqual([row["exit_code"] for row in status["jobs"]], [0, 7, 0, 0])
            starts = [json.loads(line) for line in events.read_text().splitlines()]
            self.assertEqual(len(starts), 4)
            self.assertEqual([row["ctx"] for row in starts], ["1024", "2048", "1024", "2048"])
            with socket.socket() as sock:
                self.assertNotEqual(sock.connect_ex(("127.0.0.1", port)), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
