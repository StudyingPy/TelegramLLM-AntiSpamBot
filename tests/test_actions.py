from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from telegram_llm_antispam.actions import ModerationActions
from telegram_llm_antispam.db import Database
from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.models import DecisionAction, LocalDecision, UserContext
from test_llm import _settings


class FakeBot:
    def __init__(self) -> None:
        self.deleted_messages: list[tuple[int, int]] = []
        self.banned_users: list[tuple[int, int]] = []
        self.sent_messages: list[tuple[int, str]] = []
        self.edited_messages: list[dict[str, object]] = []
        self.next_message_id = 900

    async def get_me(self):
        return SimpleNamespace(id=999, username="antispam_test_bot")

    async def get_chat_member(self, chat_id: int, user_id: int):
        if user_id == 999:
            return SimpleNamespace(
                status="administrator",
                can_delete_messages=True,
                can_restrict_members=True,
            )
        return SimpleNamespace(status="member")

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted_messages.append((chat_id, message_id))

    async def ban_chat_member(self, chat_id: int, user_id: int) -> None:
        self.banned_users.append((chat_id, user_id))

    async def send_message(self, chat_id: int, text: str):
        self.sent_messages.append((chat_id, text))
        self.next_message_id += 1
        return SimpleNamespace(message_id=self.next_message_id)

    async def edit_message_text(self, text, chat_id=None, message_id=None, reply_markup=None):
        self.edited_messages.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "bot.db")
    db.connect()
    db.migrate()
    return db


def _features(message_id: int):
    message = SimpleNamespace(
        message_id=message_id,
        chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(id=42),
        text="不稳不推 来这里几分钟赚几百 @baurpc",
    )
    context = UserContext(chat_id=-1001, user_id=42, reputation_score=50, messages_seen=1)
    return build_message_features(message, context)


def test_confirmed_spam_vote_cleans_related_messages_and_bans(tmp_path):
    db = _db(tmp_path)
    bot = FakeBot()
    try:
        settings = _settings()
        actions = ModerationActions(settings, db)
        actions.SUMMARY_DELETE_DELAY_SECONDS = 0

        first_session_id = db.create_vote_session(
            _features(10),
            LocalDecision(DecisionAction.WITHDRAW_VOTE, "known_fingerprint", 0.85),
            timeout_seconds=60,
        )
        db.set_vote_message_id(first_session_id, 110)
        second_session_id = db.create_vote_session(
            _features(11),
            LocalDecision(DecisionAction.WITHDRAW_VOTE, "known_fingerprint", 0.85),
            timeout_seconds=60,
        )
        db.set_vote_message_id(second_session_id, 111)

        db.add_vote(first_session_id, 1001, "spam")
        db.add_vote(first_session_id, 1002, "spam")
        tally = db.add_vote(first_session_id, 1003, "spam")
        assert tally is not None

        callback_message = SimpleNamespace(bot=bot)
        closed = asyncio.run(actions.close_vote_if_threshold_reached(callback_message, tally))

        assert closed is True
        assert set(bot.deleted_messages) == {
            (-1001, 10),
            (-1001, 11),
            (-1001, 110),
            (-1001, 111),
        }
        assert bot.banned_users == [(-1001, 42)]
        assert len(bot.sent_messages) == 1
        assert "反广告处理完成" in bot.sent_messages[0][1]
        assert "已清理：广告消息 2 条，投票消息 2 条" in bot.sent_messages[0][1]
        assert db.get_vote_session(first_session_id).status == "confirmed_spam"
        assert db.get_vote_session(second_session_id).status == "confirmed_spam"
    finally:
        db.close()


