# Fingerprint Review WebUI — Implementation Note

Date: 2026-10-31. Audience: project owner and the primary integration agent.
Status: implemented and tested in a sandbox. **Not deployed, not opened in a real Telegram
client, and the aiohttp adapter has not been executed** (see "Test commands and results").

## 1. Architecture and rationale

**Telegram Mini App served by an HTTP server embedded in the existing bot process.**

```
Telegram client ──HTTPS──> reverse proxy (Caddy/nginx) ──HTTP──> 127.0.0.1:8088
                                                                  │  aiohttp (same asyncio loop
                                                                  │  as aiogram polling)
                                                                  ▼
               WebUIApi (framework-free routing, auth, validation, JSON)
                                                                  ▼
               Database  ←── shared instance + RLock ──→  polling handlers / audit worker
               (FingerprintWebStore mixin: version-checked, idempotent transactions)
```

Why this shape:

- **Same process, same `Database` object.** Bot callbacks, the audit worker and WebUI actions all
  go through one connection guarded by the existing `RLock`, so they are serialized without new
  locking. Writes additionally use `BEGIN IMMEDIATE`, so a second process (for example
  `antispam-admin delete-fingerprint`) cannot change a row between the version check and the
  update.
- **No new runtime dependency.** aiohttp is already installed by aiogram; it is now also listed
  explicitly in `pyproject.toml` because the WebUI imports it.
- **Polling stays as is.** The Mini App API does not need webhooks. The server is off by
  default (`WEBUI_ENABLED=false`); when on, a bind failure is logged and the bot keeps polling.
- **Framework-independent core.** `webui/api.py` maps a plain `ApiRequest` to an `ApiResponse`.
  All routing, auth, validation and escaping is unit-tested without a server; `webui/server.py`
  is a ~40-line aiohttp adapter.
- **Static frontend, no build step.** `webui/static/{index.html, app.css, app.js}`: vanilla JS,
  Telegram theme variables, no inline script or style, CSP enforced.

Entry points (only when `WEBUI_ENABLED=true` and `WEBUI_PUBLIC_URL` is an `https://` URL):

- the reviewer's private-chat **menu button** "复核台" (`setChatMenuButton` scoped to
  `FINGERPRINT_REVIEW_USER_ID` only; disable with `WEBUI_MENU_BUTTON=false`);
- an "打开复核台" `web_app` button on the daily digest, `/fingerprints` and `/fingerprint_library`.

Existing inline-button flows (`fp:` callbacks) are unchanged and keep working in parallel.

## 2. API routes and Telegram authentication

### Authentication flow

1. The page loads `https://telegram.org/js/telegram-web-app.js` and reads
   `Telegram.WebApp.initData` (the signed query string). `initDataUnsafe` is never used.
2. `POST /api/session {"init_data": "..."}`. The server (`webui/auth.py`):
   - rejects input over 8 KB, malformed query strings and **duplicate keys**;
   - computes `secret = HMAC_SHA256(key="WebAppData", msg=bot_token)` and
     `HMAC_SHA256(secret, data_check_string)` over all fields except `hash`, sorted, `\n`-joined;
     compares in constant time;
   - only after the signature passes, checks `auth_date`: older than
     `WEBUI_INIT_DATA_MAX_AGE_SECONDS` (default 600) → expired; more than 60 s in the future →
     rejected;
   - parses `user`, requires an integer `id`, and requires
     `id == Settings.fingerprint_review_user_id` (403 otherwise).
3. On success the server returns a random opaque bearer token (`secrets.token_urlsafe(32)`),
   held in memory with an idle timeout (`WEBUI_SESSION_TTL_SECONDS`, default 1800) and a 12 h
   absolute cap (max 32 sessions). The client keeps it in `sessionStorage` (falls back to memory).
4. Every other `/api/*` call sends `Authorization: Bearer <token>`. Each request re-resolves the
   session and re-checks the reviewer ID. A 401 makes the client re-run step 2 once with the
   current `initData`; if that is stale too, the page asks the reviewer to reopen the Mini App.

The bot token used for verification is `TELEGRAM_BOT_TOKEN` (the Mini App is opened from the
existing bot). Sessions do not survive a restart; the reviewer simply reopens the Mini App.

### Routes

