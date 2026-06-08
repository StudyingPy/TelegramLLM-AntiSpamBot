from __future__ import annotations

import re

from .config import Settings
from .db import Database
from .fingerprints import (
    LOW_ENTROPY_SKELETON_HASHES,
    is_low_entropy_normalized_text,
    is_low_entropy_skeleton,
    stable_hash,
)
from .models import LLMJudgement, MessageFeatures, VoteSession
from .text import normalize_text


PHRASE_CANDIDATE_LIMIT = 256

# Minimum lengths for phrase fingerprints. Below this threshold a phrase collides
# against too many unrelated messages to be useful as a signal. Two-character CJK
# words like "可以", "我们", "什么" appear in every Chinese conversation; we must not
# turn an LLM "signal phrase" into a permanent enforcement entry for any of them.
_PHRASE_MIN_CJK_CHARS = 3
_PHRASE_MIN_LATIN_CHARS = 4

# stable_hash("") and stable_hash(<any input that normalizes to empty>) collide on
# this value (truncated SHA-256 of the empty byte string). It carries zero identifying
# signal — every empty/whitespace/zero-width/emoji-only message produces it — so any
# fingerprint stored under it acts as a universal trap. Production saw this hash get
# upgraded to content-type weight 85 via vote_confirmed and start auto-banning users
# whose messages happened to normalize to empty (vote confirmed a real spam whose
# clean_text was empty after stripping). Block it at every layer: write, read, judge.
_EMPTY_TEXT_HASH = stable_hash("")


def _is_meaningful_fingerprint_value(value: str | None) -> bool:
    """Return False for hashes known to lack identifying signal.

    Two filter groups:
      - Empty-text hash (stable_hash("")) — covers empty / whitespace / zero-width /
        emoji-only / digit-only messages.
      - Pre-computed low-entropy skeleton hashes (<url>, <mention>, <w>, ...) — any
        single-carrier or two-carrier message would collide with these.

    Low-entropy CONTENT hashes (e.g. single CJK char "元" from "300元") cannot be
    detected from the hash alone — they have to be filtered at write time using the
    original normalized text, which the public record_* functions do via
    is_low_entropy_normalized_text().
    """

    return (
        bool(value)
        and value != _EMPTY_TEXT_HASH
        and value not in LOW_ENTROPY_SKELETON_HASHES
    )


def _phrase_passes_min_length(phrase: str) -> bool:
    """A phrase fingerprint is only meaningful with enough characters to disambiguate.

    3+ CJK chars or 4+ Latin chars; below that, the phrase appears in too many
    innocent messages to be a usable spam signal.
    """

    normalized = normalize_text(phrase)
    if not normalized:
        return False
    return not is_low_entropy_normalized_text(normalized)


def fingerprint_lookup_values(features: MessageFeatures) -> tuple[tuple[str, str], ...]:
    values: list[tuple[str, str]] = []
    # Skeleton lookup is further gated by the skeleton STRING (not just the hash) so
    # a future skeletonize() output we haven't enumerated as a sentinel still gets
    # caught by the heuristic backstop.
    if _is_meaningful_fingerprint_value(features.skeleton_hash) and not is_low_entropy_skeleton(
        features.skeleton
    ):
        values.append(("skeleton", features.skeleton_hash))
    # Content lookup similarly gated by the normalized clean_text.
    if _is_meaningful_fingerprint_value(features.content_hash) and not is_low_entropy_normalized_text(
        features.clean_text
    ):
        values.append(("content", features.content_hash))
    for phrase_value in phrase_lookup_values(features):
        if _is_meaningful_fingerprint_value(phrase_value):
            values.append(("phrase", phrase_value))
    return tuple(dict.fromkeys(values))


def record_llm_spam_feedback(
    db: Database,
    features: MessageFeatures,
    judgement: LLMJudgement,
    settings: Settings,
) -> None:
    if not judgement.is_spam or judgement.confidence < settings.llm_review_threshold:
        return

    # Skeleton: must pass hash sentinel guard AND the skeleton-string heuristic
    # backstop. Without the latter, a future skeletonize() change introducing a new
    # placeholder would let a brand-new low-entropy form sneak in.
    if _is_meaningful_fingerprint_value(features.skeleton_hash) and not is_low_entropy_skeleton(
        features.skeleton
    ):
        db.upsert_fingerprint(
            "skeleton",
            features.skeleton_hash,
            settings.llm_fingerprint_initial_weight,
            "llm_spam",
        )
    for phrase in judgement.signal_phrases:
        # LLM is free to return "可以" / "OK" / "see" as signal phrases. We must NOT
        # promote those to fingerprint candidates regardless of how confident the
        # judgement was — they will collide with every innocent message.
        if not _phrase_passes_min_length(phrase):
            continue
        value = phrase_fingerprint_value(phrase)
        if _is_meaningful_fingerprint_value(value):
            db.upsert_fingerprint(
                "phrase",
                value,
                settings.llm_fingerprint_initial_weight,
                "llm_spam_phrase",
            )


