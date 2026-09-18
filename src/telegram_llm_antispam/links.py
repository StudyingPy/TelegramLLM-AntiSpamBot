from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .fingerprints import URL_RE
from .models import ExtractedLink, LinkSource


TRAILING_PUNCTUATION = ".,;:!?)]}>'\"，。！？；：、）】》"


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def canonicalize_url(url: str) -> str:
    cleaned = url.strip().strip(TRAILING_PUNCTUATION)
    if cleaned.startswith("www."):
        return f"https://{cleaned}"
    return cleaned


def domain_from_url(url: str) -> str | None:
    parsed = urlparse(canonicalize_url(url))
    host = parsed.hostname
    if not host:
        return None
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def is_whitelisted_domain(domain: str | None, whitelist: tuple[str, ...]) -> bool:
    if not domain:
        return False
    domain = domain.lower().rstrip(".")
    return any(domain == item or domain.endswith(f".{item}") for item in whitelist)


def _iter_entities(message: Any) -> tuple[Any, ...]:
    entities = _field(message, "entities") or _field(message, "caption_entities") or ()
    return tuple(entities)


def _rich_text(value: Any) -> str:
    """Flatten Bot API 10.1 RichMessage content into searchable plain text.

    aiogram versions predating RichMessage keep the unknown field in ``model_extra``
    and expose it through ``getattr``.  The helper therefore accepts both mappings
    and model-like objects, and also tolerates Telegram's older ``_type``/``texts``
    representation seen in exported client JSON.
    """

    if isinstance(value, str):
        return value
    if value is None:
        return ""

    text = _field(value, "text")
    if isinstance(text, str):
        return text
    if text is not None:
        nested = _rich_text(text)
        if nested:
            return nested

    # A RichTextConcat stores adjacent fragments in ``texts``; these must not gain
    # line breaks in the middle of mentions or words.
    texts = _field(value, "texts")
    if isinstance(texts, (list, tuple)):
        return "".join(part for item in texts if (part := _rich_text(item)))

    # Blocks, list items, table cells and similar containers are separate visible
    # sections. Newlines preserve enough structure for the LLM and fingerprints.
    parts: list[str] = []
    for key in ("blocks", "items", "children", "content", "rows", "cells"):
        children = _field(value, key)
        if isinstance(children, (list, tuple)):
            parts.extend(part for item in children if (part := _rich_text(item)))
        elif children is not None:
            part = _rich_text(children)
            if part:
                parts.append(part)
    return "\n".join(parts)


def _iter_rich_urls(value: Any) -> tuple[str, ...]:
    """Collect explicit HTTP(S) URLs embedded in rich-text entities."""

    if value is None or isinstance(value, str):
        return ()
    found: list[str] = []
    url = _field(value, "url")
    if isinstance(url, str) and url.lower().startswith(("http://", "https://", "www.")):
        found.append(url)
    for key in ("text", "texts", "blocks", "items", "children", "content", "rows", "cells"):
        child = _field(value, key)
        if isinstance(child, (list, tuple)):
            for item in child:
                found.extend(_iter_rich_urls(item))
        elif child is not None:
            found.extend(_iter_rich_urls(child))
    return tuple(found)


def extract_message_text(message: Any) -> str:
    return (
        _field(message, "text")
        or _field(message, "caption")
        or _rich_text(_field(message, "rich_message"))
        or ""
    )


def extract_links(message: Any) -> tuple[ExtractedLink, ...]:
    text = extract_message_text(message)
    collected: list[tuple[str, LinkSource]] = []

    for match in URL_RE.finditer(text):
        collected.append((match.group(0), "text"))

    for entity in _iter_entities(message):
        entity_type = _enum_value(_field(entity, "type"))
        url = _field(entity, "url")
        if entity_type == "text_link" and url:
            collected.append((url, "entity"))

    for url in _iter_rich_urls(_field(message, "rich_message")):
        collected.append((url, "entity"))

    preview_options = _field(message, "link_preview_options")
    preview_url = _field(preview_options, "url")
    if preview_url:
        collected.append((preview_url, "preview"))

    seen: set[str] = set()
    links: list[ExtractedLink] = []
    for raw_url, source in collected:
        canonical = canonicalize_url(str(raw_url))
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        links.append(ExtractedLink(url=canonical, source=source, domain=domain_from_url(canonical)))

    return tuple(links)


def extract_domains(links: tuple[ExtractedLink, ...]) -> tuple[str, ...]:
    return tuple(sorted({link.domain for link in links if link.domain}))
