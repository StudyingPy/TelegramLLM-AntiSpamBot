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
- `telegram_llm_antispam.jev_eval` is the read-only Jev experiment CLI. It opens SQLite with URI
  `mode=ro` and `PRAGMA query_only=ON`; never wire it into production handlers.
- Jev uses TypeSafe `POST /v1/systemone`, not OpenAI Chat Completions. Fixed Questions are
  `is_spam` (Noul) and `category` (Choice); persist the prompt SHA-256 and keep Noul probability
  separate from Choice confidence.
- The production DB has no time-versioned raw-message table. Body snapshots are mainly in
  `action_log.metadata_json.text_snapshot` and vote detail text. `allow`/`review` rows usually
  lack bodies; timeout release and hard-rule actions are not human truth by default.
- Historical reputation, OG and personal-channel state are not reliably reconstructable; export
  marks them missing instead of reading current values. Evaluation output is gitignored.

## Jev first-round label audit (2026-10-19; cache only)

- Never infer message-level human truth from `vote_sessions.status`. `_finalize_spam_user`
  closes sibling sessions; `auto_banned_user` also sets `confirmed_spam` with no human review.
  The 726 old positives split into 187 automatic, 18 propagated votes, 69 ambiguous multi-session
  admin closures, and 452 direct-human-event candidates (still require semantic evidence).
- Vote trigger is `trigger_vote_session_id`; sibling spam_votes metadata can be copied from the
  trigger. Admin multi-session events do not persist primary ID; list order is not evidence.
- `jev_audit` consumes frozen predictions only, preserves original labels, marks insufficient
  input/unknown labels excluded, and pairs identical rows for both models. Legacy common 423
  positives: original detected114, Jev62; these polluted labels cannot establish real Recall.
- Fixed seed20260921 audit of50 old misses includes32 empty bodies; 57 evidence reviews
  (50 misses+7 releases) are Codex reviews, not new human gold. Never infer ham account from
  release of one message, or fill historical Bio from current user_profiles, even with nearby dates.
- Action snapshots stop at500 chars, notification detail at800; boundary/longer/conflicting
  snapshots remain incomplete. Audit context recovery is separate from actual cached Jev input.
- Normal annotation seeds:7 confirmed releases (4 unique texts),6 pending normal-body candidates,
  plus71 current edit-watch baselines (33 users/1 chat), all71 pending. New baselines are a different
  time cohort and may include spam; they have no cached predictions. All raw audit artifacts are
  gitignored in `jev-audit-output/`. API calls remain paused; no production moderation change.

## Selective edited-message monitoring

- Telegram `edited_message` updates contain only the latest revision. To detect a clean
  message being turned into an ad, `edit_watch_messages` stores a 24-hour, 4,000-character
  baseline only for human accounts with fewer than five messages or reputation at/below
  `LOW_REPUTATION_THRESHOLD` (default 35). Do not expand this to every member without a
  retention/privacy review.
- A watched edit with links, mentions, diversion phrases, or hard ad signals is tagged
  `edited_message_needs_llm`; the previous text is sent as bounded `edited_from` context.
  Keep the original baseline while a vote/ban is pending; harmless edits replace it.

## Checklist message extraction

- Bot API checklist content lives in `Message.checklist` (`title`, `tasks[].text`, and
  their entity arrays), while client/MTProto exports expose the equivalent structure as
  `media.todo` (`title.text`, `list[].title.text`). Flatten both forms into the ordinary
  message text and link pipeline so rules, fingerprints and the LLM see the whole list.
- Checklist `text_link` entities may hide URLs behind task labels. Extract those entity
  URLs in addition to running the ordinary URL regex over the flattened task text.

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

- Jev observation implementation defaults to `JEV_MODE=off`;
  `shadow` runs Jev first then the existing model on one frozen `_feature_payload`.
  Only the original outcome may enter enforcement/reputation/fingerprint feedback.
  Independent expiring sidecar, bounded budget/concurrency/timeout and explicit proxy;
  2026-10-19 user explicitly authorized production shadow deployment. See
  `docs/jev-online-observation.md` for configuration and CLI.
- `jev_live_report` compares identical IDs/hashes with two independent message labels,
  groups Questions/model/provider versions, and never derives message gold from
  account bans or sibling vote closures. New `primary_vote_session_id` metadata identifies
  the actual human click target; old missing values remain unknown. Keep live exports
  under ignored `data/`, preserve historical `jev-eval-live2` caches.
