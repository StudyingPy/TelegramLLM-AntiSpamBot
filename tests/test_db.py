from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from telegram_llm_antispam.db import Database
from telegram_llm_antispam.feedback import (
    fingerprint_lookup_values,
    phrase_fingerprint_value,
    phrase_lookup_values,
    record_llm_spam_feedback,
    record_vote_ham_feedback,
    record_vote_spam_feedback,
)
from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.fingerprints import stable_hash
from telegram_llm_antispam.models import (
    DecisionAction,
    LLMJudgement,
    LocalDecision,
    UserContext,
    VoteSession,
)
from test_llm import _settings


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "bot.db")
    db.connect()
    db.migrate()
    return db


def _features():
    message = SimpleNamespace(
        message_id=10,
        chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(id=42),
        text="spam https://spam.example",
    )
    context = UserContext(chat_id=-1001, user_id=42, reputation_score=50, messages_seen=1)
    return build_message_features(message, context)


def test_vote_session_records_changed_votes(tmp_path):
    db = _db(tmp_path)
    try:
        session_id = db.create_vote_session(
            _features(),
            LocalDecision(DecisionAction.WITHDRAW_VOTE, "test", 0.8),
            timeout_seconds=60,
        )

        first = db.add_vote(session_id, voter_user_id=100, vote="spam")
        second = db.add_vote(session_id, voter_user_id=100, vote="ham")

        assert first is not None
        assert first.spam_votes == 1
        assert first.ham_votes == 0
        assert second is not None
        assert second.spam_votes == 0
        assert second.ham_votes == 1
    finally:
        db.close()


