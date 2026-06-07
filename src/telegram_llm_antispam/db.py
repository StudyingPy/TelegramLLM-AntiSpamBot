from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .models import (
    AdminNotification,
    FingerprintRecord,
    LocalDecision,
    MessageFeatures,
    SenderProfile,
    UserContext,
    VoteRecord,
    VoteSession,
    VoteTally,
)


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS user_reputation (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    reputation_score REAL NOT NULL DEFAULT 50,
    first_seen_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    messages_seen INTEGER NOT NULL DEFAULT 0,
    spam_confirmed INTEGER NOT NULL DEFAULT 0,
    ham_confirmed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    language_code TEXT,
    is_bot INTEGER NOT NULL DEFAULT 0,
    is_premium INTEGER,
    bio TEXT,
    bio_fetched_at INTEGER,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS allowed_chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    added_by_user_id INTEGER,
    added_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fingerprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint_type TEXT NOT NULL,
    value TEXT NOT NULL UNIQUE,
    weight REAL NOT NULL DEFAULT 50,
    hit_count INTEGER NOT NULL DEFAULT 0,
    false_positive_count INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'manual',
    last_hit_at INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS vote_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    original_message_id INTEGER NOT NULL,
    vote_message_id INTEGER,
    suspect_user_id INTEGER,
    skeleton_hash TEXT,
    content_hash TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    spam_votes INTEGER NOT NULL DEFAULT 0,
    ham_votes INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    closed_at INTEGER
);

