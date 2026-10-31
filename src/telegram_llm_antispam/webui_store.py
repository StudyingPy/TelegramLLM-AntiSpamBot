"""Version-checked fingerprint review and library operations for the WebUI.

Mixed into ``Database`` like ``FingerprintStore``. Every read and write runs under the
shared ``Database`` lock; writes additionally use ``BEGIN IMMEDIATE`` so a separate
process (for example the admin CLI) cannot change a row between the version check and
the update. HTTP handlers call these methods instead of issuing SQL themselves.

Version tokens cover what a reviewer bases a decision on: enabled state, readable
pattern, source, false-positive count, the stored evidence samples and (for reviews)
the review status. Hit counts and weight bumps from ordinary traffic are excluded so
that a busy fingerprint does not become "stale" every few seconds.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator

from .fingerprint_store import REVIEW_TRANSITIONS, apply_review_transition


WEBUI_SCHEMA = """
CREATE TABLE IF NOT EXISTS webui_action_requests (
    request_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    action TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    results_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webui_action_requests_created
    ON webui_action_requests(created_at);
CREATE TABLE IF NOT EXISTS fingerprint_deletions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint_id INTEGER NOT NULL,
    fingerprint_type TEXT NOT NULL,
    value TEXT NOT NULL,
    pattern_preview TEXT,
    source TEXT NOT NULL,
    evidence_count INTEGER NOT NULL,
    review_count INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    request_id TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fingerprint_deletions_fid ON fingerprint_deletions(fingerprint_id);
"""

EVIDENCE_ORDER = (
    "CASE label WHEN 'manual_ham' THEN 0 WHEN 'manual_spam' THEN 1 "
    "WHEN 'llm_benign' THEN 2 ELSE 3 END, created_at DESC, rowid DESC"
)
LIST_PATTERN_CHARS = 160
DETAIL_PATTERN_CHARS = 4000
SAMPLE_CHARS = 800
LIST_REASON_CHARS = 200
MAX_PAGE_SIZE = 50
MAX_SAMPLES = 3
REQUEST_RETENTION_SECONDS = 7 * 86400
REVIEW_VIEW_STATUSES = ("pending", "kept", "disabled", "restored")
LIBRARY_SORTS = {
    "weight": "f.enabled DESC, f.weight DESC, f.hit_count DESC, f.id ASC",
    "hits": "f.hit_count DESC, f.id ASC",
    "false_positives": "f.false_positive_count DESC, f.id ASC",
    "newest": "f.created_at DESC, f.id DESC",
    "last_hit": "COALESCE(f.last_hit_at, 0) DESC, f.id DESC",
}


class RequestIdConflict(Exception):
    """A request ID was reused for a different payload or by a different user."""


@dataclass(frozen=True, slots=True)
class LibraryFilters:
    query: str | None = None
    fingerprint_type: str | None = None
    enabled: bool | None = None
    source: str | None = None
    weight_min: float | None = None
    weight_max: float | None = None
    hits_min: int | None = None
    hits_max: int | None = None
    false_positives_min: int | None = None
    false_positives_max: int | None = None
    sort: str = "weight"


def _digest(material: Any) -> str:
    raw = json.dumps(material, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def fingerprint_version(fp: Any, evidence: list[Any]) -> str:
    return _digest([
        fp["fingerprint_type"], fp["value"], int(fp["enabled"]), fp["pattern_text"],
        fp["source"], int(fp["false_positive_count"]),
        sorted([int(e["chat_id"]), int(e["message_id"]), e["label"], e["text"],
                int(e["truncated"])] for e in evidence),
    ])


def review_version(review: Any, fp_version: str) -> str:
    return _digest([int(review["id"]), review["status"], fp_version])


def _truncate(value: Any, limit: int) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    text = str(value)
    return (text[:limit], True) if len(text) > limit else (text, False)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _query_clause(query: str | None, params: list[Any]) -> str:
    """Match readable pattern text; '#12'/'12' also match ID, hex prefixes match value."""
    if not query:
        return ""
    parts = ["f.pattern_text LIKE ? ESCAPE '\\'"]
    params.append(f"%{_escape_like(query)}%")
    id_match = re.fullmatch(r"#?([0-9]{1,18})", query)
    if id_match:
        parts.append("f.id = ?")
        params.append(int(id_match.group(1)))
    if re.fullmatch(r"[0-9a-f]{6,64}", query.lower()):
        parts.append("f.value LIKE ?")
        params.append(query.lower() + "%")
    return " AND (" + " OR ".join(parts) + ")"


def _evidence(conn, fingerprint_id: int) -> list[Any]:
    return conn.execute(
        "SELECT chat_id,message_id,label,text,truncated,created_at FROM fingerprint_evidence "
        f"WHERE fingerprint_id=? ORDER BY {EVIDENCE_ORDER}",
        (fingerprint_id,),
    ).fetchall()


def _fingerprint_summary(fp: Any, evidence: list[Any]) -> dict[str, Any]:
    pattern, truncated = _truncate(fp["pattern_text"], LIST_PATTERN_CHARS)
    return {
        "id": fp["id"],
        "type": fp["fingerprint_type"],
        "pattern_text": pattern,
        "pattern_truncated": truncated,
        "value_prefix": str(fp["value"])[:12],
        "enabled": bool(fp["enabled"]),
        "weight": float(fp["weight"]),
        "hit_count": int(fp["hit_count"]),
        "false_positive_count": int(fp["false_positive_count"]),
        "source": fp["source"],
        "has_manual_ham": int(fp["false_positive_count"]) > 0
        or any(e["label"] == "manual_ham" for e in evidence),
        "version": fingerprint_version(fp, evidence),
    }


def _fingerprint_detail(fp: Any, evidence: list[Any]) -> dict[str, Any]:
    data = _fingerprint_summary(fp, evidence)
    pattern, truncated = _truncate(fp["pattern_text"], DETAIL_PATTERN_CHARS)
    data.update({
        "pattern_text": pattern,
        "pattern_truncated": truncated,
        "value": fp["value"],
        "created_at": fp["created_at"],
        "last_hit_at": fp["last_hit_at"],
        "last_audited_at": fp["last_audited_at"],
        "samples": [
            {
                "label": e["label"],
                "text": str(e["text"])[:SAMPLE_CHARS],
                # Stored samples are already windowed to 800 chars around the match.
                "truncated": bool(e["truncated"]) or len(str(e["text"])) > SAMPLE_CHARS,
                "created_at": e["created_at"],
            }
            for e in evidence[:MAX_SAMPLES]
        ],
    })
    return data


def _review_summary(review: Any, fp: Any, evidence: list[Any]) -> dict[str, Any]:
    fp_data = _fingerprint_summary(fp, evidence)
    reason, reason_truncated = _truncate(review["reason"], LIST_REASON_CHARS)
    return {
        "id": review["id"],
        "fingerprint_id": review["fingerprint_id"],
        "status": review["status"],
        "decision": review["decision"],
        "confidence": float(review["confidence"]),
        "reason": reason,
        "reason_truncated": reason_truncated,
        "created_at": review["created_at"],
        "resolved_at": review["resolved_at"],
        "resolved_by": review["resolved_by"],
        "fingerprint": fp_data,
        "version": review_version(review, fp_data["version"]),
    }


def _request_hash(*material: Any) -> str:
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class FingerprintWebStore:
    """WebUI queries and version-checked actions; relies on Database._locked_conn."""

    if TYPE_CHECKING:
        def _locked_conn(self) -> Any: ...

    def _migrate_webui_store(self, conn) -> None:
        conn.executescript(WEBUI_SCHEMA)

    @contextmanager
    def _webui_write(self) -> Iterator[Any]:
        with self._locked_conn() as conn:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            conn.commit()

    # ------------------------------------------------------------------ reads

    def webui_ping(self) -> bool:
        with self._locked_conn() as conn:
            return conn.execute("SELECT 1").fetchone()[0] == 1

    def webui_summary(self) -> dict[str, Any]:
        with self._locked_conn() as conn:
            counts = {status: 0 for status in REVIEW_VIEW_STATUSES}
            for row in conn.execute(
                "SELECT r.status, COUNT(*) AS n FROM fingerprint_reviews r "
                "JOIN fingerprints f ON f.id=r.fingerprint_id GROUP BY r.status"
            ):
                if row["status"] in counts:
                    counts[row["status"]] = int(row["n"])
            pending = conn.execute(
                "SELECT COUNT(*) AS n, MIN(r.created_at) AS oldest, "
                "SUM(CASE WHEN f.false_positive_count>0 OR EXISTS (SELECT 1 FROM "
                "fingerprint_evidence e WHERE e.fingerprint_id=f.id AND e.label='manual_ham') "
                "THEN 1 ELSE 0 END) AS manual FROM fingerprint_reviews r "
                "JOIN fingerprints f ON f.id=r.fingerprint_id WHERE r.status='pending'"
            ).fetchone()
            library = conn.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(enabled),0) AS enabled FROM fingerprints"
            ).fetchone()
            run = conn.execute(
                "SELECT * FROM fingerprint_audit_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            scan = None
            if run is not None:
                items = conn.execute(
                    "SELECT COUNT(*) AS total, SUM(status='pending') AS remaining "
                    "FROM fingerprint_audit_items WHERE run_id=?", (run["id"],)
                ).fetchone()
                scan = {
                    "started_at": run["started_at"],
                    "finished_at": run["finished_at"],
                    "running": run["finished_at"] is None,
                    "total": int(items["total"] or 0),
                    "remaining": int(items["remaining"] or 0),
                }
        return {
            "reviews": counts,
            "pending_with_manual_ham": int(pending["manual"] or 0),
            "oldest_pending_at": pending["oldest"],
            "library_total": int(library["total"]),
            "library_enabled": int(library["enabled"]),
            "scan": scan,
            "server_time": int(time.time()),
        }

    def webui_filter_options(self) -> dict[str, list[str]]:
        with self._locked_conn() as conn:
            types = [r[0] for r in conn.execute(
                "SELECT DISTINCT fingerprint_type FROM fingerprints ORDER BY 1")]
            sources = [r[0] for r in conn.execute(
                "SELECT DISTINCT source FROM fingerprints ORDER BY 1")]
        return {"types": types[:50], "sources": sources[:50]}

    def webui_list_reviews(
        self, status: str, *, query: str | None = None, fingerprint_type: str | None = None,
        offset: int = 0, limit: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        if status not in REVIEW_VIEW_STATUSES:
            raise ValueError(f"unknown review status: {status}")
        params: list[Any] = [status]
        where = "r.status=?"
        if fingerprint_type:
            where += " AND f.fingerprint_type=?"
            params.append(fingerprint_type)
        where += _query_clause(query, params)
        order = (
            "(f.false_positive_count>0) DESC, f.false_positive_count DESC, r.created_at ASC, r.id"
            if status == "pending" else "COALESCE(r.resolved_at, r.created_at) DESC, r.id DESC"
        )
        with self._locked_conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM fingerprint_reviews r JOIN fingerprints f "
                f"ON f.id=r.fingerprint_id WHERE {where}", tuple(params),
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT r.* FROM fingerprint_reviews r JOIN fingerprints f "
                f"ON f.id=r.fingerprint_id WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, max(1, min(MAX_PAGE_SIZE, limit)), max(0, offset)),
            ).fetchall()
            items = []
            for review in rows:
                fp = conn.execute("SELECT * FROM fingerprints WHERE id=?",
                                  (review["fingerprint_id"],)).fetchone()
                items.append(_review_summary(review, fp, _evidence(conn, fp["id"])))
        return items, int(total)

    def _review_detail_locked(self, conn, review_id: int) -> dict[str, Any] | None:
        review = conn.execute("SELECT * FROM fingerprint_reviews WHERE id=?",
                              (review_id,)).fetchone()
        if review is None:
            return None
        fp = conn.execute("SELECT * FROM fingerprints WHERE id=?",
                          (review["fingerprint_id"],)).fetchone()
        if fp is None:
            return None
        evidence = _evidence(conn, fp["id"])
        data = _review_summary(review, fp, evidence)
        data["reason"], data["reason_truncated"] = _truncate(review["reason"], 600)
        data["fingerprint"] = _fingerprint_detail(fp, evidence)
        data["history"] = [dict(r) for r in conn.execute(
            "SELECT action,user_id,created_at FROM fingerprint_review_actions "
            "WHERE review_id=? ORDER BY id DESC LIMIT 20", (review_id,)
        )]
        return data

    def webui_get_review(self, review_id: int) -> dict[str, Any] | None:
        with self._locked_conn() as conn:
            return self._review_detail_locked(conn, review_id)

    def webui_list_fingerprints(
        self, filters: LibraryFilters, *, offset: int = 0, limit: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        params: list[Any] = []
        where = "1=1"
        for column, op, value in (
            ("f.fingerprint_type", "=", filters.fingerprint_type),
            ("f.source", "=", filters.source),
            ("f.enabled", "=", None if filters.enabled is None else int(filters.enabled)),
            ("f.weight", ">=", filters.weight_min),
            ("f.weight", "<=", filters.weight_max),
            ("f.hit_count", ">=", filters.hits_min),
            ("f.hit_count", "<=", filters.hits_max),
            ("f.false_positive_count", ">=", filters.false_positives_min),
            ("f.false_positive_count", "<=", filters.false_positives_max),
        ):
            if value is not None:
                where += f" AND {column} {op} ?"
                params.append(value)
        where += _query_clause(filters.query, params)
        order = LIBRARY_SORTS.get(filters.sort, LIBRARY_SORTS["weight"])
        with self._locked_conn() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM fingerprints f WHERE {where}",
                                 tuple(params)).fetchone()[0]
            rows = conn.execute(
                f"SELECT f.* FROM fingerprints f WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, max(1, min(MAX_PAGE_SIZE, limit)), max(0, offset)),
            ).fetchall()
            items = []
            for fp in rows:
                item = _fingerprint_summary(fp, _evidence(conn, fp["id"]))
                latest = conn.execute(
                    "SELECT id,status FROM fingerprint_reviews WHERE fingerprint_id=? "
                    "ORDER BY id DESC LIMIT 1", (fp["id"],)
                ).fetchone()
                item["latest_review"] = dict(latest) if latest else None
                item["created_at"] = fp["created_at"]
                item["last_hit_at"] = fp["last_hit_at"]
                items.append(item)
        return items, int(total)

    def _fingerprint_detail_locked(self, conn, fingerprint_id: int) -> dict[str, Any] | None:
        fp = conn.execute("SELECT * FROM fingerprints WHERE id=?", (fingerprint_id,)).fetchone()
        if fp is None:
            return None
        evidence = _evidence(conn, fingerprint_id)
        data = _fingerprint_detail(fp, evidence)
        reviews = conn.execute(
            "SELECT * FROM fingerprint_reviews WHERE fingerprint_id=? ORDER BY id DESC",
            (fingerprint_id,),
        ).fetchall()
        data["reviews"] = [
            {
                "id": r["id"], "status": r["status"], "decision": r["decision"],
                "confidence": float(r["confidence"]), "created_at": r["created_at"],
                "resolved_at": r["resolved_at"],
            }
            for r in reviews[:10]
        ]
        data["delete_impact"] = {"evidence_count": len(evidence), "review_count": len(reviews)}
        return data

    def webui_get_fingerprint(self, fingerprint_id: int) -> dict[str, Any] | None:
        with self._locked_conn() as conn:
            return self._fingerprint_detail_locked(conn, fingerprint_id)

    # ----------------------------------------------------------------- writes

    def _replayed_results(self, conn, request_id, user_id, request_hash):
        row = conn.execute("SELECT * FROM webui_action_requests WHERE request_id=?",
                           (request_id,)).fetchone()
        if row is None:
            return None
        if row["user_id"] != user_id or row["request_hash"] != request_hash:
            raise RequestIdConflict(request_id)
        return json.loads(row["results_json"])

    def _remember_request(self, conn, request_id, user_id, kind, action, request_hash,
                          results, now) -> None:
        conn.execute("DELETE FROM webui_action_requests WHERE created_at<?",
                     (now - REQUEST_RETENTION_SECONDS,))
        conn.execute(
            "INSERT INTO webui_action_requests"
            "(request_id,user_id,kind,action,request_hash,results_json,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (request_id, user_id, kind, action, request_hash,
             json.dumps(results, ensure_ascii=False), now),
        )

    def webui_review_action(
        self, *, request_id: str, user_id: int, action: str,
        items: list[tuple[int, str]],
    ) -> dict[str, Any]:
        """Apply keep/disable/restore to explicitly selected reviews.

        Each item is checked against the version the reviewer saw. Items are reported
        individually; the whole request and its per-item results are committed together,
        so a retried request ID returns the original outcome without acting again.
        """
        if action not in REVIEW_TRANSITIONS:
            raise ValueError(f"unknown review action: {action}")
        if not items or len({rid for rid, _ in items}) != len(items):
            raise ValueError("items must be non-empty and unique")
        request_hash = _request_hash("review", action, [[rid, ver] for rid, ver in items])
        now = int(time.time())
        with self._webui_write() as conn:
            stored = self._replayed_results(conn, request_id, user_id, request_hash)
            if stored is not None:
                return {"replayed": True, "results": stored}
            required, target = REVIEW_TRANSITIONS[action]
            results = []
            for review_id, version in items:
                review = conn.execute("SELECT * FROM fingerprint_reviews WHERE id=?",
                                      (review_id,)).fetchone()
                fp = None if review is None else conn.execute(
                    "SELECT * FROM fingerprints WHERE id=?", (review["fingerprint_id"],)
                ).fetchone()
                if review is None or fp is None:
                    status = "not_found"
                elif review["status"] == target:
                    status = "already_done"
                elif review_version(
                    review, fingerprint_version(fp, _evidence(conn, fp["id"]))
                ) != version:
                    status = "stale"
                elif review["status"] != required:
                    status = "invalid"
                elif apply_review_transition(conn, review, action, user_id, now):
                    status = "ok"
                else:
                    status = "stale"
                results.append({"review_id": review_id, "status": status})
            self._remember_request(conn, request_id, user_id, "review", action,
                                   request_hash, results, now)
        return {"replayed": False, "results": results}

    def webui_delete_fingerprint(
        self, *, request_id: str, user_id: int, fingerprint_id: int, version: str,
    ) -> dict[str, Any]:
        """Physically delete one fingerprint after the caller confirmed it.

        Evidence and review rows are removed explicitly as well as through the
        foreign-key cascade, so the result does not depend on PRAGMA foreign_keys.
        A snapshot (bounded preview, counts, operator) goes to fingerprint_deletions.
        """
        request_hash = _request_hash("delete", fingerprint_id, version)
        now = int(time.time())
        with self._webui_write() as conn:
            stored = self._replayed_results(conn, request_id, user_id, request_hash)
            if stored is not None:
                return {"replayed": True, "results": stored}
            fp = conn.execute("SELECT * FROM fingerprints WHERE id=?",
                              (fingerprint_id,)).fetchone()
            result: dict[str, Any] = {"fingerprint_id": fingerprint_id}
            if fp is None:
                deleted = conn.execute(
                    "SELECT 1 FROM fingerprint_deletions WHERE fingerprint_id=?",
                    (fingerprint_id,),
                ).fetchone()
                result["status"] = "already_done" if deleted else "not_found"
            else:
                evidence = _evidence(conn, fingerprint_id)
                if fingerprint_version(fp, evidence) != version:
                    result["status"] = "stale"
                else:
                    review_count = conn.execute(
                        "SELECT COUNT(*) FROM fingerprint_reviews WHERE fingerprint_id=?",
                        (fingerprint_id,),
                    ).fetchone()[0]
                    preview, _ = _truncate(fp["pattern_text"], 200)
                    conn.execute(
                        "INSERT INTO fingerprint_deletions(fingerprint_id,fingerprint_type,value,"
                        "pattern_preview,source,evidence_count,review_count,user_id,request_id,"
                        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (fingerprint_id, fp["fingerprint_type"], fp["value"], preview,
                         fp["source"], len(evidence), review_count, user_id, request_id, now),
                    )
                    conn.execute("DELETE FROM fingerprint_evidence WHERE fingerprint_id=?",
                                 (fingerprint_id,))
                    conn.execute("DELETE FROM fingerprint_reviews WHERE fingerprint_id=?",
                                 (fingerprint_id,))
                    conn.execute("DELETE FROM fingerprints WHERE id=?", (fingerprint_id,))
                    result.update(status="ok", evidence_count=len(evidence),
                                  review_count=int(review_count))
            results = [result]
            self._remember_request(conn, request_id, user_id, "delete", "delete",
                                   request_hash, results, now)
        return {"replayed": False, "results": results}
