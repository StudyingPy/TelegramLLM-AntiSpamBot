# Telegram 个人频道广告对策研究

## 结论

截图中的卡片是 Telegram 的 **个人频道（personal channel / personal chat）**：用户可把自己管理的公开频道挂到个人资料页，资料页会展示频道及其最新消息。广告账号因此可以在群里只发很短、很含糊的文字，把真正的广告放在资料页卡片中。

HTTP Bot API 已经能读取这组信号，不需要引入用户号或 MTProto 客户端：

- `getChat(user_id)` 返回的 `ChatFullInfo.personal_chat` 是用户当前挂在资料页的频道。该字段自 Bot API 7.2 起存在。
- `getUserPersonalChatMessages(user_id, limit)` 返回该个人频道最近 1–20 条消息。该方法自 Bot API 10.0 起存在。
- 当前开发虚拟环境使用 aiogram 3.28.2，已经映射 `personal_chat` 和 `Bot.get_user_personal_chat_messages()`。

机器人不能替用户移除资料页频道；它能做的是在用户进群或发言时读取资料证据，并按本项目原有流程放行、送审、删消息或封禁。

## 官方依据

- [Bot API: ChatFullInfo](https://core.telegram.org/bots/api#chatfullinfo)：`personal_chat` 是私聊对象对应用户的个人频道。
- [Bot API: getUserPersonalChatMessages](https://core.telegram.org/bots/api#getuserpersonalchatmessages)：按用户 ID 获取其个人频道最近 1–20 条消息。
- [Bot API 10.0 changelog](https://core.telegram.org/bots/api-changelog#may-8-2026)：新增 `getUserPersonalChatMessages`。
- [Telegram user profiles: Personal channel](https://core.telegram.org/api/profile#personal-channel)：说明个人频道与资料页预览；底层 `messages.getPersonalChannelHistory` 也只允许 bot 调用。
- [aiogram 3.28 documentation](https://docs.aiogram.dev/en/v3.28.0/api/methods/get_user_personal_chat_messages.html)：aiogram 对该 Bot API 方法的封装。

## 已采用的实现

广告账号可能先用正常资料进群，再更改或挂载个人频道。因此个人频道不复用 bio 的 7 天缓存，也不在入群时预抓取。处理链路是：

1. 先检查当前群消息是否带有招募、换资、车队、演员结算、码商、按日高额收益等引流属性。
2. 只有命中上述候选信号时，实时调用：

   ```python
   messages = await bot.get_user_personal_chat_messages(user_id=user_id, limit=3)
   ```

3. 临时提取频道标题、用户名以及最近三条消息的 `text` / `caption`。
4. 将群消息、发送者资料、频道标题和最近消息一起放入 LLM payload，由模型判断两侧是否相互印证为招揽、交易或导流。
5. 使用项目既有阈值执行结果：LLM 广告置信度不低于 85% 时自动删除并封禁，70%–85% 时撤回投票，判定正常则放行。

普通消息不会触发个人频道 API 请求；仅仅存在个人频道不会处罚。只要疑似群消息查到了个人频道，就强制调用 LLM，让模型同时看到两侧完整文字，而不是先由本地频道关键词决定是否值得调用模型。

个人频道内容只存在于本次处理的瞬时 `features.metadata["personal_chat"]` 和 LLM 请求中，不写入 `user_profiles`，不建立频道历史表，也不放入 action log 的频道快照。action log 只保留群消息原文、LLM 结果和交叉验证信号，频道读取始终是实时的。

项目依赖下限已提高为 `aiogram>=3.28,<4`，保证 `get_user_personal_chat_messages()` 存在。接口异常、无个人频道或空历史均静默降级到原审核链路。

## 边界与风险

- Bot API 没有为普通群机器人提供“某成员更换个人频道”的专门 update；本实现用可疑发言触发实时查询，不依赖变更通知。
- 机器人无法主动枚举大型群的所有成员，因此只能覆盖新入群者、发过言者及其他已获得用户 ID 的事件。
- 旧版或第三方 Bot API Server 可能不支持 Bot API 10.0 方法；必须 best-effort 降级，不能让资料抓取异常中断审核。
- 频道可以在审核后被编辑、换绑或删除；按当前需求不保存频道快照，只记录规则命中原因。
- 媒体广告可能没有文字。第一版处理频道标题、text 和 caption；图片 OCR/视频识别不在本次范围。
