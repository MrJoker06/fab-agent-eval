# RULER 与 BFCL 离线补充测试接入方案

修订日期：2026-10-09。

本文件记录已确认的实施方案。现已提供 run_supplementary.ps1、run_combined.ps1 及 supplementary/ 下的独立脚本，配置和使用方法见 [补充测试 README](supplementary/README.md)。正式环境为 Windows / NVIDIA A10 24GB / RAM 64GB，不能连接外网；评测只读取本地材料并请求 localhost 模型服务。正式 RULER 样本与三个候选 GGUF 仍需在目标机器配置，并完成 A10 校准。

参考项目根目录的两份《下载补救修订版》PDF。能力域定义参考 PDF，测试优先级、预算和实现边界以本次讨论确认的约定为准。

## 1. 两层测试与必要测试冻结

| 层次 | 项目 | 执行方式 | 时间预算 |
| --- | --- | --- | --- |
| 必要测试 | MATH-500、MMLU-Pro、IFEval、CMMLU、C-Eval | 沿用现有入口 | 三个模型合计默认 24h，保留现有配置 |
| 补充测试 | RULER、BFCL V4 | 单独选择运行 | 自行指定，包含三个模型，不占必要测试预算 |

official / fab_agent 表示参数轮，与两层执行优先级独立。两轮分别规划耗时和保存成绩，不自动共用一个 24h 预算。

**必要测试已冻结：**

- 不修改 run_all.ps1、run_suite.py、common.py 和现有五个评测 Python 脚本。
- 不改变其模型参数、数据路径、题数、抽样逻辑、缓存路径、结果目录及接口。
- 不为补充测试升级或覆盖必要测试的 Python 环境与依赖。
- 组合运行仅通过现有命令行调用必要测试入口，不向其内部加入新任务，不改成七项共享预算。
- 只有确认补充测试无法独立实现、存在不可避免的依赖时，才向用户提出改动需求。必须说明原因、已排除的独立实现方式、最小改动和影响，经讨论确认后才修改。

## 2. 正式机器完全离线

正式机器不执行在线 pip、git、模型下载、数据下载或外部服务调用。资源提前完成准备并受控导入；缺少材料时报告具体本地路径，不回退到网络。

| 材料 | 正式机器的使用方式 |
| --- | --- |
| EvalScope、bfcl-eval 和全部依赖 | 从匹配目标 Windows / Python 的本地 wheels 安装 |
| BFCL 题目、答案与工具材料 | 使用固定 bfcl-eval 包内资产，核对所选范围是否完整 |
| all-MiniLM-L6-v2 | 从完整本地模型目录加载，CPU 上运行，供 memory_vector 使用 |
| RULER 样本与评分 | 读取预生成的 8K/16K/32K 数据及冻结的本地评分代码 |
| Qwen tokenizer / chat template | 使用本地目录，核对实际 token 数，禁止 Hub 回退 |
| llama.cpp、GGUF、Python | 使用已准备的固定版本 |

补充环境以 EvalScope 1.12.0、bfcl-eval 2025.10.27.1 为初始兼容基线。PyTorch、sentence-transformers、FAISS 等依赖以实际离线验证后的 requirements-lock 为准；Windows 与 Linux wheels 不能混用。

获取缺少的资源只发生在可联网准备区或另一台准备工作站，不是正式机器运行步骤。已有完整材料直接沿用，不要求重新下载。

### MiniLM 的本地加载

已准备模型为 sentence-transformers/all-MiniLM-L6-v2。导入完整模型目录，包含权重、tokenizer、modules.json、Pooling 配置等加载所需文件，不能只复制单个权重文件。

BFCL memory_vector 默认通过模型名称调用 SentenceTransformer。补充侧必须提供实际生效的本地路径加载适配，或采用已通过断网验证的完整缓存布局；只写一个配置路径、未接到加载代码，不视为完成。

加载配置在记忆模块首次导入前生效。HF_HUB_OFFLINE、HF_DATASETS_OFFLINE、TRANSFORMERS_OFFLINE 配合显式本地路径或 local_files_only 使用。离线变量不代替材料准备和断网检查。不修改安装包源码，也不改变必要测试的缓存设置。

### 来源与记录

记录实际来源、版本或准备日期、模型规格、本地路径、任务配置、tokenizer 和 seed。SHA-256 按 PDF 要求保持可选，仅在公司流程独立要求时执行。不引入强制全量 Hash、自动换源或数据规范化框架；下载缓存不直接作为正式 release 目录。

## 3. RULER：提前生成，离线运行

采用经典 RULER v1 的 13 个任务，冻结官方 commit、任务配置、预测清洗和评分方法；其他 RULER 版本单独记录，不混用成绩。

