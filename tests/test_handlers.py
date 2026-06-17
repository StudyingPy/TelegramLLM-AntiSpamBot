from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from telegram_llm_antispam.db import Database
from telegram_llm_antispam.handlers import (
    _admin_verify_keyboard,
    _annotate_with_llm_outcome,
    _apply_verified_admin_action,
    _is_automatic_channel_forward,
    _is_whitelisted_sender,
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


def test_messages_from_other_bots_are_moderated_not_silently_skipped(tmp_path):
    """Regression: handlers used to drop every message with from_user.is_bot=True,
    which meant spammers registering a bot account (e.g. an 'AI strip / porn' promo
    bot replying to @-mentions) bypassed every rule. Production sample (2026-06-08):

      Al脱衣免费看片😍 (is_bot=True): [图片] ... #萝莉 #后入 #爆操 ... @gouj61 x9

    The bot's own messages must still be skipped (no self-moderation loops), but
    every other bot is fair game.

    Verified end-to-end: feed two messages to the router, one from our own bot id
    and one from another bot id. The first must be silently ignored; the second
    must be moderated like any user message — action_log gets a row, observation
    gets recorded.
    """
    import asyncio

    from aiogram import Bot
    from aiogram.dispatcher.event.bases import SkipHandler  # noqa: F401

    db = _db(tmp_path)
    settings = _settings()
    try:
        router = create_router(settings, db)

        # Find the catch-all message handler that _process_group_message wraps.
        handlers = router.message.handlers
        assert handlers, "router has no message handlers registered"

        # Build a fake Bot stub that pretends to be id=7777 and accepts the same
        # method calls handlers exercise without real network I/O.
        async def fake_get_me():
            return SimpleNamespace(id=7777)

        sent: list[tuple] = []

        async def fake_send_message(*args, **kwargs):
            sent.append(("send", args, kwargs))
            return SimpleNamespace(message_id=999)

        async def fake_get_chat(_user_id):
            return SimpleNamespace(bio=None)

        async def fake_get_chat_member(*args, **kwargs):
            # Restrict check: not-admin → restrictable.
            return SimpleNamespace(status=SimpleNamespace(value="member"))

        bot = SimpleNamespace(
            get_me=fake_get_me,
            send_message=fake_send_message,
            get_chat=fake_get_chat,
            get_chat_member=fake_get_chat_member,
            ban_chat_member=lambda *a, **kw: asyncio.sleep(0),
            delete_message=lambda *a, **kw: asyncio.sleep(0),
        )

        async def run_message(user_id: int, text: str, message_id: int):
            msg = SimpleNamespace(
                message_id=message_id,
                chat=SimpleNamespace(id=-1001, type="supergroup", title="t"),
                from_user=SimpleNamespace(
                    id=user_id, is_bot=True, username=None,
                    first_name="x", last_name=None,
                ),
                text=text,
                caption=None,
                entities=None,
                caption_entities=None,
                link_preview_options=None,
                bot=bot,
                sender_chat=None,
                new_chat_members=None,
            )
            # Find the @router.message() catch-all (last registered, no filters).
            for handler in handlers:
                # The catch-all handler we want has an empty filter set in aiogram.
                if not handler.filters:
                    await handler.callback(msg)
                    return
            raise AssertionError("no catch-all message handler found")

        # Allow the chat for moderation.
        db.allow_chat(-1001, "t", added_by_user_id=None)

        # Run BOTH messages within one event loop so the router's self_bot_id cache
        # is shared across calls.
        async def _both():
            await run_message(user_id=7777, text="hello from myself", message_id=1)
            await run_message(
                user_id=9999, text="加群送码 拿钱 教程 https://t.me/sca", message_id=2,
            )

        asyncio.run(_both())

        # Self message (id=7777): must NOT have produced an action_log entry.
        with db._locked_conn() as conn:  # noqa: SLF001 - test-only inspection
            self_rows = conn.execute(
                "SELECT id FROM action_log WHERE message_id = 1"
            ).fetchall()
        assert not self_rows, "our own bot message must not be moderated"

        # Other bot message (id=9999): MUST have produced an action_log entry.
        with db._locked_conn() as conn:  # noqa: SLF001
            other_rows = conn.execute(
                "SELECT id, action FROM action_log WHERE message_id = 2"
            ).fetchall()
        assert other_rows, "other bots' messages must be moderated like users'"
    finally:
        db.close()


def test_whitelisted_user_messages_skip_moderation_entirely(tmp_path):
    """Whitelisted user_ids bypass moderation entirely — no action_log, no
    fingerprint write, no LLM call. Mirrors the nmBot/客服酱 scenario where a
    friendly bot's text contains tokens or carriers our local rules would
    otherwise act on, and we want it absolutely silent."""
    import asyncio

    db = _db(tmp_path)
    settings = _settings()
    try:
        router = create_router(settings, db)
        handlers = router.message.handlers
        assert handlers

        async def fake_get_me():
            return SimpleNamespace(id=7777)

        async def fake_send_message(*args, **kwargs):
            return SimpleNamespace(message_id=999)

        async def fake_get_chat(_user_id):
            return SimpleNamespace(bio=None)

        async def fake_get_chat_member(*args, **kwargs):
            return SimpleNamespace(status=SimpleNamespace(value="member"))

        bot = SimpleNamespace(
            get_me=fake_get_me,
            send_message=fake_send_message,
            get_chat=fake_get_chat,
            get_chat_member=fake_get_chat_member,
            ban_chat_member=lambda *a, **kw: asyncio.sleep(0),
            delete_message=lambda *a, **kw: asyncio.sleep(0),
        )

        db.allow_chat(-1001, "t", added_by_user_id=None)
        # Whitelist user_id=5304501737 (nmBot in production).
        db.whitelist_user(5304501737, note="nmBot 客服酱", added_by_user_id=None)

        async def run_message(user_id: int, text: str, message_id: int):
            msg = SimpleNamespace(
                message_id=message_id,
                chat=SimpleNamespace(id=-1001, type="supergroup", title="t"),
                from_user=SimpleNamespace(
                    id=user_id, is_bot=True, username="nmnmfunbot",
                    first_name="nmBot", last_name=None,
                ),
                text=text,
                caption=None,
                entities=None,
                caption_entities=None,
                link_preview_options=None,
                bot=bot,
                sender_chat=None,
                new_chat_members=None,
            )
            for handler in handlers:
                if not handler.filters:
                    await handler.callback(msg)
                    return
            raise AssertionError("no catch-all handler")

        # Even with a message that WOULD trigger hard_spam_message (strong tokens
        # + @-mention carrier), the whitelisted user must be completely untouched.
        async def _go():
            await run_message(
                user_id=5304501737,
                text="某用户 被匿名管理员 客服酱 永久封禁 https://t.me/...",
                message_id=42,
            )

        asyncio.run(_go())

        with db._locked_conn() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT id FROM action_log WHERE message_id = 42"
            ).fetchall()
        assert not rows, "whitelisted user's message must not appear in action_log"
    finally:
        db.close()


