from __future__ import annotations

from typing import Any

from .config import Settings
from .db import Database


ADMIN_STATUSES = {"creator", "administrator"}


def is_global_admin(settings: Settings, user_id: int | None) -> bool:
    return user_id is not None and user_id in settings.admin_user_ids


async def is_chat_admin(bot: Any, chat_id: int, user_id: int | None) -> bool:
    if user_id is None:
        return False
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    status = getattr(getattr(member, "status", ""), "value", getattr(member, "status", ""))
    return str(status) in ADMIN_STATUSES


async def can_manage_chat(bot: Any, settings: Settings, chat_id: int, user_id: int | None) -> bool:
    return is_global_admin(settings, user_id) or await is_chat_admin(bot, chat_id, user_id)


def is_chat_allowed(settings: Settings, db: Database, chat_id: int) -> bool:
    if not settings.require_allowed_chat:
        return True
    return db.is_chat_allowed(chat_id, settings.allowed_chat_ids)
