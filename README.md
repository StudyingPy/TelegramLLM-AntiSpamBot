# Telegram LLM Anti-Spam Bot

一个面向 Telegram 群组的反广告机器人。当前已落地 Phase 0-5 的核心链路：

- Bot API 接入与消息监听
- SQLite 持久层和基础 migration
- 三处链接载体解析：正文 URL、`text_link`、`link_preview_options.url`
- 正文归一化、内容 hash、句式骨架 hash、SimHash
- 本地硬规则：纯标点正文 + 预览卡、命中指纹、明显广告 bio、联系方式叠加加群/收钱/色情等硬信号
- 广告 bot 调用追责：用户仅发送一个或多个 `@...bot`，被调用 bot 随后判为广告后，用户再次发送任意纯 `@bot` 消息会被直接封禁，并清理其全部已记录的纯 `@bot` 消息
- 按钮投票处置链路，含最低票数、投票改票、超时默认放行（默认 24 小时）；确认广告后会清理关联广告消息和投票消息。投票超时后消息上会挂出“前往私聊补审”按钮，管理员点击即可进入私聊复审，补充封禁或维持放行
- NewAPI/LLM 判定层：OpenAI-compatible chat completions，结构化 JSON，支持多 API/key fallback，85% 自动封禁阈值，超时降级
- 指纹/信用闭环：LLM 判广告沉淀中权重指纹，投票确认升权，投票放行降权
- OG 抓取：短正文 + `link_preview_options.url` 时安全抓取 OG 标题/描述补给 LLM
- 用户资料上下文：读取 username/昵称，best-effort 读取并缓存 bio，作为 LLM 弱信号
- 个人频道交叉验证：群消息出现招募、按日高额收益、码商等引流话术时，实时读取资料页个人频道最近消息并交给 LLM 综合判断

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
如果要配置多个 fallback，可以使用逗号分隔的复数变量：

```env
NEWAPI_BASE_URLS=https://api-a.example,https://api-b.example
NEWAPI_API_KEYS=key-a,key-b
NEWAPI_MODELS=gpt-5.4,gpt-4.1-mini
```

URL、key、model 会按位置配对；只有一个 key 或 model 时会复用于所有 URL。旧的
`NEWAPI_BASE_URL`、`NEWAPI_API_KEY`、`NEWAPI_MODEL` 仍然兼容，也可以直接写逗号分隔值。

对“正文只有标点但挂广告预览卡”的样本，机器人会独立读取
`message.link_preview_options.url`，并在 SSRF 护栏下抓取 OG 文案作为规则和 LLM
上下文。若 OG 或正文里出现联系方式叠加加群、拿码、收钱、色情、博彩等明确导流信号，
会直接清理并封禁。

用户资料会进入 LLM 上下文。群消息自带的 `username`、`first_name`、`last_name`
会稳定读取；`bio` 只有 Bot API 对该用户可见时才会读取成功，失败会静默跳过并
继续按消息内容判断。包含进群、做单、刷单、色情、博彩等明确导流信号的 bio 会作为
本地强信号直接处理。

账号可能先用正常资料进群、之后再挂广告频道。为避免 7 天 bio 缓存形成绕过，个人频道
不会随入群资料缓存；只有群消息本身出现引流迹象时，机器人实时调用
`getUserPersonalChatMessages` 读取最近 3 条文字/说明。普通消息不会触发这次查询，仅有个人
频道也不会处罚；群消息与频道内容同时呈现招募、结算、按日高收益、码商等广告语境时，
会由 LLM 综合判定。达到现有 85% 广告阈值时自动清理并封禁，70%–85% 时进入撤回投票；
LLM 判定正常则放行。

全局白名单里的用户完全跳过审核，适合放行 nmBot / 客服酱这类友好机器人。除了
`WHITELISTED_USER_IDS` 环境变量和 `antispam-admin whitelist-user` CLI，**全局管理员**
（`ADMIN_USER_IDS`）还可以直接在与 bot 的对话或群里用命令管理，群管理员无权使用：

