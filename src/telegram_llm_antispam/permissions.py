from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModerationPermissions:
    can_delete: bool
    can_restrict: bool
    target_is_restrictable: bool
    reason: str = ""


def _status(member: Any) -> str:
    status = getattr(member, "status", "")
    return getattr(status, "value", str(status))


def _bool_attr(obj: Any, name: str) -> bool:
    return bool(getattr(obj, name, False))


async def check_permissions(bot: Any, chat_id: int, target_user_id: int | None) -> ModerationPermissions:
    try:
        me = await bot.get_me()
        bot_member = await bot.get_chat_member(chat_id, me.id)
        bot_status = _status(bot_member)
        is_owner = bot_status == "creator"
        can_delete = is_owner or _bool_attr(bot_member, "can_delete_messages")
        can_restrict = is_owner or _bool_attr(bot_member, "can_restrict_members")

        target_restrictable = True
        if target_user_id is not None:
            target_member = await bot.get_chat_member(chat_id, target_user_id)
            target_status = _status(target_member)
            target_restrictable = target_status not in {"creator", "administrator"}

        return ModerationPermissions(
            can_delete=can_delete,
            can_restrict=can_restrict,
            target_is_restrictable=target_restrictable,
        )
    except Exception as exc:  # pragma: no cover - depends on Telegram API behavior.
        logger.warning("Failed to check bot permissions: %s", exc)
        return ModerationPermissions(
            can_delete=False,
            can_restrict=False,
            target_is_restrictable=False,
            reason=str(exc),
        )

