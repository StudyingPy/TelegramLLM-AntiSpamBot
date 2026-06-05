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
        self.next_message_id = 900

    async def get_me(self):
        return SimpleNamespace(id=999)

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
