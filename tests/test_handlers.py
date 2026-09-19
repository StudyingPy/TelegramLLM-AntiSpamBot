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
    _merge_llm_decision,
    _new_chat_members,
    _normal_message_reputation_reward,
    _only_bot_mentions,
    _parse_whitelist_target,
    _same_user_open_vote_repeat_decision,
    _is_anonymous_admin_message,
    _feature_message_for_user,
    _whitelist_add,
    _whitelist_list,
    _whitelist_remove,
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


def test_feature_message_for_user_preserves_rich_message():
    rich_message = {"blocks": [{"type": "heading", "text": {"type": "plain_text", "text": "广告"}}]}
    message = SimpleNamespace(
        message_id=7,
        chat=SimpleNamespace(id=-100123),
        from_user=SimpleNamespace(id=42),
        text=None,
        caption=None,
        entities=None,
        caption_entities=None,
        link_preview_options=None,
        rich_message=rich_message,
        reply_markup=None,
    )

    feature_message = _feature_message_for_user(message, message.from_user)

    assert feature_message.rich_message == rich_message
    assert feature_message.reply_markup is None


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


def test_router_registers_catchup_review_callbacks(tmp_path):
    """The catch-up review deep link needs review_ban / review_keep callbacks wired."""
    db = _db(tmp_path)
    try:
        router = create_router(_settings(), db)

        # vote, admin_verify, admin_ban, admin_release, review_ban, review_keep.
        assert len(router.callback_query.handlers) == 6
    finally:
        db.close()


def test_review_deeplink_shows_card_to_group_admin(tmp_path):
    """A `/start review_<id>` DM from a verified group admin returns the review card
    with ban/keep buttons for a timed-out session."""
    import asyncio

    from aiogram.filters import CommandObject

    from telegram_llm_antispam.models import DecisionAction, LocalDecision

    db = _db(tmp_path)
    settings = _settings()
    try:
        router = create_router(settings, db)

        message_features = build_message_features(
            SimpleNamespace(
                message_id=10,
                chat=SimpleNamespace(id=-100123),
                from_user=SimpleNamespace(id=42),
                text="加群送码拿钱 详细教程 https://spam.example",
            ),
            UserContext(chat_id=-100123, user_id=42, reputation_score=20, messages_seen=0),
        )
        session_id = db.create_vote_session(
            message_features,
            LocalDecision(DecisionAction.WITHDRAW_VOTE, "known_fingerprint", 0.85),
            timeout_seconds=-1,
        )
        db.expire_open_vote_sessions()

        answered: list[dict] = []

        async def fake_answer(text, reply_markup=None):
            answered.append({"text": text, "reply_markup": reply_markup})

        async def fake_get_chat_member(chat_id, user_id):
            # The requesting user is an administrator of the group.
            return SimpleNamespace(status=SimpleNamespace(value="administrator"))

        bot = SimpleNamespace(get_chat_member=fake_get_chat_member)
        message = SimpleNamespace(
            chat=SimpleNamespace(id=42, type="private"),
            from_user=SimpleNamespace(id=42),
            bot=bot,
            answer=fake_answer,
        )

        # Find the /start,/help handler and invoke it with the review deep link.
        start_handler = router.message.handlers[0]
        asyncio.run(
            start_handler.callback(
                message, command=CommandObject(command="start", args=f"review_{session_id}")
            )
        )

        assert len(answered) == 1
        assert "管理员私聊封禁" in answered[0]["text"]
        markup = answered[0]["reply_markup"]
        assert markup is not None
        callbacks = {b.callback_data for row in markup.inline_keyboard for b in row}
        assert callbacks == {f"review_ban:{session_id}", f"review_keep:{session_id}"}
    finally:
        db.close()


