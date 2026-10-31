"""Read-only moderation funnel and miss-proxy metrics."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


def collect_moderation_metrics(
    database_path: Path | str,
    *,
    since: int,
    until: int | None = None,
) -> dict[str, Any]:
    """Return bounded action metrics without changing the production database.

    Manual bans are only observable when they went through the bot's admin/catch-up
    action path.  The two ``manual_ban_after_*`` values are therefore useful miss
    proxies, not Recall estimates.
    """
    end = int(time.time()) if until is None else int(until)
    path = Path(database_path).resolve()
    uri = f"file:{path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        action_rows = conn.execute(
            """
            SELECT action, reason, COUNT(*) AS count
            FROM action_log
            WHERE created_at >= ? AND created_at < ?
            GROUP BY action, reason
            ORDER BY count DESC, action, reason
            """,
            (since, end),
        ).fetchall()

        def scalar(sql: str, params: tuple[object, ...] = ()) -> int:
            row = conn.execute(sql, params).fetchone()
            return int(row[0] or 0)

        manual_ban_filter = "action IN ('admin_banned_user', 'catchup_banned_user')"
        result = {
            "since": int(since),
            "until": end,
            "actions_total": scalar(
                "SELECT COUNT(*) FROM action_log WHERE created_at >= ? AND created_at < ?",
                (since, end),
            ),
            "action_counts": [
                {"action": row["action"], "reason": row["reason"], "count": row["count"]}
                for row in action_rows
            ],
            "model_requests": scalar(
                """
                SELECT COUNT(*) FROM action_log
                WHERE created_at >= ? AND created_at < ?
                    AND metadata_json LIKE '%model_chain%'
                """,
                (since, end),
            ),
            "review_total": scalar(
                """
                SELECT COUNT(*) FROM action_log
                WHERE created_at >= ? AND created_at < ? AND action = 'review'
                """,
                (since, end),
            ),
            "reviewer_unavailable": scalar(
                """
                SELECT COUNT(*) FROM action_log
                WHERE created_at >= ? AND created_at < ?
                    AND reason = 'mimo_review_unavailable'
                """,
                (since, end),
            ),
            "jev_only": scalar(
                """
                SELECT COUNT(*) FROM action_log
                WHERE created_at >= ? AND created_at < ?
                    AND json_valid(metadata_json)
                    AND json_extract(metadata_json, '$.model_chain.decision_source') = 'jev_only'
                """,
                (since, end),
            ),
            "manual_ban_total": scalar(
                f"""
                SELECT COUNT(*) FROM action_log
                WHERE created_at >= ? AND created_at < ? AND {manual_ban_filter}
                """,
                (since, end),
            ),
            "manual_ban_after_review": scalar(
                f"""
                SELECT COUNT(*) FROM action_log AS manual
                WHERE manual.created_at >= ? AND manual.created_at < ?
                    AND {manual_ban_filter.replace('action', 'manual.action')}
                    AND EXISTS (
                        SELECT 1 FROM action_log AS prior
                        WHERE prior.chat_id = manual.chat_id
                            AND prior.message_id = manual.message_id
                            AND prior.action = 'review'
                            AND prior.id < manual.id
                    )
                """,
                (since, end),
            ),
            "manual_ban_after_allow": scalar(
                f"""
                SELECT COUNT(*) FROM action_log AS manual
                WHERE manual.created_at >= ? AND manual.created_at < ?
                    AND {manual_ban_filter.replace('action', 'manual.action')}
                    AND EXISTS (
                        SELECT 1 FROM action_log AS prior
                        WHERE prior.chat_id = manual.chat_id
                            AND prior.message_id = manual.message_id
                            AND prior.action = 'allow'
                            AND prior.id < manual.id
                    )
                """,
                (since, end),
            ),
            "confirmed_vote_spam": scalar(
                """
                SELECT COUNT(*) FROM vote_sessions
                WHERE created_at >= ? AND created_at < ? AND status = 'confirmed_spam'
                """,
                (since, end),
            ),
        }
        result["interpretation"] = (
            "manual_ban_after_review/allow are observable bot-admin miss proxies; "
            "they are not human-gold Recall because external manual bans and unlogged "
            "messages are absent from the database."
        )
        return result


def format_metrics(metrics: dict[str, Any]) -> str:
    return json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=False)
