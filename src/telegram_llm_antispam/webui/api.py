"""Framework-independent HTTP API for the fingerprint review WebUI.

``WebUIApi.handle`` takes a plain ``ApiRequest`` and returns an ``ApiResponse``. The
aiohttp adapter in ``server.py`` is a thin translation layer, so routing, validation,
authentication, authorization and escaping are all exercised by unit tests without a
running HTTP server.

Every /api route except POST /api/session requires ``Authorization: Bearer <token>``
from a session created for the configured reviewer. Bearer tokens (not cookies) mean a
cross-site page cannot ride the reviewer's session, and no CORS headers are emitted.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs

from ..webui_store import (
    LIBRARY_SORTS, MAX_PAGE_SIZE, REVIEW_VIEW_STATUSES, LibraryFilters, RequestIdConflict,
)
from ..fingerprint_store import REVIEW_TRANSITIONS
from . import text
from .auth import InitDataError, SessionStore, verify_init_data


logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
MAX_BATCH_ITEMS = 50
MAX_QUERY_CHARS = 100
STATIC_DIR = Path(__file__).with_name("static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
}
# Telegram Web (web.telegram.org) embeds Mini Apps in an iframe; native clients use a
# WebView. No inline script or style is allowed; the Telegram SDK is the only remote script.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self' https://telegram.org; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors https://web.telegram.org"
)
BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Resource-Policy": "same-origin",
}
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
VERSION_RE = re.compile(r"[0-9a-f]{16}")
CODE_RE = re.compile(r"[a-z0-9_]{1,40}")


@dataclass(frozen=True, slots=True)
class ApiRequest:
    method: str
    path: str
    query_string: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def header(self, name: str) -> str:
        name = name.lower()
        for key, value in self.headers.items():
            if key.lower() == name:
                return value
        return ""


@dataclass(slots=True)
class ApiResponse:
    status: int
    body: bytes
    headers: dict[str, str]

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class HttpError(Exception):
    def __init__(self, status: int, code: str, message: str | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message or text.error_message(code)


def _bad(field_name: str) -> HttpError:
    return HttpError(400, "bad_request", text.field_error(field_name))


def _json_response(status: int, payload: Any) -> ApiResponse:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return ApiResponse(status, body, {
        **BASE_HEADERS,
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
    })


def _error_response(error: HttpError) -> ApiResponse:
    return _json_response(error.status, {"error": {"code": error.code, "message": error.message}})


class _Query:
    def __init__(self, query_string: str):
        try:
            parsed = parse_qs(query_string, keep_blank_values=False, max_num_fields=40)
        except ValueError as exc:
            raise HttpError(400, "bad_request") from exc
        self._values = {key: values[0].strip() for key, values in parsed.items()}

    def text(self, name: str, *, max_chars: int = MAX_QUERY_CHARS) -> str | None:
        value = self._values.get(name)
        if not value:
            return None
        if len(value) > max_chars:
            raise _bad(name)
        return value

    def code(self, name: str, allowed: Any = None) -> str | None:
        value = self.text(name, max_chars=40)
        if value is None:
            return None
        if not CODE_RE.fullmatch(value) or (allowed is not None and value not in allowed):
            raise _bad(name)
        return value

    def integer(self, name: str, *, default: int | None = None, low: int = 0,
                high: int = 10**9) -> int | None:
        value = self.text(name, max_chars=12)
        if value is None:
            return default
        if not re.fullmatch(r"[0-9]+", value) or not low <= int(value) <= high:
            raise _bad(name)
        return int(value)

    def number(self, name: str, *, low: float, high: float) -> float | None:
        value = self.text(name, max_chars=12)
        if value is None:
            return None
        try:
            number = float(value)
        except ValueError as exc:
            raise _bad(name) from exc
        if not math.isfinite(number) or not low <= number <= high:
            raise _bad(name)
        return number

    def boolean(self, name: str) -> bool | None:
        value = self.text(name, max_chars=5)
        if value is None:
            return None
        if value in {"1", "true"}:
            return True
        if value in {"0", "false"}:
            return False
        raise _bad(name)


class WebUIApi:
    def __init__(
        self, *, db: Any, bot_token: str | None, reviewer_user_id: int,
        init_data_max_age_seconds: int, sessions: SessionStore,
        static_dir: Path = STATIC_DIR, clock: Callable[[], float] | None = None,
    ) -> None:
        self._db = db
        self._bot_token = bot_token or ""
        self._reviewer_user_id = reviewer_user_id
        self._max_age = max(30, init_data_max_age_seconds)
        self._sessions = sessions
        self._clock = clock
        self._static = {
            path: (static_dir.joinpath(name).read_bytes(), content_type)
            for path, (name, content_type) in STATIC_FILES.items()
        }
        self._routes: list[tuple[str, re.Pattern[str], Callable[..., ApiResponse], bool]] = [
            ("POST", re.compile(r"/api/session"), self._login, False),
            ("GET", re.compile(r"/api/meta"), self._meta, True),
            ("GET", re.compile(r"/api/summary"), self._summary, True),
            ("GET", re.compile(r"/api/reviews"), self._list_reviews, True),
            ("GET", re.compile(r"/api/reviews/([0-9]{1,18})"), self._get_review, True),
            ("POST", re.compile(r"/api/reviews/actions"), self._review_actions, True),
            ("GET", re.compile(r"/api/fingerprints"), self._list_fingerprints, True),
            ("GET", re.compile(r"/api/fingerprints/([0-9]{1,18})"), self._get_fingerprint, True),
            ("POST", re.compile(r"/api/fingerprints/([0-9]{1,18})/delete"),
             self._delete_fingerprint, True),
        ]

    # ------------------------------------------------------------- entry point

    @staticmethod
    def error_response(status: int, code: str) -> ApiResponse:
        return _error_response(HttpError(status, code))

    def handle(self, request: ApiRequest) -> ApiResponse:
        try:
            return self._route(request)
        except HttpError as error:
            return _error_response(error)
        except Exception:
            logger.exception("WebUI request failed: %s %s", request.method, request.path)
            return _error_response(HttpError(500, "internal"))

    def _route(self, request: ApiRequest) -> ApiResponse:
        method = request.method.upper()
        if len(request.body) > MAX_BODY_BYTES:
            raise HttpError(413, "payload_too_large")
        if request.path == "/healthz":
            if method not in {"GET", "HEAD"}:
                raise HttpError(405, "method_not_allowed")
            return self._health()
        if request.path in self._static:
            if method not in {"GET", "HEAD"}:
                raise HttpError(405, "method_not_allowed")
            return self._static_response(request.path, head=method == "HEAD")
        path_matched = False
        for route_method, pattern, handler, needs_auth in self._routes:
            match = pattern.fullmatch(request.path)
            if match is None:
                continue
            path_matched = True
            if method != route_method:
                continue
            session = self._authorize(request) if needs_auth else None
            return handler(request, session, *match.groups())
        if path_matched:
            raise HttpError(405, "method_not_allowed")
        raise HttpError(404, "not_found")

    # ---------------------------------------------------------------- helpers

    def _now(self) -> float | None:
        return self._clock() if self._clock else None

    def _health(self) -> ApiResponse:
        try:
            ok = self._db.webui_ping()
        except Exception:
            ok = False
        return _json_response(200 if ok else 503, {"status": "ok" if ok else "error"})

    def _static_response(self, path: str, *, head: bool) -> ApiResponse:
        body, content_type = self._static[path]
        headers = {**BASE_HEADERS, "Content-Type": content_type, "Cache-Control": "no-cache"}
        if content_type.startswith("text/html"):
            headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        return ApiResponse(200, b"" if head else body, headers)

    def _authorize(self, request: ApiRequest):
        header = request.header("authorization")
        token = header[7:].strip() if header[:7].lower() == "bearer " else None
        session = self._sessions.resolve(token)
        if session is None:
            raise HttpError(401, "unauthenticated")
        if session.user_id != self._reviewer_user_id:
            # Only reachable if configuration changed under a live session.
            self._sessions.revoke(token)
            raise HttpError(403, "forbidden")
        return session

    def _json_body(self, request: ApiRequest) -> dict[str, Any]:
        content_type = request.header("content-type").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise HttpError(415, "unsupported_media_type")
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise HttpError(400, "bad_json") from exc
        if not isinstance(payload, dict):
            raise HttpError(400, "bad_json")
        return payload

    @staticmethod
    def _request_id(payload: dict[str, Any]) -> str:
        value = payload.get("request_id")
        if not isinstance(value, str) or not REQUEST_ID_RE.fullmatch(value):
            raise _bad("request_id")
        return value

    @staticmethod
    def _version(value: Any) -> str:
        if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
            raise _bad("version")
        return value

    @staticmethod
    def _page(query: _Query) -> tuple[int, int]:
        offset = query.integer("offset", default=0, high=100_000)
        limit = query.integer("limit", default=20, low=1, high=MAX_PAGE_SIZE)
        return offset or 0, limit or 20

    # ----------------------------------------------------------------- routes

    def _login(self, request: ApiRequest, _session) -> ApiResponse:
        payload = self._json_body(request)
        init_data = payload.get("init_data")
        if not isinstance(init_data, str):
            raise _bad("init_data")
        try:
            verified = verify_init_data(init_data, self._bot_token,
                                        max_age_seconds=self._max_age, now=self._now())
        except InitDataError as error:
            logger.warning("WebUI login rejected: %s", error.code)
            if error.code == "server_not_configured":
                raise HttpError(503, "server_not_configured") from error
            if error.code == "expired":
                raise HttpError(401, "init_data_expired") from error
            raise HttpError(401, "init_data_invalid") from error
        if verified.user.id != self._reviewer_user_id:
            logger.warning("WebUI login refused for non-reviewer user %s", verified.user.id)
            raise HttpError(403, "forbidden")
        token = self._sessions.create(verified.user.id, verified.user.first_name)
        logger.info("WebUI session opened for reviewer %s", verified.user.id)
        return _json_response(200, {
            "token": token,
            "expires_in": self._sessions.idle_ttl_seconds,
            "display_name": verified.user.first_name,
        })

    def _meta(self, _request, _session) -> ApiResponse:
        return _json_response(200, {
            "labels": text.LABELS,
            "filters": self._db.webui_filter_options(),
            "sorts": list(LIBRARY_SORTS),
            "page_size_max": MAX_PAGE_SIZE,
            "batch_max": MAX_BATCH_ITEMS,
        })

    def _summary(self, _request, _session) -> ApiResponse:
        return _json_response(200, self._db.webui_summary())

    def _list_reviews(self, request: ApiRequest, _session) -> ApiResponse:
        query = _Query(request.query_string)
        status = query.code("status", REVIEW_VIEW_STATUSES) or "pending"
        offset, limit = self._page(query)
        items, total = self._db.webui_list_reviews(
            status, query=query.text("q"), fingerprint_type=query.code("type"),
            offset=offset, limit=limit,
        )
        return _json_response(200, {"items": items, "total": total,
                                    "offset": offset, "limit": limit})

    def _get_review(self, _request, _session, review_id: str) -> ApiResponse:
        review = self._db.webui_get_review(int(review_id))
        if review is None:
            raise HttpError(404, "review_not_found")
        return _json_response(200, {"review": review})

    def _list_fingerprints(self, request: ApiRequest, _session) -> ApiResponse:
        query = _Query(request.query_string)
        filters = LibraryFilters(
            query=query.text("q"),
            fingerprint_type=query.code("type"),
            enabled=query.boolean("enabled"),
            source=query.code("source"),
            weight_min=query.number("weight_min", low=0, high=100),
            weight_max=query.number("weight_max", low=0, high=100),
            hits_min=query.integer("hits_min"),
            hits_max=query.integer("hits_max"),
            false_positives_min=query.integer("fp_min"),
            false_positives_max=query.integer("fp_max"),
            sort=query.code("sort", LIBRARY_SORTS) or "weight",
        )
        offset, limit = self._page(query)
        items, total = self._db.webui_list_fingerprints(filters, offset=offset, limit=limit)
        return _json_response(200, {"items": items, "total": total,
                                    "offset": offset, "limit": limit})

    def _get_fingerprint(self, _request, _session, fingerprint_id: str) -> ApiResponse:
        fingerprint = self._db.webui_get_fingerprint(int(fingerprint_id))
        if fingerprint is None:
            raise HttpError(404, "fingerprint_not_found")
        return _json_response(200, {"fingerprint": fingerprint})

    def _review_actions(self, request: ApiRequest, session) -> ApiResponse:
        payload = self._json_body(request)
        request_id = self._request_id(payload)
        action = payload.get("action")
        if action not in REVIEW_TRANSITIONS:
            raise _bad("action")
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise _bad("items")
        if len(raw_items) > MAX_BATCH_ITEMS:
            raise HttpError(400, "too_many_items", text.error_message(
                "too_many_items", limit=MAX_BATCH_ITEMS))
        items: list[tuple[int, str]] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise _bad("items")
            review_id = raw.get("review_id")
            if not isinstance(review_id, int) or isinstance(review_id, bool) or review_id <= 0:
                raise _bad("review_id")
            items.append((review_id, self._version(raw.get("version"))))
        if len({rid for rid, _ in items}) != len(items):
            raise HttpError(400, "duplicate_items")
        try:
            outcome = self._db.webui_review_action(
                request_id=request_id, user_id=session.user_id, action=action, items=items,
            )
        except RequestIdConflict as error:
            raise HttpError(409, "request_id_conflict") from error
        include_detail = len(items) == 1
        results = []
        summary = {status: 0 for status in ("ok", "already_done", "stale", "not_found", "invalid")}
        for result in outcome["results"]:
            status = result["status"]
            summary[status] = summary.get(status, 0) + 1
            entry = {
                "review_id": result["review_id"],
                "status": status,
                "message": text.item_message(status, action),
            }
            if include_detail:
                entry["review"] = self._db.webui_get_review(result["review_id"])
            results.append(entry)
        logger.info("WebUI review action %s by %s: %s%s", action, session.user_id, summary,
                    " (replayed)" if outcome["replayed"] else "")
        return _json_response(200, {
            "request_id": request_id, "action": action, "replayed": outcome["replayed"],
            "results": results, "summary": summary,
        })

    def _delete_fingerprint(self, request: ApiRequest, session, fingerprint_id: str) -> ApiResponse:
        payload = self._json_body(request)
        request_id = self._request_id(payload)
        version = self._version(payload.get("version"))
        if payload.get("confirm") is not True:
            raise HttpError(400, "confirmation_required")
        try:
            outcome = self._db.webui_delete_fingerprint(
                request_id=request_id, user_id=session.user_id,
                fingerprint_id=int(fingerprint_id), version=version,
            )
        except RequestIdConflict as error:
            raise HttpError(409, "request_id_conflict") from error
        result = dict(outcome["results"][0])
        result["message"] = text.item_message(result["status"], "delete")
        logger.info("WebUI delete fingerprint %s by %s: %s%s", fingerprint_id, session.user_id,
                    result["status"], " (replayed)" if outcome["replayed"] else "")
        return _json_response(200, {
            "request_id": request_id, "replayed": outcome["replayed"], "result": result,
            "fingerprint": self._db.webui_get_fingerprint(int(fingerprint_id)),
        })
