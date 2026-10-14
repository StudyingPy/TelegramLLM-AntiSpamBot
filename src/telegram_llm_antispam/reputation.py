from __future__ import annotations

import time

from .config import Settings
from .models import DecisionAction, LocalDecision, MessageFeatures, UserContext


def trust_context(
    context: UserContext, settings: Settings | None = None, *, now: int | None = None
) -> dict[str, object]:
    now = int(time.time()) if now is None else now
    start = context.joined_at if context.joined_at is not None else context.first_seen_at
    hours = settings.new_user_observation_hours if settings else 72
    count = settings.trusted_normal_message_min_count if settings else 30
    threshold = settings.high_reputation_threshold if settings else 80
    low = settings.low_reputation_threshold if settings else 35
    trusted = bool(
        start is not None
        and now - start >= max(0, hours) * 3600
        and context.normal_messages_seen >= max(1, count)
        and context.reputation_score >= threshold
    )
    return {
        "tier": "trusted" if trusted else (
            "low_reputation" if context.reputation_score <= low else "observed"
        ),
        "is_trusted": trusted,
        "reputation_score": context.reputation_score,
        "normal_messages_seen": context.normal_messages_seen,
        "observation_started_at": start,
    }


def is_trusted_sender(features: MessageFeatures) -> bool:
    context = features.metadata.get("user_trust")
    return isinstance(context, dict) and context.get("is_trusted") is True


def route_trusted_message_to_llm(
    features: MessageFeatures, decision: LocalDecision
) -> LocalDecision:
    if (
        not is_trusted_sender(features)
        or decision.action not in {DecisionAction.BAN, DecisionAction.WITHDRAW_VOTE}
        or decision.reason == "repeated_bot_only_invocation_after_ad"
    ):
        return decision
    metadata = dict(decision.metadata)
    metadata.setdefault("local_rule_reason", decision.reason)
    metadata.setdefault("local_rule_action", decision.action.value)
    metadata.setdefault("local_rule_confidence", decision.confidence)
    return LocalDecision(
        action=DecisionAction.REVIEW,
        reason="trusted_user_needs_llm",
        confidence=decision.confidence,
        should_call_llm=True,
        metadata=metadata,
    )