- `/whitelist <user_id> [备注]`：加入全局白名单；也可以直接回复某人的消息 `/whitelist [备注]`，
  自动取被回复者的 ID（省去手动查 user_id）。
- `/unwhitelist <user_id>`：从白名单表移出（环境变量配置的 ID 不受影响）。
- `/list_whitelist`：查看当前白名单（环境变量 + 数据库表）。

初始化数据库：

```powershell
antispam-admin init-db
```

启动机器人：

```powershell
antispam-bot
```

机器人需要在目标群组拥有删除消息权限；如果要自动封禁，还需要封禁/限制成员权限。

## Linux 一键部署

在 VPS 上可以用 GitHub 仓库部署 systemd 服务。仓库是私有仓库时，推荐先把
`deploy/install.sh` 上传到服务器，然后执行：

```bash
sudo bash install.sh
```

脚本会交互式生成 `.env`、创建应用用户、安装虚拟环境、初始化 SQLite，并启用
`telegram-llm-antispam-bot.service`。默认会创建专用系统用户 `antispambot`，
并在 `/var/lib/telegram-llm-antispam-bot/.ssh/deploy_key` 生成 SSH deploy key；
脚本会打印公钥并暂停，等你把它添加到 GitHub 仓库的只读 Deploy key 后再继续拉取。

如果你用 GitHub token 下载私有仓库里的安装脚本，也可以这样启动：

```bash
curl -fsSL \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  https://raw.githubusercontent.com/StudyingPy/TelegramLLM-AntiSpamBot/main/deploy/install.sh \
  | sudo bash
```

可用环境变量覆盖默认值：

```bash
APP_DIR=/opt/telegram-llm-antispam-bot \
APP_USER=antispambot \
BRANCH=main \
REPO_URL=git@github.com:StudyingPy/TelegramLLM-AntiSpamBot.git \
SERVICE_NAME=telegram-llm-antispam-bot \
sudo -E bash deploy/install.sh
```

已有部署可以使用更新模式：

```bash
sudo bash /opt/telegram-llm-antispam-bot/deploy/install.sh update
```

更新模式会拉取最新代码、保留现有 `.env`、重写 systemd service、运行数据库迁移并
重启服务；如果 `pyproject.toml` 依赖指纹没有变化，会跳过虚拟环境和 Python 包重装。
也可以用 `DEPLOY_MODE=update sudo -E bash deploy/install.sh` 指定同样行为。

## 当前阶段边界

Telegram Bot API 删除消息后不能原样恢复到原消息位。当前投票阶段默认保留原消息供
管理员判断；投票确认广告、管理员跳过投票或本地强信号封禁时，会删除该用户仍在打开
投票中的广告消息、bot 投票消息和当前命中消息，并发送一条 2 分钟后自动删除的群内
处理总结。投票“放行”会记录为假阳性并恢复用户信用。

投票默认存活 24 小时（`VOTE_TIMEOUT_SECONDS`），给管理员充足的处置窗口。超时后消息
默认放行，但 bot 会把群内投票消息改写为超时提示，并挂出“前往私聊补审”按钮。管理员
点击后会通过 `t.me/<bot>?start=review_<会话ID>` 深链接进入私聊，bot 校验点击者确实
是该群管理员后，展示复审卡片和“确认封禁 / 维持放行”按钮：确认封禁会补删原消息与投票
消息并封禁用户，维持放行则记录管理员决定并撤下补审按钮。

复审卡片会带上与全局管理通知一致的完整详情（原文、用户资料、bio、OG、LLM 判定、
链接），这份详情在投票创建时就缓存在会话上，因此不依赖是否配置了管理通知；升级前
创建的旧会话没有缓存时，会退回到 action_log 里的正文快照。

这条超时提示消息（含补审按钮）会在 `VOTE_EXPIRED_MESSAGE_TTL_SECONDS`（默认 21600 秒 /
6 小时）后自动从群里删除。删除后补审的群内入口也随之消失，即补审窗口 = 该时长。
（best-effort 定时删除，bot 在此期间重启会丢失未触发的删除任务，消息残留。）