def test_open_vote_detail_deeplink_uses_cached_original_detail(tmp_path):
    """The open-vote detail button remains useful after another bot deletes the
    replied-to message because the private card renders vote_sessions.detail_text."""

    import asyncio

    from aiogram.filters import CommandObject

    db = _db(tmp_path)
    settings = _settings()
    try:
        router = create_router(settings, db)
        features = build_message_features(
            SimpleNamespace(
                message_id=11,
                chat=SimpleNamespace(id=-100123),
                from_user=SimpleNamespace(id=42),
                text="原消息已被其他 bot 删除",
            ),
            UserContext(
                chat_id=-100123,
                user_id=42,
                reputation_score=50,
                messages_seen=1,
            ),
        )
        session_id = db.create_vote_session(
            features,
            LocalDecision(DecisionAction.WITHDRAW_VOTE, "known_fingerprint", 0.8),
            timeout_seconds=60,
            detail_text="缓存详情：原文、资料、LLM",
        )

        answered: list[dict] = []

        async def answer(text, reply_markup=None):
            answered.append({"text": text, "reply_markup": reply_markup})

        async def get_chat_member(_chat_id, _user_id):
            return SimpleNamespace(status=SimpleNamespace(value="administrator"))

        message = SimpleNamespace(
            chat=SimpleNamespace(id=7, type="private"),
            from_user=SimpleNamespace(id=7),
            bot=SimpleNamespace(get_chat_member=get_chat_member),
            answer=answer,
        )
        asyncio.run(
            router.message.handlers[0].callback(
                message,
                command=CommandObject(command="start", args=f"review_{session_id}"),
            )
        )

        assert len(answered) == 1
        assert answered[0]["text"].startswith("管理员私聊封禁")
        assert "缓存详情：原文、资料、LLM" in answered[0]["text"]
        callbacks = {
            button.callback_data
            for row in answered[0]["reply_markup"].inline_keyboard
            for button in row
        }
        assert callbacks == {f"review_ban:{session_id}", f"review_keep:{session_id}"}
    finally:
        db.close()


def test_admin_release_callback_rejects_non_admin(tmp_path):
    import asyncio

    db = _db(tmp_path)
    settings = _settings()
    try:
        router = create_router(settings, db)
        features = build_message_features(
            SimpleNamespace(
                message_id=12,
                chat=SimpleNamespace(id=-100123),
                from_user=SimpleNamespace(id=42),
                text="测试",
            ),
            UserContext(
                chat_id=-100123,
                user_id=42,
                reputation_score=50,
                messages_seen=1,
            ),
        )
        session_id = db.create_vote_session(
            features,
            LocalDecision(DecisionAction.WITHDRAW_VOTE, "test", 0.8),
            timeout_seconds=60,
        )

        async def get_chat_member(_chat_id, _user_id):
            return SimpleNamespace(status=SimpleNamespace(value="member"))

        answers: list[tuple[str, bool]] = []

        async def answer(text, show_alert=False):
            answers.append((text, show_alert))

        callback = SimpleNamespace(
            data=f"admin_release:{session_id}",
            from_user=SimpleNamespace(id=99),
            bot=SimpleNamespace(get_chat_member=get_chat_member),
            message=None,
            answer=answer,
        )
        handler = next(
            item
            for item in router.callback_query.handlers
            if item.callback.__name__ == "on_admin_release"
        )
        asyncio.run(handler.callback(callback))

        assert answers == [("只有管理员可以跳过投票放行", True)]
        assert db.get_vote_session(session_id).status == "open"
    finally:
        db.close()


def test_parse_whitelist_target_explicit_id_and_note():
    message = SimpleNamespace(reply_to_message=None)
    assert _parse_whitelist_target(message, "12345 nmBot 客服酱") == (12345, "nmBot 客服酱")
    assert _parse_whitelist_target(message, "  678  ") == (678, None)


def test_parse_whitelist_target_from_reply():
    """Replying to a user's message resolves the target; args become the note."""
    message = SimpleNamespace(
        reply_to_message=SimpleNamespace(from_user=SimpleNamespace(id=999))
    )
    assert _parse_whitelist_target(message, "友好机器人") == (999, "友好机器人")
    assert _parse_whitelist_target(message, None) == (999, None)


def test_parse_whitelist_target_rejects_garbage():
    message = SimpleNamespace(reply_to_message=None)
    assert _parse_whitelist_target(message, "not_a_number") == (None, None)
    assert _parse_whitelist_target(message, "") == (None, None)
    assert _parse_whitelist_target(message, None) == (None, None)


def test_whitelist_add_remove_list_roundtrip(tmp_path):
    db = _db(tmp_path)
    settings = _settings()
    try:
        add_text = _whitelist_add(db, 12345, "nmBot", added_by=100)
        assert "已加入白名单" in add_text
        assert "12345" in add_text
        assert db.is_user_whitelisted(12345, ()) is True

        list_text = _whitelist_list(db, settings)
        assert "12345" in list_text
        assert "nmBot" in list_text

        remove_text = _whitelist_remove(db, 12345)
        assert "已移出白名单" in remove_text
        assert db.is_user_whitelisted(12345, ()) is False

        # Removing again reports it was absent.
        assert "不在白名单表中" in _whitelist_remove(db, 12345)
    finally:
        db.close()


