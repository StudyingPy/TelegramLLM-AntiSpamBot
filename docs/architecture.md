# Architecture Notes

## Pipeline

1. Telegram message handler receives group/supergroup messages.
2. Join service messages with `new_chat_members` are converted into profile checks for the
   joined users, so spammy profile bio can be handled even when the service message has no text.
3. Feature extraction parses links from text, `text_link` entities, and link previews.
4. For short text with `link_preview_options.url`, the OG fetcher validates the URL,
   blocks private/internal destinations, limits redirects/bytes/time, and extracts
   title/description text for the LLM payload.
5. Text is normalized and converted to content/skeleton fingerprints.
6. User profile context is cached from message sender fields. Bio is fetched best-effort via
   `get_chat(user_id)` when Bot API exposes it, and explicit bio spam signals are handled locally.
7. Local rules check known fingerprints, reputation, repeat windows, repeated open votes, profile
   bio signals, and hard carrier signals.
8. Decisions are applied by the action layer:
   - allow/review only logs observations
   - withdraw + vote opens an inline vote session while preserving the original message for review
   - ban deletes the current hit plus the same user's open-vote suspect/vote messages, bans when
     permissions allow it, and posts a group summary that is deleted after 2 minutes
9. Vote callbacks update `vote_sessions`, `vote_session_votes`, reputation, and action logs.
10. Confirmed-spam vote callbacks close all open vote sessions for the suspect user, clean the
   related suspect messages and bot vote prompts, ban the user, and update admin notifications.
11. A background sweeper expires stale open vote sessions as `expired_released`, logs the
   default-release action, and edits the vote message when Telegram allows it.
12. Feedback updates fingerprints and reputation:
   - LLM spam creates medium-weight skeleton/phrase fingerprints
   - vote-confirmed spam boosts skeleton/content fingerprints
   - vote-released messages mark false positives and lower fingerprint weight

## Phase Status

- Phase 0 is wired: bot runner, SQLite schema, permission checks, message pipeline.
- Phase 1 is wired: local extraction and hard rules.
- Phase 2 is wired: `llm.py` calls OpenAI-compatible NewAPI gateways when `NEWAPI_*`
  environment variables are present, supports comma-separated multi-provider fallback, parses
  strict JSON, and falls back to local rules only after all providers time out, fail transport, or
  return invalid model output.
- Phase 3 is wired: button votes, vote changes, threshold close, action logs, timeout default
  release, confirmed-spam cleanup, admin skip-ban cleanup, and transient group summaries are active.
  Message restoration remains a future admin workflow because Telegram Bot API cannot undelete the
  original message in place.
- Phase 4 is wired for core feedback: LLM/vote fingerprint updates, false-positive downgrades,
  reputation changes, learned phrase fingerprint lookup, and repeat-window fast bans for
  link-bearing messages from new/low-rep senders.
- Phase 5 is wired for the high-value OG case: short text plus preview URL. Broader OG fetching
  can be enabled later after observing cost and abuse patterns. OG requests pin the already
  validated public IP for the actual connection to avoid DNS rebinding between validation and fetch.
- Sender profile context is wired: username/display name is stable from each message, while bio
  is best-effort and cached because Bot API may not expose it for ordinary group users. Explicitly
  spammy bio content is now a local ban signal, including on join service messages.
