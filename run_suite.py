"""执行 run_all.ps1 配置的整轮评测：依次启动、评测、关闭各模型。"""
import argparse
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen


def merge(*values):
    result = {}
    for value in values:
        for key, item in value.items():
            if isinstance(item, dict) and isinstance(result.get(key), dict):
                result[key] = merge(result[key], item)
            else:
                result[key] = deepcopy(item)
    return result


def resolve_plan(config):
    profile_name = config["profile"]
    profile = config["profiles"][profile_name]
    task_seconds = config["hours"] * 3600 * (1 - config["reserve"]) / config["budget_models"] / config["budget_projects"]
    if task_seconds <= 0:
        raise ValueError("整轮预算必须为正")
    plan = {"profile": profile_name, "hours": config["hours"], "models": [],
            "run_dir": config["run_dir"], "python": config["python"], "eval_root": config["eval_root"]}
    for model in config["models"]:
        server_args = merge(config["server"]["args"], model.get("server_args", {}))
        command = [config["server"]["executable"], "-m", model["gguf"], "--alias", model["name"]]
        for key, value in server_args.items():
            if value is True:
                command.append(key)
            elif value is not False and value is not None:
                command.extend([key, str(value)])
        api_url = f"http://127.0.0.1:{server_args['--port']}/v1"
        item = {"name": model["name"], "gguf": model["gguf"], "command": command,
                "api_url": api_url, "ready_timeout": config["server"]["ready_timeout"], "jobs": []}
        for base_task in config["tasks"]:
            task = merge(base_task, base_task.get("profile_overrides", {}).get(profile_name, {}),
                         model.get("dataset_overrides", {}).get(base_task["name"], {}))
            thinking = False if config.get("smoke") else task["thinking"]
            mode = "thinking" if thinking else "direct"
            max_tokens = 2048 if config.get("smoke") else task["max_tokens"]
            generation = merge(profile["common"], profile["families"][model["family"]][mode],
                               {"seed": config["seed"], "max_tokens": max_tokens},
                               model.get("generation", {}), task.get("generation", {}))
            extra = {"min_p": generation.pop("min_p", 0.0),
                     "chat_template_kwargs": {"enable_thinking": thinking}}
            generation["extra_body"] = merge(extra, generation.get("extra_body", {}))
            seconds = task["seconds_per_sample"]
            if isinstance(seconds, dict):
                seconds = seconds[profile_name]
            if not math.isfinite(seconds) or seconds <= 0:
                raise ValueError("每题估算耗时必须为正")
            count = (5 if config.get("smoke") else config.get("sample_override") or task.get("samples")
                     or math.floor(task_seconds / seconds))
            if count < 1:
                raise ValueError("预算不足一题，请修改耗时估算或总预算")
            name = task["name"]
            output = Path(config["eval_root"]) / "outputs" / profile_name / model["name"] / name / Path(config["run_dir"]).name
            job = {
                "benchmark": name, "model": model["name"], "profile": profile_name,
                "api_url": api_url, "api_key": config.get("api_key", "EMPTY"),
                "data_dir": task.get("data_dir") or str(Path(config["data_root"]) / name),
                "samples": count, "seed": config["seed"], "few_shot_num": task["few_shot_num"],
                "dataset_args": task.get("dataset_args", {}), "generation_config": generation,
                "batch_size": task["batch_size"], "hours": config["hours"],
                "models": config["budget_models"], "projects": config["budget_projects"],
                "seconds_per_sample": seconds, "task_seconds": task_seconds,
                "output": str(output), "script": str(Path(config["eval_root"]) / task["script"]),
            }
            item["jobs"].append(job)
        plan["models"].append(item)
    return plan


def read_api(url):
    with urlopen(url, timeout=2) as response:
        return json.load(response)


def check_model(api_url, name):
    ids = [entry["id"] for entry in read_api(api_url + "/models")["data"]]
    if name not in ids:
        raise RuntimeError(f"服务别名不符：需要 {name}，实际 {ids}")


def start_server(model, log):
    health = model["api_url"].removesuffix("/v1") + "/health"
    try:
        read_api(health)
    except OSError:
        pass
    else:
        raise RuntimeError("端口已有服务；停止它，或用 -ExistingServer 评测单个已启动模型")
    if not Path(model["gguf"]).is_file():
        raise FileNotFoundError(f"请在 run_all.ps1 填写实际 GGUF 路径：{model['gguf']}")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(model["command"], stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
    try:
        deadline = time.monotonic() + model["ready_timeout"]
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"llama-server 启动失败，退出码 {process.returncode}；查看 server.log")
            try:
                if read_api(health).get("status") == "ok":
                    check_model(model["api_url"], model["name"])
                    return process
            except OSError:
                pass
            time.sleep(0.5)
        raise TimeoutError("等待 llama-server 就绪超时")
    except BaseException:
        stop_server(process)
        raise


def stop_server(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def execute_plan(plan, dry_run=False, check_data=False, existing_server=False):
    run_dir = Path(plan["run_dir"])
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        print(f"PLAN {run_dir / 'plan.json'}")
        return 0
    if existing_server and len(plan["models"]) != 1:
        raise ValueError("-ExistingServer 须配合 -Model 指定一个模型")
    status = {"profile": plan["profile"], "jobs": [], "models": [], "passed": False}
    try:
        for model in plan["models"]:
            folder = run_dir / model["name"]
            folder.mkdir(exist_ok=True)
            server = None
            with (folder / "server.log").open("a", encoding="utf-8") as server_log:
                try:
                    if not check_data:
                        if existing_server:
                            check_model(model["api_url"], model["name"])
                        else:
                            print(f"START {model['name']}", flush=True)
                            server = start_server(model, server_log)
                    for job in model["jobs"]:
                        job_file = folder / (job["benchmark"] + ".json")
                        job_file.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
                        command = [plan["python"], job["script"], "--job-config", str(job_file)]
                        if check_data:
                            command.append("--check-data")
                        print(f"RUN {job['model']} / {job['benchmark']}: {job['samples']} 题，temperature={job['generation_config']['temperature']}", flush=True)
                        log_file = folder / (job["benchmark"] + ".log")
                        with log_file.open("w", encoding="utf-8") as log:
                            worker = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
                            try:
                                code = worker.wait()
                            except KeyboardInterrupt:
                                worker.terminate()
                                worker.wait()
                                raise
                        status["jobs"].append({"model": job["model"], "dataset": job["benchmark"],
                                               "exit_code": code, "log": str(log_file), "output": job["output"]})
                        print(f"{'OK' if code == 0 else 'FAILED'} {job['benchmark']}；日志 {log_file}", flush=True)
                    status["models"].append({"model": model["name"], "started": not check_data and not existing_server})
                except (OSError, RuntimeError) as error:
                    print(f"FAILED {model['name']}: {error}", file=sys.stderr, flush=True)
                    status["models"].append({"model": model["name"], "error": str(error)})
                finally:
                    stop_server(server)
                    if server is not None:
                        print(f"STOP {model['name']}", flush=True)
        expected = sum(len(model["jobs"]) for model in plan["models"])
        status["passed"] = (len(status["jobs"]) == expected and expected > 0
                            and all(job["exit_code"] == 0 for job in status["jobs"])
                            and not any("error" in model for model in status["models"]))
    finally:
        (run_dir / "suite_status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"RESULT {run_dir / 'suite_status.json'}", flush=True)
    return 0 if status["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check-data", action="store_true")
    parser.add_argument("--existing-server", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    return execute_plan(resolve_plan(config), args.dry_run, args.check_data, args.existing_server)


if __name__ == "__main__":
    raise SystemExit(main())
