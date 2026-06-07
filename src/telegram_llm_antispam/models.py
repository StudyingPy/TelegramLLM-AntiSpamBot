from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


LinkSource = Literal["text", "entity", "preview"]


class DecisionAction(str, Enum):
    ALLOW = "allow"
    REVIEW = "review"
    WITHDRAW_VOTE = "withdraw_vote"
    BAN = "ban"


@dataclass(frozen=True, slots=True)
class ExtractedLink:
    url: str
    source: LinkSource
    domain: str | None = None


@dataclass(frozen=True, slots=True)
class UserContext:
    chat_id: int
    user_id: int
    reputation_score: float
    messages_seen: int
    first_seen_at: int | None = None
    last_seen_at: int | None = None

    @property
    def is_first_message(self) -> bool:
        return self.messages_seen <= 0


@dataclass(frozen=True, slots=True)
class SenderProfile:
    user_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language_code: str | None = None
    is_bot: bool = False
    is_premium: bool | None = None
    bio: str | None = None
    bio_fetched_at: int | None = None
    updated_at: int | None = None

    @property
    def display_name(self) -> str:
        return " ".join(part for part in (self.first_name, self.last_name) if part).strip()

    def to_payload(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name or None,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "language_code": self.language_code,
            "is_bot": self.is_bot,
            "is_premium": self.is_premium,
            "bio": self.bio,
            "bio_fetched_at": self.bio_fetched_at,
        }


@dataclass(frozen=True, slots=True)
class MessageFeatures:
    chat_id: int
    message_id: int
    user_id: int | None
    text: str
    clean_text: str
    skeleton: str
    content_hash: str
    skeleton_hash: str
    simhash: int
    links: tuple[ExtractedLink, ...]
    link_domains: tuple[str, ...]
    mention_count: int
    has_preview_url: bool
    is_empty_or_punctuation: bool
    is_first_message: bool
    sender_reputation: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FingerprintRecord:
    id: int
    fingerprint_type: str
    value: str
    weight: float
    hit_count: int
    false_positive_count: int
    source: str


@dataclass(frozen=True, slots=True)
class LocalDecision:
    action: DecisionAction
    reason: str
    confidence: float
    should_call_llm: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ActionResult:
    action_log_id: int | None = None
    vote_session_id: int | None = None
    deleted: bool | None = None
    banned: bool | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AdminNotification:
    id: int
    vote_session_id: int | None
    action_log_id: int | None
    notify_user_id: int
    message_id: int
    base_text: str
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class LLMJudgement:
    is_spam: bool
    confidence: float
    category: str | None = None
    skeleton_hash: str | None = None
    signal_phrases: tuple[str, ...] = ()


class LLMOutcomeStatus(str, Enum):
    DISABLED = "disabled"
    OK = "ok"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class LLMOutcome:
    """Always-on summary of the LLM hop, even when no judgement was produced.

    `status=DISABLED` means no provider was configured.
    `status=FAILED` means every configured provider was tried and all of them failed
    (timeout/transport/JSON parse). `error` carries the last error string for ops triage.
    `status=OK` means at least one provider returned a parseable judgement.
    `provider_count` is the number of configured providers (0 when disabled).
    """

    status: LLMOutcomeStatus
    provider_count: int = 0
    judgement: LLMJudgement | None = None
    error: str | None = None

    @property
    def is_spam(self) -> bool:
        return self.judgement is not None and self.judgement.is_spam


@dataclass(frozen=True, slots=True)
class VoteTally:
    session_id: int
    chat_id: int
    suspect_user_id: int | None
    spam_votes: int
    ham_votes: int
    status: str
    changed: bool


@dataclass(frozen=True, slots=True)
class VoteRecord:
    voter_user_id: int
    vote: str
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class VoteSession:
    id: int
    chat_id: int
    original_message_id: int
    vote_message_id: int | None
    suspect_user_id: int | None
    skeleton_hash: str | None
    content_hash: str | None
    status: str
    spam_votes: int
    ham_votes: int
    reason: str
    created_at: int
    expires_at: int
    closed_at: int | None


@dataclass(frozen=True, slots=True)
class OGPreview:
    url: str
    final_url: str
    title: str | None = None
    description: str | None = None
    site_name: str | None = None
    image_alt: str | None = None
    text: str = ""
    truncated: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "title": self.title,
            "description": self.description,
            "site_name": self.site_name,
            "image_alt": self.image_alt,
            "text": self.text,
            "truncated": self.truncated,
        }
