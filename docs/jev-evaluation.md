# Jev 离线评测

当前批量调用已暂停。第一轮会话标签混入了自动处置、用户级封禁和连带关闭，旧报告不能用于模型选型。使用下面的缓存审计流程，暂不运行 `--call-api`。

## 标签审计与缓存复算

```powershell
$env:PYTHONPATH = 'src'
.venv/Scripts/python.exe -m telegram_llm_antispam.jev_audit `
  --dataset jev-eval-live2/dataset.jsonl `
  --predictions jev-eval-live2/predictions.jsonl `
  --mapping jev-audit-output/human_mapping.json `
  --sample-related jev-audit-output/sample_related.json `
  --annotations jev-audit-output/review_annotations.jsonl `
  --normal-candidates jev-audit-output/normal_candidates_raw.json `
  --out-dir jev-audit-output
```

此命令不导入网络客户端，只读本地缓存。输入文件 SHA-256、随机种子和样本来源保存在 `metrics.json`。

- `label_audit_report.md`：标签生成审计、同样本比较、消息级与用户级描述性结果及数据缺口。
- `miss_audit.md` / `miss_audit_sample.jsonl`：固定随机50条的逐条语义复核、历史资料、投票、主触发事件和相关会话。
- `stratified_dataset.jsonl` / `comparison.csv`：独立保留旧标签、新标签、事件来源、输入证据层、排除原因。
- `annotation_queue.jsonl`：733条旧标签消息的盲标队列，隐藏预测和处置；由两名真人独立标注。
- `normal_annotation_queue.jsonl`：已有7条真人放行、6条正常正文待复核候选及71条新群聊候选；候选全部pending。
- `new_chat_annotation_queue.jsonl`：71条近期短期正文快照，33用户/1群，没有Jev预测，不与第一轮历史集混算。

审计中的语义备注由 Codex 逐条复核形成，不能冒充新增真人金标。已知人审事件也只是候选真值：一次封禁可能基于资料而非消息正文。`auto_banned_user` 绝不能当人工确认；投票只把 `trigger_vote_session_id` 对应消息视为直接对象，兄弟会话仍是用户级证据；管理员多会话操作没有可靠的主会话记录。

`reference.label` 只有在人审事件、语义证据与可用输入一致时才用于消息指标；空正文、输入不足和未审计的消息保留unknown。独立消息识别、需要Bio/OG/频道上下文的识别、资料识别、Bot调用行为分别作为任务。Choice广告类别不会覆盖Noul，更不能生成真值。

原LLM与Jev只在同一组成功样本上比较，明确失败、停用、本地路径未调用、缺少调用遥测分别列出。原请求没有完整存档，输入上下文一致性标为未知。第一轮70%/85%映射只能称为分数映射，不能称完整系统回放。

## 真人标注规范

每位标注者填写 `message_label`（spam/ham/unknown）、`profile_or_account_label`（独立判断或unknown）、`input_complete`、`context_required`、`evidence_spans`、原因和reviewer_id。标注者不看模型结果或最终处罚；分歧交第三人，证据不足保持unknown。按群、用户、时间分组切分并去重，同一广告模板不能跨调参与测试集。

正文/资料与可疑关键词不是标签。Bio有机器人链接而正文正常、产品推荐、技术外链、兼职/金融/加密讨论、引用/举报广告与玩笑均需单独覆盖；当前7条放行仅4个唯一正文，不能估计真实误杀率。

线上新增的 `edit_watch_messages` 只短期保留新/低信誉用户的基线，可能是编辑后的版本；其中也有可疑招揽。它能提供真实文本候选，但没有正常真值，也没有历史完整上下文。更有代表性的样本需补充Telegram群史导出或已有审计机器人的原始日志，不能用当前Bio补历史。

`antispam-jev-eval` 是只读实验工具，不接入生产审核链路。它从 SQLite 的
`action_log` / `vote_sessions` 中恢复已经持久化的正文快照，生成数据集，再用一套固定
Questions 调用 Jev `POST /v1/systemone`。

## 安全边界

- `--db` 使用 SQLite `mode=ro` 和 `PRAGMA query_only=ON`；工具不会写生产数据库。
- Jev state 不包含原 LLM 输出、最终动作、投票结果、人工标签或事后解释。
- 当前 Bio、信誉、OG、个人频道内容不会冒充历史状态；无法恢复的字段写入 `missing`。
- API key 只从 `JEV_API_KEY`（或 `TYPESAFE_API_KEY`）读取，不写入输出。
- 评测输出可能含真实 Telegram 文本，默认写入 `jev-eval-output/` / `jev-eval-live*/`，已在
  `.gitignore` 排除，提交前仍应检查 `git status`。

## 生成数据集（不调用 API）

```powershell
$env:PYTHONPATH = 'src'
python -m telegram_llm_antispam.jev_eval `
  --db /path/to/bot.db `
  --out-dir jev-eval-output `
  --seed 20260921
```

## 调用 Jev

```powershell
$env:PYTHONPATH = 'src'
$env:JEV_API_KEY = '从密钥管理器注入，不要提交'
python -m telegram_llm_antispam.jev_eval `
  --dataset jev-eval-output/dataset.jsonl `
  --out-dir jev-eval-live `
  --call-api `
  --concurrency 8 `
  --proxy http://127.0.0.1:2235
```

未来恢复实验后，`--concurrency` 应按账户限额保守设置。同一冻结输入下的
`--use-cache` 可复用已成功预测，失败行会重试；第一轮缓存没有请求内容哈希，不能用于修改了正文、上下文或Questions的新实验。
只使用已有结果重建报告时省略 `--call-api` 并保留 `--use-cache`。

输出文件：

- `dataset.jsonl`：原始文字（保留零宽字符）、可恢复历史上下文、人工参考标签和原 LLM 仅供比较的数据。
- `predictions.jsonl`：Jev model、Questions 哈希、noul 原值、Choice 完整概率/置信度、耗时、usage、错误和原始响应。
- `comparison.csv`：逐条比较与 70%/85% 初步阈值回放。
- `report.md`：未通过事件与输入审计的数据只输出待审计提示，避免再次生成无效的总体指标；详细统计使用审计工具。

Jev 的 `noul` 没有 Choice 那样的 `confidence` 字段；报告将 noul 概率本身作为初步阈值回放分数，Choice confidence 单独保存，不能混用。
