from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from telegram_llm_antispam.db import Database
from telegram_llm_antispam.handlers import (
    _admin_verify_keyboard,
    _apply_verified_admin_action,
    _is_anonymous_admin_message,
    create_router,
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
