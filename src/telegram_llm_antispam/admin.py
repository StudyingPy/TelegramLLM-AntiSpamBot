from __future__ import annotations

from typing import Any
import time

from .config import Settings
from .db import Database


ADMIN_STATUSES = {"creator", "administrator"}


class ChatAdminCache:
    def __init__(self, ttl: int = 300) -> None:
        self._ttl = ttl
        self._groups: dict[int, tuple[float, set[int]]] = {}
        self._members: dict[tuple[int, int], tuple[float, bool]] = {}

    def invalidate(self, chat_id: int) -> None:
        self._groups.pop(chat_id, None)
        self._members = {key: value for key, value in self._members.items() if key[0] != chat_id}

    async def is_admin(self, bot: Any, chat_id: int, user_id: int, *, is_bot=False) -> bool | None:
        now = time.monotonic()
        group = self._groups.get(chat_id)
        if group and now - group[0] < self._ttl and (not is_bot or user_id in group[1]):
            return user_id in group[1]
        member = self._members.get((chat_id, user_id))
        if member and now - member[0] < self._ttl:
            return member[1]
        try:
            if hasattr(bot, "get_chat_administrators") and not is_bot:
                members = await bot.get_chat_administrators(chat_id)
                ids = {item.user.id for item in members}
                self._groups[chat_id] = (now, ids)
                return user_id in ids
            if hasattr(bot, "get_chat_member"):
                item = await bot.get_chat_member(chat_id, user_id)
                status = getattr(item.status, "value", item.status)
                result = str(status) in ADMIN_STATUSES
                self._members[(chat_id, user_id)] = (now, result)
                return result
            # Lightweight test adapters have no Telegram membership surface.
            return False
        except Exception:
            return None


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
