"""独立补充调度：RULER / BFCL，标准库可运行 DryRun。"""
import argparse
from datetime import datetime, timezone, timedelta
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler

from common import ROOT, merge, save_json

OFFLINE_CATEGORIES = {
    "simple_python", "simple_java", "simple_javascript", "multiple", "parallel",
    "parallel_multiple", "irrelevance", "live_simple", "live_multiple", "live_parallel",
    "live_parallel_multiple", "live_irrelevance", "live_relevance", "multi_turn_base",
    "multi_turn_miss_func", "multi_turn_miss_param", "multi_turn_long_context",
    "memory_kv", "memory_vector", "memory_rec_sum", "format_sensitivity",
}


def expand(value, eval_root, assets):
    if isinstance(value, dict):
        return {key: expand(item, eval_root, assets) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item, eval_root, assets) for item in value]
    if isinstance(value, str):
        return value.replace("{eval}", str(eval_root)).replace("{assets}", assets)
    return value


def allocate(cells, seconds, override, smoke):
    minimum = sum(cell["seconds_per_sample"] for cell in cells)
    remaining = max(0, seconds - minimum)
    for cell in cells:
        cost = cell["seconds_per_sample"]
        if not math.isfinite(cost) or cost <= 0:
            raise ValueError("分层耗时估计必须为正")
        cell["samples"] = (1 if smoke else override or cell.get("configured_samples")
                           or 1 + math.floor(remaining / len(cells) / cost))
        cell["budget_seconds"] = cost + remaining / len(cells)
    return max(0, minimum - seconds) if not (override or smoke or all(cell.get("configured_samples") for cell in cells)) else 0


def make_cells(config, args):
    cells = []
    ruler = config["ruler"]
    for length in (args.lengths or ruler["lengths"]):
        setting = ruler["length_settings"][str(length)]
        for task in ruler["tasks"]:
            cell = merge({"benchmark": "ruler", "key": f"ruler/{length}/{task}",
                          "task": task, "length": length, "sample_unit": "scene",
                          "configured_samples": ruler["samples_per_cell"]}, setting,
                         ruler.get("cell_overrides", {}).get(f"{length}/{task}", {}))
            cells.append(cell)
    bfcl = config["bfcl"]
    for category in bfcl["categories"]:
        if category not in OFFLINE_CATEGORIES:
            raise ValueError(f"不支持或不是离线 BFCL 类别：{category}")
        cost_key = ("memory_scenario" if category.startswith("memory_") else
                    "multi_turn" if category.startswith("multi_turn_") else
                    "format_base" if category == "format_sensitivity" else "single")
        cells.append(merge({
            "benchmark": "bfcl", "key": f"bfcl/{category}", "category": category,
            "sample_unit": cost_key, "ctx_size": config["server_args"]["--ctx-size"],
            "configured_samples": bfcl["samples_per_category"],
            "seconds_per_sample": bfcl["seconds_per_sample"][cost_key],
        }, bfcl.get("category_overrides", {}).get(category, {})))
    return cells


def select_known(requested, known, label):
    unknown = set(requested or []) - set(known)
    if unknown:
        raise ValueError(f"{label} 未配置：{sorted(unknown)}")


def resolve_plan(config, args):
    cells = make_cells(config, args)
    select_known(args.models, [model["name"] for model in config["models"]], "模型")
    select_known(args.ruler_tasks, config["ruler"]["tasks"], "RULER 任务")
    select_known(args.bfcl_categories, config["bfcl"]["categories"], "BFCL 类别")
    total_weight = config["ruler"]["weight"] + config["bfcl"]["weight"]
    if not math.isfinite(args.hours) or args.hours <= 0 or not 0 <= config["reserve"] < 1 or config["budget_models"] < 1 or total_weight <= 0:
        raise ValueError("总预算、余量、模型数和权重配置无效")
    available = args.hours * 3600 * (1 - config["reserve"]) / config["budget_models"]
    gaps = {}
    for name in ("ruler", "bfcl"):
        group = [cell for cell in cells if cell["benchmark"] == name]
        gaps[name] = allocate(group, available * config[name]["weight"] / total_weight, args.samples, args.smoke)
    plan = {"profile": args.profile, "hours": args.hours, "run_dir": args.run_dir,
            "python": args.python or config["python"], "models": [], "coverage_shortfall_seconds_per_model": gaps,
            "note": "耗时为初始估计；必要测试预算不变。选择部分项目不重新分配其他项目预算。"}
    profile = config["profiles"][args.profile]
    for model in config["models"]:
        if args.models and model["name"] not in args.models:
            continue
        jobs = []
        for cell in cells:
            name = cell["benchmark"]
            if name not in args.datasets:
                continue
            if name == "ruler" and args.ruler_tasks and cell["task"] not in args.ruler_tasks:
                continue
            if name == "bfcl" and args.bfcl_categories and cell["category"] not in args.bfcl_categories:
                continue
            settings = merge(config[name], model.get("dataset_overrides", {}).get(name, {}), cell,
                             model.get("dataset_overrides", {}).get(cell["key"], {}))
            mode = "thinking" if settings["thinking"] and not args.smoke else "direct"
            generation = merge(profile["common"], profile["families"][model["family"]][mode],
                               {"seed": config["seed"]}, model.get("generation", {}), settings["generation"])
            extra = {"chat_template_kwargs": {"enable_thinking": mode == "thinking"}}
            for key in ("top_k", "min_p", "repetition_penalty"):
                if key in generation:
                    extra[key] = generation.pop(key)
            generation["extra_body"] = merge(extra, generation.get("extra_body", {}))
            generation["stream"] = False
            if generation.get("n", 1) != 1 or generation.get("max_retries", 0) != 0 or generation.get("retries", 0) != 0:
                raise ValueError("补充正式基线要求 n=1、max_retries=0")
            server_args = merge(config["server_args"], model.get("server_args", {}), settings.get("server_args", {}),
                                {"--port": config["port"], "--ctx-size": settings["ctx_size"]})
            if server_args.get("--host") != "127.0.0.1":
                raise ValueError("补充服务只绑定 127.0.0.1")
            command = [config["llama_server"], "-m", model["gguf"], "--alias", model["name"]]
            for key, value in server_args.items():
                if value is True:
                    command.append(key)
                elif value is not False and value is not None:
                    command.extend([key, str(value)])
            key = cell["key"].replace("/", "_")
            jobs.append(merge(settings, {
                "model": model["name"], "tokenizer": model["tokenizer"], "profile": args.profile,
                "seed": config["seed"], "api_url": f"http://127.0.0.1:{config['port']}/v1", "api_key": "EMPTY",
                "generation_config": generation,
                "output": str(ROOT / "outputs" / args.profile / model["name"] / key / Path(args.run_dir).name),
                "script": str(ROOT / f"eval_{name}.py"), "command": command,
                "ready_timeout": config["server_ready_timeout"],
            }))
        plan["models"].append({"name": model["name"], "jobs": jobs})
    return plan


