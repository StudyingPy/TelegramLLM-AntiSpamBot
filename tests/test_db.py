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
    """Build a MessageFeatures with a multi-word skeleton that is NOT classified as
    low-entropy. The previous fixture text 'spam https://spam.example' produces
    skeleton '<w> <url>' which is now correctly rejected by feedback filters as a
    universal collider. Real spam fingerprint tests need a fixture with discriminative
    content."""
    message = SimpleNamespace(
        message_id=10,
        chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(id=42),
        text="加群送码拿钱 详细教程 https://spam.example",
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


def test_record_fingerprint_hit_accumulates_weight_up_to_cap(tmp_path):
    """Regression: an LLM-derived fingerprint starts at weight 50, which maps to a
    fixed 0.50 confidence via min(0.90, weight/100). Repeated hits on a known-spam
    fingerprint are corroborating evidence and must raise its weight (and thus
    confidence) instead of staying pinned at 50%, capped below the auto-ban line."""
    db = _db(tmp_path)
    try:
        db.upsert_fingerprint("phrase", "known-spam-phrase", 50, "llm_spam_phrase")
        fp = db.get_fingerprint("known-spam-phrase")
        assert fp is not None
        assert fp.weight == 50

        increment, cap = 5, 80
        new_weight = db.record_fingerprint_hit(
            fp.id, weight_increment=increment, weight_cap=cap
        )
        assert new_weight == 55

        # Climbs with each hit but never exceeds the cap.
        last = new_weight
        for _ in range(20):
            last = db.record_fingerprint_hit(
                fp.id, weight_increment=increment, weight_cap=cap
            )
        assert last == cap

        bumped = db.get_fingerprint("known-spam-phrase")
        assert bumped is not None
        assert bumped.weight == cap
        assert bumped.hit_count == 21

        # An already-higher weight is never dragged down to the cap.
        db.upsert_fingerprint("content", "vote-confirmed", 85, "vote_confirmed")
        strong = db.get_fingerprint("vote-confirmed")
        assert strong is not None
        held = db.record_fingerprint_hit(
            strong.id, weight_increment=increment, weight_cap=cap
        )
        assert held == 85
    finally:
        db.close()


def test_recent_skeleton_senders_counts_distinct_users(tmp_path):
    db = _db(tmp_path)
    try:
        features = _features()
        db.record_observation(features)
        # Same text → same skeleton, different sender. Counts as a second distinct user.
        other_message = SimpleNamespace(
            message_id=11,
            chat=SimpleNamespace(id=-1001),
            from_user=SimpleNamespace(id=43),
            text="加群送码拿钱 详细教程 https://spam.example",
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
                # 4-char CJK phrase clears the minimum-length guard. "日赚3000"
                # normalizes to "日赚" (2 chars) and would be rejected.
                signal_phrases=("日赚过万",),
            ),
            settings,
        )

        skeleton = db.get_fingerprint(features.skeleton_hash)
        phrase = db.get_fingerprint(phrase_fingerprint_value("日赚过万") or "")

        assert skeleton is not None
        assert skeleton.weight == settings.llm_fingerprint_initial_weight
        assert phrase is not None
        assert phrase.source == "llm_spam_phrase"
    finally:
        db.close()


def test_phrase_fingerprints_are_used_in_lookup(tmp_path):
    """LLM signal phrases of 3+ CJK chars are persisted and later matched.

    Originally this test used "日赚3000" — but normalize_text strips digits, leaving
    "日赚" (2 CJK chars), which is now below the minimum-phrase-length guard. That
    guard exists because 2-char CJK words like "可以"/"我们" collide against every
    Chinese conversation. Use a true 4-char CJK phrase instead, which is what we
    want to match anyway."""
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
                signal_phrases=("日赚过万",),
            ),
            settings,
        )

        future_message = SimpleNamespace(
            message_id=12,
            chat=SimpleNamespace(id=-1001),
            from_user=SimpleNamespace(id=44),
            text="日赚过万 点击领取教程",
        )
        future_context = UserContext(chat_id=-1001, user_id=44, reputation_score=50, messages_seen=1)
        future = build_message_features(future_message, future_context)
        phrase_values = phrase_lookup_values(future)
        strongest = db.get_strongest_fingerprint(tuple(("phrase", value) for value in phrase_values))

        assert phrase_fingerprint_value("日赚过万") in phrase_values
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