def test_whitelist_note_is_html_escaped(tmp_path):
    """Notes are user-supplied and rendered under HTML parse mode — must be escaped."""
    db = _db(tmp_path)
    try:
        text = _whitelist_add(db, 1, "<b>x</b>", added_by=None)
        assert "<b>x</b>" not in text
        assert "&lt;b&gt;" in text
    finally:
        db.close()


def _run_command_handler(router, command_name: str, message, command):
    """Invoke the router message handler registered for a given /command."""
    import asyncio

    for handler in router.message.handlers:
        for f in handler.filters:
            cmds = getattr(getattr(f, "callback", None), "commands", None)
            if cmds and command_name in cmds:
                asyncio.run(handler.callback(message, command=command))
                return True
    raise AssertionError(f"no handler for /{command_name}")


def test_whitelist_command_requires_global_admin(tmp_path):
    """Only global admins (ADMIN_USER_IDS) may run /whitelist; others are refused and
    nothing is written."""
    from dataclasses import replace

    from aiogram.filters import CommandObject

    db = _db(tmp_path)
    settings = replace(_settings(), admin_user_ids=(100,))
    try:
        router = create_router(settings, db)
        answers: list[str] = []

        async def fake_answer(text, reply_markup=None):
            answers.append(text)

        def _msg(uid):
            return SimpleNamespace(
                chat=SimpleNamespace(id=uid, type="private"),
                from_user=SimpleNamespace(id=uid),
                reply_to_message=None,
                answer=fake_answer,
            )

        # Non-admin (id=42) refused.
        _run_command_handler(
            router, "whitelist", _msg(42), CommandObject(command="whitelist", args="777")
        )
        assert "只有全局管理员" in answers[-1]
        assert db.is_user_whitelisted(777, ()) is False

        # Global admin (id=100) succeeds.
        _run_command_handler(
            router, "whitelist", _msg(100), CommandObject(command="whitelist", args="777 friend")
        )
        assert "已加入白名单" in answers[-1]
        assert db.is_user_whitelisted(777, ()) is True
    finally:
        db.close()


def test_review_deeplink_rejects_non_admin(tmp_path):
    """A non-admin who opens the deep link gets refused, no review card."""
    import asyncio

    from aiogram.filters import CommandObject

    from telegram_llm_antispam.models import DecisionAction, LocalDecision

    db = _db(tmp_path)
    settings = _settings()
    try:
        router = create_router(settings, db)
        message_features = build_message_features(
            SimpleNamespace(
                message_id=10,
                chat=SimpleNamespace(id=-100123),
                from_user=SimpleNamespace(id=42),
                text="加群送码拿钱 详细教程 https://spam.example",
            ),
            UserContext(chat_id=-100123, user_id=42, reputation_score=20, messages_seen=0),
        )
        session_id = db.create_vote_session(
            message_features,
            LocalDecision(DecisionAction.WITHDRAW_VOTE, "known_fingerprint", 0.85),
            timeout_seconds=-1,
        )
        db.expire_open_vote_sessions()

        answered: list[dict] = []

        async def fake_answer(text, reply_markup=None):
            answered.append({"text": text, "reply_markup": reply_markup})

        async def fake_get_chat_member(chat_id, user_id):
            return SimpleNamespace(status=SimpleNamespace(value="member"))

        bot = SimpleNamespace(get_chat_member=fake_get_chat_member)
        message = SimpleNamespace(
            chat=SimpleNamespace(id=999, type="private"),
            from_user=SimpleNamespace(id=999),
            bot=bot,
            answer=fake_answer,
        )

        start_handler = router.message.handlers[0]
        asyncio.run(
            start_handler.callback(
                message, command=CommandObject(command="start", args=f"review_{session_id}")
            )
        )

        assert len(answered) == 1
        assert "只有该群组的管理员" in answered[0]["text"]
        assert answered[0]["reply_markup"] is None
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