- Local Jev completion validation: 270 tests passed (40 Jev observation tests),
  scoped Ruff and diff whitespace checks passed before rollout.

## Jev shadow production rollout (2026-10-19)

- Deployed commit `c303b72` with the existing `deploy/install.sh update` workflow;
  service active, polling restored, `NRestarts=0`, dependency check passed.
- Explicit runtime configuration: shadow, pinned `jev-1.13.0`, existing allowed
  chats `-1002244262360,-1002102431475`, sample 0.1, UTC daily cap 200,
  timeout 3s, concurrency 2, retention 7 days, no local-bypass calls.
  Original MiMo/fallback configuration and enforcement thresholds preserved.
- Key entered through masked input and SSH stdin, stored only in server `.env`
  with mode600; do not copy it into docs, commands or source. Startup logging
  confirms the effective mode without including secrets; sidecar mode is600.
- Preflight used one synthetic technical message: native API succeeded in484ms,
  2451 input/110 output tokens, Noul .02/category benign. Synthetic smoke data
  must remain separate from real observation accuracy/latency metrics.
- Recovery point: `/opt/telegram-llm-antispam-bot/data/backups/pre-jev-20261019-095013-147653/`
  contains `env.backup`, `moderation.sqlite` (integrity_check ok), package versions
  and previous commit `7bb2488`. To disable observation set JEV_MODE=off and
  restart; do not restore the old moderation DB and lose intervening live events.
- Deployment helper/preflight copies are ignored under `data/jev-rollout/`.
  Live reports default to `data/jev-live-export/`. Historical batch reruns remain
  paused. Model agreement is not accuracy; human consensus remains required.
- Post-deploy paired synthetic smoke succeeded under the service user: Jev473ms,
  original MiMo5883ms, identical input hash, no actions invoked. Stored separately
  in `data/jev-rollout/synthetic-observations.db`. Initial real report had0 rows;
  do not confuse smoke success with real chat accuracy. Export directory mode700.

## Jev full primary / MiMo review rollout (2026-10-19, supersedes shadow runtime)

- 2026-10-20 prompt/input lab: production unchanged. Private frozen export20 records
  (11 joins,1 empty ordinary,8 nonempty including1 truncated edit), 20 successful
  model pairs,18 agreements, no independent consensus gold. Of6 untruncated MiMo
  positive messages, original Noul>=.5 catches4; short Chinese question/full frozen
  state catches5. Never call this human Recall. Plain body fixes one covert adult
  sample but loses one Bio-supported diversion sample; English is not uniformly better.
- Lab tool `jev_prompt_lab.py` + docs `jev-prompt-experiments.md`:32 fixed real inputs
  (20 live +12 pending chat candidates),6 variants,157 deduped API requests allOK,
  260396 input/17202 output tokens. Data/results ignored in `data/jev-tuning/results/`;
  raw zero-width characters retained. No changes to production prompt, thresholds,
  provider settings or feedback. Cached-only report rebuild is default; API requires
  explicit --call-api with cap. More independent holdout labels needed before rollout.
- Final prompt-lab verification:286 tests passed; scoped Ruff/diff checks passed.
  Production rechecked at4cff3f2, review mode + MiMo-only reviewer, active/NRestarts0.
- Old Playground request not found as a complete model/Questions/state record in
  accessible local chat/history inventory; do not claim exact reproduction. Native
  docs confirm independent questions and warn about literal reading, excess context
  and conflicting criteria. Short prompt rephrases semantics as well as length.

- User requested every message-model request go through Jev first, await MiMo
  second/review, and pause jyai.i71.uk/gpt-5.4-mini and api.guicore.com/gpt-5.6-terra.
  Deployed `4cff3f2`; service active and polling, NRestarts0. Active NewAPI list now
  contains only api.xiaomimimo.com / mimo-v2.6-flash; old credentials remain in the
  protected backup, not in command arguments or source. Existing Jev key preserved.
- `JEV_MODE=review`, sample1, daily cap0, all moderated model-request groups. In
  review mode no sampling/cache/cap/cooldown or concurrency skips; semaphore queues
  primary requests and keeps per-call3s timeout. Sidecar failure still calls Jev
  and reviewer; report records modes separately. Existing hard-rule-only/admin/
  whitelist paths remain outside model routing; fingerprint quality audit uses MiMo.
