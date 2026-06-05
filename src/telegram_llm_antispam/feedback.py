from __future__ import annotations

from .config import Settings
from .db import Database
from .fingerprints import stable_hash
from .models import LLMJudgement, MessageFeatures, VoteSession
from .text import normalize_text


def fingerprint_lookup_values(features: MessageFeatures) -> tuple[tuple[str, str], ...]:
    return (
        ("skeleton", features.skeleton_hash),
        ("content", features.content_hash),
    )


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