- 单针检索：niah_single_1、niah_single_2、niah_single_3。
- 多键检索：niah_multikey_1、niah_multikey_2、niah_multikey_3。
- 多值、多查询：niah_multivalue、niah_multiquery。
- 变量追踪：vt。
- 信息聚合：cwe、fwe。
- 问答：qa_1、qa_2。

### 样本准备与长度

按资源清单提前准备 8K、16K、32K 数据。正式长上下文比较重点为 16K/32K；8K 用于较短长度对照或流程检查，4K/8K 成绩不能替代 16K/32K。Ridge 64K 为可选后续补测，不参与三个模型公共长度的直接比较。

```text
fab_insight/datasets/ruler_generated/
├── 8k/
├── 16k/
├── 32k/
├── 64k/                       # 可选，Ridge 后续补测
└── generation_manifest.yaml
```

准备区按冻结任务的真实依赖准备语料、tokenizer 和生成环境；可能涉及 Paul Graham 文本、SQuAD、HotpotQA、NLTK 资源。生成记录至少包含 commit、任务列表、长度、样本数、seed、tokenizer 版本/路径、模板及配置。

保留 input、outputs、index、length 等官方字段。隔离区只读取、抽样、推理和评分，不把生成器的联网依赖留到正式运行时处理。需要新增未准备的长度或任务时，由准备区补齐后重新导入。

Qwen3.8 两个量化版本使用对应基础模型 tokenizer，Qwen3-14B 使用其 tokenizer。三个模型对照使用相同题目内容，实际 token 数分别记录；不同 tokenizer 生成的不同题库不能直接当作配对题目。

### 抽样与评分

- 按任务 × 配置长度分层，层内调用 StratifiedSampler，固定 seed 和样本 ID。
- 全部 13 任务的 16K/32K 至少需要 26 个场景；包含 8K/16K/32K 则至少 39 个。每层一题只表示覆盖，不代表分数稳定。
- 层数量按实际启用的任务和长度计算，三个模型复用同一公共样本选择。
- 抽查实际 token 长度，核对输入、模板与预留输出能放入上下文；不按文件名推断，不静默截断。
- 沿用官方各任务 metric 和清洗逻辑，不统一改成简单 exact-match，不使用在线 LLM 裁判。
- 报告任务 × 长度成绩、每长度任务平均分、样本数、实际 token 数、空回答和截断情况；无法运行的长度明确标记未完成。

## 4. BFCL V4：使用包内材料与官方流程

BFCL 安装包提供题目、参考答案、工具环境和评分程序，一般不需要在正式机器额外下载题库。所选版本及其包内材料须提前核对。

| 范围 | 执行方式 |
| --- | --- |
| 20 个离线类别 | 支持单轮、Live、相关性、多轮与三种记忆后端，按配置抽样 |
| format_sensitivity | 支持冻结版本全部 26 个 Prompt 变体，基础题数独立配置并单独报告 |
| web_search_base / web_search_no_snippet | 排除，不提供联网开关，不要求 SERPAPI_API_KEY |

live_relevance 不计入官方 Overall。缺少 Web Search 或使用抽样时，不报告为官方完整 BFCL Overall；保留类别成绩和实际覆盖范围。

原生适配器通过 bfcl_eval 函数加载材料，普通 dataset_id 不能直接指定任意 JSONL。补充侧将固定 ID 的选择接到加载流程，复用官方工具执行和评分，不修改包内题目或必要测试代码。

### 抽样与状态

- 单轮、Live：按类别分层，层内使用 StratifiedSampler，保留官方 ID 和参考答案。
- 多轮：选择完整会话，保留所有轮次、工具定义和初始状态。
- Memory：选择完整场景及其依赖，保留初始化与前置推理；状态相关部分串行执行。每个模型、每轮运行独立保存记忆，不复用另一模型生成的状态。
- format_sensitivity：先选择基础题，再执行全部 26 个变体，保持题目配对，不在展开记录中任意抽样。

计划分别列出题数、会话数、记忆场景数、格式基础题数和预计请求数；执行后记录真实请求数及耗时。多轮和初始化请求计入补充预算。

### 接口与参数

单轮、多轮、记忆采用 FC 模式，需要真实可解析的 tool_calls，并正确处理 tool 消息与会话历史。格式敏感性采用官方 Prompt 模式；接口失败不自动切换模式并沿用同一成绩名称。

BFCL handler 使用自己的请求路径，不能假设 TaskConfig 自动转发全部参数。补充侧验证并接入 max_tokens、seed、采样参数、Thinking 设置、超时和重试控制，记录实际请求参数。兼容处理仅作用于补充进程，不改安装包文件或必要测试。

## 5. 新增入口与配置结构

