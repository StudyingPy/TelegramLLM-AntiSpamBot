from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from .fingerprints import skeletonize, stable_hash
from .text import normalize_text


STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS maintenance_marks (name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS processed_messages (
    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    normal INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
    PRIMARY KEY(chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS fingerprint_evidence (
    fingerprint_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
    text TEXT NOT NULL, label TEXT NOT NULL, truncated INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(fingerprint_id, chat_id, message_id, label),
    FOREIGN KEY(fingerprint_id) REFERENCES fingerprints(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS fingerprint_feedback (
    session_id INTEGER NOT NULL, fingerprint_id INTEGER NOT NULL, created_at INTEGER NOT NULL,
    PRIMARY KEY(session_id, fingerprint_id)
);
CREATE TABLE IF NOT EXISTS fingerprint_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint_id INTEGER NOT NULL,
    signature TEXT NOT NULL, decision TEXT NOT NULL, confidence REAL NOT NULL,
    reason TEXT NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL,
    resolved_at INTEGER, resolved_by INTEGER, notified_at INTEGER,
    UNIQUE(fingerprint_id, signature),
    FOREIGN KEY(fingerprint_id) REFERENCES fingerprints(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fingerprint_pending_review
    ON fingerprint_reviews(fingerprint_id) WHERE status='pending';
CREATE TABLE IF NOT EXISTS fingerprint_audit_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at INTEGER NOT NULL,
    finished_at INTEGER, notified_at INTEGER
);
CREATE TABLE IF NOT EXISTS fingerprint_review_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER NOT NULL,
    action TEXT NOT NULL, user_id INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fingerprint_audit_items (
    run_id INTEGER NOT NULL, fingerprint_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', error TEXT,
    PRIMARY KEY(run_id, fingerprint_id)
);
CREATE INDEX IF NOT EXISTS idx_fingerprint_evidence_id ON fingerprint_evidence(fingerprint_id);
"""


def _metadata(value: str) -> dict[str, Any]:
    try:
        data = json.loads(value)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _put_evidence(conn, fid, chat_id, message_id, text, label, timestamp):
    if not text:
        return
    row = conn.execute("SELECT pattern_text FROM fingerprints WHERE id=?", (fid,)).fetchone()
    if row is None:
        return
    pattern = row["pattern_text"] or ""
    offset = text.find(pattern) if pattern else 0
    start = max(0, offset - 200)
    conn.execute(
        """INSERT OR REPLACE INTO fingerprint_evidence
           (fingerprint_id,chat_id,message_id,text,label,truncated,created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (fid, chat_id, message_id, text[start:start + 800], label, int(len(text) > 800), timestamp),
    )
    conn.execute(
        """DELETE FROM fingerprint_evidence WHERE fingerprint_id=? AND rowid NOT IN
           (SELECT rowid FROM fingerprint_evidence WHERE fingerprint_id=? ORDER BY
            CASE label WHEN 'manual_ham' THEN 0 WHEN 'manual_spam' THEN 1
                       WHEN 'llm_benign' THEN 2 ELSE 3 END, created_at DESC, rowid DESC LIMIT 3)""",
        (fid, fid),
    )


class FingerprintStore:
    """SQLite maintenance methods mixed into Database; all writes use its shared lock."""

    def _migrate_fingerprint_store(self, conn: sqlite3.Connection) -> None:
        conn.executescript(STORE_SCHEMA)
        if conn.execute(
            "SELECT 1 FROM maintenance_marks WHERE name='trusted_history_v1'"
        ).fetchone() is None:
            for row in conn.execute(
                "SELECT chat_id,message_id,user_id,metadata_json,created_at FROM action_log "
                "WHERE action='allow' AND message_id IS NOT NULL AND user_id IS NOT NULL"
            ).fetchall():
                if _metadata(row["metadata_json"]).get("reputation_reward", 0) > 0:
                    conn.execute(
                        "INSERT OR IGNORE INTO processed_messages VALUES (?,?,?,?,?)",
                        (row["chat_id"], row["message_id"], row["user_id"], 1, row["created_at"]),
                    )
            conn.execute(
                """UPDATE user_reputation SET normal_messages_seen=(
                   SELECT COUNT(*) FROM processed_messages p WHERE p.chat_id=user_reputation.chat_id
                   AND p.user_id=user_reputation.user_id AND p.normal=1)"""
            )
            conn.execute("INSERT INTO maintenance_marks VALUES ('trusted_history_v1')")

        if conn.execute(
            "SELECT 1 FROM maintenance_marks WHERE name='fingerprint_evidence_v1'"
        ).fetchone() is None:
            fingerprints = {r["value"]: dict(r) for r in conn.execute("SELECT * FROM fingerprints")}
            for row in conn.execute(
                "SELECT * FROM action_log WHERE metadata_json LIKE '%signal_phrases%' "
                "OR metadata_json LIKE '%fingerprint_id%' ORDER BY id"
            ).fetchall():
                meta = _metadata(row["metadata_json"])
                outcome = meta.get("llm_outcome") or {}
                if not isinstance(outcome, dict):
                    outcome = {}
                phrases = []
                for data, key in ((meta, "signal_phrases"), (meta, "llm_signal_phrases"),
                                  (outcome, "signal_phrases")):
                    if isinstance(data, dict) and isinstance(data.get(key), list):
                        phrases.extend(p for p in data[key] if isinstance(p, str))
                matched = set()
                for phrase in phrases:
                    fp = fingerprints.get(stable_hash(f"phrase:{normalize_text(phrase)}"))
                    if fp and fp["fingerprint_type"] == "phrase":
                        conn.execute(
                            "UPDATE fingerprints SET pattern_text=? WHERE id=?",
                            (phrase[:4000], fp["id"]),
                        )
                        matched.add(fp["id"])
                text = str(meta.get("text_snapshot") or "")
                for kind, value, pattern in (
                    ("content", stable_hash(normalize_text(text)), normalize_text(text)),
                    ("skeleton", stable_hash(skeletonize(text)), skeletonize(text)),
                ):
                    fp = fingerprints.get(value)
                    if text and fp and fp["fingerprint_type"] == kind:
                        conn.execute(
                            "UPDATE fingerprints SET pattern_text=? WHERE id=?",
                            (pattern[:4000], fp["id"]),
                        )
                        matched.add(fp["id"])
                if isinstance(meta.get("fingerprint_id"), int):
                    matched.add(meta["fingerprint_id"])
                is_spam = outcome.get("is_spam", meta.get("llm_is_spam"))
                label = "llm_benign" if is_spam is False else (
                    "llm_spam" if is_spam is True else "local_hit"
                )
                for fid in matched:
                    _put_evidence(conn, fid, row["chat_id"], row["message_id"] or 0,
                                  text, label, row["created_at"])
            conn.execute("INSERT INTO maintenance_marks VALUES ('fingerprint_evidence_v1')")
            conn.execute("UPDATE fingerprints SET enabled=0 WHERE source='disabled_false_positive'")

        if conn.execute(
            "SELECT 1 FROM maintenance_marks WHERE name='phrase_ham_feedback_v1'"
        ).fetchone() is None:
            for row in conn.execute("SELECT id FROM vote_sessions WHERE status='released'").fetchall():
                self._apply_ham_feedback(
                    conn, row["id"], self._fingerprint_false_positive_penalty, phrases_only=True
                )
            conn.execute("INSERT INTO maintenance_marks VALUES ('phrase_ham_feedback_v1')")

    def _apply_ham_feedback(self, conn, session_id, penalty, *, phrases_only=False):
        session = conn.execute("SELECT * FROM vote_sessions WHERE id=?", (session_id,)).fetchone()
        if session is None or session["status"] != "released":
            return
        ids = set()
        if not phrases_only:
            for value in (session["skeleton_hash"], session["content_hash"]):
                fp = conn.execute("SELECT id FROM fingerprints WHERE value=?", (value,)).fetchone()
                if fp:
                    ids.add(fp["id"])
        rows = conn.execute(
            "SELECT metadata_json FROM action_log WHERE chat_id=? AND message_id=? "
            "AND action IN ('withdraw_vote','ban') ORDER BY id",
            (session["chat_id"], session["original_message_id"]),
        ).fetchall()
        text = ""
        for row in rows:
            meta = _metadata(row["metadata_json"])
            text = text or str(meta.get("text_snapshot") or "")
            fid = meta.get("fingerprint_id")
            fp = conn.execute("SELECT * FROM fingerprints WHERE id=?", (fid,)).fetchone()
            if fp and (not phrases_only or fp["fingerprint_type"] == "phrase"):
                ids.add(fp["id"])
        now = int(time.time())
        for fid in ids:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO fingerprint_feedback VALUES (?,?,?)",
                (session_id, fid, now),
            ).rowcount
            if inserted:
                conn.execute(
                    """UPDATE fingerprints SET weight=max(0,weight-?),
                       false_positive_count=false_positive_count+1,last_hit_at=? WHERE id=?""",
                    (penalty, now, fid),
                )
                _put_evidence(conn, fid, session["chat_id"], session["original_message_id"],
                              text, "manual_ham", session["closed_at"] or now)

    def apply_vote_ham_feedback(self, session_id: int, penalty: float) -> None:
        with self._locked_conn() as conn:
            self._apply_ham_feedback(conn, session_id, penalty)
            conn.commit()

    def add_fingerprint_evidence(self, value, features, label):
        with self._locked_conn() as conn:
            fp = conn.execute("SELECT id FROM fingerprints WHERE value=?", (value,)).fetchone()
            if fp:
                _put_evidence(conn, fp["id"], features.chat_id, features.message_id,
                              features.text, label, int(time.time()))
                conn.commit()

    def add_vote_spam_evidence(self, session):
        text = self.get_action_text_snapshot(session.chat_id, session.original_message_id)
        if not text:
            return
        with self._locked_conn() as conn:
            for value in {session.content_hash, session.skeleton_hash}:
                fp = conn.execute("SELECT * FROM fingerprints WHERE value=?", (value,)).fetchone()
                if fp is None:
                    continue
                pattern = skeletonize(text) if fp["fingerprint_type"] == "skeleton" else normalize_text(text)
                if fp["enabled"] and stable_hash(pattern) == value:
                    conn.execute("UPDATE fingerprints SET pattern_text=? WHERE id=?",
                                 (pattern[:4000], fp["id"]))
                _put_evidence(conn, fp["id"], session.chat_id, session.original_message_id,
                              text, "manual_spam", int(time.time()))
            conn.commit()

    def fingerprint_audit_payload(self, fingerprint_id: int) -> dict[str, Any] | None:
        with self._locked_conn() as conn:
            row = conn.execute("SELECT * FROM fingerprints WHERE id=?", (fingerprint_id,)).fetchone()
            if row is None:
                return None
            payload = dict(row)
            payload["samples"] = [dict(r) for r in conn.execute(
                "SELECT text,label,truncated FROM fingerprint_evidence WHERE fingerprint_id=? "
                "ORDER BY created_at DESC LIMIT 3", (fingerprint_id,)
            )]
        # Hit counts alone are not new semantic evidence. Human feedback and bounded
        # examples are; a kept recommendation is not re-notified for the same input.
        signature = stable_hash(json.dumps(
            [payload["pattern_text"], payload["source"], payload["false_positive_count"],
             payload["samples"]], ensure_ascii=False, sort_keys=True
        ))
        payload["signature"] = signature
        return payload

    def get_or_start_fingerprint_audit(self, interval_seconds: int, limit: int):
        now = int(time.time())
        with self._locked_conn() as conn:
            running = conn.execute(
                "SELECT * FROM fingerprint_audit_runs WHERE finished_at IS NULL ORDER BY id LIMIT 1"
            ).fetchone()
            if running:
                return running["id"]
            last = conn.execute("SELECT MAX(started_at) FROM fingerprint_audit_runs").fetchone()[0]
            if last is not None and now - last < interval_seconds:
                return None
            run_id = conn.execute(
                "INSERT INTO fingerprint_audit_runs(started_at) VALUES (?)", (now,)
            ).lastrowid
            rows = conn.execute(
                """SELECT id FROM fingerprints WHERE enabled=1 ORDER BY
                   CASE WHEN false_positive_count>0 AND
                        (last_audited_at IS NULL OR last_hit_at>last_audited_at) THEN 0
                        WHEN last_audited_at IS NULL AND created_at>=? THEN 1 ELSE 2 END,
                   COALESCE(last_audited_at,0),id LIMIT ?""", (now-86400, limit)
            ).fetchall()
            conn.executemany("INSERT INTO fingerprint_audit_items(run_id,fingerprint_id) VALUES (?,?)",
                             [(run_id, r["id"]) for r in rows])
            conn.commit()
            return run_id

    def pending_fingerprint_audit_items(self, run_id, limit):
        with self._locked_conn() as conn:
            return [r["fingerprint_id"] for r in conn.execute(
                "SELECT fingerprint_id FROM fingerprint_audit_items WHERE run_id=? "
                "AND status='pending' ORDER BY rowid LIMIT ?", (run_id, limit)
            )]

    def record_fingerprint_audit_result(self, run_id, payload, result):
        now = int(time.time())
        fid = payload["id"]
        with self._locked_conn() as conn:
            current = conn.execute("SELECT enabled FROM fingerprints WHERE id=?", (fid,)).fetchone()
            if current and current["enabled"]:
                pending = conn.execute(
                    "SELECT 1 FROM fingerprint_reviews WHERE fingerprint_id=? AND status='pending'",
                    (fid,),
                ).fetchone()
                if not pending:
                    status = "pending" if (
                        result.decision == "disable" and result.confidence >= 0.90
                    ) else "not_recommended"
                    conn.execute(
                        """INSERT INTO fingerprint_reviews
                           (fingerprint_id,signature,decision,confidence,reason,status,created_at)
                           VALUES (?,?,?,?,?,?,?) ON CONFLICT(fingerprint_id,signature)
                           DO UPDATE SET decision=excluded.decision,confidence=excluded.confidence,
                           reason=excluded.reason,status=excluded.status
                           WHERE fingerprint_reviews.status='not_recommended'""",
                        (fid, payload["signature"], result.decision, result.confidence,
                         result.reason[:600], status, now),
                    )
                conn.execute(
                    "UPDATE fingerprints SET last_audited_at=?,last_audit_signature=? WHERE id=?",
                    (now, payload["signature"], fid),
                )
            conn.execute(
                "UPDATE fingerprint_audit_items SET status='done',error=NULL "
                "WHERE run_id=? AND fingerprint_id=?", (run_id, fid)
            )
            conn.commit()

    def finish_fingerprint_audit(self, run_id):
        with self._locked_conn() as conn:
            conn.execute("UPDATE fingerprint_audit_runs SET finished_at=? WHERE id=?",
                         (int(time.time()), run_id))
            conn.commit()

    def pending_fingerprint_reviews(self, *, unnotified=False, disabled=False, offset=0, limit=1000):
        with self._locked_conn() as conn:
            condition = " AND r.notified_at IS NULL" if unnotified else ""
            status = "disabled" if disabled else "pending"
            return [dict(r) for r in conn.execute(
                "SELECT r.*,f.pattern_text,f.fingerprint_type,f.enabled FROM fingerprint_reviews r "
                "JOIN fingerprints f ON f.id=r.fingerprint_id WHERE r.status=?" + condition +
                " ORDER BY r.confidence DESC,r.id LIMIT ? OFFSET ?", (status, limit, offset)
            )]

    def count_fingerprint_reviews(self, *, disabled=False):
        with self._locked_conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM fingerprint_reviews r JOIN fingerprints f "
                "ON f.id=r.fingerprint_id WHERE r.status=?", ("disabled" if disabled else "pending",)
            ).fetchone()[0]

    def get_fingerprint_review(self, review_id):
        with self._locked_conn() as conn:
            row = conn.execute("SELECT * FROM fingerprint_reviews WHERE id=?", (review_id,)).fetchone()
            return dict(row) if row else None

    def mark_fingerprint_reviews_notified(self, review_ids):
        with self._locked_conn() as conn:
            conn.executemany("UPDATE fingerprint_reviews SET notified_at=? WHERE id=?",
                             [(int(time.time()), rid) for rid in review_ids])
            conn.commit()

    def fingerprint_review_notification_due(self):
        with self._locked_conn() as conn:
            last = conn.execute("SELECT MAX(notified_at) FROM fingerprint_reviews").fetchone()[0]
        return last is None or int(time.time()) - last >= 86400

    def resolve_fingerprint_review(self, review_id, action, user_id):
        required = "disabled" if action == "restore" else "pending"
        status = {"disable": "disabled", "keep": "kept", "restore": "restored"}.get(action)
        if status is None:
            return False
        with self._locked_conn() as conn:
            row = conn.execute("SELECT * FROM fingerprint_reviews WHERE id=?", (review_id,)).fetchone()
            if row is None or row["status"] != required:
                return False
            fp = conn.execute("SELECT enabled FROM fingerprints WHERE id=?",
                              (row["fingerprint_id"],)).fetchone()
            if fp is None or (action == "restore" and fp["enabled"]):
                return False
            conn.execute(
                "UPDATE fingerprint_reviews SET status=?,resolved_at=?,resolved_by=? WHERE id=?",
                (status, int(time.time()), user_id, review_id),
            )
            if action != "keep":
                conn.execute("UPDATE fingerprints SET enabled=? WHERE id=?",
                             (int(action == "restore"), row["fingerprint_id"]))
            conn.execute(
                "INSERT INTO fingerprint_review_actions(review_id,action,user_id,created_at) "
                "VALUES (?,?,?,?)", (review_id, action, user_id, int(time.time())),
            )
            conn.commit()
            return True
