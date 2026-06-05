# Telegram LLM Anti-Spam Bot

一个面向 Telegram 群组的反广告机器人。当前已落地 Phase 0-5 的核心链路：

- Bot API 接入与消息监听
- SQLite 持久层和基础 migration
- 三处链接载体解析：正文 URL、`text_link`、`link_preview_options.url`
- 正文归一化、内容 hash、句式骨架 hash、SimHash
- 本地硬规则：纯标点正文 + 预览卡、新用户首条非白名单外链、命中指纹
- 撤消息 + 按钮投票的处置链路，含最低票数、投票改票、超时默认放行
- NewAPI/LLM 判定层：OpenAI-compatible chat completions，结构化 JSON，超时降级
- 指纹/信用闭环：LLM 判广告沉淀中权重指纹，投票确认升权，投票放行降权
- OG 抓取：短正文 + `link_preview_options.url` 时安全抓取 OG 标题/描述补给 LLM
- 用户资料上下文：读取 username/昵称，best-effort 读取并缓存 bio，作为 LLM 弱信号

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
```

编辑 `.env`：

```env
TELEGRAM_BOT_TOKEN=123456:your-token
DATABASE_PATH=data/bot.db
WHITELIST_DOMAINS=github.com,python.org
NEWAPI_BASE_URL=https://your-newapi-host
NEWAPI_API_KEY=your-api-key
NEWAPI_MODEL=gpt-5.4
```

`NEWAPI_BASE_URL` 和 `NEWAPI_API_KEY` 留空时，机器人会自动降级成纯本地规则模式。

对“正文只有一个点但挂广告预览卡”的样本，机器人会独立读取
`message.link_preview_options.url`。这类消息会先按强信号撤回并发起投票；
若配置了 NewAPI，还会在 SSRF 护栏下抓取 OG 文案作为 LLM 上下文。

用户资料会进入 LLM 上下文，但不会单独触发封禁。群消息自带的 `username`、
`first_name`、`last_name` 会稳定读取；`bio` 只有 Bot API 对该用户可见时才会
读取成功，失败会静默跳过并继续按消息内容判断。

初始化数据库：

```powershell
antispam-admin init-db
```

启动机器人：

```powershell
antispam-bot
```

机器人需要在目标群组拥有删除消息权限；如果要自动封禁，还需要封禁/限制成员权限。

## 当前阶段边界

Telegram Bot API 删除消息后不能原样恢复到原消息位。当前投票“放行”会记录为假阳性并恢复用户信用，后续可以补“重发原消息快照/申诉面板”来贴近可恢复体验。