def test_llm_spam_feedback_refuses_to_promote_short_phrases(tmp_path):
    """Regression: LLM "signal phrases" like "可以" / "OK" / "我们" used to be stored as
    weight-50 phrase fingerprints. With fingerprint_review_weight=40, weight 50 is
    enough to trigger WITHDRAW_VOTE on every later message containing the same
    2-char CJK word — i.e. every Chinese conversation. Minimum length: 3 CJK chars
    or 4 Latin chars."""
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
                # Mix of short (should be dropped) and long (should be kept) phrases.
                signal_phrases=("可以", "OK", "see", "日赚过万", "click here"),
            ),
            settings,
        )

        for short in ("可以", "OK", "see"):
            value = phrase_fingerprint_value(short)
            if value is not None:
                assert db.get_fingerprint(value) is None, (
                    f"short phrase {short!r} must not be persisted as a fingerprint"
                )

        # Long phrases still get persisted as before.
        kept = db.get_fingerprint(phrase_fingerprint_value("日赚过万") or "")
        assert kept is not None, "long CJK phrase should still be persisted"
        kept_latin = db.get_fingerprint(phrase_fingerprint_value("click here") or "")
        assert kept_latin is not None, "long Latin phrase should still be persisted"
    finally:
        db.close()


def test_low_entropy_skeleton_hash_is_not_emitted_for_lookup():
    """A message that is just a URL has skeleton "<url>" and content_hash from
    normalize_text — fingerprint_lookup_values must NOT include the skeleton
    lookup entry (it would match every bare-URL message). It can still emit a
    content-typed entry, which is per-URL specific."""
    bare_url_message = SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(id=42),
        text="https://example.com/article",
    )
    context = UserContext(chat_id=-1001, user_id=42, reputation_score=50, messages_seen=0)
    features = build_message_features(bare_url_message, context)
    assert features.skeleton == "<url>"

    sentinel_skeleton_hash = stable_hash("<url>")
    lookup = fingerprint_lookup_values(features)

    skeleton_hashes_emitted = [value for kind, value in lookup if kind == "skeleton"]
    assert sentinel_skeleton_hash not in skeleton_hashes_emitted, (
        f"low-entropy <url> skeleton must not be looked up: {lookup!r}"
    )


def test_whitelisted_user_persists_and_combines_with_env(tmp_path):
    """Whitelist feature mirrors the allow_chat shape: env-configured ids take
    effect instantly, runtime-added ids persist in whitelisted_users table. Both
    sources must be honored by is_user_whitelisted."""
    db = _db(tmp_path)
    try:
        # User not in env, not in db → not whitelisted.
        assert db.is_user_whitelisted(5304501737, ()) is False

        # Add via env-style configured tuple → whitelisted (no DB row needed).
        assert db.is_user_whitelisted(5304501737, (5304501737,)) is True

        # Add via DB → whitelisted even when env is empty. Survives lookup with
        # empty env tuple, which is how production runs without WHITELISTED_USER_IDS.
        db.whitelist_user(5304501737, note="nmBot 客服酱", added_by_user_id=42)
        assert db.is_user_whitelisted(5304501737, ()) is True

        rows = db.list_whitelisted_users()
        assert len(rows) == 1
        assert rows[0]["user_id"] == 5304501737
        assert rows[0]["note"] == "nmBot 客服酱"

        removed = db.unwhitelist_user(5304501737)
        assert removed is True
        assert db.is_user_whitelisted(5304501737, ()) is False
        # env removal not affected by unwhitelist — env-configured ids always win.
        assert db.is_user_whitelisted(5304501737, (5304501737,)) is True

        # unwhitelist on a missing row is a no-op, returns False.
        assert db.unwhitelist_user(999999) is False
    finally:
        db.close()
