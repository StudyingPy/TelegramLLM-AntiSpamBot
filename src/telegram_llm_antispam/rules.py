from __future__ import annotations

from .config import Settings
from .links import is_whitelisted_domain
from .models import DecisionAction, FingerprintRecord, LocalDecision, MessageFeatures


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

        if high_weight and low_rep and not high_rep:
            return LocalDecision(
                action=DecisionAction.BAN,
                reason="known_high_weight_fingerprint_low_reputation",
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