```text
eval/
├── run_all.ps1                 # 已有必要测试入口，冻结
├── run_suite.py                # 已有必要测试调度，冻结
├── common.py                   # 已有公共代码，冻结
├── eval_*.py                   # 已有五项必要测试，冻结
├── run_supplementary.ps1        # 新增：补充配置与入口
├── run_combined.ps1             # 新增：调用两层并汇总
└── supplementary/
    ├── config.json             # 独立本地路径、参数和耗时估计
    ├── README.md               # 离线安装、配置与使用
    ├── run_suite.py             # 补充调度、预算和服务管理
    ├── common.py                # 补充参数与抽样
    ├── eval_ruler.py            # RULER TaskConfig 入口
    ├── eval_bfcl.py             # BFCL TaskConfig 入口
    ├── adapters/               # 本地材料、评分和请求适配
    ├── preparation/            # 准备区说明及确有需要的薄脚本
    ├── requirements-lock.txt   # 验证后的补充环境版本
    ├── .cache/                 # 补充专用缓存
    ├── samples/                # 固定样本与选择记录
    ├── runs/                   # 计划、日志和状态
    └── outputs/                # 预测、评分和报告
```

模型、tokenizer、embedding、预生成数据、官方代码和 wheels 可位于独立导入的 fab_insight release 目录。补充入口集中配置本地路径，每项独立设置抽样、generation 和服务参数。

补充优先使用独立 .venv-supplementary，支持显式 Python 路径，不依赖 laptop_lab。必要测试的环境和路径选择保持现状；补充可读取既有配置快照作为参考，不写回必要配置。

RULER 在补充进程注册自定义 EvalScope 基准，BFCL 复用原生 AgentAdapter 与官方流程。两层分别保存原生预测、逐题评分、实际配置和报告，不合成为一个能力总分。

## 6. 服务参数与上下文

补充侧独立定义 official / fab_agent 参数，并支持项目级覆盖。与必要测试模型名称对应，但不改其 profile。补充首轮默认 Thinking off，后续可单独做对照。

| 项目 | 初始建议 | 正式运行前确认 |
| --- | --- | --- |
| RULER 8K | 输出初值 512 token，并发 1 | 完整输入、模板和输出预算可放入上下文 |
| RULER 16K/32K | 输出初值 512 token，并发 1，按长度独立配置 ctx-size | 不能沿用不足的 16K 上下文；根据完整请求、生成配置核对容量和显存 |
| BFCL 单轮/多轮/记忆 | FC，输出初值 2048 token，并发 1 | 工具调用、返回消息、最长会话可处理 |
| BFCL 格式敏感性 | 官方 Prompt 模式及全部变体 | 格式评分与参数转发正确 |

以上为规划起点，不是 A10 实测最优配置。上下文容量依据实际 GGUF、KV、显存和 token 数验证。补充端管理自己的服务启动和重启，正式运行保留 --no-context-shift，不以滑动或截断掩盖超长问题。

必要与补充服务生命周期分开管理。组合入口等待必要测试退出，再启动补充；A10 一次加载一个模型。手动 ExistingServer 模式遵循各入口约定，不擅自停止外部服务。

## 7. 独立预算与组合运行

必要测试保留三模型默认 24h 及现有分配方法。补充测试单独指定时长，在 RULER、BFCL 和各子任务之间分配；不把必要测试分母从五项改成七项，不减少其默认样本量。

补充耗时需在三个模型上校准，以同一层最慢模型的耗时规划公共样本。RULER 按长度估算，BFCL 按类别、完整会话和记忆依赖估算；格式敏感性包含 26 个变体，Memory 包含前置推理。时间余量单独配置，不套用必要测试的统一秒/题。

预算不足以覆盖全部配置时，计划报告覆盖缺口，用户可增加时间或明确选择部分范围；不漏测后宣称完整。预算是抽样规划，不是已经验证的硬性完成时限。

### 预期使用接口

从独立 eval 仓库根目录的 PowerShell 7 运行。先填写 supplementary/config.json，并用 -DryRun 查看计划、-CheckData 检查本地材料。运行参数、分层题数单位和当前验证边界见 supplementary/README.md。

```powershell
# 必要测试：现有命令，三个模型合计默认 24h。
./run_all.ps1 -Profile official -Hours 24

# 补充测试：示例独立给 6h，包含三个模型。
# 6h 不是已确认的默认值，补充预算由用户指定。
./run_supplementary.ps1 -Profile official -Hours 6

# 一次启动两层，按顺序执行：24h 必要 + 6h 补充。
./run_combined.ps1 -Profile official -RequiredHours 24 -SupplementaryHours 6
```

