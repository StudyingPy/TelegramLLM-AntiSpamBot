from __future__ import annotations

import re

from .config import Settings
from .db import Database
from .fingerprints import stable_hash
from .models import LLMJudgement, MessageFeatures, VoteSession
from .text import normalize_text


PHRASE_CANDIDATE_LIMIT = 256

# stable_hash("") and stable_hash(<any input that normalizes to empty>) collide on
# this value (truncated SHA-256 of the empty byte string). It carries zero identifying
# signal — every empty/whitespace/zero-width/emoji-only message produces it — so any
# fingerprint stored under it acts as a universal trap. Production saw this hash get
# upgraded to content-type weight 85 via vote_confirmed and start auto-banning users
# whose messages happened to normalize to empty (vote confirmed a real spam whose
# clean_text was empty after stripping). Block it at every layer: write, read, judge.
_EMPTY_TEXT_HASH = stable_hash("")


def _is_meaningful_fingerprint_value(value: str | None) -> bool:
    """Return False for hashes that carry no identifying signal.

    Empty strings, whitespace-only, zero-width-only, emoji-only, and digit-only inputs
    all normalize to "" and produce the same hash. Treat that hash as a sentinel,
    never as a real fingerprint.
    """

    return bool(value) and value != _EMPTY_TEXT_HASH


def fingerprint_lookup_values(features: MessageFeatures) -> tuple[tuple[str, str], ...]:
    values: list[tuple[str, str]] = []
    if _is_meaningful_fingerprint_value(features.skeleton_hash):
        values.append(("skeleton", features.skeleton_hash))
    if _is_meaningful_fingerprint_value(features.content_hash):
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

    if _is_meaningful_fingerprint_value(features.skeleton_hash):
        db.upsert_fingerprint(
            "skeleton",
            features.skeleton_hash,
            settings.llm_fingerprint_initial_weight,
            "llm_spam",
        )
    for phrase in judgement.signal_phrases:
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
    # by default), so a single bad ingest here taints future enforcement. Block empty-
    # text hashes — they have no identifying value and cause cross-content collisions
    # against any later message whose normalized text is also empty (stickers, voice,
    # photos with no caption, emoji-only messages, ...).
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
        for chunk in re.findall(r"[\u4e00-\u9fff]{2,}|[a-z_]{3,}", normalized):
            _append_candidate(candidates, chunk)
            if re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
                _append_cjk_ngrams(candidates, chunk)
        if len(candidates) >= PHRASE_CANDIDATE_LIMIT:
            break

    return tuple(candidates[:PHRASE_CANDIDATE_LIMIT])


def _append_cjk_ngrams(candidates: list[str], text: str) -> None:
    max_size = min(12, len(text))
    for size in range(2, max_size + 1):
        for index in range(len(text) - size + 1):
            _append_candidate(candidates, text[index : index + size])
            if len(candidates) >= PHRASE_CANDIDATE_LIMIT:
                return


def _append_candidate(candidates: list[str], phrase: str) -> None:
    if phrase and phrase not in candidates and len(candidates) < PHRASE_CANDIDATE_LIMIT:
        candidates.append(phrase)
