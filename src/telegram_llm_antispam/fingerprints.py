from __future__ import annotations

import hashlib
import re

from .text import WHITESPACE_RE, normalize_text, strip_emoji, strip_zero_width


URL_RE = re.compile(
    r"(?i)\b(?:https?://|www\.)[^\s<>()\[\]{}\"']+",
    re.UNICODE,
)
EMAIL_RE = re.compile(r"(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b")
MENTION_RE = re.compile(r"(?<!\w)@[\w_]{3,}", re.UNICODE)
TOKEN_RE = re.compile(
    r"<url>|<email>|<mention>|[\u4e00-\u9fff]+|[a-z_]+|[^\s]",
    re.IGNORECASE | re.UNICODE,
)


def stable_hash(value: str, length: int = 32) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def skeletonize(text: str) -> str:
    """Build a structure-preserving fingerprint for repeat detection.

    Carriers (URL/email/@mention) and Latin word tokens are replaced with placeholders
    so paraphrased advertisements with the same skeleton still collide. CJK characters
    are KEPT verbatim \u2014 they're the actual content, not the structure. Folding every
    Chinese token into a single `<zh>` placeholder catastrophically over-generalizes
    short messages: any two unrelated zh-only sentences (e.g. "\u4e0d\u6e05\u695a" and "\u597d\u7684") end
    up sharing the same skeleton hash. Once such a hash gets upgraded to a high-weight
    fingerprint via vote-confirmed spam, every short Chinese sentence by a normal-rep
    user is auto-banned. That bug is what we're fixing here.
    """

    base = strip_zero_width(text).lower()
    base = URL_RE.sub(" <url> ", base)
    base = EMAIL_RE.sub(" <email> ", base)
    base = MENTION_RE.sub(" <mention> ", base)
    base = strip_emoji(base)
    base = "".join(char for char in base if not char.isdigit())
    base = WHITESPACE_RE.sub(" ", base).strip()

    skeleton_tokens: list[str] = []
    previous: str | None = None
    for token in TOKEN_RE.findall(base):
        if token in {"<url>", "<email>", "<mention>"}:
            mapped = token
        elif re.fullmatch(r"[\u4e00-\u9fff]+", token):
            mapped = token  # keep CJK verbatim \u2014 it IS the content
        elif re.fullmatch(r"[a-z_]+", token):
            mapped = "<w>"
        else:
            mapped = token

        # Collapse runs of identical placeholders ("<url> <url>" \u2192 "<url>") but keep
        # CJK runs separate so the structure of the original message stays observable.
        if mapped == previous and mapped.startswith("<"):
            continue
        skeleton_tokens.append(mapped)
        previous = mapped

    return " ".join(skeleton_tokens)


def content_hash(text: str) -> str:
    return stable_hash(normalize_text(text))


def skeleton_hash(text: str) -> str:
    return stable_hash(skeletonize(text))


def _ngrams(text: str, size: int = 3) -> list[str]:
    compact = WHITESPACE_RE.sub("", text)
    if not compact:
        return []
    if len(compact) <= size:
        return [compact]
    return [compact[index : index + size] for index in range(len(compact) - size + 1)]


def simhash(text: str, bits: int = 64) -> int:
    tokens = _ngrams(normalize_text(text))
    if not tokens:
        return 0

    vector = [0] * bits
    for token in tokens:
        digest = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16)
        for bit in range(bits):
            vector[bit] += 1 if digest & (1 << bit) else -1

    result = 0
    for bit, weight in enumerate(vector):
        if weight > 0:
            result |= 1 << bit
    return result