run_combined.ps1 的 RequiredHours 默认 24，SupplementaryHours 由用户明确指定。合计规划时间为两层预算之和；示例为 30h，不是全部项目共 24h。

组合脚本只通过命令行调用原 run_all.ps1 和新 run_supplementary.ps1，记录各层退出码、配置及报告位置，生成独立组合汇总。补充失败或未完成，不改变必要测试成绩；汇总明确每层成功、失败或未运行，本次所选范围未全部完成时返回非零退出码。

## 8. 隔离区执行链路

导入前，由准备工作站完成固定资源、RULER 样本、MiniLM 完整目录和 Windows wheels 的准备。使用匹配目标 OS / Python 的新环境验证安装及小样本，并主动断网检查；已有完整资源直接沿用。

隔离区流程：

1. 导入源码、runtime、GGUF、数据、tokenizer、MiniLM、wheels 和版本记录到明确本地路径。
2. 为补充测试创建独立 Python 环境，从本地 wheels 安装；必要环境不变：

   ```powershell
   python -m pip install --no-index `
     --find-links "D:/fab_insight/wheels" `
     -r "D:/fab_insight/requirements-lock-supplementary.txt"
   ```

   上述 lock 文件由已验证的 supplementary/requirements-lock.txt 导入，路径可配置。

3. 在补充入口填写本地路径，必要测试继续使用原配置。
4. 用补充 DryRun / CheckData 核对计划、token 长度、BFCL ID/答案、Memory 依赖和 MiniLM 本地加载。保持轻量检查，不另建强制资源校验框架。
5. 小样本请求 localhost，验证参数、工具接口和耗时，随后生成正式公共抽样计划。
6. 单独执行补充层，或从组合入口顺序执行两层。
7. 分别保存实际配置、样本 ID、预测、评分、请求数和耗时，按能力域解读成绩。

正式环境不依赖旧机器隐式缓存。缺少材料时由准备区补齐后重新导入，不尝试在线修复。

## 9. 验收标准

- 必要源码、配置、环境、抽样、缓存与结果路径保持原状，组合调用不改变其预算。
- 补充仅使用明确导入目录，不读取 laptop_lab 或未记录的用户缓存。
- Windows 新环境可通过 --no-index 安装；正式流程不访问公网，模型请求只发往 localhost。
- MiniLM 断网可加载，memory_vector 不下载模型，记忆按模型和运行隔离。
- RULER 使用预生成数据和官方评分，覆盖实际启用任务/长度；16K/32K 成绩不由较短长度代替。
- BFCL ID/答案匹配，会话和依赖完整，格式变体配对，两个 Web Search 类别明确排除。
- 模拟服务验证请求参数、工具消息与失败记录，模拟成绩不当作模型能力结果。
- 目标 A10 与三个真实 GGUF 上完成上下文、接口和耗时校准，再规划正式题数。
- 组合入口一次执行两层，独立保存状态和报告；一层失败不覆盖另一层成绩。
- 文档区分现有入口与待实现接口，说明独立预算、本地配置和完整运行链路。

### 本轮实现与验证范围

已实现独立补充与组合入口、分层抽样、离线 tokenizer / MiniLM 加载、BFCL 官方类别/会话/记忆/格式处理、请求参数转发、官方 RULER 规则评分和原生报告。组合入口只调用原必要测试命令。

已检查真实 BFCL 20 个离线类别及格式敏感性的数据和参考答案，核对 Memory 依赖及 MiniLM 本地加载。模拟 localhost API 验证了单轮、格式变体、记忆前置快照、RULER 合成输入评分和失败无重试；临时模拟服务验证重启、失败清理与端口释放。组合 DryRun 保持必要测试的原预算和题数。

尚未在 A10 与三个候选 GGUF 上完成正式性能或能力测试。本机 RULER 使用合成数据验证接口，不代替正式 16K/32K 成绩；配置中的耗时与显存参数仍需目标机器实测。

## 10. 参考材料

- [离线大模型选型与评测部署方案](../FAB_Agent_离线大模型选型与评测部署方案_下载补救修订版.pdf)
- [离线评测资源准备清单](../FAB_Agent_离线评测资源准备清单_下载补救修订版.pdf)

PDF 位于原 deployment 项目根目录，未随公开 eval 仓库发布；独立使用时随资源文档导入。下列地址仅供准备区查阅和记录来源，隔离区不访问：

- RULER：https://github.com/NVIDIA/RULER
- 经典任务定义：https://github.com/NVIDIA/RULER/blob/main/scripts/synthetic.yaml
- 经典评分入口：https://github.com/NVIDIA/RULER/blob/main/scripts/eval/evaluate.py
- EvalScope BFCL V4：https://evalscope.readthedocs.io/en/latest/third_party/bfcl_v4.html