- Enforcement, reputation and learning use successful independent MiMo review;
  disagreement follows MiMo, reviewer unavailable becomes REVIEW (even if the local
  model-requested hit was BAN). Primary failure still waits for MiMo. Administrator
  detail now exposes Jev primary result, MiMo review and conflict/pending status.
- Validation:280 local mocked tests passed, scoped Ruff and diff checks passed.
  Previous runtime recovery point:
  `/opt/telegram-llm-antispam-bot/data/backups/pre-jev-20261019-100608-881212/`
  (env + SQLite integrity-ok backup, previous code c303b72). Disable with modeoff
  and restart if needed. Switching back to shadow also needs a positive daily cap
  and explicit chat allowlist, since review configuration uses cap0/empty groups.

- Use `.venv/Scripts/python.exe` and `.venv/Scripts/ruff.exe`. In the restricted
  Windows sandbox, async tests can hang before executing their coroutine because
  `asyncio`'s event-loop `socketpair()` blocks in `socket.accept()`. A pytest
  `faulthandler_timeout` identifies this; run the mocked test suite outside the
  sandbox instead of changing application/event-loop code to work around it.

## Production deployment

- 2026-10-20 user approved deploying the tested short Chinese/full-context variant.
  Commit `6e05725` promotes exactly the lab's `short_zh_full` Questions; policy
  `telegram-message-short-zh-v2`, hash
  `c6163e85797b1001f9dd2d6a1aa8fd776f31fe22ce2294093ac8c6d3d343cbdd`.
  Full `_feature_payload`, MiMo prompt/review authority, thresholds, model versions,
  paused backup providers and runtime settings stay as before. Legacy long Questions
  remain `LEGACY_QUESTIONS` so lab controls/cache replay do not silently change.
- Pre-deploy backup integrity passed at
  `/opt/telegram-llm-antispam-bot/data/backups/pre-jev-20261019-172220-761777/`
  (server timestamp is UTC; user date is2026-10-20 Hong Kong). Previous commit4cff3f2.
  287 local regression tests and scoped Ruff passed before deployment.
- Rollout completed:6e05725 active, polling restored, NRestarts0; startup confirmed
  v2/hashc6163e85. Active Questions exactly match server lab manifest; `.env` is
  byte-identical to the pre-deploy backup. Real synthetic dual-model smoke: Jev474ms,
  MiMo2097ms, identical input hash, bothOK, no actions. Test DB is separate at
  `data/jev-rollout/synthetic-short-zh-v2-20261020.db`; do not count as live gold.

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
- PowerShell here-string pipelines to `ssh ... bash -s` can append a trailing CR
  to the last command argument. Prefer a directly quoted SSH command for health
  checks, or explicitly strip CR from streamed shell scripts before Bash reads them.
- 2026-10-14: deployed `46d07a7` using update mode; remote worktree clean, 500-character
  threshold effective, 3 LLM providers enabled, service active with zero restart-loop
  count and Bot polling restored. Remote offline rule/merge smoke checks passed.

## Generic phrase fingerprints and release feedback (2026-10-15)

- Read-only production audit: action 19780 / vote 1070 (`居然机器人没头像`) matched
  phrase fingerprint 9055, `机器人`, source `llm_spam_phrase`, weight 60, two hits.
  The LLM said benign at 0.99, but short-message `_merge_llm_decision` keeps the
  local fingerprint WITHDRAW_VOTE. Earlier action 18310 (`机器人又来刷屏了`)
  also matched this phrase and received a benign LLM judgement.
- The current phrase minimum allows ordinary three-character nouns such as `机器人`.
  Length alone does not ensure an advertising phrase is distinctive.
- Administrator release calls `record_vote_ham_feedback`, which only penalizes the
  session's content/skeleton hashes. It misses the actual phrase fingerprint ID in
  the original action metadata. Fingerprint 9055 consequently still had weight 60
  and false_positive_count 0 after vote 1070 was released. Future fixes should
  target the actual matched fingerprint, preserving the release idempotency gate.
- This audit changed neither production data nor moderation logic.

## Trust tiers and fingerprint quality review (2026-10-15)

- Implemented protection requires all three: 72 observation hours, 30 distinct
  model-benign new messages, reputation >=80. Rejoin resets `joined_at`; legacy
  accounts use `first_seen_at`. Normal history is independent of ALLOW-only +2
  rewards; edits, joins and duplicate message IDs do not count. Existing reputation
  is preserved and historical positive-reward records are backfilled without rewards.
