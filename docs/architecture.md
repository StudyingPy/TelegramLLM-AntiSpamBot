# Architecture Notes

## Pipeline

1. Telegram message handler receives group/supergroup messages.
2. Feature extraction parses links from text, `text_link` entities, and link previews.
3. For short text with `link_preview_options.url`, the OG fetcher validates the URL,
   blocks private/internal destinations, limits redirects/bytes/time, and extracts
   title/description text for the LLM payload.
4. Text is normalized and converted to content/skeleton fingerprints.
5. User profile context is cached from message sender fields. Bio is fetched best-effort via
   `get_chat(user_id)` when Bot API exposes it, and treated as a weak LLM signal only.
6. Local rules check known fingerprints, reputation, repeat windows, and hard carrier signals.
7. Decisions are applied by the action layer:
   - allow/review only logs observations
   - withdraw + vote deletes the message and opens an inline vote session
   - ban deletes the message and bans when permissions allow it
8. Vote callbacks update `vote_sessions`, `vote_session_votes`, reputation, and action logs.
9. A background sweeper expires stale open vote sessions as `expired_released`, logs the
   default-release action, and edits the vote message when Telegram allows it.
10. Feedback updates fingerprints and reputation:
   - LLM spam creates medium-weight skeleton/phrase fingerprints
   - vote-confirmed spam boosts skeleton/content fingerprints
   - vote-released messages mark false positives and lower fingerprint weight

## Phase Status

- Phase 0 is wired: bot runner, SQLite schema, permission checks, message pipeline.
- Phase 1 is wired: local extraction and hard rules.
- Phase 2 is wired: `llm.py` calls an OpenAI-compatible NewAPI gateway when `NEWAPI_*`
  environment variables are present, parses strict JSON, and falls back to local rules on
  timeout, transport errors, or invalid model output.
- Phase 3 is partially wired: button votes, vote changes, threshold close, action logs, and
  timeout default release are active. Message restoration remains a future admin workflow
  because Telegram Bot API cannot undelete the original message in place.
- Phase 4 is wired for core feedback: LLM/vote fingerprint updates, false-positive downgrades,
  reputation changes, learned phrase fingerprint lookup, and repeat-window fast bans for
  link-bearing messages from new/low-rep senders.
- Phase 5 is wired for the high-value OG case: short text plus preview URL. Broader OG fetching
  can be enabled later after observing cost and abuse patterns. OG requests pin the already
  validated public IP for the actual connection to avoid DNS rebinding between validation and fetch.
- Sender profile context is wired: username/display name is stable from each message, while bio
  is best-effort and cached because Bot API may not expose it for ordinary group users.
