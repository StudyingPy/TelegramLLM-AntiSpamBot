import json
import sqlite3

from telegram_llm_antispam.jev_eval import (
    JEV_PROMPT_HASH,
    JEV_QUESTIONS,
    _jev_state,
    build_dataset,
    write_comparison,
)


def _db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE action_log (
            id INTEGER PRIMARY KEY, chat_id INTEGER, message_id INTEGER, user_id INTEGER,
            action TEXT, reason TEXT, confidence REAL, metadata_json TEXT, created_at INTEGER
        );
        CREATE TABLE vote_sessions (
            id INTEGER PRIMARY KEY, chat_id INTEGER, original_message_id INTEGER,
            status TEXT, spam_votes INTEGER, ham_votes INTEGER, detail_text TEXT, created_at INTEGER
        );
        """
    )
    conn.commit()
    return conn


def test_build_dataset_preserves_text_but_does_not_trust_terminal_status(tmp_path):
    path = tmp_path / "bot.db"
    conn = _db(path)
    text = "不要看\u200b广告 @example"
    detail = (
        "用户资料：昵称 @name\nBio：正常资料\n"
        "正文：\n<blockquote>不要看\u200b广告 @example</blockquote>"
    )
    conn.execute(
        "INSERT INTO action_log VALUES (1,-1001,10,20,'withdraw_vote','llm_spam',0.8,?,100)",
        (json.dumps({"text_snapshot": text, "llm_outcome": {"status": "ok", "is_spam": True, "confidence": 0.8}}),),
    )
    conn.execute(
        "INSERT INTO vote_sessions VALUES (1,-1001,10,'confirmed_spam',2,0,?,101)", (detail,)
    )
    conn.commit()
    conn.close()

    rows = build_dataset(path)
    assert len(rows) == 1
    assert rows[0]["raw_text"] == text  # zero-width character is retained
    assert rows[0]["reference"]["label"] is None
    assert rows[0]["reference"]["human_confirmed"] is False
    assert rows[0]["historical_context"]["reputation_at_moderation"] is None
    assert rows[0]["original_llm"]["confidence"] == 0.8


def test_jev_state_excludes_labels_and_prior_decisions():
    sample = {
        "chat_id": -1001,
        "message_id": 2,
        "raw_text": "正常消息",
        "clean_text": "正常消息",
        "reference": {"label": "spam"},
        "original_llm": {"is_spam": True, "confidence": 0.99},
        "original_action": {"action": "ban"},
        "historical_context": {"sender_profile_at_moderation": None},
    }
    state = _jev_state(sample)
    encoded = json.dumps(state, ensure_ascii=False)
    assert "reference" not in state
    assert "original_llm" not in state
    assert "ban" not in encoded
    assert JEV_QUESTIONS["is_spam"]["type"] == "noul"
    assert JEV_QUESTIONS["category"]["type"] == "choice"
    assert len(JEV_PROMPT_HASH) == 64


def test_comparison_can_be_generated_without_api(tmp_path):
    dataset = [{
        "sample_id": "-1001:1", "raw_text": "广告", "reference": {"label": "spam", "source": "human", "human_confirmed": True},
        "original_action": {"action": "withdraw_vote", "reason": "llm_spam"}, "original_llm": None,
    }]
    predictions = [{"sample_id": "-1001:1", "status": "not_run"}]
    rows = write_comparison(dataset, predictions, tmp_path / "comparison.csv", review_threshold=0.7, ban_threshold=0.85)
    assert rows[0]["jev_status"] == "not_run"