def test_only_new_llm_confirmed_normal_messages_earn_reputation():
    settings = _settings()
    normal = _annotate_with_llm_outcome(
        LocalDecision(
            action=DecisionAction.ALLOW,
            reason="llm_not_spam",
            confidence=0.17,
        ),
        LLMOutcome(
            status=LLMOutcomeStatus.OK,
            provider_count=1,
            judgement=LLMJudgement(
                is_spam=False,
                confidence=0.17,
                category="unknown",
            ),
        ),
    )

    assert _normal_message_reputation_reward(
        settings,
        normal,
        update_type="message",
        is_edit=False,
    ) == settings.normal_message_reputation_reward

    # Edits cannot repeatedly farm trust from one message.
    assert _normal_message_reputation_reward(
        settings,
        normal,
        update_type="edited_message",
        is_edit=True,
    ) == 0

    # An LLM-normal result that still leaves a fingerprint vote open is not an
    # actual release and therefore earns no normal-message reward.
    fingerprint_vote = LocalDecision(
        action=DecisionAction.WITHDRAW_VOTE,
        reason="known_fingerprint",
        confidence=0.8,
        metadata=normal.metadata,
    )
    assert _normal_message_reputation_reward(
        settings,
        fingerprint_vote,
        update_type="message",
        is_edit=False,
    ) == 0


def test_router_persists_reputation_reward_for_llm_normal_messages(tmp_path):
    import asyncio
    import json

    class NormalJudge:
        async def judge(self, _features):
            return LLMOutcome(
                status=LLMOutcomeStatus.OK,
                provider_count=1,
                judgement=LLMJudgement(
                    is_spam=False,
                    confidence=0.12,
                    category="benign",
                ),
            )

    db = _db(tmp_path)
    settings = _settings()
    try:
        router = create_router(settings, db, llm=NormalJudge())
        db.allow_chat(-1001, "t", added_by_user_id=None)

        async def get_me():
            return SimpleNamespace(id=7777)

        async def get_chat(_user_id):
            return SimpleNamespace(bio=None)

        bot = SimpleNamespace(get_me=get_me, get_chat=get_chat)

        async def send(message_id: int):
            message = SimpleNamespace(
                message_id=message_id,
                chat=SimpleNamespace(id=-1001, type="supergroup", title="t"),
                from_user=SimpleNamespace(
                    id=42,
                    is_bot=False,
                    username="member",
                    first_name="Member",
                    last_name=None,
                    language_code="zh-hans",
                    is_premium=None,
                ),
                text=f"这是正常讨论消息 {message_id}",
                caption=None,
                entities=None,
                caption_entities=None,
                link_preview_options=None,
                bot=bot,
                sender_chat=None,
                is_automatic_forward=False,
                new_chat_members=None,
            )
            for handler in router.message.handlers:
                if not handler.filters:
                    await handler.callback(message)
                    return
            raise AssertionError("no catch-all message handler")

        asyncio.run(send(100))
        asyncio.run(send(101))

        context = db.get_user_context(-1001, 42)
        assert context.messages_seen == 2
        assert context.reputation_score == 50 + 2 * settings.normal_message_reputation_reward

        with db._locked_conn() as conn:  # noqa: SLF001 - test-only inspection
            rows = conn.execute(
                """
                SELECT metadata_json
                FROM action_log
                WHERE chat_id = -1001 AND user_id = 42
                ORDER BY id
                """
            ).fetchall()
        assert len(rows) == 2
        assert all(
            json.loads(row["metadata_json"])["reputation_reward"]
            == settings.normal_message_reputation_reward
            for row in rows
        )
    finally:
        db.close()