def record_vote_spam_feedback(db: Database, session: VoteSession, settings: Settings) -> None:
    # Vote-confirmed spam upgrades content/skeleton fingerprints to a high weight (85
    # by default), so a single bad ingest here taints future enforcement. We have
    # only the hash, not the original strings, on a VoteSession — fall back to the
    # hash-sentinel filter alone here. The write side that creates the session at
    # vote-open time should ALSO not store empty/low-entropy hashes, but this is the
    # final safety net.
    if _is_meaningful_fingerprint_value(session.skeleton_hash):
        db.upsert_fingerprint(
            "skeleton",
            session.skeleton_hash,
            settings.vote_confirmed_fingerprint_weight,
            "vote_confirmed",
        )
    if _is_meaningful_fingerprint_value(session.content_hash):
        db.upsert_fingerprint(
            "content",
            session.content_hash,
            settings.vote_confirmed_fingerprint_weight,
            "vote_confirmed",
        )


def record_vote_ham_feedback(db: Database, session: VoteSession, settings: Settings) -> None:
    for value in (session.skeleton_hash, session.content_hash):
        if _is_meaningful_fingerprint_value(value):
            db.mark_fingerprint_false_positive(value, settings.fingerprint_false_positive_penalty)


def phrase_fingerprint_value(phrase: str) -> str | None:
    normalized = normalize_text(phrase)
    if not normalized:
        return None
    return stable_hash(f"phrase:{normalized}")


def phrase_lookup_values(features: MessageFeatures) -> tuple[str, ...]:
    values: list[str] = []
    for phrase in _profile_and_message_phrase_candidates(features):
        # Apply the same minimum-length guard to LOOKUP candidates that we apply to
        # WRITE candidates. Otherwise a phrase we'd never write (because it's too
        # short) could still match against a stale low-entropy phrase fingerprint
        # left over from before this commit. The purge admin command cleans those,
        # but defense in depth keeps us safe before the operator runs it.
        if not _phrase_passes_min_length(phrase):
            continue
        value = phrase_fingerprint_value(phrase)
        if value and value not in values:
            values.append(value)
        if len(values) >= PHRASE_CANDIDATE_LIMIT:
            break
    return tuple(values)


def _profile_and_message_phrase_candidates(features: MessageFeatures) -> tuple[str, ...]:
    texts = [features.clean_text]

    og_preview = features.metadata.get("og_preview")
    if isinstance(og_preview, dict):
        texts.extend(
            str(og_preview.get(key) or "")
            for key in ("title", "description", "site_name", "image_alt", "text")
        )

    sender_profile = features.metadata.get("sender_profile")
    if isinstance(sender_profile, dict):
        texts.extend(
            str(sender_profile.get(key) or "")
            for key in ("username", "display_name", "first_name", "last_name", "bio")
        )

    candidates: list[str] = []
    for text in texts:
        normalized = normalize_text(text)
        if not normalized:
            continue
        _append_candidate(candidates, normalized)
        # CJK chunk minimum bumped to 3 (was 2). Latin minimum bumped to 4 (was 3).
        # Both match _phrase_passes_min_length so we never generate a candidate that
        # would just be rejected later.
        for chunk in re.findall(r"[一-鿿]{3,}|[a-z_]{4,}", normalized):
            _append_candidate(candidates, chunk)
            if re.fullmatch(r"[一-鿿]+", chunk):
                _append_cjk_ngrams(candidates, chunk)
        if len(candidates) >= PHRASE_CANDIDATE_LIMIT:
            break

    return tuple(candidates[:PHRASE_CANDIDATE_LIMIT])


def _append_cjk_ngrams(candidates: list[str], text: str) -> None:
    max_size = min(12, len(text))
    # CJK n-grams: minimum size 3 (was 2). 2-char CJK n-grams like "可以"/"我们"
    # collide with every conversation and inflate the phrase fingerprint table
    # without adding signal. Anything that originally needed a 2-char fingerprint
    # to be useful was a false positive waiting to happen.
    for size in range(_PHRASE_MIN_CJK_CHARS, max_size + 1):
        for index in range(len(text) - size + 1):
            _append_candidate(candidates, text[index : index + size])
            if len(candidates) >= PHRASE_CANDIDATE_LIMIT:
                return


def _append_candidate(candidates: list[str], phrase: str) -> None:
    if phrase and phrase not in candidates and len(candidates) < PHRASE_CANDIDATE_LIMIT:
        candidates.append(phrase)
