"""Select visual evidence before any Telegram media/profile lookup."""

from __future__ import annotations

import re

from .models import MessageFeatures
from .repeats import compact_repeat_text, recruitment_repeat_key
from .reputation import is_trusted_sender


_PROFILE_POINTER = re.compile(
    r"(?:看|点|点击|打开|戳|查看)(?:下|一下)?(?:我|本人)(?:的)?(?:头像|简介|主页|资料)"
    r"|(?:点|点击|看|戳)(?:我的)?头像(?:进群|加群|了解|联系)"
)
_PROFILE_AD = re.compile(
    r"看我?简介|看我?主页|点我进群|头像进群|主页赚钱|群发|代发|刷单|日赚|稳赚|博彩|招嫖"
)


def profile_inspection_reason(features: MessageFeatures) -> str | None:
    """Profile images supplement a specific signal, never mere newness/premium."""
    if features.metadata.get("giveaway_reply") or features.metadata.get("quoted_or_code_message"):
        return None
    text = compact_repeat_text(features.text)
    if len(text) > 120:
        return None
    if _PROFILE_POINTER.search(text):
        return "message_points_to_profile"
    if is_trusted_sender(features):
        return None
    if recruitment_repeat_key(features.text):
        return "short_recruitment_needs_profile"
    # A short/ambiguous message plus the current sender's own advertising name
    # can warrant avatar/status inspection. A generic bio link alone cannot.
    if len(text) <= 8:
        profile = features.metadata.get("sender_profile") or {}
        if isinstance(profile, dict):
            name = " ".join(
                str(profile.get(key) or "")
                for key in (
                    "display_name",
                    "first_name",
                    "last_name",
                    "bio",
                )
            )
            visible = "".join(char for char in compact_repeat_text(name) if char.isalnum())
            if _PROFILE_AD.search(visible):
                return "short_message_with_profile_ad_signal"
    return None
