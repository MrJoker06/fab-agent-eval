"""mmlu_pro：读取统一入口配置，使用原生 EvalScope TaskConfig。"""
from common import arguments, prepare_sample, execute
from evalscope import TaskConfig

args = arguments("mmlu_pro", max_tokens=4096, few_shot_num=5)
data_dir, subsets = prepare_sample("mmlu_pro", args)

task_cfg = TaskConfig(
    model=args.model,
    api_url=args.api_url,
    api_key=args.api_key,
    eval_type="openai_api",
    datasets=["mmlu_pro"],
    dataset_args={
        "mmlu_pro": {
            "few_shot_num": args.few_shot,
            **args.dataset_args,
            "dataset_id": str(data_dir),
            "subset_list": subsets,
        },
    },
    limit=None,  # 已抽取精确的总题数。
    generation_config=args.generation_config,
    model_args={"max_retries": 0},
    eval_batch_size=args.batch_size,
    judge={"strategy": "rule"},
    seed=args.seed,
    work_dir=str(args.output),
    no_timestamp=True,
)
execute(task_cfg, args)