CREATE TABLE IF NOT EXISTS vote_session_votes (
    session_id INTEGER NOT NULL,
    voter_user_id INTEGER NOT NULL,
    vote TEXT NOT NULL CHECK (vote IN ('spam', 'ham')),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, voter_user_id),
    FOREIGN KEY (session_id) REFERENCES vote_sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER,
    user_id INTEGER,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    confidence REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vote_session_id INTEGER,
    action_log_id INTEGER,
    notify_user_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    base_text TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS message_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    user_id INTEGER,
    skeleton_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    simhash TEXT NOT NULL,
    link_domains_json TEXT NOT NULL DEFAULT '[]',
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fingerprints_value ON fingerprints(value);
CREATE INDEX IF NOT EXISTS idx_fingerprints_type_value ON fingerprints(fingerprint_type, value);
CREATE INDEX IF NOT EXISTS idx_user_profiles_updated_at ON user_profiles(updated_at);
CREATE INDEX IF NOT EXISTS idx_allowed_chats_added_at ON allowed_chats(added_at);
CREATE INDEX IF NOT EXISTS idx_vote_sessions_status ON vote_sessions(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_admin_notifications_vote_session
    ON admin_notifications(vote_session_id);
CREATE INDEX IF NOT EXISTS idx_observations_skeleton_time
    ON message_observations(skeleton_hash, created_at);
"""


class Database:
    def __init__(self, path: Path, default_reputation: float = 50) -> None:
        self._path = path
        self._default_reputation = default_reputation
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    @classmethod
    def from_settings(cls, settings: Settings) -> "Database":
        return cls(settings.database_path, default_reputation=settings.default_reputation)

    def connect(self) -> None:
        if self._path != Path(":memory:"):
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def migrate(self) -> None:
        with self._locked_conn() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.commit()

    def allow_chat(self, chat_id: int, title: str | None, added_by_user_id: int | None) -> None:
        with self._locked_conn() as conn:
            conn.execute(
                """
                INSERT INTO allowed_chats (chat_id, title, added_by_user_id, added_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = excluded.title,
                    added_by_user_id = excluded.added_by_user_id,
                    added_at = excluded.added_at
                """,
                (chat_id, title, added_by_user_id, _now()),
            )
            conn.commit()

    def disallow_chat(self, chat_id: int) -> None:
        with self._locked_conn() as conn:
            conn.execute("DELETE FROM allowed_chats WHERE chat_id = ?", (chat_id,))
            conn.commit()

    def is_chat_allowed(self, chat_id: int, configured_chat_ids: tuple[int, ...]) -> bool:
        if chat_id in configured_chat_ids:
            return True
        with self._locked_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM allowed_chats WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return row is not None

    def get_user_profile(self, user_id: int) -> SenderProfile | None:
        with self._locked_conn() as conn:
            row = conn.execute(
                """
                SELECT user_id, username, first_name, last_name, language_code, is_bot,
                    is_premium, bio, bio_fetched_at, updated_at
                FROM user_profiles
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        return _sender_profile_from_row(row) if row is not None else None

    def upsert_user_profile(self, profile: SenderProfile) -> SenderProfile:
        now = _now()
        with self._locked_conn() as conn:
            conn.execute(
                """
                INSERT INTO user_profiles (
                    user_id, username, first_name, last_name, language_code, is_bot,
                    is_premium, bio, bio_fetched_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    language_code = excluded.language_code,
                    is_bot = excluded.is_bot,
                    is_premium = COALESCE(excluded.is_premium, user_profiles.is_premium),
                    bio = COALESCE(excluded.bio, user_profiles.bio),
                    bio_fetched_at = COALESCE(excluded.bio_fetched_at, user_profiles.bio_fetched_at),
                    updated_at = excluded.updated_at
                """,
                (
                    profile.user_id,
                    profile.username,
                    profile.first_name,
                    profile.last_name,
                    profile.language_code,
                    int(profile.is_bot),
                    None if profile.is_premium is None else int(profile.is_premium),
                    profile.bio,
                    profile.bio_fetched_at,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT user_id, username, first_name, last_name, language_code, is_bot,
                    is_premium, bio, bio_fetched_at, updated_at
                FROM user_profiles
                WHERE user_id = ?
                """,
                (profile.user_id,),
            ).fetchone()
            conn.commit()
        return _sender_profile_from_row(row)

    def update_user_profile_bio(self, user_id: int, bio: str | None) -> SenderProfile | None:
        now = _now()
        with self._locked_conn() as conn:
            conn.execute(
                """
                UPDATE user_profiles
                SET bio = ?, bio_fetched_at = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (bio, now, now, user_id),
            )
            row = conn.execute(
                """
                SELECT user_id, username, first_name, last_name, language_code, is_bot,
                    is_premium, bio, bio_fetched_at, updated_at
                FROM user_profiles
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            conn.commit()
        return _sender_profile_from_row(row) if row is not None else None

    def get_user_context(self, chat_id: int, user_id: int) -> UserContext:
        now = _now()
        with self._locked_conn() as conn:
            row = conn.execute(
                """
                SELECT chat_id, user_id, reputation_score, first_seen_at, last_seen_at, messages_seen
                FROM user_reputation
                WHERE chat_id = ? AND user_id = ?
                """,
                (chat_id, user_id),
            ).fetchone()

        if row is None:
            return UserContext(
                chat_id=chat_id,
                user_id=user_id,
                reputation_score=self._default_reputation,
                messages_seen=0,
                first_seen_at=now,
                last_seen_at=None,
            )

        return UserContext(
            chat_id=row["chat_id"],
            user_id=row["user_id"],
            reputation_score=row["reputation_score"],
            messages_seen=row["messages_seen"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
        )

    def record_message_seen(self, features: MessageFeatures) -> None:
        if features.user_id is None:
            return
        now = _now()
        with self._locked_conn() as conn:
            conn.execute(
                """
                INSERT INTO user_reputation (
                    chat_id, user_id, reputation_score, first_seen_at, last_seen_at, messages_seen
                )
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    messages_seen = user_reputation.messages_seen + 1
                """,
                (
                    features.chat_id,
                    features.user_id,
                    self._default_reputation,
                    now,
                    now,
                ),
            )
            conn.commit()

    def record_observation(self, features: MessageFeatures) -> None:
        with self._locked_conn() as conn:
            conn.execute(
                """
                INSERT INTO message_observations (
                    chat_id, message_id, user_id, skeleton_hash, content_hash, simhash,
                    link_domains_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    features.chat_id,
                    features.message_id,
                    features.user_id,
                    features.skeleton_hash,
                    features.content_hash,
                    str(features.simhash),
                    json.dumps(list(features.link_domains), ensure_ascii=False),
                    _now(),
                ),
            )
            conn.commit()

    def get_fingerprint(self, value: str) -> FingerprintRecord | None:
        with self._locked_conn() as conn:
            row = conn.execute(
                """
                SELECT id, fingerprint_type, value, weight, hit_count, false_positive_count, source
                FROM fingerprints
                WHERE value = ?
                """,
                (value,),
            ).fetchone()

        if row is None:
            return None
        return FingerprintRecord(
            id=row["id"],
            fingerprint_type=row["fingerprint_type"],
            value=row["value"],
            weight=row["weight"],
            hit_count=row["hit_count"],
            false_positive_count=row["false_positive_count"],
            source=row["source"],
        )

    def get_strongest_fingerprint(self, values: tuple[tuple[str, str], ...]) -> FingerprintRecord | None:
        if not values:
            return None
        with self._locked_conn() as conn:
            rows = []
            for fingerprint_type, value in values:
                row = conn.execute(
                    """
                    SELECT id, fingerprint_type, value, weight, hit_count, false_positive_count, source
                    FROM fingerprints
                    WHERE fingerprint_type = ? AND value = ?
                    """,
                    (fingerprint_type, value),
                ).fetchone()
                if row is not None:
                    rows.append(row)

        if not rows:
            return None
        row = max(rows, key=lambda item: item["weight"])
        return FingerprintRecord(
            id=row["id"],
            fingerprint_type=row["fingerprint_type"],
            value=row["value"],
            weight=row["weight"],
            hit_count=row["hit_count"],
            false_positive_count=row["false_positive_count"],
            source=row["source"],
        )

    def record_fingerprint_hit(self, fingerprint_id: int) -> None:
        with self._locked_conn() as conn:
            conn.execute(
                """
                UPDATE fingerprints
                SET hit_count = hit_count + 1, last_hit_at = ?
                WHERE id = ?
                """,
                (_now(), fingerprint_id),
            )
            conn.commit()

    def upsert_fingerprint(
        self,
        fingerprint_type: str,
        value: str,
        weight: float,
        source: str,
    ) -> None:
        now = _now()
        bounded_weight = max(0.0, min(100.0, weight))
        with self._locked_conn() as conn:
            conn.execute(
                """
                INSERT INTO fingerprints (
                    fingerprint_type, value, weight, hit_count, false_positive_count,
                    source, last_hit_at, created_at
                )
                VALUES (?, ?, ?, 0, 0, ?, ?, ?)
                ON CONFLICT(value) DO UPDATE SET
                    fingerprint_type = excluded.fingerprint_type,
                    weight = max(fingerprints.weight, excluded.weight),
                    source = excluded.source,
                    last_hit_at = excluded.last_hit_at
                """,
                (fingerprint_type, value, bounded_weight, source, now, now),
            )
            conn.commit()

    def mark_fingerprint_false_positive(self, value: str, penalty: float) -> None:
        with self._locked_conn() as conn:
            conn.execute(
                """
                UPDATE fingerprints
                SET
                    false_positive_count = false_positive_count + 1,
                    weight = max(0, weight - ?),
                    source = CASE
                        WHEN false_positive_count + 1 >= 3 THEN 'disabled_false_positive'
                        ELSE source
                    END
                WHERE value = ?
                """,
                (penalty, value),
            )
            conn.commit()

    def list_fingerprints(
        self,
        *,
        fingerprint_type: str | None = None,
        min_weight: float = 0.0,
        limit: int = 50,
    ) -> tuple[dict[str, Any], ...]:
        params: list[Any] = [min_weight]
        type_clause = ""
        if fingerprint_type:
            type_clause = " AND fingerprint_type = ?"
            params.append(fingerprint_type)
        params.append(limit)
        with self._locked_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT id, fingerprint_type, value, weight, hit_count, false_positive_count,
                    source, last_hit_at, created_at
                FROM fingerprints
                WHERE weight >= ?{type_clause}
                ORDER BY weight DESC, hit_count DESC, id ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def delete_fingerprint(self, fingerprint_id: int) -> bool:
        with self._locked_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM fingerprints WHERE id = ?",
                (fingerprint_id,),
            )
            conn.commit()
        return cursor.rowcount > 0

    def count_recent_skeleton_senders(
        self,
        skeleton_hash: str,
        window_seconds: int,
        exclude_user_id: int | None = None,
    ) -> int:
        since = _now() - window_seconds
        with self._locked_conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT user_id) AS sender_count
                FROM message_observations
                WHERE skeleton_hash = ?
                    AND created_at >= ?
                    AND user_id IS NOT NULL
                    AND (? IS NULL OR user_id != ?)
                """,
                (skeleton_hash, since, exclude_user_id, exclude_user_id),
            ).fetchone()
        return int(row["sender_count"] if row is not None else 0)

    def record_action(
        self,
        features: MessageFeatures,
        decision: LocalDecision,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        payload = dict(decision.metadata)
        if metadata:
            payload.update(metadata)

        with self._locked_conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO action_log (
                    chat_id, message_id, user_id, action, reason, confidence,
                    metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    features.chat_id,
                    features.message_id,
                    features.user_id,
                    decision.action.value,
                    decision.reason,
                    decision.confidence,
                    json.dumps(payload, ensure_ascii=False),
                    _now(),
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def create_vote_session(
        self,
        features: MessageFeatures,
        decision: LocalDecision,
        timeout_seconds: int,
    ) -> int:
        now = _now()
        with self._locked_conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO vote_sessions (
                    chat_id, original_message_id, suspect_user_id, skeleton_hash, content_hash,
                    reason, created_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    features.chat_id,
                    features.message_id,
                    features.user_id,
                    features.skeleton_hash,
                    features.content_hash,
                    decision.reason,
                    now,
                    now + timeout_seconds,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def set_vote_message_id(self, session_id: int, vote_message_id: int) -> None:
        with self._locked_conn() as conn:
            conn.execute(
                "UPDATE vote_sessions SET vote_message_id = ? WHERE id = ?",
                (vote_message_id, session_id),
            )
            conn.commit()

    def add_vote(self, session_id: int, voter_user_id: int, vote: str) -> VoteTally | None:
        if vote not in {"spam", "ham"}:
            raise ValueError("vote must be spam or ham")

        now = _now()
        with self._locked_conn() as conn:
            session = conn.execute(
                """
                SELECT id, chat_id, suspect_user_id, spam_votes, ham_votes, status
                FROM vote_sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            if session is None:
                return None
            if session["status"] != "open":
                return VoteTally(
                    session_id=session["id"],
                    chat_id=session["chat_id"],
                    suspect_user_id=session["suspect_user_id"],
                    spam_votes=session["spam_votes"],
                    ham_votes=session["ham_votes"],
                    status=session["status"],
                    changed=False,
                )

            existing = conn.execute(
                """
                SELECT vote
                FROM vote_session_votes
                WHERE session_id = ? AND voter_user_id = ?
                """,
                (session_id, voter_user_id),
            ).fetchone()

            changed = False
            spam_delta = 0
            ham_delta = 0
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO vote_session_votes (
                        session_id, voter_user_id, vote, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (session_id, voter_user_id, vote, now, now),
                )
                changed = True
                spam_delta = 1 if vote == "spam" else 0
                ham_delta = 1 if vote == "ham" else 0
            elif existing["vote"] != vote:
                conn.execute(
                    """
                    UPDATE vote_session_votes
                    SET vote = ?, updated_at = ?
                    WHERE session_id = ? AND voter_user_id = ?
                    """,
                    (vote, now, session_id, voter_user_id),
                )
                changed = True
                spam_delta = 1 if vote == "spam" else -1
                ham_delta = 1 if vote == "ham" else -1

            if changed:
                conn.execute(
                    """
                    UPDATE vote_sessions
                    SET spam_votes = spam_votes + ?, ham_votes = ham_votes + ?
                    WHERE id = ?
                    """,
                    (spam_delta, ham_delta, session_id),
                )
                conn.commit()

            updated = conn.execute(
                """
                SELECT id, chat_id, suspect_user_id, spam_votes, ham_votes, status
                FROM vote_sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()

        return VoteTally(
            session_id=updated["id"],
            chat_id=updated["chat_id"],
            suspect_user_id=updated["suspect_user_id"],
            spam_votes=updated["spam_votes"],
            ham_votes=updated["ham_votes"],
            status=updated["status"],
            changed=changed,
        )

    def list_vote_records(self, session_id: int, limit: int = 20) -> tuple[VoteRecord, ...]:
        with self._locked_conn() as conn:
            rows = conn.execute(
                """
                SELECT voter_user_id, vote, created_at, updated_at
                FROM vote_session_votes
                WHERE session_id = ?
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()

        return tuple(
            VoteRecord(
                voter_user_id=row["voter_user_id"],
                vote=row["vote"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        )

    def close_vote_session(self, session_id: int, status: str) -> None:
        with self._locked_conn() as conn:
            conn.execute(
                """
                UPDATE vote_sessions
                SET status = ?, closed_at = ?
                WHERE id = ? AND status = 'open'
                """,
                (status, _now(), session_id),
            )
            conn.commit()

    def get_vote_session(self, session_id: int) -> VoteSession | None:
        with self._locked_conn() as conn:
            row = conn.execute(
                "SELECT * FROM vote_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return _vote_session_from_row(row) if row is not None else None

    def list_vote_sessions_for_user(
        self,
        chat_id: int,
        user_id: int,
        *,
        statuses: tuple[str, ...] | None = None,
        limit: int = 50,
    ) -> tuple[VoteSession, ...]:
        params: list[Any] = [chat_id, user_id]
        status_clause = ""
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            status_clause = f" AND status IN ({placeholders})"
            params.extend(statuses)
        params.append(limit)

        with self._locked_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM vote_sessions
                WHERE chat_id = ? AND suspect_user_id = ?
                    {status_clause}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return tuple(_vote_session_from_row(row) for row in rows)

    def expire_open_vote_sessions(self, limit: int = 100) -> tuple[VoteSession, ...]:
        now = _now()
        with self._locked_conn() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM vote_sessions
                WHERE status = 'open' AND expires_at <= ?
                ORDER BY expires_at ASC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            if not rows:
                return ()

            ids = [row["id"] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"""
                UPDATE vote_sessions
                SET status = 'expired_released', closed_at = ?
                WHERE status = 'open' AND id IN ({placeholders})
                """,
                (now, *ids),
            )
            conn.commit()

        return tuple(
            _vote_session_from_row(row, status="expired_released", closed_at=now) for row in rows
        )

    def record_vote_session_action(
        self,
        session_id: int,
        action: str,
        reason: str,
        confidence: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> int | None:
        with self._locked_conn() as conn:
            row = conn.execute(
                "SELECT * FROM vote_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None

            payload = {
                "vote_session_id": row["id"],
                "vote_message_id": row["vote_message_id"],
                "spam_votes": row["spam_votes"],
                "ham_votes": row["ham_votes"],
                "status": row["status"],
            }
            if metadata:
                payload.update(metadata)

            cursor = conn.execute(
                """
                INSERT INTO action_log (
                    chat_id, message_id, user_id, action, reason, confidence,
                    metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["chat_id"],
                    row["original_message_id"],
                    row["suspect_user_id"],
                    action,
                    reason,
                    confidence,
                    json.dumps(payload, ensure_ascii=False),
                    _now(),
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def record_admin_notification(
        self,
        *,
        vote_session_id: int | None,
        action_log_id: int | None,
        notify_user_id: int,
        message_id: int,
        base_text: str,
    ) -> int:
        now = _now()
        with self._locked_conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO admin_notifications (
                    vote_session_id, action_log_id, notify_user_id, message_id,
                    base_text, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    vote_session_id,
                    action_log_id,
                    notify_user_id,
                    message_id,
                    base_text,
                    now,
                    now,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def list_admin_notifications(self, vote_session_id: int) -> tuple[AdminNotification, ...]:
        with self._locked_conn() as conn:
            rows = conn.execute(
                """
                SELECT id, vote_session_id, action_log_id, notify_user_id, message_id,
                    base_text, created_at, updated_at
                FROM admin_notifications
                WHERE vote_session_id = ?
                ORDER BY id ASC
                """,
                (vote_session_id,),
            ).fetchall()

        return tuple(_admin_notification_from_row(row) for row in rows)

    def touch_admin_notification(self, notification_id: int) -> None:
        with self._locked_conn() as conn:
            conn.execute(
                "UPDATE admin_notifications SET updated_at = ? WHERE id = ?",
                (_now(), notification_id),
            )
            conn.commit()

    def adjust_reputation(self, chat_id: int, user_id: int, delta: float) -> None:
        now = _now()
        with self._locked_conn() as conn:
            conn.execute(
                """
                INSERT INTO user_reputation (
                    chat_id, user_id, reputation_score, first_seen_at, last_seen_at, messages_seen
                )
                VALUES (?, ?, ?, ?, ?, 0)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    reputation_score = max(0, min(100, user_reputation.reputation_score + ?)),
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    chat_id,
                    user_id,
                    max(0, min(100, self._default_reputation + delta)),
                    now,
                    now,
                    delta,
                ),
            )
            conn.commit()

    def _locked_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() must be called before use")
        self._lock.acquire()
        return _LockedConnection(self._conn, self._lock)  # type: ignore[return-value]


class _LockedConnection:
    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock

    def __enter__(self) -> sqlite3.Connection:
        return self._conn

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._lock.release()


def _now() -> int:
    return int(time.time())


def _vote_session_from_row(
    row: sqlite3.Row,
    status: str | None = None,
    closed_at: int | None = None,
) -> VoteSession:
    return VoteSession(
        id=row["id"],
        chat_id=row["chat_id"],
        original_message_id=row["original_message_id"],
        vote_message_id=row["vote_message_id"],
        suspect_user_id=row["suspect_user_id"],
        skeleton_hash=row["skeleton_hash"],
        content_hash=row["content_hash"],
        status=status if status is not None else row["status"],
        spam_votes=row["spam_votes"],
        ham_votes=row["ham_votes"],
        reason=row["reason"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        closed_at=closed_at if closed_at is not None else row["closed_at"],
    )


def _admin_notification_from_row(row: sqlite3.Row) -> AdminNotification:
    return AdminNotification(
        id=row["id"],
        vote_session_id=row["vote_session_id"],
        action_log_id=row["action_log_id"],
        notify_user_id=row["notify_user_id"],
        message_id=row["message_id"],
        base_text=row["base_text"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _sender_profile_from_row(row: sqlite3.Row) -> SenderProfile:
    is_premium = row["is_premium"]
    return SenderProfile(
        user_id=row["user_id"],
        username=row["username"],
        first_name=row["first_name"],
        last_name=row["last_name"],
        language_code=row["language_code"],
        is_bot=bool(row["is_bot"]),
        is_premium=None if is_premium is None else bool(is_premium),
        bio=row["bio"],
        bio_fetched_at=row["bio_fetched_at"],
        updated_at=row["updated_at"],
    )
