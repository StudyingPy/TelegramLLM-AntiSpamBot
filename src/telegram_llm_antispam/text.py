from __future__ import annotations

import re
import unicodedata


ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
WHITESPACE_RE = re.compile(r"\s+")
MENTION_RE = re.compile(r"(?<!\w)@[\w_]{3,}", re.UNICODE)


def strip_zero_width(text: str) -> str:
    return ZERO_WIDTH_RE.sub("", text)


def strip_emoji(text: str) -> str:
    chars: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if category in {"So", "Cs"}:
            continue
        chars.append(char)
    return "".join(chars)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", strip_zero_width(text))
    text = strip_emoji(text).lower()
    text = "".join(char for char in text if not char.isdigit())
    return WHITESPACE_RE.sub(" ", text).strip()


def is_empty_or_punctuation(text: str) -> bool:
    compact = "".join(char for char in text if not char.isspace())
    if not compact:
        return True
    return all(not char.isalnum() for char in compact)


def count_mentions(text: str) -> int:
    return len(MENTION_RE.findall(text))