- Global/group/anonymous administrators bypass message moderation. New-member
  moderation targets invitees even when the inviter is an administrator/whitelisted.
  Role caches live 300 seconds and are invalidated by chat_member updates, which
  also record joins. Identity lookup failures suppress destructive enforcement.
- `user_trust` is the shared protection flag for rules, repeat shortcuts and LLM
  decisions. Trusted local BAN/VOTE hits become REVIEW before calling the model;
  benign releases, spam >=70% votes, model failure remains REVIEW. Confirmed ad-Bot
  repeat invocation and human-confirmed ban execution retain their existing paths.
- FingerprintStore extends SQLite with additive columns and idempotent maintenance
  marks: `trusted_history_v1`, `fingerprint_evidence_v1`, `phrase_ham_feedback_v1`.
  Actual matched phrase IDs receive release feedback once per session/ID; timeout
  releases are excluded. Match counts alone do not strengthen weights.
- Daily audit defaults: 1,000 entries/run, <=10 entries/request, bounded request
  context, three <=800-character message samples. Missing readable patterns are
  insufficient evidence. Failed batches remain pending and retry after 300 seconds;
  restart resumes the same run. A finished run is not repeated until its start is
  at least 24 hours old.
- New readable learning only comes from group/OG content; Bio/personal-channel
  context is not added to persistent evidence. Unknown historical local hits are
  labeled `local_hit`, not invented LLM or human spam confirmation.
- Audit output must contain exactly the supplied IDs and valid keep/disable/
  insufficient_evidence decisions. Disable confidence >=0.90 creates a proposal,
  never an automatic disable. Keep decisions and pending proposals suppress duplicate
  recommendations for unchanged evidence. Disabled fingerprints reject automatic upserts.
- Digest and all private `/fingerprints`, `fp_review` deep-link and `fp:` callbacks
  are limited to configured reviewer 5439265612. One digest per 24 hours; send failure
  retains unnotified proposals. Disable/keep/restore callbacks are transactional and
  idempotent; an operation ledger preserves prior actions. Disabled entries are restorable.
- Deployment backup created with SQLite backup API (integrity_check passed):
  `/opt/telegram-llm-antispam-bot/data/backups/bot-pre-trust-review-20261015-025342-846566.sqlite`.
- Deployed `7bb2488`; 213 tests and production Ruff passed. Live migration restored
  167 normal messages for user 6336926031 in chat -1002102431475 and confirmed trusted
  protection. Fingerprint 9055 recovered `机器人`, two missed human-ham feedback
  events lowered its weight to 5 without automatically disabling it.
- Initial rollout flushed the first five real recommendations with `notify_ready`
  to 5439265612 while the first 1,000-item run continued in the background. Delivery
  was acknowledged and notification timestamps persisted; the live list keeps growing
  while the daily digest cooldown prevents additional messages within 24 hours.

## Fingerprint review UX research (2026-10-15; proposal, not implemented)

- Official Bot API currently lists 10.3 (2026-08-24). Private-chat topics can be enabled
  through BotFather and routed with `message_thread_id`; do not assume old private-chat
  limitations. Topic organization does not prove independent notification behavior on
  every client. Private-chat pinning always suppresses pin notifications.
- Recommended direction: separate review Bot DM for low-volume notifications plus a
  Mini App for evidence comparison, filtering and batch review. Same-Bot private topics
  are the lower-cost alternative. Both must restrict all reads/writes and digests to
  5439265612. New Bot requires initial Start; no Bot API setting overrides user muting.
- `setChatMenuButton(chat_id=...)` can expose a permanent Mini App entry only to reviewer.
  Validate signed `initData` and freshness server-side, then exact user ID. Do not trust
  `initDataUnsafe`; `sendData` is keyboard-launch-only and closes the app, unsuitable for
  continuous menu-launched reviews. HTTPS API can coexist with existing polling.
- Rich Messages now support details/tables/embedded buttons, but local aiogram 3.28.2
  lacks `send_rich_message` and the newer disabled-button field; verify SDK/API support
  before choosing a native-rich-message implementation.
- Keep the existing no-duplicate/one-digest-per-24h rules; a live pinned panel is an entry,
  not a notification substitute. Batch actions need explicit selection, version checks
  and per-item idempotency; opening a queue never means approving its suggestions.
