# Architecture Notes

## Pipeline

1. Telegram message handler receives group/supergroup messages.
2. Linked-channel posts auto-forwarded by Telegram (`is_automatic_forward=True`, sender
   `777000`, `sender_chat` = source channel) are skipped before any rule runs, so the
   group's own channel content is never deleted/banned. Telegram's service account
   `777000` is also whitelisted by default; operator whitelist entries (env
   `WHITELISTED_USER_IDS` / `whitelisted_users` table) bypass moderation here too.
3. Join service messages with `new_chat_members` are converted into profile checks for the
   joined users, so spammy profile bio can be handled even when the service message has no text.
4. Feature extraction parses links from text, `text_link` entities, and link previews.
5. For short text or punctuation-only text with `link_preview_options.url`, the OG fetcher
   validates the URL, blocks private/internal destinations, limits redirects/bytes/time, and
   extracts title/description text for local rules and the LLM payload.
6. Text is normalized and converted to content/skeleton fingerprints.
7. User profile context is cached from message sender fields. Bio is fetched best-effort via
   `get_chat(user_id)` when Bot API exposes it, and explicit bio spam signals are handled locally.
   Separately, a group message containing recruitment, code-trading, settlement, or high daily-pay
   language triggers a live `getUserPersonalChatMessages(user_id, limit=3)` lookup. This lookup is
   not cached, so an account that attached an advertising channel after joining cannot reuse its
   earlier clean profile state.
8. Local rules check known fingerprints, reputation, repeat windows, repeated open votes, profile
   bio signals, and hard carrier signals. A suspicious group message plus a currently attached
   personal channel forces an LLM hop with both contexts. The normal LLM thresholds then apply:
   high-confidence spam is auto-banned, medium-confidence spam opens a vote, and benign output is
   allowed. Having a personal channel by itself never changes the decision.
   Bot-only invocation messages (one or more whitespace-separated `@...bot` usernames and no
   other content) are persisted separately. If a named bot responds with content classified as
   advertising, that invocation marks its human caller. The caller's next bot-only message bans
   them regardless of which bot usernames it contains, and supplies every recorded bot-only
   message ID to the cleanup action.
9. Decisions are applied by the action layer:
   - allow/review only logs observations
   - withdraw + vote opens an inline vote session while preserving the original message for review
   - ban deletes the current hit plus the same user's open-vote suspect/vote messages, bans when
     permissions allow it, and posts a group summary that is deleted after 2 minutes
10. Vote callbacks update `vote_sessions`, `vote_session_votes`, reputation, and action logs.
    Open vote messages also expose administrator-only immediate ban/release actions and a
    private-chat detail deep link backed by the cached session detail, so review remains possible
    when another moderation bot has already deleted the original message.
11. Confirmed-spam vote callbacks close all open vote sessions for the suspect user, clean the
   related suspect messages and bot vote prompts, ban the user, and update admin notifications.
12. A background sweeper expires stale open vote sessions as `expired_released`, logs the
   default-release action, and edits the vote message when Telegram allows it.
13. Feedback updates fingerprints and reputation:
   - LLM spam creates medium-weight skeleton/phrase fingerprints
   - vote-confirmed spam boosts skeleton/content fingerprints
   - vote-released messages mark false positives and lower fingerprint weight
   - each new message explicitly judged non-spam by the LLM adds a small reputation reward
   - bare human `@username` mentions are excluded from phrase fingerprints; only usernames
     ending in `bot` retain carrier-only phrase matching

## Phase Status

- Phase 0 is wired: bot runner, SQLite schema, permission checks, message pipeline.
- Phase 1 is wired: local extraction and hard rules.
- Phase 2 is wired: `llm.py` calls OpenAI-compatible NewAPI gateways when `NEWAPI_*`
  environment variables are present, supports comma-separated multi-provider fallback, parses
  strict JSON, bans non-high-reputation users when LLM spam confidence reaches 85%, and falls
  back to local rules only after all providers time out, fail transport, or return invalid model
  output.
- Phase 3 is wired: button votes, vote changes, threshold close, action logs, timeout default
  release, confirmed-spam cleanup, admin skip-ban cleanup, and transient group summaries are active.
  Message restoration remains a future admin workflow because Telegram Bot API cannot undelete the
  original message in place.
- Phase 4 is wired for core feedback: LLM/vote fingerprint updates, false-positive downgrades,
  reputation changes, learned phrase fingerprint lookup, and repeat-window fast bans for
  link-bearing messages from new/low-rep senders.
- Phase 5 is wired for the high-value OG cases: short text plus preview URL, and punctuation-only
  preview-card messages. Broader OG fetching can be enabled later after observing cost and abuse
  patterns. OG requests pin the already validated public IP for the actual connection to avoid DNS
  rebinding between validation and fetch.
- Sender profile context is wired: username/display name is stable from each message, while bio
  is best-effort and cached because Bot API may not expose it for ordinary group users. Explicitly
  spammy bio content is now a local ban signal, including on join service messages.
- Personal-channel cross-checking is wired for suspicious message bodies. The latest three text or
  caption values are read live and included in the LLM payload; no personal-channel cache/history
  table or channel-content action-log snapshot is retained.
