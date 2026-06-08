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


# --- Low-entropy fingerprint sentinels ------------------------------------------------
#
# A fingerprint is only useful if its hash uniquely identifies a class of related
# messages. The original empty-text bug was the extreme case (stable_hash("") matched
# every empty/whitespace/emoji-only message). The same shape — a hash that matches a
# huge swath of unrelated messages — applies to any skeleton or phrase that is
# structurally too generic to discriminate. Examples:
#
#   skeleton "<url>"        ← any message that is a bare URL
#   skeleton "<mention>"    ← any "@someone" with no other content
#   skeleton "<w>"          ← any single English word
#   normalized "元"          ← any "300元" / "500元" / "$N元" (digits stripped)
#
# Each of these gets created naturally by skeletonize() / normalize_text() and is
# perfectly legitimate as a SKELETON OF A REAL MESSAGE. The bug is letting the
# feedback loop upgrade them to high-weight fingerprints. If one of these classes is
# vote-confirmed as spam, every later innocent message in the same class collides.
#
# We block this at the fingerprint-write layer (and read layer) by treating their
# precomputed hashes as sentinels. Kept here, next to skeletonize(), so that any
# future change to placeholder names automatically updates this list (the eyes will
# land on this comment when skeletonize is edited).

# Single-token / two-token skeletons made up entirely of structural placeholders.
# These are the skeletonize() outputs for messages that contain only a carrier and
# nothing else. A real ad always has descriptive text alongside its carrier — these
# patterns alone are not predictive.
_LOW_ENTROPY_SKELETONS: frozenset[str] = frozenset(
    [
        "<url>",
        "<mention>",
        "<email>",
        "<w>",
        # common two-placeholder combinations — "send a URL with a single English
        # word", "URL followed by URL" (extracted from multipost), etc.
        "<url> <w>",
        "<w> <url>",
        "<url> <mention>",
        "<mention> <url>",
        "<url> <url>",
        "<w> <mention>",
        "<mention> <w>",
        "<email> <w>",
        "<w> <email>",
    ]
)


def is_low_entropy_skeleton(skeleton: str) -> bool:
    """Return True for skeletons that match too broadly to be useful fingerprints.

    Two ways to qualify:
      1. The skeleton is in the curated _LOW_ENTROPY_SKELETONS set (known patterns
         that arise from carrier-only messages).
      2. Heuristic backstop: after stripping placeholders/punctuation, fewer than 3
         characters of "real" content remain. This catches future low-entropy shapes
         we haven't enumerated yet (e.g. someone changes skeletonize() and a new
         placeholder gets introduced), at the cost of also rejecting genuinely tiny
         messages — which is the right trade because tiny messages aren't reliable
         fingerprints anyway.
    """

    if skeleton in _LOW_ENTROPY_SKELETONS:
        return True
    stripped = re.sub(r"<\w+>|[\s\W_]+", "", skeleton, flags=re.UNICODE)
    return len(stripped) < 3


def is_low_entropy_normalized_text(normalized: str) -> bool:
    """Return True for normalize_text() outputs that are too short to identify content.

    normalize_text strips digits, zero-width chars, and emoji, so a message like
    "300元" collapses to "元" — a single CJK char that appears in many unrelated
    messages. Anything under 3 CJK characters / 4 Latin characters is a coin flip
    rather than a fingerprint.
    """

    if not normalized:
        return True
    # any CJK char counts as 1; Latin words as their length
    cjk_chars = sum(1 for c in normalized if "一" <= c <= "鿿")
    latin_chars = sum(1 for c in normalized if c.isascii() and c.isalnum())
    if cjk_chars >= 3:
        return False
    if latin_chars >= 4:
        return False
    return True


# Pre-computed sentinel hash sets, so callers can do a single set membership test
# instead of recomputing skeletonize/normalize on every lookup.
LOW_ENTROPY_SKELETON_HASHES: frozenset[str] = frozenset(
    stable_hash(s) for s in _LOW_ENTROPY_SKELETONS
)


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