def metric_summary(plan, status):
    groups, bfcl = {}, []
    for model in plan["models"]:
        for job in model["jobs"]:
            if job["benchmark"] == "ruler":
                group = groups.setdefault((model["name"], job["length"]), {"planned": [], "scores": {}})
                group["planned"].append(job["task"])
    for row in status["jobs"]:
        if row["exit_code"] != 0:
            continue
        output = Path(row["output"])
        if row["key"].startswith("ruler/"):
            file = output / "ruler_official_score.json"
            if file.exists():
                score = json.loads(file.read_text(encoding="utf-8"))
                groups[(row["model"], score["length"])]["scores"][score["task"]] = score
        else:
            reports = list((output / "reports").rglob("fab_bfcl.json"))
            if reports:
                report = json.loads(reports[0].read_text(encoding="utf-8"))
                bfcl.append({"model": row["model"], "category": row["key"].split("/", 1)[1],
                             "score_percent": report["metrics"][0]["score"] * 100,
                             "count": report["num"], "report": str(output / "reports/report.html")})
    ruler = []
    for (model, length), group in groups.items():
        scores = group["scores"]
        ruler.append({
            "model": model, "length": length, "planned_tasks": group["planned"],
            "completed_tasks": sorted(scores), "missing_tasks": sorted(set(group["planned"]) - set(scores)),
            "mean_of_completed_tasks_percent": sum(row["score_percent"] for row in scores.values()) / len(scores) if scores else None,
            "task_scores": scores,
        })
    return {"ruler": ruler, "bfcl_categories": bfcl, "official_bfcl_overall": None}


def read_api(url):
    with build_opener(ProxyHandler({})).open(url, timeout=3) as response:
        return json.load(response)


def stop_server(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def check_model(job):
    ids = [row["id"] for row in read_api(job["api_url"] + "/models")["data"]]
    if job["model"] not in ids:
        raise RuntimeError(f"服务别名不符：需要 {job['model']}，实际 {ids}")


def start_server(job, log):
    health = job["api_url"].removesuffix("/v1") + "/health"
    try:
        read_api(health)
    except OSError:
        pass
    else:
        raise RuntimeError("端口已有服务，请停止它或用 -ExistingServer 选择单个模型")
    for file in (job["command"][0], job["command"][2]):
        if not Path(file).is_file():
            raise FileNotFoundError(file)
    process = subprocess.Popen(job["command"], stdout=log, stderr=subprocess.STDOUT,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        deadline = time.monotonic() + job["ready_timeout"]
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"服务启动失败：{process.returncode}")
            try:
                if read_api(health).get("status") == "ok":
                    check_model(job)
                    return process
            except OSError:
                pass
            time.sleep(0.5)
        raise TimeoutError("服务启动超时，查看 server.log")
    except BaseException:
        stop_server(process)
        raise


def worker(plan, job, file, log, check=False):
    command = [plan["python"], "-u", job["script"], "--job-config", str(file)]
    if check:
        command.append("--check-data")
    environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, env=environment)
        try:
            return process.wait()
        except BaseException:
            stop_server(process)
            raise


