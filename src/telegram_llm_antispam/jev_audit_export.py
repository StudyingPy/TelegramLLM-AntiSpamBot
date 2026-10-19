"""Create a narrow read-only action/session export for Jev label auditing."""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
from pathlib import Path
from typing import Any


def export_rows(db_path: Path, pairs: list[tuple[int, int]]) -> dict[str, list[dict[str, Any]]]:
    if not pairs:
        return {"actions": [], "sessions": []}
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    actions, sessions = [], []
    try:
        conn.execute("BEGIN")
        pairs = list(dict.fromkeys(pairs))
        for offset in range(0, len(pairs), 200):
            batch = pairs[offset : offset + 200]
            action_where = " OR ".join("(chat_id = ? AND message_id = ?)" for _ in batch)
            session_where = " OR ".join("(chat_id = ? AND original_message_id = ?)" for _ in batch)
            params = tuple(value for pair in batch for value in pair)
            actions.extend(
                conn.execute(
                    f"SELECT id, chat_id, message_id, user_id, action, reason, confidence, metadata_json, created_at FROM action_log WHERE {action_where}",
                    params,
                ).fetchall()
            )
            sessions.extend(
                conn.execute(
                    f"SELECT id, chat_id, original_message_id, suspect_user_id, status, spam_votes, ham_votes, reason, detail_text, created_at, closed_at FROM vote_sessions WHERE {session_where}",
                    params,
                ).fetchall()
            )
    finally:
        conn.close()
    return {"actions": [dict(row) for row in actions], "sessions": [dict(row) for row in sessions]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--pairs", type=Path, help="JSON list of [chat_id, message_id] pairs.")
    parser.add_argument(
        "--normal-candidates",
        action="store_true",
        help="Export bounded live edit baselines for human annotation only.",
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260921)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.normal_candidates:
        conn = sqlite3.connect(f"file:{args.db.resolve().as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        try:
            rows = conn.execute(
                "SELECT chat_id,message_id,user_id,text_snapshot,created_at,expires_at "
                "FROM edit_watch_messages WHERE length(text_snapshot)>0 "
                "AND expires_at>CAST(strftime('%s','now') AS INTEGER) "
                "ORDER BY chat_id,message_id LIMIT 1000"
            ).fetchall()
        finally:
            conn.close()
        chosen = random.Random(args.seed).sample(list(rows), min(max(args.limit, 0), len(rows)))
        result = {
            "normal_candidates": [dict(r) for r in chosen],
            "provenance": {
                "table": "edit_watch_messages",
                "seed": args.seed,
                "purpose": "pending_human_annotation_not_ham_truth",
                "historical_profile_available": False,
            },
        }
    else:
        if args.pairs is None:
            parser.error("--pairs is required unless --normal-candidates is used")
        pairs = [tuple(item) for item in json.loads(args.pairs.read_text(encoding="utf-8"))]
        result = export_rows(args.db, pairs)
    args.output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({key: len(value) for key, value in result.items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
