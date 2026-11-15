"""Narrow post-model policy for independently corroborated collection-job scams."""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import replace

from .models import DecisionAction, LocalDecision, MessageFeatures
from .reputation import is_trusted_sender
from .text import strip_zero_width


_COLLECTION_JOB = re.compile(
    r"(?:(?:用)?(?:微信|vx|v信)(?:帮我|帮忙|找人|招人)"
    r"|(?:帮我|帮忙|找人|招人)(?:用)?(?:微信|vx|v信))"
    r"(?:收资|代收资金|代收款|收款)[，,:：;；!！。]*"
    r"(?:一天|每天|日结|日入)[:：]?"
    r"(?P<amount>[1-9][0-9o〇]{2,5})(?:元|块|rmb)?"
    r"(?:私聊我|联系我|有意私聊)?"
)


def financial_collection_offer(text: str) -> int | None:
    compact = "".join(unicodedata.normalize("NFKC", strip_zero_width(text)).casefold().split())
    if len(compact) > 120:
        return None
    match = _COLLECTION_JOB.fullmatch(compact.strip("，,。!！~～"))
    if match is None:
        return None
    amount = int(match["amount"].replace("o", "0").replace("〇", "0"))
    return amount if amount >= 1000 else None


def corroborated_financial_recruitment(
    features: MessageFeatures,
    decision: LocalDecision,
) -> LocalDecision:
    """Keep both raw scores; this is an explicit policy override, not calibration."""
    if (
        decision.action not in {DecisionAction.REVIEW, DecisionAction.WITHDRAW_VOTE}
        or features.user_id is None
        or is_trusted_sender(features)
        or features.metadata.get("giveaway_reply")
        or features.metadata.get("quoted_or_code_message")
    ):
        return decision
    amount = financial_collection_offer(features.text)
    if amount is None:
        return decision
    chain = decision.metadata.get("model_chain") or {}
    reviewer = decision.metadata.get("llm_outcome") or {}
    if not isinstance(chain, dict) or not isinstance(reviewer, dict):
        return decision
    primary = chain.get("primary_result") or {}
    if not isinstance(primary, dict):
        return decision
    visual = decision.metadata.get("visual_review")
    if isinstance(visual, dict) and (
        visual.get("status") != "ok" or visual.get("message_is_promotional") is not True
    ):
        return decision
    if (
        chain.get("decision_source") != "mimo_review"
        or chain.get("reviewer_status") != "ok"
        or primary.get("status") != "ok"
        or primary.get("category") != "scam"
        or reviewer.get("status") != "ok"
        or reviewer.get("is_spam") is not True
    ):
        return decision
    primary_score, reviewer_score = primary.get("noul"), reviewer.get("confidence")
    if any(
        type(score) not in (float, int) or not math.isfinite(score) or not 0 <= score <= 1
        for score in (primary_score, reviewer_score)
    ):
        return decision
    if primary_score < 0.90 or reviewer_score < 0.80:
        return decision
    metadata = {
        **decision.metadata,
        "corroborated_recruitment": {
            "signal": "wechat_collection_job_with_daily_pay",
            "daily_amount": amount,
            "jev_noul": primary_score,
            "mimo_confidence": reviewer_score,
            "previous_action": decision.action.value,
            "previous_reason": decision.reason,
        },
    }
    return replace(
        decision,
        action=DecisionAction.BAN,
        reason="corroborated_financial_recruitment",
        should_call_llm=False,
        metadata=metadata,
    )
