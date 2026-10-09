# 准备区材料交付

正式隔离区不联网，也不运行 RULER 数据生成器。本目录仅记录提前准备要求，不增加自动下载或换源框架。

1. 在准备工作站冻结 RULER 代码 commit，按照该版本真实任务参数生成 8K/16K/32K 样本，保存其 input / outputs 原始字段、生成命令、任务、数量、seed、tokenizer 和模板。需要完整经典范围时覆盖 synthetic.yaml 的 13 个任务。
2. 带上冻结代码的 scripts/synthetic.yaml、scripts/eval/synthetic/constants.py、scripts/eval/evaluate.py 和许可证；正式评分只加载其中的规则与清洗函数。
3. 导入 Qwen3-14B 与 Qwen3.8-27B 完整 tokenizer，保留实际版本。使用同一套题目内容比较三个模型，并报告各 tokenizer 实际计数。
4. 为目标 Windows / Python 3.12 提前准备 supplementary/requirements-lock.txt 对应的全部 wheels，包含 BFCL 包内资产；在新的离线环境用 --no-index 安装一次，不把联网补依赖留到目标机器。
5. 导出 sentence-transformers/all-MiniLM-L6-v2 完整目录，含 modules.json、Pooling 配置、tokenizer 和权重；在断网环境验证 CPU 加载和 384 维输出。不要只带单个 model.safetensors。
6. 准备 llama-server.exe 的完整 CUDA runtime 目录及实际候选 GGUF，记录版本、来源和路径。补充运行不拉取 Git，也不替换必要测试的环境。
7. 完成主动断网下的 CheckData 和最小推理，再按公司流程导入完整 release 目录。SHA-256 依公司要求可选。

若已有材料符合上述内容，直接配置本地路径并检查，不必重新下载。正式脚本缺少材料时只报告缺失项，不在线修复。
