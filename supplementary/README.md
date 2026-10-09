# 完全离线的 RULER / BFCL 补充测试

必要测试仍由原 run_all.ps1 执行。本目录不导入或修改必要测试的 common.py、run_suite.py、五个评测脚本或环境。新增入口是 run_supplementary.ps1 和 run_combined.ps1，命令均从独立 eval 仓库根目录的 PowerShell 7 执行；在原 deployment 根目录执行时加 ./eval/ 前缀。

## 环境与本地材料

正式机器不在线下载。先导入匹配 Windows x64 / Python 3.12 的完整 wheels，再为补充测试创建独立环境：

```powershell
py -3.12 -m venv .venv-supplementary
./.venv-supplementary/Scripts/python.exe -m pip install --no-index `
  --find-links "D:/fab_insight/wheels" `
  -r ./supplementary/requirements-lock.txt
```

requirements-lock.txt 来自已有 Windows 评测环境的版本记录，本轮使用该环境验证脚本；它包含完整依赖，材料包应对应这个 lock。另一个环境已有固定依赖时可用 -PythonExe 指向其 Python，不在运行时安装或升级依赖。不要在必要测试的 venv 内追加补充依赖。

需有以下本地目录：

- 三个候选 GGUF 和 llama-server.exe；没有实际文件也能 DryRun。
- 对应 Qwen3-14B / Qwen3.8-27B tokenizer 完整目录；后两种量化共用基础模型 tokenizer。
- 固定版本 RULER 源码：scripts/synthetic.yaml、scripts/eval/synthetic/constants.py、scripts/eval/evaluate.py，以及许可证。
- RULER 预生成样本和 generation_manifest.yaml。
- memory_vector 使用的 all-MiniLM-L6-v2 完整目录，包含 modules.json、Pooling 配置、tokenizer 和权重。
- bfcl-eval==2025.10.27.1 安装包自带题目、参考答案、工具说明及记忆材料；检查时从包内读取，不需要将它们再转为 JSONL。

BFCL 仅允许 20 个离线类别和 format_sensitivity；两个 Web Search 类别被拒绝。MiniLM 在记忆模块导入前映射到显式本地目录，CPU 加载并核对 384 维输出。所有评测进程设置 Hugging Face 离线变量，API 只允许 localhost 地址，SDK 不继承 HTTP 代理。

## 配置位置

所有补充配置位于 **supplementary/config.json**，支持 -Config 指定另一个 JSON：

| 配置 | 含义 |
| --- | --- |
| assets_root | 导入材料根目录，默认 D:/fab_insight |
| python / llama_server | 补充 Python 与服务路径 |
| models | 三个模型的 GGUF、tokenizer、家族、模型级参数 |
| profiles | 独立 official / fab_agent 参数定义 |
| ruler.data_dir / source_dir / manifest | 预生成题库、官方代码及生成记录 |
| ruler.tasks / lengths | 完整 13 任务，默认正式长度 16K/32K |
| ruler.length_settings | 各长度 ctx_size、秒/场景估计 |
| ruler.samples_per_cell | 每个任务 × 长度的数量；null 按预算规划 |
| ruler.cell_overrides | 按 16384/niah_single_1 等键覆盖参数；configured_samples 可指定该层题数 |
| bfcl.embedding_path | MiniLM 完整本地目录 |
| bfcl.categories | 离线类别和格式敏感性列表 |
| bfcl.samples_per_category | 独立题目/会话数、Memory 场景数或格式基础题数 |
| bfcl.category_overrides | 每类别的 generation、ctx_size、configured_samples 等 |
| ruler.weight / bfcl.weight | 两项在补充预算内的分配权重，初值 0.4 / 0.6 |
| server_args | 仅用于补充服务的启动参数 |

路径支持 {assets} 和 {eval} 占位符，{eval} 是仓库根目录。配置内的秒/样本、上下文和输出预算为初始假设，不是 A10 实测结果；默认 32K RULER 计划使用 ctx 40960，BFCL long_context 使用 ctx 65536，必须先确认目标模型显存容量，不能保证三个模型都能容纳。

