# Project Memory

## Bot-only advertising invocation correlation

- “纯 `@bot`” means one or more whitespace-separated Telegram-style bot usernames with no other
  content. Multiple names are allowed, and the later invocation does not need to name the same bot.
- `bot_only_messages` persists message IDs both for restart-safe correlation and eventual cleanup.
- An explicit reply from the advertising bot wins; standalone bot output falls back to the closest
  preceding invocation naming that bot in a five-minute window.
- Once a called bot is classified as advertising, the caller's next pure `@bot` message is an
  immediate ban. The ban action must delete every recorded pure `@bot` message from that caller,
  not only the triggering message.

## Personal-channel profile advertising

- A profile channel card is Telegram's personal channel. `getChat(user_id)` exposes it as
  `ChatFullInfo.personal_chat`; Bot API 10.0 adds `getUserPersonalChatMessages(user_id, limit)` for
  its last 1–20 posts. aiogram 3.28 supports both and is now the project dependency floor.
- Accounts may join clean and attach an ad channel later. Do not reuse the bio cache: only a group
  message with recruitment/payment/code-trading diversion language triggers a live fetch of the
  latest three personal-channel messages.
- Merely having a personal channel is never a spam signal. Once a suspicious group message yields
  a live personal channel, include both contexts in the LLM payload and force the LLM hop. The
  normal LLM thresholds decide auto-ban/vote/allow; do not locally auto-ban this cross-check path.
- Personal-channel data is transient feature input. Do not persist it in `user_profiles`, a history
  table, or action-log snapshots; API failures silently fall back to the original moderation path.
- Full design and official references: `docs/personal-channel-mitigation.md`.