def test_router_live_personal_channel_crosscheck_auto_bans(tmp_path):
    import asyncio
    import json

    db = _db(tmp_path)
    settings = _settings()
    try:
        class CombinedJudge:
            def __init__(self) -> None:
                self.personal_chats: list[object] = []

            async def judge(self, features):
                self.personal_chats.append(features.metadata.get("personal_chat"))
                return LLMOutcome(
                    status=LLMOutcomeStatus.OK,
                    provider_count=1,
                    judgement=LLMJudgement(
                        is_spam=True,
                        confidence=0.98,
                        category="traffic_diversion",
                        signal_phrases=("2000+一天", "进群演员结算"),
                    ),
                )

        judge = CombinedJudge()
        router = create_router(settings, db, llm=judge)
        db.allow_chat(-1001, "t", added_by_user_id=None)

        personal_chat_calls: list[tuple[int, int]] = []
        deleted: list[tuple[int, int]] = []
        banned: list[tuple[int, int]] = []

        async def get_me():
            return SimpleNamespace(id=7777, username="moderatorbot")

        async def get_chat(_user_id):
            return SimpleNamespace(bio=None)

        async def get_user_personal_chat_messages(*, user_id, limit):
            personal_chat_calls.append((user_id, limit))
            channel = SimpleNamespace(
                id=-100999,
                title="财天下飞机进群演员结算",
                username=None,
            )
            return [
                SimpleNamespace(
                    chat=channel,
                    text="没及时回复的每天下午六点私聊我核对结算 @CaiG018",
                    caption=None,
                )
            ]

        async def get_chat_member(_chat_id, user_id):
            if user_id == 7777:
                return SimpleNamespace(
                    status=SimpleNamespace(value="administrator"),
                    can_delete_messages=True,
                    can_restrict_members=True,
                )
            return SimpleNamespace(status=SimpleNamespace(value="member"))

        async def delete_message(*, chat_id, message_id):
            deleted.append((chat_id, message_id))

        async def ban_chat_member(chat_id, user_id):
            banned.append((chat_id, user_id))

        async def send_message(_chat_id, _text):
            return SimpleNamespace(message_id=900)

        bot = SimpleNamespace(
            get_me=get_me,
            get_chat=get_chat,
            get_user_personal_chat_messages=get_user_personal_chat_messages,
            get_chat_member=get_chat_member,
            delete_message=delete_message,
            ban_chat_member=ban_chat_member,
            send_message=send_message,
        )
        message = SimpleNamespace(
            message_id=123,
            chat=SimpleNamespace(id=-1001, type="supergroup", title="t"),
            from_user=SimpleNamespace(
                id=42,
                is_bot=False,
                username=None,
                first_name="Achilles",
                last_name=None,
                language_code="zh-hans",
                is_premium=None,
            ),
            text="2000+一天",
            caption=None,
            entities=None,
            caption_entities=None,
            link_preview_options=None,
            bot=bot,
            sender_chat=None,
            is_automatic_forward=False,
            new_chat_members=None,
        )

        async def dispatch():
            for handler in router.message.handlers:
                if not handler.filters:
                    await handler.callback(message)
                    return
            raise AssertionError("no catch-all message handler")

        asyncio.run(dispatch())

        assert personal_chat_calls == [(42, 3)]
        assert judge.personal_chats == [
            {
                "title": "财天下飞机进群演员结算",
                "username": None,
                "messages": ("没及时回复的每天下午六点私聊我核对结算 @CaiG018",),
            }
        ]
        assert deleted == [(-1001, 123)]
        assert banned == [(-1001, 42)]
        with db._locked_conn() as conn:  # noqa: SLF001 - test-only inspection
            row = conn.execute(
                "SELECT reason, metadata_json FROM action_log WHERE message_id = 123"
            ).fetchone()
        assert row["reason"] == "llm_spam_high_confidence"
        metadata = json.loads(row["metadata_json"])
        assert metadata["deleted"] is True
        assert metadata["banned"] is True
        assert metadata["local_signal"] == "message_personal_channel_crosscheck"
        assert "personal_chat" not in metadata
    finally:
        db.close()


def test_only_bot_mentions_accepts_one_or_many_bots_without_other_content():
    assert _only_bot_mentions("@HelperBot") == ("helperbot",)
    assert _only_bot_mentions("  @FirstBot\n@second_bot @FIRSTBOT  ") == (
        "firstbot",
        "second_bot",
    )


def test_only_bot_mentions_rejects_non_bot_mentions_and_any_other_content():
    assert _only_bot_mentions("@ordinary_user") == ()
    assert _only_bot_mentions("@HelperBot 请帮忙") == ()
    assert _only_bot_mentions("@HelperBot, @SecondBot") == ()