项目级 generation 可以覆盖温度、top_p、max_tokens 等；模型 dataset_overrides 可按 ruler、bfcl 或 ruler/16384/vt、bfcl/simple_python 覆盖。top_k、min_p、repetition_penalty 和 chat_template_kwargs 通过 extra_body 发送给 llama.cpp。正式基线 n=1、stream=False、max_retries=0。

## RULER 题库布局

支持以下两种官方样本导出布局，每个任务和长度必须唯一：

```text
ruler_generated/
├── 16k/niah_single_1.jsonl
├── 32k/niah_single_1.jsonl
└── generation_manifest.yaml

# 或
ruler_generated/16384/niah_single_1/validation.jsonl
```

同一长度内可有官方生成器附加的目录层级，脚本递归寻找任务文件。不要同时保留同任务的多个 test/validation 文件。记录需包含 input 字符串和非空 outputs 字符串列表；index、length 等字段原样保留。

manifest 为 YAML，至少包含：

```yaml
ruler_commit: <准备区冻结的官方commit>
tokenizer: tokenizers/qwen38_27b
lengths: [8192, 16384, 32768]
seed: 42
tasks: [niah_single_1, niah_single_2, niah_single_3, niah_multikey_1,
        niah_multikey_2, niah_multikey_3, niah_multivalue, niah_multiquery,
        vt, cwe, fwe, qa_1, qa_2]
```

提前生成时也保留样本数量、模板和其余实际配置。隔离区不执行生成器，不下载 NLTK / NeMo / SQuAD / HotpotQA。评分直接加载冻结源码的指标函数与清洗函数，避免执行旧 evaluate.py 的导入副作用。

三个模型使用相同题目，分别用其本地 tokenizer 检查完整请求长度；报告记录实际输入 token 数。不同 tokenizer 的题目长度可能不同，不以目录名代替实际长度。超过配置上下文时失败，不截断。

## 运行顺序

```powershell
# 只生成计划，不导入 EvalScope、不读取数据、不加载 GGUF。
./run_supplementary.ps1 -Profile official -Hours 6 -DryRun

# 实际读取两道官方单轮题；不启动模型、不发送请求。
./run_supplementary.ps1 -Hours 6 -Model qwen3_14b_q5_k_m `
  -Datasets bfcl -BfclCategories simple_python -Samples 2 -CheckData

# 已手动启动服务时，先用最小单轮范围验证工具调用。
./run_supplementary.ps1 -Hours 6 -Model qwen3_14b_q5_k_m `
  -Datasets bfcl -BfclCategories simple_python -Samples 2 -ExistingServer

# 验证预生成 8K 数据；-Lengths 需在 length_settings 中有相应配置。
./run_supplementary.ps1 -Hours 6 -Datasets ruler `
  -Lengths 8192 -RulerTasks niah_single_1 -Samples 2 -CheckData

# 正式补充轮：由脚本管理服务，按需要切换上下文并重启。
./run_supplementary.ps1 -Profile official -Hours 6

