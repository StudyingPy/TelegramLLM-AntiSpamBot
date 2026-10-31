import sqlite3

from telegram_llm_antispam.metrics import collect_moderation_metrics


def test_collect_moderation_metrics_is_read_only_and_counts_review_followups(tmp_path):
    path = tmp_path / "bot.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE action_log (
            id INTEGER PRIMARY KEY,
            chat_id INTEGER,
            message_id INTEGER,
            action TEXT,
            reason TEXT,
            metadata_json TEXT,
            created_at INTEGER
        );
        CREATE TABLE vote_sessions (
            id INTEGER PRIMARY KEY,
            created_at INTEGER,
            status TEXT
        );
        INSERT INTO action_log VALUES
            (1, -1001, 10, 'review', 'llm_low_confidence', '{}', 100),
            (2, -1001, 10, 'catchup_banned_user', 'catchup_review_ban', '{}', 200),
            (3, -1001, 11, 'allow', 'llm_not_spam', '{}', 300),
            (4, -1001, 11, 'admin_banned_user', 'admin_skip_vote_ban', '{}', 400),
            (5, -1001, 12, 'review', 'mimo_review_unavailable',
             '{"model_chain":{"decision_source":"pending_review"}}', 500),
            (6, -1001, 13, 'ban', 'llm_spam_high_confidence',
             '{"model_chain":{"decision_source":"jev_only"}}', 600);
        INSERT INTO vote_sessions VALUES (1, 700, 'confirmed_spam');
        """
    )
    conn.commit()
    conn.close()

    metrics = collect_moderation_metrics(path, since=0, until=1000)

    assert metrics["actions_total"] == 6
    assert metrics["review_total"] == 2
    assert metrics["reviewer_unavailable"] == 1
    assert metrics["jev_only"] == 1
    assert metrics["manual_ban_total"] == 2
    assert metrics["manual_ban_after_review"] == 1
    assert metrics["manual_ban_after_allow"] == 1
    assert metrics["confirmed_vote_spam"] == 1

    check = sqlite3.connect(path)
    assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    check.close()