def test_expire_open_vote_sessions_marks_timeout_and_logs(tmp_path):
    db = _db(tmp_path)
    try:
        session_id = db.create_vote_session(
            _features(),
            LocalDecision(DecisionAction.WITHDRAW_VOTE, "test", 0.8),
            timeout_seconds=-1,
        )

        expired = db.expire_open_vote_sessions()
        db.record_vote_session_action(
            session_id,
            action="vote_expired_released",
            reason="vote_timeout_default_release",
            metadata={"checked": True},
        )

        assert len(expired) == 1
        assert expired[0].status == "expired_released"

        conn = sqlite3.connect(tmp_path / "bot.db")
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT action, reason, metadata_json
                FROM action_log
                WHERE action = 'vote_expired_released'
                """
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row["reason"] == "vote_timeout_default_release"
        assert json.loads(row["metadata_json"])["checked"] is True
    finally:
        db.close()


def test_vote_feedback_boosts_and_downgrades_fingerprints(tmp_path):
    db = _db(tmp_path)
    settings = _settings()
    try:
        session_id = db.create_vote_session(
            _features(),
            LocalDecision(DecisionAction.WITHDRAW_VOTE, "test", 0.8),
            timeout_seconds=60,
        )
        session = db.get_vote_session(session_id)
        assert session is not None

        record_vote_spam_feedback(db, session, settings)
        boosted = db.get_fingerprint(session.skeleton_hash or "")
        assert boosted is not None
        assert boosted.weight == settings.vote_confirmed_fingerprint_weight

        record_vote_ham_feedback(db, session, settings)
        downgraded = db.get_fingerprint(session.skeleton_hash or "")
        assert downgraded is not None
        assert downgraded.false_positive_count == 1
        assert downgraded.weight == (
            settings.vote_confirmed_fingerprint_weight
            - settings.fingerprint_false_positive_penalty
        )
    finally:
        db.close()


def test_recent_skeleton_senders_counts_distinct_users(tmp_path):
    db = _db(tmp_path)
    try:
        features = _features()
        db.record_observation(features)
        other_message = SimpleNamespace(
            message_id=11,
            chat=SimpleNamespace(id=-1001),
            from_user=SimpleNamespace(id=43),
            text="spam https://spam.example",
        )
        other_context = UserContext(chat_id=-1001, user_id=43, reputation_score=50, messages_seen=1)
        db.record_observation(build_message_features(other_message, other_context))

        assert db.count_recent_skeleton_senders(features.skeleton_hash, 300) == 2
        assert db.count_recent_skeleton_senders(features.skeleton_hash, 300, exclude_user_id=42) == 1
    finally:
        db.close()


def test_allowed_chat_can_come_from_env_or_database(tmp_path):
    db = _db(tmp_path)
    try:
        assert db.is_chat_allowed(-1001, (-1001,)) is True
        assert db.is_chat_allowed(-1002, ()) is False

        db.allow_chat(-1002, "Allowed", added_by_user_id=42)

        assert db.is_chat_allowed(-1002, ()) is True

        db.disallow_chat(-1002)

        assert db.is_chat_allowed(-1002, ()) is False
    finally:
        db.close()


def test_llm_spam_feedback_creates_medium_weight_fingerprints(tmp_path):
    db = _db(tmp_path)
    settings = _settings()
    features = _features()
    try:
        record_llm_spam_feedback(
            db,
            features,
            LLMJudgement(
                is_spam=True,
                confidence=0.95,
                category="ads",
                signal_phrases=("日赚3000",),
            ),
            settings,
        )

        skeleton = db.get_fingerprint(features.skeleton_hash)
        phrase = db.get_fingerprint(phrase_fingerprint_value("日赚3000") or "")

        assert skeleton is not None
        assert skeleton.weight == settings.llm_fingerprint_initial_weight
        assert phrase is not None
        assert phrase.source == "llm_spam_phrase"
    finally:
        db.close()


def test_phrase_fingerprints_are_used_in_lookup(tmp_path):
    db = _db(tmp_path)
    settings = _settings()
    learned = _features()
    try:
        record_llm_spam_feedback(
            db,
            learned,
            LLMJudgement(
                is_spam=True,
                confidence=0.95,
                category="ads",
                signal_phrases=("日赚3000",),
            ),
            settings,
        )

        future_message = SimpleNamespace(
            message_id=12,
            chat=SimpleNamespace(id=-1001),
            from_user=SimpleNamespace(id=44),
            text="日赚8000，点击领取教程",
        )
        future_context = UserContext(chat_id=-1001, user_id=44, reputation_score=50, messages_seen=1)
        future = build_message_features(future_message, future_context)
        phrase_values = phrase_lookup_values(future)
        strongest = db.get_strongest_fingerprint(tuple(("phrase", value) for value in phrase_values))

        assert phrase_fingerprint_value("日赚3000") in phrase_values
        assert strongest is not None
        assert strongest.fingerprint_type == "phrase"
    finally:
        db.close()


def test_record_vote_spam_feedback_refuses_empty_text_hash(tmp_path):
    """Critical regression: vote_confirmed feedback for a message whose normalized
    text was empty used to upgrade stable_hash('') to weight 85. Once that happened
    every empty/whitespace/emoji-only message by a normal-rep user was banned at 95%
    confidence with reason 'known_high_weight_fingerprint'. Production hit this twice
    (users 'hengrao' and 'Kong C7'). The write-side filter must drop the record.
    """
    db = _db(tmp_path)
    settings = _settings()
    empty_hash = stable_hash("")
    try:
        poisoned_session = VoteSession(
            id=1,
            chat_id=-1001,
            original_message_id=100,
            vote_message_id=None,
            suspect_user_id=42,
            skeleton_hash=empty_hash,
            content_hash=empty_hash,
            status="confirmed_spam",
            spam_votes=3,
            ham_votes=0,
            reason="vote_threshold_spam",
            created_at=0,
            expires_at=0,
            closed_at=0,
        )

        record_vote_spam_feedback(db, poisoned_session, settings)

        # Neither a content-typed nor a skeleton-typed row should exist for the empty
        # hash — both upserts must have been refused.
        leaked = db.get_fingerprint(empty_hash)
        assert leaked is None, (
            f"empty-text hash leaked into DB as fingerprint: {leaked!r}"
        )
    finally:
        db.close()


def test_fingerprint_lookup_values_drops_empty_text_hash():
    """Read-side defense: even if a stale e3b0c4 row exists in DB, build_message_features
    on an empty message must not ask the DB to look it up. fingerprint_lookup_values
    is what handlers.py passes to get_strongest_fingerprint, and it must filter."""
    empty_message = SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(id=42),
        text="",
    )
    context = UserContext(chat_id=-1001, user_id=42, reputation_score=50, messages_seen=0)
    features = build_message_features(empty_message, context)
    empty_hash = stable_hash("")
    assert features.skeleton_hash == empty_hash
    assert features.content_hash == empty_hash

    lookup = fingerprint_lookup_values(features)

    assert empty_hash not in [value for _kind, value in lookup], (
        f"fingerprint_lookup_values must not emit the empty-text hash: {lookup!r}"
    )