def test_confirmed_ham_vote_rewards_user_reputation(tmp_path):
    db = _db(tmp_path)
    bot = FakeBot()
    try:
        settings = _settings()
        actions = ModerationActions(settings, db)
        features = _features(12)
        db.record_message_seen(features)
        session_id = db.create_vote_session(
            features,
            LocalDecision(DecisionAction.WITHDRAW_VOTE, "known_fingerprint", 0.8),
            timeout_seconds=60,
        )
        db.add_vote(session_id, 1001, "ham")
        db.add_vote(session_id, 1002, "ham")
        tally = db.add_vote(session_id, 1003, "ham")
        assert tally is not None

        edits: list[str] = []

        async def edit_text(text, **_kwargs):
            edits.append(text)

        callback_message = SimpleNamespace(bot=bot, edit_text=edit_text)
        closed = asyncio.run(actions.close_vote_if_threshold_reached(callback_message, tally))

        assert closed is True
        context = db.get_user_context(features.chat_id, features.user_id or 0)
        assert context.reputation_score == 50 + settings.ham_reputation_reward
        assert edits and "投票结束：放行" in edits[0]
    finally:
        db.close()


def test_admin_release_closes_vote_rewards_once_and_updates_message(tmp_path):
    db = _db(tmp_path)
    bot = FakeBot()
    try:
        settings = _settings()
        actions = ModerationActions(settings, db)
        features = _features(13)
        db.record_message_seen(features)
        session_id = db.create_vote_session(
            features,
            LocalDecision(DecisionAction.WITHDRAW_VOTE, "known_fingerprint", 0.8),
            timeout_seconds=60,
        )
        db.set_vote_message_id(session_id, 113)

        ok, text = asyncio.run(
            actions.admin_release_vote_session(bot, session_id, moderator_user_id=9001)
        )

        assert ok is True
        assert text == "已放行"
        assert db.get_vote_session(session_id).status == "released"
        context = db.get_user_context(features.chat_id, features.user_id or 0)
        assert context.reputation_score == 50 + settings.ham_reputation_reward
        assert bot.edited_messages[-1]["message_id"] == 113
        assert bot.edited_messages[-1]["reply_markup"] is None
        assert "管理员已放行" in bot.edited_messages[-1]["text"]

        with db._locked_conn() as conn:  # noqa: SLF001 - test-only inspection
            log = conn.execute(
                """
                SELECT action, reason, metadata_json
                FROM action_log
                WHERE action = 'admin_released_user'
                """
            ).fetchone()
        assert log is not None
        assert log["reason"] == "admin_skip_vote_release"
        assert '"moderator_user_id": 9001' in log["metadata_json"]

        # Repeated/delayed callbacks are idempotent and cannot grant +8 twice.
        second_ok, second_text = asyncio.run(
            actions.admin_release_vote_session(bot, session_id, moderator_user_id=9001)
        )
        assert second_ok is False
        assert "已处理" in second_text
        assert db.get_user_context(features.chat_id, features.user_id or 0).reputation_score == (
            50 + settings.ham_reputation_reward
        )
    finally:
        db.close()


def test_expired_vote_message_gets_catchup_review_button(tmp_path):
    """When a vote times out, the group message is edited to carry a deep-link
    button into the private-chat catch-up review."""
    db = _db(tmp_path)
    bot = FakeBot()
    try:
        actions = ModerationActions(_settings(), db)
        # Capture scheduled deletions instead of spawning real long-lived tasks.
        scheduled: list[tuple] = []
        actions._schedule_message_deletion = (  # type: ignore[method-assign]
            lambda bot, chat_id, message_id, delay, *, label="message": scheduled.append(
                (chat_id, message_id, delay, label)
            )
        )
        session_id = db.create_vote_session(
            _features(10),
            LocalDecision(DecisionAction.WITHDRAW_VOTE, "known_fingerprint", 0.85),
            timeout_seconds=-1,
        )
        db.set_vote_message_id(session_id, 110)

        expired = asyncio.run(actions.expire_due_vote_sessions(bot))

        assert expired == 1
        assert db.get_vote_session(session_id).status == "expired_released"
        assert len(bot.edited_messages) == 1
        edit = bot.edited_messages[0]
        assert edit["message_id"] == 110
        assert "前往私聊补审" in _keyboard_text(edit["reply_markup"])
        assert f"start=review_{session_id}" in _keyboard_url(edit["reply_markup"])
        # The timed-out message is scheduled for deletion at the configured TTL.
        assert scheduled == [(-1001, 110, _settings().vote_expired_message_ttl_seconds, "expired vote")]
    finally:
        db.close()