def test_automatic_channel_forward_is_detected():
    """A linked channel's post auto-forwarded into the discussion group carries
    is_automatic_forward=True and a sender_chat for the source channel."""
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-1001, type="supergroup"),
        is_automatic_forward=True,
        sender_chat=SimpleNamespace(id=-1009999),
    )

    assert _is_automatic_channel_forward(message) is True


def test_anonymous_admin_post_is_not_an_automatic_forward():
    """An anonymous admin posts with sender_chat == own chat id but without
    is_automatic_forward; it must not be mistaken for a linked-channel forward."""
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-1001, type="supergroup"),
        is_automatic_forward=False,
        sender_chat=SimpleNamespace(id=-1001),
    )

    assert _is_automatic_channel_forward(message) is False


def test_telegram_service_account_is_whitelisted_by_default(tmp_path):
    """777000 ("Telegram") bypasses moderation with no operator configuration."""
    db = _db(tmp_path)
    settings = _settings()
    try:
        assert _is_whitelisted_sender(db, settings, 777000) is True
        assert _is_whitelisted_sender(db, settings, 12345) is False
    finally:
        db.close()


def test_automatic_channel_forward_skips_moderation_entirely(tmp_path):
    """A linked-channel promo post (the 端午 failure) must never be deleted/banned:
    no action_log row, even though its text trips spam rules."""
    import asyncio

    db = _db(tmp_path)
    settings = _settings()
    try:
        router = create_router(settings, db)
        handlers = router.message.handlers
        assert handlers

        async def fake_get_me():
            return SimpleNamespace(id=7777)

        async def fake_send_message(*args, **kwargs):
            return SimpleNamespace(message_id=999)

        bot = SimpleNamespace(
            get_me=fake_get_me,
            send_message=fake_send_message,
            ban_chat_member=lambda *a, **kw: asyncio.sleep(0),
            delete_message=lambda *a, **kw: asyncio.sleep(0),
        )

        db.allow_chat(-1001, "t", added_by_user_id=None)

        msg = SimpleNamespace(
            message_id=80241,
            chat=SimpleNamespace(id=-1001, type="supergroup", title="t"),
            from_user=SimpleNamespace(
                id=777000, is_bot=False, username=None,
                first_name="Telegram", last_name=None,
            ),
            text="端午八折优惠码 Dragon Boat Festival 可用时间 2026.06.17",
            caption=None,
            entities=None,
            caption_entities=None,
            link_preview_options=None,
            bot=bot,
            sender_chat=SimpleNamespace(id=-1009999),
            is_automatic_forward=True,
            new_chat_members=None,
        )

        async def _go():
            for handler in handlers:
                if not handler.filters:
                    await handler.callback(msg)
                    return
            raise AssertionError("no catch-all handler")

        asyncio.run(_go())

        with db._locked_conn() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT id FROM action_log WHERE message_id = 80241"
            ).fetchall()
        assert not rows, "auto-forwarded channel post must not appear in action_log"
    finally:
        db.close()