All JSON is UTF-8, `Cache-Control: no-store`. Errors are `{"error": {"code", "message"}}` with a
Simplified Chinese `message`; codes are stable English identifiers for clients/logs.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET/HEAD | `/`, `/index.html`, `/static/app.js`, `/static/app.css` | none | Static shell (CSP on HTML) |
| GET/HEAD | `/healthz` | none | `200 {"status":"ok"}` after a `SELECT 1`; `503 {"status":"error"}` otherwise |
| POST | `/api/session` | initData | Body `{"init_data"}` → `{"token","expires_in","display_name"}` |
| GET | `/api/meta` | bearer | Chinese labels for all codes, filter options (types/sources), sort keys, limits |
| GET | `/api/summary` | bearer | Review counts by status, pending-with-human-ham count, oldest pending, library totals, latest audit run progress |
| GET | `/api/reviews?status=pending\|kept\|disabled\|restored&q=&type=&offset=&limit=` | bearer | Review queue/history page |
| GET | `/api/reviews/{id}` | bearer | Review detail: fingerprint, ≤3 samples, model reason, action history, `version` |
| POST | `/api/reviews/actions` | bearer | Body `{"request_id","action":"keep\|disable\|restore","items":[{"review_id","version"}]}` (1–50 items) |
| GET | `/api/fingerprints?q=&type=&enabled=&source=&weight_min=&weight_max=&hits_min=&hits_max=&fp_min=&fp_max=&sort=&offset=&limit=` | bearer | Library page; `sort` ∈ `weight, hits, false_positives, newest, last_hit` |
| GET | `/api/fingerprints/{id}` | bearer | Library detail incl. `reviews[]` and `delete_impact{evidence_count, review_count}` |
| POST | `/api/fingerprints/{id}/delete` | bearer | Body `{"request_id","version","confirm":true}` |

`q` matches readable `pattern_text` (LIKE with `%`/`_` escaped), `#123`/`123` also matches the ID,
and a hex string of ≥ 6 chars also matches the fingerprint value prefix. `limit` ≤ 50.

Action responses report every item:

```json
{"request_id": "…", "action": "disable", "replayed": false,
 "results": [{"review_id": 12, "status": "ok", "message": "已停用，该指纹将停止匹配。"}],
 "summary": {"ok": 1, "already_done": 0, "stale": 0, "not_found": 0, "invalid": 0}}
```

| Item status | Meaning |
| --- | --- |
| `ok` | Applied now |
| `already_done` | The review is already in the target state (double tap, other tab, bot button) |
| `stale` | State or evidence changed since the reviewer loaded it; nothing written |
| `not_found` | Review or fingerprint no longer exists |
| `invalid` | Current state does not allow this action (e.g. restore on a pending review) |

Single-item requests also return the fresh `review` (or `fingerprint` for delete) so the page can
reload the current item in place. Delete without `"confirm": true` is a 400; a stale version
deletes nothing.

## 3. Screen and interaction specification (UI text is Simplified Chinese)

