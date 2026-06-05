from __future__ import annotations

from typing import Any

from .fingerprints import content_hash, simhash, skeletonize, stable_hash
from .links import extract_domains, extract_links, extract_message_text
from .models import MessageFeatures, SenderProfile, UserContext
from .text import count_mentions, is_empty_or_punctuation, normalize_text


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _nested_id(obj: Any, name: str) -> int | None:
    nested = _field(obj, name)
    value = _field(nested, "id")
    return int(value) if value is not None else None


def build_message_features(
    message: Any,
    user_context: UserContext | None = None,
    sender_profile: SenderProfile | None = None,
    default_reputation: float = 50,
) -> MessageFeatures:
    chat_id = _nested_id(message, "chat")
    message_id = _field(message, "message_id")
    user_id = _nested_id(message, "from_user")
    text = extract_message_text(message)
    clean_text = normalize_text(text)
    skeleton = skeletonize(text)
    links = extract_links(message)

    if chat_id is None:
        raise ValueError("message.chat.id is required")
    if message_id is None:
        raise ValueError("message.message_id is required")

    metadata: dict[str, Any] = {
        "link_count": len(links),
        "domains": list(extract_domains(links)),
    }
    if sender_profile is not None:
        metadata["sender_profile"] = sender_profile.to_payload()

    return MessageFeatures(
        chat_id=chat_id,
        message_id=int(message_id),
        user_id=user_id,
        text=text,
        clean_text=clean_text,
        skeleton=skeleton,
        content_hash=content_hash(text),
        skeleton_hash=stable_hash(skeleton),
        simhash=simhash(text),
        links=links,
        link_domains=extract_domains(links),
        mention_count=count_mentions(text),
        has_preview_url=any(link.source == "preview" for link in links),
        is_empty_or_punctuation=is_empty_or_punctuation(clean_text),
        is_first_message=user_context.is_first_message if user_context else True,
        sender_reputation=user_context.reputation_score if user_context else default_reputation,
        metadata=metadata,
    )
