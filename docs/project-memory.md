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

## Economic-article hard-rule false positive (2026-10-14)

- Reports 19672/19674 used `hard_spam_message`. This is a local keyword rule with
  fixed confidence 0.96 and `should_call_llm=False`, not an LLM judgement. Replaying
  the linked MacroChina article (`/u/60/archives/2026/4314.html`) reproduces BAN:
  `降低几千亿的出口退税` matches `几千`; `出口几百万辆汽车` matches `几百`;
  the citation URL satisfies the generic carrier check.
- Notification bodies stop at 800 characters and BAN log snapshots at 500, while
  these hits occur after character 2600. Future diagnostics should record matched
  tokens, bounded context windows and carrier type, not only the opening text.
- Implemented conservative mitigation: remove only `几百`/`几千` from hard tokens.
  `LONG_MESSAGE_LLM_MIN_CHARS` defaults to 500 non-whitespace/non-zero-width message
  characters. Long local BAN/WITHDRAW_VOTE hits become `REVIEW` with LLM enabled;
  benign output releases, spam uses the normal thresholds, and failure/disabled
  output leaves a logged review without destructive action. Short rules stay intact.
- Apply the length guard both at RuleEngine's return and after handler repeat
  shortcuts, before calling the model. This covers content/phrase fingerprints,
  body/OG/profile rules, suspicious edits and repeats. Preserve original local-rule
  evidence in metadata when merging the LLM result. Using `WITHDRAW_VOTE` as the
  long-text fallback would keep a vote even after the model says benign.
- `WHITELIST_DOMAINS` currently does not bypass hard body/OG rules; the rule engine's
  `_has_non_whitelisted_link` helper has no callers. A domain setting alone is not
  a fix for this incident. Attribution or message length must not grant blanket immunity.

## Windows test runtime

- Use `.venv/Scripts/python.exe` and `.venv/Scripts/ruff.exe`. In the restricted
  Windows sandbox, async tests can hang before executing their coroutine because
  `asyncio`'s event-loop `socketpair()` blocks in `socket.accept()`. A pytest
  `faulthandler_timeout` identifies this; run the mocked test suite outside the
  sandbox instead of changing application/event-loop code to work around it.

## Production deployment

- SSH alias: `OP-JP-Own` in `C:/Users/1/.ssh/config` (root, port 22). It uses
  `C:/Users/1/.ssh/op_jp_own_ed25519` and an explicit Ncat SOCKS5 ProxyCommand via
  `127.0.0.1:2235`. Use the existing alias; do not restore global proxy variables.
- Application: `/opt/telegram-llm-antispam-bot`; service/user:
  `telegram-llm-antispam-bot.service` / `antispambot`; production branch: `main`.
  Check the remote worktree is clean and its HEAD first. Push the intended commit,
  then run `ssh OP-JP-Own "sudo bash /opt/telegram-llm-antispam-bot/deploy/install.sh update"`.
  This keeps `.env`, skips reinstall when the committed dependency fingerprint is
  unchanged, initializes the database and restarts systemd. Verify deployed HEAD,
  `systemctl is-active`, startup logs and bot polling after the restart.
- Local Git HTTPS network commands need per-command
  `git -c http.proxy=http://127.0.0.1:2235 ...`. Keep unrelated JEV files, `.gitignore`,
  `pyproject.toml` and pre-existing memory edits out of production commits.
