# Jev 提示词和输入实验工具

`python -m telegram_llm_antispam.jev_prompt_lab` 是独立离线实验入口，不导入审核动作层、不改变运行中的生产 Questions。
默认只重建缓存报告；只有 `--call-api` 才会发请求。Key 仅从 `JEV_API_KEY` / `TYPESAFE_API_KEY` 读取。

```powershell
.venv/Scripts/python.exe -m telegram_llm_antispam.jev_prompt_lab --live data/jev-tuning/jev-tuning-source/dataset.jsonl --candidates jev-audit-output/new_chat_annotation_queue.jsonl --candidate-count 12 --seed 20261020 --out-dir data/jev-tuning/experiment
```

真实实验需在上述固定数据集上显式增加 `--call-api --max-calls 200`；本地联网还需 `--proxy http://127.0.0.1:2235`。
命令不会获取用户当前资料或读取生产审核库。使用已有 `--dataset` 可保证后续比较固定样本。
`--variants` 限定方案，`--model` 固定模型。请求哈希包含完整 state、Questions、model；并发重复请求合并，预算按实际不同请求预检，旧成功缓存不再付费调用。

缓存复算：

```powershell
.venv/Scripts/python.exe -m telegram_llm_antispam.jev_prompt_lab --dataset data/jev-tuning/results/dataset.jsonl --out-dir data/jev-tuning/results
```

输出 `dataset.jsonl`、`manifest.json`、`predictions.jsonl`、`comparison.csv`、`metrics.json`、`execution.json` 和 `report.md`。
默认目录位于 Git 忽略的 `data/`，原始消息与缓存不得提交。候选正常消息不会自动变成 ham；MiMo 判断不会自动变成金标。
各方案比较只用共同成功样本，入群、空正文、截断分开处理。纯正文方案刻意减少资料，不得声称与原模型拥有相同上下文。

2026-10-20 首轮实验：32 条真实输入，六种新请求方案，157 次去重物理请求全部成功。
同一组六条非截断 MiMo 阳性消息上，原 Questions 在 0.5 门槛检出4条，中文短问题/原输入检出5条；这是模型参照对照，不是人工召回率。
只精简输入收益较小；纯正文改善一条隐晦招揽，却漏掉需要 Bio 的消息。首轮实验没有修改生产提示词。
完整私有分析见 `data/jev-tuning/results/analysis.md`，包含当前分母、错误案例和边界，不据此自动上线或修改处罚阈值。

2026-10-20 用户在查看结果后另行授权部署中文短问题＋原完整输入。生产采用与实验逐字段一致的
`short_zh_full` Questions，保留 Jev 全量初审→MiMo 独立复核。版本及 Questions 哈希写入观察记录和启动日志。
`production_replay` 与 `long_compact` 两个实验名固定引用旧的 `LEGACY_QUESTIONS`，用于复现首轮控制组；
它们在本工具中不跟随当前生产默认提示词变化。
