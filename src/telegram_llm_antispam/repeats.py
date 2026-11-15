from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

from .fingerprints import stable_hash
from .models import DecisionAction, LocalDecision, MessageFeatures
from .text import strip_zero_width

if TYPE_CHECKING:
    from .config import Settings
    from .db import Database


# Full short recruitment utterances, not keyword mentions inside a discussion/quote.
_LURE_RE = re.compile(
    r"(?:有没有|有)?(?:会|能|想|愿意)?"
    r"(?:网购|网上购物|购物|赚钱|做单|刷单|兼职|赚点零花钱)"
    r"(?:的朋友们|的朋友|的人|的)?"
    r"(?:来|找我|私聊我|联系我)"
    r"(?:找我|私聊我|联系我|看我简介|看简介|看主页)?"
)
_INVITE_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)[\w-]+", re.I)
_PROFILE_CTA_RE = re.compile(r"(?:看我?简介|看我?主页|主页赚钱|私聊我|联系我|加我)")
_GIVEAWAY_KEYWORD_RES = (
    re.compile(r'(?:回复|发送)(?:关键词|口令)?\s*[:：]?\s*[“「『"【](.{1,60}?)[”」』"】]'),
    re.compile(r"(?:关键词|口令)\s*[:：]\s*([^\s，。！？,!?]{1,60})"),
    re.compile(r"(?:回复|发送)\s+(.{1,60}?)\s+(?:参与|参加|即可|就能)"),
)


def giveaway_recruitment_keys(prompt: str) -> tuple[str, ...]:
    """Extract only explicit keyword instructions; the caller verifies authority."""
    if not any(token in prompt for token in ("抽奖", "开奖", "中奖")):
        return ()
    return tuple(dict.fromkeys(
        key for pattern in _GIVEAWAY_KEYWORD_RES
        for match in pattern.finditer(prompt[:4000])
        if (key := recruitment_repeat_key(match.group(1))) is not None
    ))[:8]


def compact_repeat_text(text: str) -> str:
    # Keep digits and actual words; skeletons merge unrelated URLs and lottery codes.
    value = unicodedata.normalize("NFKC", strip_zero_width(text)).casefold()
    return "".join(char for char in value if not char.isspace()).strip("!！。.,，?？~～")


def recruitment_repeat_key(text: str) -> str | None:
    if len(text) > 120:
        return None
    value = compact_repeat_text(text)
    return stable_hash(value) if _LURE_RE.fullmatch(value) else None


def giveaway_reply_matches(text: str, prompt: str) -> bool:
    """Only a literal keyword in a giveaway instruction can be a protected reply."""
    value = compact_repeat_text(text)
    normalized = compact_repeat_text(prompt)
    return bool(
        value
        and len(value) <= 60
        and value in normalized
        and any(token in normalized for token in ("抽奖", "开奖", "中奖"))
        and any(token in normalized for token in ("回复", "发送", "口令", "关键词"))
    )


def repeated_recruitment_decision(
    settings: Settings, db: Database, features: MessageFeatures,
) -> LocalDecision | None:
    if features.user_id is None or features.metadata.get("update_type", "message") != "message":
        return None
    if features.metadata.get("giveaway_reply"):
        # Let the model release the keyword even if a generalized fingerprint exists.
        return LocalDecision(
            DecisionAction.REVIEW, "giveaway_keyword_needs_llm", 0.0,
            should_call_llm=True,
            metadata={"giveaway_reply": features.metadata["giveaway_reply"]},
        )
    key = recruitment_repeat_key(features.text)
    if key is None:
        return None
    evidence = db.record_recruitment_repeat(
        features, key, window_seconds=settings.recruitment_repeat_window_seconds,
    )
    features.metadata["recruitment_repeat"] = evidence
    same_sender = evidence["same_sender_count"] >= max(3, settings.recruitment_repeat_min_messages)
    across_senders = evidence["distinct_senders"] >= max(3, settings.repeat_min_distinct_senders)
    if not same_sender and not across_senders:
        return None

    profile = features.metadata.get("sender_profile") or {}
    bio = str(profile.get("bio") or "")
    name = " ".join(str(profile.get(field) or "") for field in (
        "username", "display_name", "first_name", "last_name",
    ))
    # Evidence belongs to this sender. Other accounts' advertising bios never
    # justify banning the next person who happens to use the same sentence.
    carrier = bool(_INVITE_RE.search(bio))
    profile_cta = bool(_PROFILE_CTA_RE.search(compact_repeat_text(name + " " + bio)))
    corroborated = carrier or profile_cta
    metadata = {
        "recruitment_repeat": evidence,
        "profile_invite_link": carrier,
        "profile_diversion_cta": profile_cta,
    }
    if corroborated:
        metadata["additional_message_ids_to_delete"] = evidence["same_sender_message_ids"]
        return LocalDecision(
            DecisionAction.BAN, "repeated_recruitment_with_profile_diversion", 0.97,
            metadata=metadata,
        )
    if same_sender:
        return LocalDecision(
            DecisionAction.WITHDRAW_VOTE, "repeated_recruitment_same_sender", 0.80,
            should_call_llm=True, metadata=metadata,
        )
    # Cross-account repetition without this sender's own diversion evidence is
    # context for the model, never a forced vote/ban on a first-time participant.
    return LocalDecision(
        DecisionAction.REVIEW, "repeated_recruitment_needs_llm", 0.5,
        should_call_llm=True, metadata=metadata,
    )
