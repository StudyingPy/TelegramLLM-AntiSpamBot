"""Separate, expiring observation store; has no moderation DB or action access."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .jev import canonical

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS observations (
 id TEXT PRIMARY KEY, created_at REAL NOT NULL, expires_at REAL NOT NULL,
 chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, user_id INTEGER,
 update_type TEXT NOT NULL, payload_hash TEXT NOT NULL, request_hash TEXT NOT NULL,
 record_json TEXT NOT NULL, jev_json TEXT, baseline_json TEXT, final_json TEXT
);
CREATE INDEX IF NOT EXISTS observations_expiry ON observations(expires_at);
CREATE INDEX IF NOT EXISTS observations_message ON observations(chat_id,message_id);
CREATE TABLE IF NOT EXISTS request_budgets(day TEXT PRIMARY KEY, calls INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS jev_cache (
 request_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL, result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS annotations (
 observation_id TEXT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
 reviewer_id TEXT NOT NULL, task TEXT NOT NULL, label TEXT NOT NULL,
 sufficient INTEGER NOT NULL, payload_hash TEXT NOT NULL, note TEXT NOT NULL,
 updated_at REAL NOT NULL, PRIMARY KEY(observation_id,reviewer_id,task)
);
"""


class ObservationStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, timeout=0.05, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)

    def begin(self, record: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO observations(id,created_at,expires_at,chat_id,message_id,user_id,"
                "update_type,payload_hash,request_hash,record_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    record["id"],
                    record["created_at"],
                    record["expires_at"],
                    record["chat_id"],
                    record["message_id"],
                    record["user_id"],
                    record["update_type"],
                    record["payload_hash"],
                    record["request_hash"],
                    canonical(record),
                ),
            )

    def reserve(self, day: str, limit: int) -> bool:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR IGNORE INTO request_budgets VALUES(?,0)", (day,))
            return (
                self._conn.execute(
                    "UPDATE request_budgets SET calls=calls+1 WHERE day=? AND calls<?", (day, limit)
                ).rowcount
                == 1
            )

    def cached(self, request_hash: str, now: float) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT result_json FROM jev_cache WHERE request_hash=? AND expires_at>?",
                (request_hash, now),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def finish_jev(self, record: dict, result: dict) -> None:
        encoded = canonical(result)
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE observations SET jev_json=? WHERE id=?", (encoded, record["id"])
            )
            if result.get("status") == "ok":
                self._conn.execute(
                    "INSERT OR REPLACE INTO jev_cache VALUES(?,?,?)",
                    (record["request_hash"], record["expires_at"], encoded),
                )

    def finish_baseline(self, observation_id: str, result: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE observations SET baseline_json=? WHERE id=?",
                (canonical(result), observation_id),
            )

    def finish_action(self, observation_id: str, result: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE observations SET final_json=? WHERE id=?",
                (canonical(result), observation_id),
            )

    def prune(self, now: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM observations WHERE expires_at<=?", (now,))
            self._conn.execute("DELETE FROM jev_cache WHERE expires_at<=?", (now,))
            # No plaintext in budgets. Keeping old days has no evaluation value.
            self._conn.execute(
                "DELETE FROM request_budgets WHERE day<date(?, 'unixepoch','-7 days')", (now,)
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def read_observations(path: Path, now: float | None = None) -> tuple[list[dict], list[dict]]:
    conn = connect_readonly(path)
    try:
        now = time.time() if now is None else now
        records = conn.execute(
            "SELECT * FROM observations WHERE expires_at>? ORDER BY created_at,id", (now,)
        ).fetchall()
        annotations = conn.execute(
            "SELECT a.* FROM annotations a JOIN observations o ON o.id=a.observation_id WHERE o.expires_at>?",
            (now,),
        ).fetchall()
    finally:
        conn.close()
    rows = []
    for row in records:
        value = json.loads(row["record_json"])
        for key, column in (
            ("jev", "jev_json"),
            ("baseline", "baseline_json"),
            ("final", "final_json"),
        ):
            value[key] = json.loads(row[column]) if row[column] else {"status": "incomplete"}
        rows.append(value)
    return rows, [dict(r) for r in annotations]


def import_annotations(path: Path, annotations: list[dict]) -> int:
    """Explicit offline import; validate every row before committing any of them."""
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with conn:
            for row in annotations:
                original = conn.execute(
                    "SELECT payload_hash,expires_at FROM observations WHERE id=?",
                    (row.get("observation_id"),),
                ).fetchone()
                if (
                    not original
                    or original["expires_at"] <= time.time()
                    or row.get("payload_hash") != original["payload_hash"]
                ):
                    raise ValueError("unknown, expired or mismatched observation")
                if (
                    row.get("task") not in {"message", "profile", "account", "behavior"}
                    or row.get("label") not in {"spam", "ham", "unknown"}
                    or type(row.get("evidence_sufficient")) is not bool
                    or not isinstance(row.get("reviewer_id"), str)
                    or not row["reviewer_id"].strip()
                    or not isinstance(row.get("note"), str)
                    or len(row["note"]) > 2000
                ):
                    raise ValueError("invalid human annotation")
                conn.execute(
                    "INSERT INTO annotations VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(observation_id,reviewer_id,task) "
                    "DO UPDATE SET label=excluded.label,sufficient=excluded.sufficient,payload_hash=excluded.payload_hash,note=excluded.note,updated_at=excluded.updated_at",
                    (
                        row["observation_id"],
                        row["reviewer_id"].strip(),
                        row["task"],
                        row["label"],
                        row["evidence_sufficient"],
                        row["payload_hash"],
                        row["note"],
                        time.time(),
                    ),
                )
        return len(annotations)
    finally:
        conn.close()
