from __future__ import annotations

import re

from .config import Settings
from .links import is_whitelisted_domain
from .models import DecisionAction, FingerprintRecord, LocalDecision, MessageFeatures
from .text import normalize_text


class RuleEngine:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def evaluate(
        self,
        features: MessageFeatures,
        fingerprint: FingerprintRecord | None = None,
    ) -> LocalDecision:
        if fingerprint is not None:
            return self._evaluate_fingerprint(features, fingerprint)

        profile_spam = _profile_spam_decision(features)
        if profile_spam is not None:
            return profile_spam

        if features.is_empty_or_punctuation and features.has_preview_url:
            return LocalDecision(
                action=DecisionAction.WITHDRAW_VOTE,
                reason="empty_or_punctuation_with_link_preview",
                confidence=self._settings.preview_punctuation_confidence,
                should_call_llm=True,
            )

        if features.links:
            return LocalDecision(
                action=DecisionAction.REVIEW,
                reason="link_message_needs_llm",
                confidence=0.45,
                should_call_llm=True,
            )

        return LocalDecision(
            action=DecisionAction.REVIEW,
            reason="unmatched_message_needs_llm",
            confidence=0.0,
            should_call_llm=True,
        )

    def _evaluate_fingerprint(
        self,
        features: MessageFeatures,
        fingerprint: FingerprintRecord,
    ) -> LocalDecision:
        high_weight = fingerprint.weight >= self._settings.fingerprint_ban_weight
        low_rep = features.sender_reputation <= self._settings.reputation_ban_threshold
        high_rep = features.sender_reputation >= self._settings.high_reputation_threshold

        if high_weight and not high_rep:
            return LocalDecision(
                action=DecisionAction.BAN,
                reason=(
                    "known_high_weight_fingerprint_low_reputation"
                    if low_rep
                    else "known_high_weight_fingerprint"
                ),
                confidence=0.95,
                should_call_llm=False,
                metadata={"fingerprint_id": fingerprint.id},
            )

        if fingerprint.weight >= self._settings.fingerprint_review_weight:
            return LocalDecision(
                action=DecisionAction.WITHDRAW_VOTE,
                reason="known_fingerprint",
                confidence=min(0.90, fingerprint.weight / 100),
                should_call_llm=False,
                metadata={"fingerprint_id": fingerprint.id},
            )

        return LocalDecision(
            action=DecisionAction.REVIEW,
            reason="weak_fingerprint_needs_llm",
            confidence=min(0.55, fingerprint.weight / 100),
            should_call_llm=True,
            metadata={"fingerprint_id": fingerprint.id},
        )

    def _has_non_whitelisted_link(self, features: MessageFeatures) -> bool:
        for domain in features.link_domains:
            if not is_whitelisted_domain(domain, self._settings.whitelist_domains):
                return True
        return False


def _profile_spam_decision(features: MessageFeatures) -> LocalDecision | None:
    profile = features.metadata.get("sender_profile")
    if not isinstance(profile, dict):
        return None

    bio = str(profile.get("bio") or "")
    if not _looks_like_spam_bio(bio):
        return None

    return LocalDecision(
        action=DecisionAction.BAN,
        reason="spam_profile_bio",
        confidence=0.96,
        should_call_llm=False,
        metadata={"profile_signal": "bio"},
    )


def _looks_like_spam_bio(value: str) -> bool:
    normalized = normalize_text(value)
    if not normalized:
        return False

    has_contact_or_link = bool(
        re.search(r"https?://|t\.me/|telegram|@\w{3,}", value, flags=re.IGNORECASE)
    )
    if not has_contact_or_link:
        return False

    hard_tokens = (
        "点击进群",
        "进群了解",
        "做单",
        "教程",
        "刷单",
        "返利",
        "日结",
        "兼职",
        "赚钱",
        "几分钟赚",
        "几百",
        "几千",
        "客服",
        "私聊",
        "裸聊",
        "看片",
        "反差",
        "破处",
        "博彩",
        "下注",
        "盘口",
        "空投",
        "代币",
    )
    return any(token in normalized for token in hard_tokens)