Shell: title "指纹复核台", "刷新" link, a one-line summary (e.g. "待复核 4 条，其中 2 条有人工误判证据，
最久已等待 4 天。上次扫描完成于 10-31 09:31。" or "质量扫描进行中，剩余 120 / 1000 条。"), and four tabs
with counts: **待复核 / 已保留 / 已停用 / 指纹库**. Mobile shows one column; ≥ 900 px shows list and
detail side by side. Colours come from the Telegram theme (light/dark); the matched phrase is
highlighted the same way in the detail heading and inside every evidence sample.

- **List** — search box "搜索短语、模板或指纹编号" (debounced), "筛选" panel. Review tabs filter by
  指纹类型; the library adds 状态, 来源, 权重 range, 命中次数 range, 误判次数 range and 排序.
  Rows show the readable pattern (or "未能恢复原文"), type, 权重 / 命中 / 误判, a "有人工放行证据"
  tag, and for reviews the model decision + confidence + reason and waiting time. "加载更多"
  paginates; filters and scroll position are kept when returning from a detail.
  Pending order: human false-positive evidence first, then false-positive count, then oldest.
- **Batch** (待复核 and 已停用 tabs only) — rows have checkboxes, nothing is preselected, there is
  no select-all, max 50. A bottom bar shows "已选 N 条" and "停用所选 / 保留所选" or "恢复所选".
  A confirmation sheet lists every selected pattern and explains that changed items are skipped;
  the result sheet reports each item ("成功 2 条，此前已处理 0 条，状态或证据已变化 1 条…").
- **Review detail** — pattern, "复核 #x，指纹 #y", tags (类型 / 启用中·已停用 / 来源), facts grid
  (权重, 命中次数, 人工误判, 创建, 最近命中, 最近扫描), **人工反馈** and **模型建议** in separate
  sections ("模型只提出建议，是否停用由你决定。"), up to three **证据样本** with label
  (人工放行 / 人工确认广告 / 模型判正常 / 模型判广告 / 本地规则命中), time, "（原文较长，已截断）",
  "展开全文"; 操作记录; 指纹值. Sticky actions: pending → "停用并下一条", "保留并下一条",
  "稍后处理" (moves the item to the end of this page's queue, no server change);
  disabled → "恢复"; otherwise a status note.
- **Library detail** — same facts and samples plus 复核记录 (links to reviews) and a danger
  button "删除指纹". The confirmation sheet quotes the pattern and says how many evidence samples
  and review records will be removed, that it cannot be undone, and suggests "停用" instead when
  the goal is only to stop matching. Button: "确认删除".
- **Stale / errors** — toast "该指纹的状态或证据已变化，已重新加载最新内容，请确认后再操作。" and the
  detail is re-rendered from the server's fresh copy. Network failures retry writes twice with the
  same `request_id`. Not opened from Telegram → "请在 Telegram 中通过机器人的“打开复核台”按钮或聊天菜单进入。";
  other accounts → "仅指定复核人可以使用指纹复核台。"; expired → "登录已失效，请关闭后从 Telegram 重新打开复核台。"
- Telegram integration: `ready()`, `expand()`, BackButton closes the detail on mobile, haptic
  feedback on results, `themeChanged` re-applies the scheme.

## 4. Files changed and migration steps

New:

- `src/telegram_llm_antispam/webui_store.py` — `FingerprintWebStore` mixin: filtered queries,
  version tokens, `webui_review_action`, `webui_delete_fingerprint`, request-ID ledger, schema.
- `src/telegram_llm_antispam/webui/{__init__,auth,api,text,server}.py` — initData verification and
  sessions; framework-free API; Chinese messages and labels; aiohttp adapter and lifecycle.
- `src/telegram_llm_antispam/webui/static/{index.html,app.css,app.js}` — the Mini App.
- `tests/test_webui.py` — 43 tests.
- `WEBUI_IMPLEMENTATION_NOTE.md` — this note.

Modified:

- `fingerprint_store.py` — `resolve_fingerprint_review` now delegates to a shared
  `apply_review_transition()` + `REVIEW_TRANSITIONS`, used by both the bot callbacks and the
  WebUI. Same rules, same writes, same return values.
- `db.py` — `Database` also inherits `FingerprintWebStore`; `migrate()` calls
  `_migrate_webui_store()`. (File uses CRLF line endings; edits preserved them.)
- `config.py` — `WEBUI_ENABLED`, `WEBUI_HOST`, `WEBUI_PORT`, `WEBUI_PUBLIC_URL` (must be
  `https://`, normalized to a trailing `/`, otherwise ignored), `WEBUI_INIT_DATA_MAX_AGE_SECONDS`,
  `WEBUI_SESSION_TTL_SECONDS`, `WEBUI_MENU_BUTTON`. All have defaults, so existing `Settings(...)`
  calls in tests still work.
- `app.py` — starts the WebUI inside the guarded `try`, sets the menu button, and closes the
  WebUI before `db.close()` on shutdown.
- `fingerprint_audit.py` — `webui_url()`, `set_review_menu_button()`, and an optional `webui=`
  argument on `review_list` / `library_list`; the digest gets the extra button. No change when
  the WebUI is not configured.
- `tests/test_fingerprint_audit.py` — 2 tests for the entry buttons and the menu button.
- `pyproject.toml` — explicit `aiohttp>=3.9,<4`; package data for `webui/static/*`.
- `.env.example`, `README.md` — new settings and a short Chinese section.

Schema (created by `Database.migrate()`, idempotent `CREATE ... IF NOT EXISTS`; existing tables
untouched):

- `webui_action_requests(request_id PK, user_id, kind, action, request_hash, results_json,
  created_at)` — idempotency ledger, rows older than 7 days are pruned on each write.
- `fingerprint_deletions(...)` — audit row per WebUI deletion: fingerprint id/type/value, a
  200-char pattern preview, source, evidence/review counts, operator, request ID, time.

Migration / rollout:

1. Deploy code (`install.sh update` works; `pyproject.toml` changed, so it reinstalls the venv once).
2. Restart; migrations run at startup (`antispam-admin init-db` also works).
3. Configure HTTPS, then set `WEBUI_ENABLED=true` and `WEBUI_PUBLIC_URL=https://…/`, restart.

Reverse proxy examples (the app serves at `/`; a dedicated subdomain is simplest. A path prefix
works if the proxy strips it and `WEBUI_PUBLIC_URL` includes it with a trailing slash, because the
frontend uses relative URLs):

```caddy
review.example.com {
    reverse_proxy 127.0.0.1:8088
}
```

```nginx
server {
    listen 443 ssl http2;
    server_name review.example.com;
    # ssl_certificate / ssl_certificate_key ...
    client_max_body_size 64k;
    location / { proxy_pass http://127.0.0.1:8088; proxy_set_header Host $host; }
}
```

No BotFather setting is required for `web_app` inline buttons or a per-chat menu button.

Lifecycle: started after the routers and background tasks, inside the same `try/finally` as
polling. Health check `GET /healthz`. On SIGTERM (systemd stop) polling ends, the background tasks
are cancelled, `AppRunner.cleanup()` stops the listener and waits for in-flight requests, then the
database closes. Rollback: `WEBUI_ENABLED=false` and restart; the two new tables are inert. The
menu button persists in the reviewer's chat until reset, e.g.
`await bot.set_chat_menu_button(chat_id=<reviewer>, menu_button=MenuButtonDefault())`.

## 5. Security and concurrency assumptions

Security:

- Only `FINGERPRINT_REVIEW_USER_ID` can sign in; the ID comes from server settings and is never
  sent to or embedded in the frontend. Checked at sign-in and on every request.
- Bearer token in a header, no cookies → no CSRF surface; no CORS headers are emitted.
- Signed initData can be exchanged more than once within its freshness window (so a page reload
  works). Anyone able to read it within 10 minutes could open a session; keep the default window
  short and serve only over HTTPS.
- `WEBUI_HOST` defaults to loopback. Binding to a public interface would expose plain HTTP; don't.
- Headers: CSP `default-src 'none'; script-src 'self' https://telegram.org; style-src 'self';
  img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none';
  frame-ancestors https://web.telegram.org`, plus `nosniff`, `no-referrer`, `Permissions-Policy`,
  `Cross-Origin-Resource-Policy`. The Telegram SDK is the only third-party script, as Telegram
  requires.
- Untrusted text (patterns, samples, model reasons) is returned as JSON strings and rendered only
  via `textContent`/text nodes. Tests assert the frontend has no `innerHTML`/`outerHTML`/
  `insertAdjacentHTML`/`document.write`/`eval` and no inline scripts.
- Bounds: request body 64 KB, initData 8 KB, page ≤ 50, batch ≤ 50, samples ≤ 3 × 800 chars,
  list pattern 160 chars, detail pattern 4000, reason 600, query text 100.
- Internal errors return a generic Chinese message; details go to the server log. initData and
  tokens are never logged.

Concurrency and idempotency:

- **Version token** (16 hex chars) = hash of what the reviewer bases a decision on:
  type, value, enabled, pattern_text, source, false_positive_count and the stored evidence rows
  (chat, message, label, text, truncated); review versions add review ID and status. Weight and
  hit-count bumps from ordinary traffic are **excluded** so a busy fingerprint doesn't go stale
  every few seconds; a new evidence sample **does** make it stale (UX doc requirement).
- Every write is one `BEGIN IMMEDIATE` transaction under the shared lock: per-item checks and
  writes plus the request-ledger row commit together. Replaying a `request_id` returns the stored
  results (`replayed: true`) without acting; reusing it with a different payload or user → 409.
- Bot callbacks still use status checks; whichever side acts first wins, the other sees
  `stale`/`already_done` (WebUI) or "该建议已处理或指纹已变化。" (bot).
- Opening any page or queue never writes. The WebUI never touches `notified_at`, so the
  one-digest-per-24h and no-duplicate rules are unchanged; thresholds and learning policy are
  untouched.
- Sessions are in-process memory: one bot process is assumed.

## 6. Test commands and results

Environment: Python 3.13.18. PyPI and GitHub were unreachable from the sandbox, so `aiogram`,
`aiohttp`, `openai` and `httpx` could not be installed.

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_webui.py` | **43 passed** (needs none of the missing packages) |
| `PYTHONPATH=<scratch stubs> python -m pytest tests/test_db.py tests/test_fingerprint_audit.py tests/test_webui.py` | **88 passed**. The scratch stubs only supply `aiogram.types` button classes and `openai` exception names so these modules import; they are not shipped. |
| `pyright` on `webui/api.py`, `webui/auth.py`, `webui/text.py`, `webui_store.py` | 0 errors |
| `tests/test_handlers.py`, `tests/test_llm.py`, `tests/test_notifications.py` | **Not run** (need real aiogram/openai/httpx) |
| `ruff check` | **Not run** (ruff unavailable) |

`tests/test_webui.py` covers: initData signature/freshness/shape; sign-in for reviewer vs. other
user, expired, wrong bot, missing bot token; 401/403 on every API route incl. a forged session for
another user; idle/absolute session expiry; reads never change state; single-action idempotency
by request ID and by state; request-ID conflict; stale on new evidence with fresh item returned;
weight/hit bumps not stale; bot-first and WebUI-first races; batch per-item results, untouched
unselected rows, replay; batch validation; delete confirmation, stale delete, linked-row cleanup
(also with `foreign_keys=OFF`), audit row, replay and repeat; library search/filters/LIKE escaping/
sorting; invalid filter messages; review ordering; bounded/escaped output and headers; static shell
CSP and no HTML sinks; Chinese text for every error, result, field and label; Chinese frontend copy.

Mutation check: removing any one of the review version check, delete version check, delete
confirmation, per-request reviewer check, sign-in reviewer check, freshness check, HMAC check,
request replay or explicit evidence deletion makes at least one test fail.

Browser check (Chromium via Playwright, mobile 390×844 light/dark and desktop 1280×800): the same
`WebUIApi` was served by a throwaway stdlib HTTP harness with demo data, and `telegram-web-app.js`
was replaced by a stub carrying correctly signed initData. Verified: sign-in, list, detail,
"停用并下一条", batch confirm/result, filters, delete confirmation and deletion, other-user and
outside-Telegram screens, a `<script>` evidence sample rendered as inert text, a stale action
after another process added evidence (nothing disabled, fresh evidence shown, retry succeeded), no
console errors or CSP violations. **Not verified in real Telegram iOS/Android/Desktop/Web clients.**

## 7. Integration checklist for the primary agent

1. `pip install -e ".[dev]"`, then `pytest` (full suite) and `ruff check`.
2. Smoke-test the aiohttp adapter locally with `WEBUI_ENABLED=true`:
   `curl -s localhost:8088/healthz`; `curl -sI localhost:8088/` (CSP present);
   `curl -s -XPOST -H 'Content-Type: application/json' -d '{"init_data":"x"}' localhost:8088/api/session`
   (401, Chinese JSON); a > 64 KB body returns 413; stop the bot and confirm a clean shutdown.
3. Put HTTPS in front, set `WEBUI_PUBLIC_URL`, restart; log should show
   "Fingerprint review menu button set for reviewer chat".
4. On the reviewer's phone and desktop: open via menu button, digest button and `/fingerprints`;
   check theme, back button, disable/keep/restore, "稍后处理", batch with one stale item, delete,
   and a double tap on an action.
5. Open the same URL from another account: expect "仅指定复核人可以使用指纹复核台。".
6. Confirm the digest is still at most one per 24 h and the old inline buttons still work.

Open points for the owner:

- Restore works only through a disabled review, as in the bot. Fingerprints disabled outside the
  review flow (legacy `disabled_false_positive` migration) are shown as 已停用 in the library but
  have no restore button; adding manual disable/restore would be a policy change, so it was left out.
- Deleting via the old bot button is not written to `fingerprint_deletions`;
  `fingerprint_review_actions` rows of deleted reviews remain (no FK; pre-existing behavior).
- The UX doc's separate review bot and pinned status panel are not implemented. A separate bot
  would only need its token passed to `WebUIApi(bot_token=...)` in `webui/server.py`.
- "稍后处理" ordering lives in the page only and resets on reload.
