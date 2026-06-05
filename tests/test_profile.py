from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from telegram_llm_antispam.db import Database
from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.models import UserContext
from telegram_llm_antispam.profile import get_sender_profile, sender_profile_from_user
from test_llm import _settings


class FakeBot:
    def __init__(self) -> None:
        self.calls = 0

    async def get_chat(self, user_id: int):
        self.calls += 1
        return SimpleNamespace(id=user_id, bio="看片加群，联系客服")


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "bot.db")
    db.connect()
    db.migrate()
    return db


def test_sender_profile_from_user_extracts_basic_fields():
    user = SimpleNamespace(
        id=42,
        username="promo_agent",
        first_name="成人",
        last_name="客服",
        language_code="zh-hans",
        is_bot=False,
        is_premium=True,
    )

    profile = sender_profile_from_user(user)

    assert profile.username == "promo_agent"
    assert profile.display_name == "成人 客服"
    assert profile.to_payload()["is_premium"] is True


def test_get_sender_profile_fetches_and_caches_bio(tmp_path):
    db = _db(tmp_path)
    settings = _settings()
    bot = FakeBot()
    user = SimpleNamespace(id=42, username="promo_agent", first_name="成人", is_bot=False)
    try:
        first = asyncio.run(get_sender_profile(bot, db, user, settings))
        second = asyncio.run(get_sender_profile(bot, db, user, settings))

        assert first.bio == "看片加群，联系客服"
        assert second.bio == "看片加群，联系客服"
        assert bot.calls == 1
    finally:
        db.close()


def test_build_message_features_includes_sender_profile_payload():
    user = SimpleNamespace(id=42, username="promo_agent", first_name="成人", is_bot=False)
    profile = sender_profile_from_user(user)
    message = SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=-1001),
        from_user=user,
        text="hello",
    )
    context = UserContext(chat_id=-1001, user_id=42, reputation_score=50, messages_seen=1)

    features = build_message_features(message, context, sender_profile=profile)

    assert features.metadata["sender_profile"]["username"] == "promo_agent"
    assert features.metadata["sender_profile"]["display_name"] == "成人"
