from __future__ import annotations

import logging
import time
from typing import Any

from .config import Settings
from .db import Database
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