- Detailed proposal and official sources: `docs/fingerprint-review-ux.md`.

## Production LLM endpoint (2026-10-17)

- Production `.env` primary provider is `https://api.xiaomimimo.com`; it was upgraded
  from `mimo-v2.5` to `mimo-v2.6-flash`. Fallbacks remain `gpt-5.4-mini` on
  `jyai.i71.uk` (currently returns model_not_found) and `gpt-5.6-terra` on
  `api.guicore.com`. Restart the service after `.env` edits.
- MiMo V2.6 model IDs require the tier suffix: `mimo-v2.6-flash`, `mimo-v2.6-pro`,
  `mimo-v2.6-pro-ultraspeed`. There is no plain `mimo-v2.6` model ID.
- Official list price in CNY per million tokens (2026-09): Flash cache-hit 0.02,
  input 1, output 2; Pro 0.025 / 3 / 6. Batch API is 50% of real-time. V2.6 keeps
  the V2.5 price level.
- Config backup: `.env.bak-20261017-223832`. Verify edits with
  `antispam-admin show-config`, `systemctl is-active`, polling startup, and a 200
  from the endpoint. `sed -i` preserved `antispambot` ownership and mode 600 here.

## Recruitment-message diagnostics (2026-10-21; read-only investigation)

- Unmatched ordinary messages always request the model (`unmatched_message_needs_llm`),
  including text without links. The hard body rule separately requires a carrier;
  `兼职`/`日结` alone therefore do not locally ban. The model payload has no recent
  conversation/message-history field, so differently worded recruitment posts are
  evaluated individually unless an existing repeat/fingerprint rule applies.
- Runtime verified: Jev review mode, MiMo `mimo-v2.6-flash`, vote threshold .70,
  ban threshold .85. Jev probability does not determine enforcement. MiMo spam
  confidence below .70 becomes `REVIEW`: action-log/admin-notification path only,
  no deletion or group vote. MiMo `is_spam=false` becomes `ALLOW` even at low confidence.
- Exact supplied recruitment texts had earlier same-author/same-chat records at
  2026-10-20 20:21–21:36 Hong Kong: Jev flagged some at .70–.87 while MiMo used
  spam .55/.62 or benign .15/.18/.35, yielding only REVIEW/ALLOW. These are not
  confirmed original-sender matches by ID. The user corrected the pasted 22:12
  timestamp: it was their handling time, not the original send time. Do not use
  absence at 22:12 to claim lost Telegram updates or a moderation bypass.
- Production diagnostics should use SQLite URI `mode=ro` plus `query_only=ON`,
  exact supplied-message matching and minimal decision fields. Root Git inspection
  needs per-command `git -c safe.directory=/opt/telegram-llm-antispam-bot ...`;
  avoid changing global Git configuration. No moderation/configuration was changed.

## Obfuscated recruitment miss audit (2026-10-31; read-only)

- The live database is currently `/opt/telegram-llm-antispam-bot/data/bot.db`; older
  recovery points use `moderation.sqlite`. Do not assume the backup filename is the
  active database. Read it with SQLite URI `mode=ro` and `PRAGMA query_only=ON`.
- In the 2026-10-30 through 2026-10-31 Hong Kong window, `action_log` had 287 rows:
  10 `mimo_review_unavailable` reviews and 7 `llm_low_confidence` reviews. The
  supplied manual samples concentrated in these paths: `果聊48r` had Jev 40% with
  MiMo parse failure; the computer-income message had MiMo 62% traffic-diversion
  review; `想作诗 看朱*叶 简*单`, `简单莱作.事 美曰·可专`, `响正/米跟上看猪*耶`,
  and `有·手就 能zuo 看头/香` were repeatedly low-confidence or provider-failure
  reviews before manual handling. `包*转/签的莱吧好~作` eventually reached a
  repeat-open-vote auto-ban, so do not count every pasted sample as a miss.
- Current enforcement thresholds are MiMo spam confidence `<0.70` → `REVIEW` and
  `>=0.85` → `BAN`; a failed independent MiMo review is forcibly converted to
  `REVIEW` even when Jev or a local rule had stronger spam evidence. This protects
  against false bans but leaves short obfuscated ads without deletion or a vote.
- These counts are diagnostics, not Recall: the production schema lacks a complete
  human-gold message table, and manually banned messages may have only notification
  text or no action-log row. Preserve this distinction in future reports.

