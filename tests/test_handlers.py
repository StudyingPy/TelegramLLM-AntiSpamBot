from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from telegram_llm_antispam.db import Database
from telegram_llm_antispam.handlers import (
    _admin_verify_keyboard,
    _annotate_with_llm_outcome,
    _apply_verified_admin_action,
    _new_chat_members,
    _same_user_open_vote_repeat_decision,
    _is_anonymous_admin_message,
    create_router,
)
from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.models import (
    DecisionAction,
    LLMJudgement,
    LLMOutcome,
    LLMOutcomeStatus,
    LocalDecision,
    UserContext,
)
from test_llm import _settings


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "bot.db")
    db.connect()
    db.migrate()
    return db


def test_anonymous_admin_message_is_detected_by_sender_chat():
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100123, type="supergroup"),
        sender_chat=SimpleNamespace(id=-100123),
    )

    assert _is_anonymous_admin_message(message) is True


def test_channel_sender_chat_is_not_treated_as_anonymous_admin():
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100123, type="supergroup"),
        sender_chat=SimpleNamespace(id=-100999),
    )

    assert _is_anonymous_admin_message(message) is False


def test_admin_verify_keyboard_encodes_action_and_chat_id():
    keyboard = _admin_verify_keyboard("allow_chat", -100123)

    assert keyboard.inline_keyboard[0][0].text == "确认管理员身份"
    assert keyboard.inline_keyboard[0][0].callback_data == "admin_verify:allow_chat:-100123"


def test_verified_admin_allow_and_deny_actions_update_allowlist(tmp_path):
    db = _db(tmp_path)
    settings = _settings()
    try:
        allowed_text = _apply_verified_admin_action(
            "allow_chat",
            settings=settings,
            db=db,
            chat_id=-100123,
            title="Test Group",
            user_id=42,
        )

        assert "已允许当前群组" in allowed_text
        assert db.is_chat_allowed(-100123, ()) is True

        denied_text = _apply_verified_admin_action(
            "deny_chat",
            settings=settings,
            db=db,
            chat_id=-100123,
            title="Test Group",
            user_id=42,
        )

        assert "已禁用当前群组" in denied_text
        assert db.is_chat_allowed(-100123, ()) is False
    finally:
        db.close()


def test_router_registers_edited_message_moderation_handler(tmp_path):
    db = _db(tmp_path)
    try:
        router = create_router(_settings(), db)

        assert len(router.edited_message.handlers) == 1
    finally:
        db.close()


def test_same_user_open_vote_repeat_bans_without_new_vote(tmp_path):
    db = _db(tmp_path)
    settings = _settings()
    try:
        message = SimpleNamespace(
            message_id=7,
            chat=SimpleNamespace(id=-100123),
            from_user=SimpleNamespace(id=42),
            text="不稳不推 来这里几分钟赚几百 @baurpc",
        )
        context = UserContext(chat_id=-100123, user_id=42, reputation_score=50, messages_seen=1)
        features = build_message_features(message, context)
        session_id = db.create_vote_session(
            features,
            LocalDecision(DecisionAction.WITHDRAW_VOTE, "known_fingerprint", 0.85),
            timeout_seconds=60,
        )

        repeat_message = SimpleNamespace(
            message_id=8,
            chat=SimpleNamespace(id=-100123),
            from_user=SimpleNamespace(id=42),
            text="不稳不推 来这里几分钟赚几百 @baurpc",
        )
        repeat_features = build_message_features(repeat_message, context)
        decision = _same_user_open_vote_repeat_decision(settings, db, repeat_features)

        assert decision is not None
        assert decision.action == DecisionAction.BAN
        assert decision.metadata["open_vote_session_ids"] == [session_id]
    finally:
        db.close()


def test_new_chat_members_extracts_action_users_from_client_payload():
    message = SimpleNamespace(action=SimpleNamespace(users=[7775538527]))

    members = _new_chat_members(message)

    assert len(members) == 1
    assert members[0].id == 7775538527


def _stub_decision(reason: str = "unmatched_message_needs_llm") -> LocalDecision:
    return LocalDecision(
        action=DecisionAction.REVIEW,
        reason=reason,
        confidence=0.0,
        should_call_llm=True,
    )


def test_annotate_with_llm_outcome_records_disabled_state():
    """Regression: notifications previously could not distinguish 'LLM not configured'
    from 'LLM ran and judged not-spam' — both showed 'review / 0%' with no LLM line.
    Now disabled state is explicit in decision.metadata."""

    outcome = LLMOutcome(status=LLMOutcomeStatus.DISABLED, provider_count=0)
    annotated = _annotate_with_llm_outcome(_stub_decision(), outcome)

    payload = annotated.metadata["llm_outcome"]
    assert payload["status"] == "disabled"
    assert payload["provider_count"] == 0
    # action and reason are preserved — annotation is observability-only.
    assert annotated.action == DecisionAction.REVIEW
    assert annotated.reason == "unmatched_message_needs_llm"


def test_annotate_with_llm_outcome_records_failure_with_error():
    """Regression: when all providers fail (timeout / transport / parse error), the
    incident is now visible. Before, judge() swallowed errors and returned None,
    indistinguishable from 'LLM disabled' or 'LLM said not-spam'."""

    outcome = LLMOutcome(
        status=LLMOutcomeStatus.FAILED,
        provider_count=2,
        error="TimeoutError: timeout after 8.0s",
    )
    annotated = _annotate_with_llm_outcome(_stub_decision(), outcome)

    payload = annotated.metadata["llm_outcome"]
    assert payload["status"] == "failed"
    assert payload["provider_count"] == 2
    assert "Timeout" in payload["error"]


def test_annotate_with_llm_outcome_records_ok_judgement_payload():
    outcome = LLMOutcome(
        status=LLMOutcomeStatus.OK,
        provider_count=1,
        judgement=LLMJudgement(
            is_spam=True,
            confidence=0.92,
            category="ads",
            signal_phrases=("加群", "拿码"),
        ),
    )
    annotated = _annotate_with_llm_outcome(_stub_decision(), outcome)

    payload = annotated.metadata["llm_outcome"]
    assert payload["status"] == "ok"
    assert payload["is_spam"] is True
    assert payload["confidence"] == 0.92
    assert payload["category"] == "ads"
    assert payload["signal_phrases"] == ["加群", "拿码"]
