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

## Rich-message GuestMode ads

- Bot API 10.1 messages can carry `Message.rich_message` while `Message.text` is empty;
  aiogram 3.28 keeps this newer field as an allowed extra, so always pass it through the
  feature adapter instead of assuming the SDK has a typed field.
- Flatten rich blocks and nested rich-text fragments into searchable text before link,
  fingerprint, local-rule, and LLM processing. Extract explicit HTTP(S) URLs from rich-text
  URL entities as well. Guest-mode bot output is still a message from another bot and must
  not be skipped as if it were the moderation bot's own output.
- The exported client JSON may use legacy `_type`/`texts` names, while Bot API uses `type`;
  the extractor intentionally accepts both forms.
- Inline keyboard labels and hidden button URLs are also message content. Bot API uses
  `inline_keyboard`, while exported client JSON may expose `rows[].buttons[]`; both are
  flattened before fingerprints and LLM evaluation.

## Bio and username corroboration

- Bio is contextual evidence, not a learned message phrase source. Never add Bio/username
  text to phrase-fingerprint lookup candidates; otherwise an empty or punctuation-only group
  message can inherit another user's Bio fingerprint and open a false-positive vote.
- A Bio containing advertising/payment/recruitment wording plus an advertising-looking
  display name or username may be locally banned. A suspicious Bio without username
  corroboration goes through the LLM; weak patterns such as a personal-channel link or
  `私聊：@xxxbot` do not create a profile-spam decision by themselves.
