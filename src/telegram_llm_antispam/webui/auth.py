"""Telegram Mini App authentication for the fingerprint review WebUI.

The client sends ``Telegram.WebApp.initData`` once; the server verifies the HMAC
signature with the bot token, rejects stale or future-dated data, and only then issues
a short-lived opaque session token. ``initDataUnsafe`` and any client-supplied user ID
are never trusted. Authorization (the exact reviewer ID) is checked separately by the
API on every request.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qsl


MAX_INIT_DATA_LENGTH = 8192
FUTURE_SKEW_SECONDS = 60
SESSION_ABSOLUTE_TTL_SECONDS = 12 * 3600
MAX_SESSIONS = 32


class InitDataError(Exception):
    """initData failed validation. ``code`` is stable and safe to log."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class WebAppUser:
    id: int
    first_name: str
    username: str | None


@dataclass(frozen=True, slots=True)
class VerifiedInitData:
    user: WebAppUser
    auth_date: int


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def sign_init_data(fields: dict[str, str], bot_token: str) -> str:
    """Build a signed initData string. Used by tests and local tooling only."""
    from urllib.parse import urlencode

    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    digest = hmac.new(_secret_key(bot_token), check.encode("utf-8"), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def verify_init_data(
    init_data: str, bot_token: str, *, max_age_seconds: int, now: float | None = None,
) -> VerifiedInitData:
    """Validate Telegram WebApp initData per the Bot API "validating data" procedure."""
    if not bot_token:
        raise InitDataError("server_not_configured")
    if not isinstance(init_data, str) or not init_data or len(init_data) > MAX_INIT_DATA_LENGTH:
        raise InitDataError("malformed")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise InitDataError("malformed") from exc
    fields: dict[str, str] = {}
    for key, value in pairs:
        if key in fields:
            raise InitDataError("malformed")
        fields[key] = value
    received = fields.pop("hash", "")
    if len(received) != 64 or any(c not in "0123456789abcdef" for c in received):
        raise InitDataError("bad_signature")
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    expected = hmac.new(_secret_key(bot_token), check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InitDataError("bad_signature")

    # Freshness is only meaningful after the signature proves auth_date is genuine.
    try:
        auth_date = int(fields.get("auth_date", ""))
    except ValueError as exc:
        raise InitDataError("malformed") from exc
    current = time.time() if now is None else now
    if auth_date > current + FUTURE_SKEW_SECONDS:
        raise InitDataError("future_auth_date")
    if current - auth_date > max_age_seconds:
        raise InitDataError("expired")

    try:
        user = json.loads(fields.get("user", ""))
    except ValueError as exc:
        raise InitDataError("malformed") from exc
    if not isinstance(user, dict):
        raise InitDataError("malformed")
    user_id = user.get("id")
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise InitDataError("malformed")
    first_name = user.get("first_name")
    username = user.get("username")
    return VerifiedInitData(
        user=WebAppUser(
            id=user_id,
            first_name=first_name[:64] if isinstance(first_name, str) else "",
            username=username[:64] if isinstance(username, str) else None,
        ),
        auth_date=auth_date,
    )


@dataclass(slots=True)
class _Session:
    user_id: int
    display_name: str
    created_at: float
    last_seen_at: float


class SessionStore:
    """In-memory opaque bearer tokens with idle and absolute expiry.

    Sessions live only in this process: a restart signs the reviewer out, and they
    reopen the Mini App to get a fresh initData. Tokens are random, not derived from
    initData, so a leaked token cannot be replayed after expiry.
    """

    def __init__(
        self, *, idle_ttl_seconds: int, absolute_ttl_seconds: int = SESSION_ABSOLUTE_TTL_SECONDS,
        max_sessions: int = MAX_SESSIONS, clock: Callable[[], float] = time.time,
    ) -> None:
        self._idle = max(60, idle_ttl_seconds)
        self._absolute = max(self._idle, absolute_ttl_seconds)
        self._max = max(1, max_sessions)
        self._clock = clock
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    @property
    def idle_ttl_seconds(self) -> int:
        return self._idle

    def create(self, user_id: int, display_name: str = "") -> str:
        now = self._clock()
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge(now)
            while len(self._sessions) >= self._max:
                oldest = min(self._sessions, key=lambda k: self._sessions[k].last_seen_at)
                del self._sessions[oldest]
            self._sessions[token] = _Session(user_id, display_name, now, now)
        return token

    def resolve(self, token: str | None) -> _Session | None:
        if not token or len(token) > 128:
            return None
        now = self._clock()
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if (now - session.last_seen_at > self._idle
                    or now - session.created_at > self._absolute):
                del self._sessions[token]
                return None
            session.last_seen_at = now
            return session

    def revoke(self, token: str | None) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    def _purge(self, now: float) -> None:
        for key in [k for k, s in self._sessions.items()
                    if now - s.last_seen_at > self._idle or now - s.created_at > self._absolute]:
            del self._sessions[key]