def test_catchup_ban_bans_expired_session_and_cleans_messages(tmp_path):
    """A timed-out (expired_released) session can still be banned via catch-up
    review; it deletes the original + vote messages and bans the suspect."""
    db = _db(tmp_path)
    bot = FakeBot()
    try:
        actions = ModerationActions(_settings(), db)
        actions.SUMMARY_DELETE_DELAY_SECONDS = 0
        session_id = db.create_vote_session(
            _features(10),
            LocalDecision(DecisionAction.WITHDRAW_VOTE, "known_fingerprint", 0.85),
            timeout_seconds=-1,
        )
        db.set_vote_message_id(session_id, 110)
        db.expire_open_vote_sessions()
        assert db.get_vote_session(session_id).status == "expired_released"

        ok, text = asyncio.run(actions.catchup_ban_vote_session(bot, session_id, moderator_user_id=7))

        assert ok is True
        assert text == "已封禁"
        assert bot.banned_users == [(-1001, 42)]
        assert (-1001, 10) in bot.deleted_messages
        assert (-1001, 110) in bot.deleted_messages
        assert db.get_vote_session(session_id).status == "admin_banned"
    finally:
        db.close()


def test_catchup_keep_records_release_and_drops_button(tmp_path):
    """Keeping a timed-out session after review records the decision and edits the
    group message to remove the catch-up button."""
    db = _db(tmp_path)
    bot = FakeBot()
    try:
        actions = ModerationActions(_settings(), db)
        session_id = db.create_vote_session(
            _features(10),
            LocalDecision(DecisionAction.WITHDRAW_VOTE, "known_fingerprint", 0.85),
            timeout_seconds=-1,
        )
        db.set_vote_message_id(session_id, 110)
        db.expire_open_vote_sessions()

        ok, text = asyncio.run(actions.catchup_keep_vote_session(bot, session_id, moderator_user_id=7))

        assert ok is True
        assert text == "已维持放行"
        assert bot.banned_users == []
        assert bot.edited_messages[-1]["reply_markup"] is None
        assert "维持放行" in bot.edited_messages[-1]["text"]
        assert db.get_vote_session(session_id).status == "expired_released"
    finally:
        db.close()


def test_catchup_ban_attributes_moderator_in_admin_notification(tmp_path):
    """The edited global-admin notification (same in-place edit as a live vote
    result) must attribute the catch-up decision to the moderator's ID."""
    db = _db(tmp_path)
    bot = FakeBot()
    try:
        actions = ModerationActions(_settings(), db)
        actions.SUMMARY_DELETE_DELAY_SECONDS = 0
        session_id = db.create_vote_session(
            _features(10),
            LocalDecision(DecisionAction.WITHDRAW_VOTE, "known_fingerprint", 0.85),
            timeout_seconds=-1,
        )
        db.set_vote_message_id(session_id, 110)
        db.expire_open_vote_sessions()
        # A global admin received the notification at vote-open time (user 555, msg 700).
        db.record_admin_notification(
            vote_session_id=session_id,
            action_log_id=None,
            notify_user_id=555,
            message_id=700,
            base_text="反广告处理记录",
        )

        ok, _ = asyncio.run(actions.catchup_ban_vote_session(bot, session_id, moderator_user_id=7))

        assert ok is True
        dm_edits = [e for e in bot.edited_messages if e["chat_id"] == 555 and e["message_id"] == 700]
        assert dm_edits, "global admin notification should have been edited"
        assert "补审操作者：7" in dm_edits[-1]["text"]
    finally:
        db.close()