## MiMo refusal versus transport failure (2026-10-31; diagnosis only)

- In the 2026-10-30 through 2026-10-31 production window, all 10 `mimo_review_unavailable`
  rows were response-content failures: 4 `empty content` and 6 invalid JSON responses.
  No timeout, connection, or API error appeared in those rows. This makes safety refusal
  or provider-side content filtering plausible, but not proven.
- `src/telegram_llm_antispam/llm.py` currently stores only a generic error string after
  parsing. Future diagnostics must record bounded metadata such as HTTP status, response
  content type/length, `finish_reason`, message-level `refusal` presence, provider model,
  elapsed time, and a raw-response hash. Never persist raw sensitive refusal text.
- Treat `empty content`, `parse_error`, transport failure, API error, and timeout as
  separate operational classes. A refusal/empty-content response may justify a guarded
  local vote fallback when local evidence is strong; transport/API failures need retry,
  provider failover, and alerting instead.
- `message_observations` still stores only hashes and link domains; the new
  `recent_user_messages` sidecar supplies bounded short-lived bodies for clustering.
  Do not reconstruct history from `user_profiles` or assume `action_log` has bodies for
  allow/review rows.
- The moderation prompt already mentions a few obfuscations (`近裙/进裙/今群`, `壹`,
  `sou款`), but the October 31 samples show additional separator, homophone, and mixed
  Latin/CJK variants. Add normalization as a contextual signal, requiring a carrier or
  recruitment intent before vote escalation; obfuscation alone is not an auto-ban rule.

## Jev-only fallback and recent-message clustering (2026-10-31)

- When `JEV_MODE=review` receives a valid Jev result but the MiMo reviewer fails,
  enforcement now uses Jev's Noul probability as a one-model judgement and records
  `model_chain.decision_source=jev_only`. A failed Jev primary still remains pending;
  this fallback does not invent a confidence value from a missing reviewer.
- `recent_user_messages` is a separate bounded sidecar: default 24-hour retention,
  at most 24 stored candidates per chat/user, and at most 6 recent/similar messages in
  the model payload. Ranking prefers the same skeleton, then SimHash distance, then
  recency. Action logs retain only cluster counts/IDs, not the body snapshots.
- `antispam-admin moderation-metrics --hours N` is read-only. Its manual-ban-after-review
  and manual-ban-after-allow values are observable miss proxies, not Recall; external
  moderator bans and messages without bot action rows remain unobserved.

## Fingerprint source guard and library management (2026-10-31)

- LLM `signal_phrases` are model-selected hints only. A phrase fingerprint may be
  persisted only when its normalized text occurs in the group message or OG preview;
  profile/personal-channel context and model summaries cannot create one.
- Fingerprint evidence migration must apply the same source check to historical
  `signal_phrases` and `fingerprint_id` metadata. Maintenance mark
  `phrase_source_guard_v1` removes legacy `llm_spam_phrase` rows without an anchored
  source sample. Keep this guard when changing action-log recovery.
- Private `/fingerprints` for the configured reviewer now opens the pending review
  list and a paginated library view. Library deletion is callback-confirmed and
  cascades its stored evidence/review rows through SQLite foreign keys.

## Fingerprint review Mini App integration (2026-10-31)

- The review WebUI is an optional Telegram Mini App embedded in the bot process. Keep
  `WEBUI_ENABLED=false` by default; when enabled, bind the aiohttp server to loopback and
  expose it through an HTTPS reverse proxy. Telegram entry buttons are emitted only when
  `WEBUI_PUBLIC_URL` is a valid `https://` URL.
- Authenticate only with the signed Telegram Web App `initData`: verify the HMAC and freshness
  before parsing identity, then require the exact `FINGERPRINT_REVIEW_USER_ID` on sign-in and every
  bearer-token request. Never use `initDataUnsafe` for authorization.
- The WebUI and bot callbacks share one `Database` instance. Review and deletion writes use the
  version token plus the request-ID ledger; preserve these checks when extending batch actions.
  End-user WebUI and bot text remains Simplified Chinese; agent handoff text remains English.
- On this Windows sandbox, aiohttp/asyncio tests that create or close loop socketpairs can hang
  inside the event loop before the coroutine runs. Framework-free `tests/test_webui.py` remains
  the reliable local validation; run the real aiohttp listener and Telegram Mini App checks on a
  normal Linux deployment or a real Telegram client.
