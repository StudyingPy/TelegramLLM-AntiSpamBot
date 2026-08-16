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