def test_catchup_keep_attributes_moderator_in_admin_notification(tmp_path):
    db = _db(tmp_path)
    bot = FakeBot()
    try:
        actions = ModerationActions(_settings(), db)
        session_id = db.create_vote_session(
            _features(10),
            LocalDecision(DecisionAction.WITHDRAW_VOTE, "known_fingerprint", 0.85),
            timeout_seconds=-1,
        )
        db.set_vote_message_id(session_id, 110)
        db.expire_open_vote_sessions()
        db.record_admin_notification(
            vote_session_id=session_id,
            action_log_id=None,
            notify_user_id=555,
            message_id=700,
            base_text="反广告处理记录",
        )

        ok, _ = asyncio.run(actions.catchup_keep_vote_session(bot, session_id, moderator_user_id=9))

        assert ok is True
        assert db.get_vote_session(session_id).status == "expired_released"
        dm_edits = [e for e in bot.edited_messages if e["chat_id"] == 555 and e["message_id"] == 700]
        assert dm_edits
        assert "补审操作者：9" in dm_edits[-1]["text"]
        assert "维持放行" in dm_edits[-1]["text"]
    finally:
        db.close()


def test_catchup_ban_rejects_already_finalized_session(tmp_path):
    """A session that was already confirmed/banned cannot be re-banned via catch-up."""
    db = _db(tmp_path)
    bot = FakeBot()
    try:
        actions = ModerationActions(_settings(), db)
        session_id = db.create_vote_session(
            _features(10),
            LocalDecision(DecisionAction.WITHDRAW_VOTE, "known_fingerprint", 0.85),
            timeout_seconds=60,
        )
        db.close_vote_session(session_id, "confirmed_spam")

        ok, text = asyncio.run(actions.catchup_ban_vote_session(bot, session_id, moderator_user_id=7))

        assert ok is False
        assert "已处理" in text
        assert bot.banned_users == []
    finally:
        db.close()


def test_delete_message_later_deletes_after_delay(tmp_path):
    """The deletion coroutine issues a real delete_message once its delay elapses."""
    db = _db(tmp_path)
    bot = FakeBot()
    try:
        actions = ModerationActions(_settings(), db)
        asyncio.run(actions._delete_message_later(bot, -1001, 110, 0, label="expired vote"))
        assert (-1001, 110) in bot.deleted_messages
    finally:
        db.close()


def test_withdraw_and_vote_caches_detail_text_on_session(tmp_path):
    """_withdraw_and_vote stores the full moderation detail on the session so catch-up
    review can render it independent of admin-notification config."""
    db = _db(tmp_path)
    bot = FakeBot()
    try:
        actions = ModerationActions(_settings(), db)
        features = _features(10)
        decision = LocalDecision(DecisionAction.WITHDRAW_VOTE, "llm_spam", 0.91)

        answered: list[dict] = []

        async def fake_answer(text, reply_markup=None, **kwargs):
            answered.append({"text": text, "reply_markup": reply_markup})
            return SimpleNamespace(message_id=110)

        message = SimpleNamespace(
            message_id=10,
            bot=bot,
            answer=fake_answer,
        )

        result = asyncio.run(actions._withdraw_and_vote(message, features, decision))

        session = db.get_vote_session(result.vote_session_id)
        assert session.detail_text is not None
        assert "反广告处理记录" in session.detail_text
        assert f"投票会话：<code>{result.vote_session_id}</code>" in session.detail_text

        keyboard = answered[0]["reply_markup"]
        callbacks = {
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        }
        assert callbacks == {
            f"vote:{result.vote_session_id}:spam",
            f"vote:{result.vote_session_id}:ham",
            f"admin_ban:{result.vote_session_id}",
            f"admin_release:{result.vote_session_id}",
        }
        assert "原消息详情" in _keyboard_text(keyboard)
        assert (
            f"https://t.me/antispam_test_bot?start=review_{result.vote_session_id}"
            in _keyboard_url(keyboard)
        )
    finally:
        db.close()


def _keyboard_text(markup) -> str:
    return " ".join(
        button.text for row in markup.inline_keyboard for button in row
    )


def _keyboard_url(markup) -> str:
    return " ".join(
        (button.url or "") for row in markup.inline_keyboard for button in row
    )