# 一次执行两层：三个模型的必要测试 24h + 补充测试 6h。
./run_combined.ps1 -Profile official -RequiredHours 24 -SupplementaryHours 6
```

6h 只是示例，不是补充层的默认值；Hours / SupplementaryHours 必须明确指定。组合入口也支持 -DryRun、-CheckData、-Model、-RequiredDatasets、-SupplementaryDatasets、-SupplementaryConfig、-SupplementaryPython。必要层仍通过原参数调用 run_all.ps1，补充脚本绝不改变其配置。

必要层使用它已配置的 Python，组合命令的 -SupplementaryPython 只影响补充层。组合脚本使用独立 PowerShell 子进程，先完整执行必要层，再执行补充层；不会同时加载多个 GGUF。必要层和补充层各自的退出码、日志和报告位置写入组合汇总。

正式补充任务先无模型检查材料，再启动服务。相邻任务的服务参数相同时复用补充服务，参数变化时结束自己的服务再重启。ExistingServer 只允许单个模型，脚本不停止它；服务上下文应与计划匹配，需由用户配置。所有长请求另有本地 token 容量检查，实际服务若容量更小仍可能拒绝请求。

## 抽样单位和预算

-Samples N 是**每个选中子任务的单位数**，不是全部项目合计 N 题：

- RULER：每个任务 × 长度 N 个场景。
- BFCL 单轮 / Live：每类别 N 道题。
- BFCL 多轮：每类别 N 个完整会话。
- BFCL Memory：每后端 N 个完整场景，保留该场景全部题目和前置依赖，数量上限为来源场景数。
- BFCL 格式敏感性：N 个基础题，每题展开全部 26 个变体。

层内使用 EvalScope StratifiedSampler，固定 seed，保存官方 ID 或原始行号。正式比较不要对三个模型单独更改抽样数。相同来源、数量和 seed 复用相同样本，来源变化时不会覆盖旧样本；选择新的 seed 或另一个样本目录。

未显式设题数时，先为全部配置的子任务分配至少一个单位，再按补充预算规划更多样本。预算不足时 DryRun 给出覆盖缺口，正式运行停止并要求调整时长、范围配置或耗时假设。显式 -Samples 或配置数量可覆盖规划，会使实际耗时超过预算；所有预算都是规划，当前没有硬性终止计时器。

-Model 和项目/任务筛选保留完整三模型和已配置补充项目的预算基线，避免补跑改变题数；-Lengths 改变的是 RULER 正式配置范围。-Smoke 为每层一个单位，仍不是官方全量成绩。

BFCL 多轮和 Memory 的一次样本可能触发多次 API 请求，格式变体也会放大数量。运行记录包含真实请求数、token 用量、截断和耗时，budget_estimate.json 给出加余量的实测秒/单位。用目标三个模型最慢耗时修订配置后再正式运行。

## 输出和成绩含义

```text
supplementary/
├── samples/<项目>/.../sample.json       # 固定原始题目、ID、来源
├── runs/<运行ID>/plan.json
├── runs/<运行ID>/suite_status.json
├── runs/<运行ID>/summary.md
├── runs/<运行ID>/metrics_summary.json   # 按长度的任务平均分及覆盖情况
├── runs/combined_<运行ID>/combined_status.json
└── outputs/<profile>/<模型>/<子任务>/<运行ID>/
    ├── selection.json
    ├── api_calls.jsonl
    ├── predictions/
    ├── reviews/
    ├── reports/
    ├── budget_estimate.json
    └── ruler_official_score.json        # RULER 官方批量评分
```

RULER 原生报告 acc 为 0–1 的逐题得分均值；ruler_official_score.json 额外调用官方批量指标得到百分制结果，官方批量舍入可能与逐题均值末位不同，以该文件为官方任务分数。metrics_summary.json 按模型和长度汇总任务分数、平均分及未完成任务，不把部分完成的平均分当作完整覆盖。

BFCL 各类别及格式敏感性独立报告。Memory 状态放在本模型、本子任务、本轮输出下，不沿用另一模型状态。请求参数、前置推理也进入 api_calls.jsonl；SDK 错误不重试，原生框架即使把异常包装成预测，也会使补充任务最终返回失败。

流程完成不意味着题目全部答对。补充失败不覆盖必要成绩；组合汇总明确每层的状态，不把未完成写成通过。不输出缺少 Web Search 的官方全量 BFCL Overall，不把两层分数平均成一个总分。

## 本地验证与边界

回归脚本使用真实包内 BFCL 数据、显式本地 MiniLM 和 tokenizer，RULER 使用合成输入及冻结官方评分。模拟 API 不测试候选模型能力：

```powershell
./.venv-supplementary/Scripts/python.exe ./supplementary/tests/test_offline.py `
  --ruler-source "D:/fab_insight/frameworks/RULER" `
  --tokenizer "D:/fab_insight/tokenizers/qwen3_14b" `
  --embedding "D:/fab_insight/models/all-MiniLM-L6-v2"
```

目标机器还需准备正式 RULER 数据、对应 tokenizer 和三个真实 GGUF，完成 A10 显存、上下文、接口及耗时校准。本地流程验证不能保证 16K/32K、长多轮或指定小时预算在目标模型上实际可运行。

BFCL 依赖与接口参考：[EvalScope BFCL V4](https://evalscope.readthedocs.io/en/latest/third_party/bfcl_v4.html)。该链接供准备区查阅，正式运行不访问。
