from __future__ import annotations

import logging
import time
from typing import Any

from .config import Settings
from .db import Database
from .links import extract_message_text
from .models import SenderProfile


logger = logging.getLogger(__name__)


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def sender_profile_from_user(user: Any) -> SenderProfile:
    return SenderProfile(
        user_id=int(_field(user, "id")),
        username=_field(user, "username"),
        first_name=_field(user, "first_name"),
        last_name=_field(user, "last_name"),
        language_code=_field(user, "language_code"),
        is_bot=bool(_field(user, "is_bot", False)),
        is_premium=_field(user, "is_premium"),
    )


async def get_sender_profile(
    bot: Any,
    db: Database,
    user: Any,
    settings: Settings,
) -> SenderProfile:
    profile = db.upsert_user_profile(sender_profile_from_user(user))
    if not settings.profile_bio_fetch_enabled:
        return profile
    if not _should_fetch_bio(profile, settings):
        return profile

    try:
        chat = await bot.get_chat(profile.user_id)
    except Exception as exc:  # pragma: no cover - depends on Telegram API permissions/state.
        logger.info("Could not fetch user bio for %s: %s", profile.user_id, exc)
        return db.update_user_profile_bio(profile.user_id, profile.bio) or profile

    bio = _field(chat, "bio")
    return db.update_user_profile_bio(profile.user_id, bio) or profile


def _should_fetch_bio(profile: SenderProfile, settings: Settings) -> bool:
    if profile.bio_fetched_at is None:
        return True
    return int(time.time()) - profile.bio_fetched_at >= settings.profile_bio_cache_ttl_seconds


async def fetch_personal_chat_for_crosscheck(
    bot: Any,
    user_id: int,
    *,
    limit: int = 3,
) -> dict[str, object] | None:
    """Fetch the personal channel currently attached to a user's profile.

    This deliberately has no database cache: callers invoke it only after the group
    message itself contains a traffic-diversion signal. A spammer may join with a clean
    profile and attach/change the channel later, so reusing the week-long bio cache here
    would preserve exactly the bypass this check is meant to close.

    The returned structure is transient feature input, not a channel-history snapshot.
    """

    try:
        messages = await bot.get_user_personal_chat_messages(user_id=user_id, limit=limit)
    except Exception as exc:  # pragma: no cover - depends on Telegram API version/state.
        logger.info("Could not fetch personal channel for %s: %s", user_id, exc)
        return None

    if not messages:
        return None

    chat = _field(messages[0], "chat")
    texts = tuple(
        text
        for message in messages
        if (text := extract_message_text(message).strip())
    )
    title = _field(chat, "title")
    username = _field(chat, "username")
    if not title and not username and not texts:
        return None

    return {
        "title": title,
        "username": username,
        "messages": texts,
    }