def execute_plan(plan, args):
    run_dir = Path(plan["run_dir"])
    save_json(run_dir / "plan.json", plan)
    print(f"PLAN {run_dir / 'plan.json'}", flush=True)
    for model in plan["models"]:
        for job in model["jobs"]:
            print(f"{model['name']} / {job['key']}: {job['samples']} {job['sample_unit']}, ctx={job['ctx_size']}", flush=True)
    gaps = {name: gap for name, gap in plan["coverage_shortfall_seconds_per_model"].items()
            if name in args.datasets and gap > 0}
    if args.dry_run:
        if gaps:
            print(f"预算覆盖缺口（每模型秒）：{gaps}；调整时长/耗时估计后再正式执行")
        return 0
    if gaps:
        raise ValueError(f"补充预算不足以覆盖全部配置：每模型缺少 {gaps} 秒；先用 DryRun 核对")
    if args.existing_server and len(plan["models"]) != 1:
        raise ValueError("-ExistingServer 必须选择一个模型")
    status = {"profile": plan["profile"], "hours": plan["hours"], "jobs": [], "passed": False}
    try:
        for model in plan["models"]:
            folder = run_dir / model["name"]
            folder.mkdir(parents=True, exist_ok=True)
            server, active_command = None, None
            with (folder / "server.log").open("w", encoding="utf-8") as server_log:
                try:
                    for job in model["jobs"]:
                        key = job["key"].replace("/", "_")
                        file, log = folder / f"{key}.json", folder / f"{key}.log"
                        save_json(file, job)
                        # 材料缺失在加载 GGUF 前报告；CheckData 永不启动服务。
                        code = worker(plan, job, file, log if args.check_data else folder / f"{key}_preflight.log", True)
                        if code != 0 and not args.check_data:
                            log = folder / f"{key}_preflight.log"
                        error = None
                        if code == 0 and not args.check_data:
                            try:
                                if args.existing_server:
                                    check_model(job)
                                elif active_command != job["command"]:
                                    stop_server(server)
                                    server = None
                                    active_command = None
                                    server = start_server(job, server_log)
                                    active_command = job["command"]
                                code = worker(plan, job, file, log)
                            except (OSError, RuntimeError) as exc:
                                code, error = 1, str(exc)
                                stop_server(server)
                                server, active_command = None, None
                        status["jobs"].append({"model": job["model"], "key": job["key"], "exit_code": code,
                                               "output": job["output"], "log": str(log), "error": error})
                        save_json(run_dir / "suite_status.json", status)
                        print(f"{'OK' if code == 0 else 'FAILED'} {job['key']} / {job['model']}", flush=True)
                finally:
                    stop_server(server)
        expected = sum(len(model["jobs"]) for model in plan["models"])
        status["passed"] = expected > 0 and len(status["jobs"]) == expected and all(row["exit_code"] == 0 for row in status["jobs"])
    finally:
        save_json(run_dir / "suite_status.json", status)
        metrics = metric_summary(plan, status)
        save_json(run_dir / "metrics_summary.json", metrics)
        rows = ["# 补充测试运行结果", "", "| 模型 | 子任务 | 状态 | 输出 |", "| --- | --- | --- | --- |"]
        rows += [f"| {row['model']} | {row['key']} | {'通过' if row['exit_code'] == 0 else '失败'} | {row['output']} |" for row in status["jobs"]]
        if metrics["ruler"]:
            rows += ["", "## RULER 按长度汇总", "", "| 模型 | 长度 | 完成/配置任务 | 已完成任务平均分（%） |", "| --- | --- | --- | --- |"]
            for row in metrics["ruler"]:
                mean = row["mean_of_completed_tasks_percent"]
                display = f"{mean:.2f}" if mean is not None else "未评分"
                rows.append(f"| {row['model']} | {row['length']} | {len(row['completed_tasks'])}/{len(row['planned_tasks'])} | {display} |")
        (run_dir / "summary.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"RESULT {run_dir / 'suite_status.json'}", flush=True)
    return 0 if status["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--hours", type=float, required=True)
    parser.add_argument("--profile", choices=["official", "fab_agent"], default="official")
    parser.add_argument("--python")
    parser.add_argument("--models", nargs="+")
    parser.add_argument("--datasets", nargs="+", choices=["ruler", "bfcl"], default=["ruler", "bfcl"])
    parser.add_argument("--lengths", nargs="+", type=int)
    parser.add_argument("--ruler-tasks", nargs="+")
    parser.add_argument("--bfcl-categories", nargs="+")
    parser.add_argument("--samples", type=int, default=0)
    parser.add_argument("--smoke", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--check-data", action="store_true")
    parser.add_argument("--existing-server", action="store_true")
    parser.add_argument("--run-dir")
    args = parser.parse_args()
    if args.samples < 0:
        parser.error("samples 必须非负")
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S_%f")
    args.run_dir = args.run_dir or str(ROOT / "runs" / f"{stamp}_{args.profile}")
    raw = json.loads(args.config.read_text(encoding="utf-8-sig"))
    config = expand(raw, ROOT.parent, raw["assets_root"])
    return execute_plan(resolve_plan(config, args), args)


if __name__ == "__main__":
    raise SystemExit(main())