def test_ad_bot_invoker_is_banned_on_next_bot_only_message_and_all_calls_are_deleted(
    tmp_path,
):
    import asyncio

    db = _db(tmp_path)
    settings = _settings()
    try:
        router = create_router(settings, db)
        handlers = router.message.handlers
        db.allow_chat(-1001, "t", added_by_user_id=None)

        banned: list[tuple[int, int]] = []
        deleted: list[tuple[int, int]] = []

        async def get_me():
            return SimpleNamespace(id=7777, username="moderatorbot")

        async def get_chat(_user_id):
            return SimpleNamespace(bio=None)

        async def get_chat_member(chat_id, user_id):
            if user_id == 7777:
                return SimpleNamespace(
                    status=SimpleNamespace(value="administrator"),
                    can_delete_messages=True,
                    can_restrict_members=True,
                )
            return SimpleNamespace(status=SimpleNamespace(value="member"))

        async def ban_chat_member(chat_id, user_id):
            banned.append((chat_id, user_id))

        async def delete_message(chat_id, message_id):
            deleted.append((chat_id, message_id))

        async def send_message(_chat_id, _text):
            return SimpleNamespace(message_id=900)

        bot = SimpleNamespace(
            get_me=get_me,
            get_chat=get_chat,
            get_chat_member=get_chat_member,
            ban_chat_member=ban_chat_member,
            delete_message=delete_message,
            send_message=send_message,
        )

        async def dispatch(message_id, user_id, *, username, is_bot, text):
            message = SimpleNamespace(
                message_id=message_id,
                chat=SimpleNamespace(id=-1001, type="supergroup", title="t"),
                from_user=SimpleNamespace(
                    id=user_id,
                    username=username,
                    is_bot=is_bot,
                    first_name="x",
                    last_name=None,
                ),
                text=text,
                caption=None,
                entities=None,
                caption_entities=None,
                link_preview_options=None,
                reply_to_message=None,
                sender_chat=None,
                is_automatic_forward=False,
                new_chat_members=None,
                bot=bot,
            )
            catch_all = next(handler for handler in handlers if not handler.filters)
            await catch_all.callback(message)

        async def scenario():
            # First invocation is merely remembered.
            await dispatch(10, 42, username="human", is_bot=False, text="@AdBot")
            # The called bot's hard-spam output confirms that invocation as abusive.
            await dispatch(
                11,
                99,
                username="AdBot",
                is_bot=True,
                text="\u62ff\u7801 @AdBot",
            )
            # Any later bot-only call is enough; it need not name the same bot.
            await dispatch(
                12,
                42,
                username="human",
                is_bot=False,
                text="@DifferentBot @ThirdBot",
            )

        asyncio.run(scenario())

        assert (-1001, 99) in banned
        assert (-1001, 42) in banned
        assert (-1001, 10) in deleted
        assert (-1001, 12) in deleted
    finally:
        db.close()


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


def _known_fingerprint_features(reputation: int = 10):
    """A benign-looking multi-token message that matched a known phrase/skeleton
    fingerprint: local decision is WITHDRAW_VOTE with should_call_llm=True. Low
    reputation so an LLM ad-confirmation is allowed to escalate to BAN."""
    message = SimpleNamespace(
        message_id=9,
        chat=SimpleNamespace(id=-100123),
        from_user=SimpleNamespace(id=42),
        text="看我主页 有你想要的",
    )
    context = UserContext(
        chat_id=-100123, user_id=42, reputation_score=reputation, messages_seen=1
    )
    return build_message_features(message, context)


def test_known_fingerprint_vote_upgrades_to_ban_when_llm_confirms_ad():
    """A known phrase/skeleton fingerprint hit now goes through the LLM. When the LLM
    confirms spam at high confidence, the WITHDRAW_VOTE must upgrade to BAN — so a
    known-spam shape is no longer treated more leniently than an unmatched message."""
    settings = _settings()
    features = _known_fingerprint_features(reputation=10)
    local = LocalDecision(
        action=DecisionAction.WITHDRAW_VOTE,
        reason="known_fingerprint",
        confidence=0.50,
        should_call_llm=True,
    )
    judgement = LLMJudgement(
        is_spam=True,
        confidence=0.96,
        category="traffic_diversion",
        signal_phrases=("看我主页", "有你想要的"),
    )

    merged = _merge_llm_decision(local, judgement, features, settings)

    assert merged.action == DecisionAction.BAN
    assert merged.confidence == 0.96


def test_known_fingerprint_vote_survives_llm_not_spam():
    """When the LLM says not-spam, the known-fingerprint WITHDRAW_VOTE must remain a
    vote (not flip to ALLOW) — a single mislabeled fingerprint can never connect-ban,
    but a genuine shape match still earns a chat dispute rather than a silent pass."""
    settings = _settings()
    features = _known_fingerprint_features(reputation=50)
    local = LocalDecision(
        action=DecisionAction.WITHDRAW_VOTE,
        reason="known_fingerprint",
        confidence=0.50,
        should_call_llm=True,
    )
    judgement = LLMJudgement(
        is_spam=False,
        confidence=0.90,
        category="benign",
        signal_phrases=(),
    )

    merged = _merge_llm_decision(local, judgement, features, settings)

    assert merged.action == DecisionAction.WITHDRAW_VOTE
    assert merged.reason == "known_fingerprint"
