from __future__ import annotations

import re

from .config import Settings
from .db import Database
from .fingerprints import stable_hash
from .models import LLMJudgement, MessageFeatures, VoteSession
from .text import normalize_text


PHRASE_CANDIDATE_LIMIT = 256


def fingerprint_lookup_values(features: MessageFeatures) -> tuple[tuple[str, str], ...]:
    values = [
        ("skeleton", features.skeleton_hash),
        ("content", features.content_hash),
    ]
    values.extend(("phrase", value) for value in phrase_lookup_values(features))
    return tuple(dict.fromkeys(values))


def record_llm_spam_feedback(
    db: Database,
    features: MessageFeatures,
    judgement: LLMJudgement,
    settings: Settings,
) -> None:
    if not judgement.is_spam or judgement.confidence < settings.llm_review_threshold:
        return

    db.upsert_fingerprint(
        "skeleton",
        features.skeleton_hash,
        settings.llm_fingerprint_initial_weight,
        "llm_spam",
    )
    for phrase in judgement.signal_phrases:
        value = phrase_fingerprint_value(phrase)
        if value:
            db.upsert_fingerprint(
                "phrase",
                value,
                settings.llm_fingerprint_initial_weight,
                "llm_spam_phrase",
            )


def record_vote_spam_feedback(db: Database, session: VoteSession, settings: Settings) -> None:
    if session.skeleton_hash:
        db.upsert_fingerprint(
            "skeleton",
            session.skeleton_hash,
            settings.vote_confirmed_fingerprint_weight,
            "vote_confirmed",
        )
    if session.content_hash:
        db.upsert_fingerprint(
            "content",
            session.content_hash,
            settings.vote_confirmed_fingerprint_weight,
            "vote_confirmed",
        )


def record_vote_ham_feedback(db: Database, session: VoteSession, settings: Settings) -> None:
    for value in (session.skeleton_hash, session.content_hash):
        if value:
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
