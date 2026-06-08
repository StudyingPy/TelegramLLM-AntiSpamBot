from __future__ import annotations

import re

from .config import Settings
from .fingerprints import stable_hash
from .links import is_whitelisted_domain
from .models import DecisionAction, FingerprintRecord, LocalDecision, MessageFeatures
from .text import normalize_text


# stable_hash("") — any fingerprint stored under this value is a universal trap
# because every empty/whitespace/zero-width/emoji-only message produces the same hash.
# rules.py defends here as a second line in case feedback.py's write-side filter is
# bypassed (e.g. by data already in the DB from before the filter was added).
_EMPTY_TEXT_HASH = stable_hash("")


class RuleEngine:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def evaluate(
        self,
        features: MessageFeatures,
        fingerprint: FingerprintRecord | None = None,
    ) -> LocalDecision:
        # Drop any fingerprint that points at the empty-text sentinel hash. This is
        # the same guard feedback.fingerprint_lookup_values applies on the read path;
        # we duplicate it here so a stale DB entry (e.g. created before the write-side
        # filter existed) cannot leak through to produce a 95%-confidence BAN against
        # any empty/whitespace/emoji-only message.
        if fingerprint is not None and fingerprint.value == _EMPTY_TEXT_HASH:
            fingerprint = None

        fingerprint_decision = (
            self._evaluate_fingerprint(features, fingerprint) if fingerprint is not None else None
        )
        if fingerprint_decision is not None and fingerprint_decision.action == DecisionAction.BAN:
            return fingerprint_decision

        profile_spam = _profile_spam_decision(features)
        if profile_spam is not None:
            return profile_spam

        message_spam = _hard_spam_message_decision(features)
        if message_spam is not None:
            return message_spam

        preview_spam = _hard_spam_preview_decision(features)
        if preview_spam is not None:
            return preview_spam

        if fingerprint_decision is not None:
            return fingerprint_decision

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
        # Only the strictest fingerprint type (full normalized-content hash) is allowed
        # to escalate to BAN. Skeleton/phrase types over-generalize by construction,
        # so a single misclassified ad would otherwise auto-ban every later message
        # that shares its shape. Keep them at WITHDRAW_VOTE so the chat can dispute.
        ban_eligible_type = fingerprint.fingerprint_type == "content"

        if high_weight and ban_eligible_type and not high_rep:
            return LocalDecision(
                action=DecisionAction.BAN,
                reason=(
                    "known_high_weight_fingerprint_low_reputation"
                    if low_rep
                    else "known_high_weight_fingerprint"
                ),
                confidence=0.95,
                should_call_llm=False,
                metadata={
                    "fingerprint_id": fingerprint.id,
                    "fingerprint_type": fingerprint.fingerprint_type,
                },
            )

        if fingerprint.weight >= self._settings.fingerprint_review_weight:
            return LocalDecision(
                action=DecisionAction.WITHDRAW_VOTE,
                reason=(
                    "known_high_weight_fingerprint_generalized"
                    if high_weight
                    else "known_fingerprint"
                ),
                confidence=min(0.90, fingerprint.weight / 100),
                should_call_llm=False,
                metadata={
                    "fingerprint_id": fingerprint.id,
                    "fingerprint_type": fingerprint.fingerprint_type,
                },
            )

        return LocalDecision(
            action=DecisionAction.REVIEW,
            reason="weak_fingerprint_needs_llm",
            confidence=min(0.55, fingerprint.weight / 100),
            should_call_llm=True,
            metadata={
                "fingerprint_id": fingerprint.id,
                "fingerprint_type": fingerprint.fingerprint_type,
            },
        )

    def _has_non_whitelisted_link(self, features: MessageFeatures) -> bool:
        for domain in features.link_domains:
            if not is_whitelisted_domain(domain, self._settings.whitelist_domains):
                return True
        return False


# Hard signals split into two tiers:
#
# STRONG tokens almost only appear in ads / fraud / 引流 copy. A bio that mentions
# any of these alongside a contact carrier is a confident BAN — false positives here
# are rare enough that the trade-off is worth it.
#
# WEAK tokens (加群 / 客服 / 私聊 / 群一个 / 教程) DO appear in spam, but normal users
# also write "私聊我 @xxx", "进 X 群一起讨论", "这个教程有用 https://...". Banning on
# weak tokens in the BIO field has caused real false positives (e.g. a regular member
# whose bio was just "私聊 @kbXXXX 频道: https://t.me/..."). Weak tokens stay active for
# the message-body path (where the carrier + context is more reliable), but BIO matching
# is restricted to STRONG tokens only.
_STRONG_SPAM_TOKENS = (
    "拿码",
    "收钱",
    "做单",
    "刷单",
    "返利",
    "日结",
    "日入",
    "日赚",
    "稳赚",
    "兼职",
    "赚钱",
    "几分钟赚",
    "几百",
    "几千",
    "成人版",
    "成人片",
    "裸聊",
    "看片",
    "推特调教",
    "调教",
    "反差",
    "破处",
    "博彩",
    "下注",
    "盘口",
    "空投",
    "代币",
    "冼米",  # 洗码黑话变形,常见广告体
    "翻身",  # "新手来冼米翻身" 等
    "收米",  # 博彩黑话
)

_WEAK_SPAM_TOKENS = (
    "点击进群",
    "进群了解",
    "加群",
    "群一个",
    "教程",
    "客服",
    "私聊",
)

_ALL_SPAM_TOKENS = _STRONG_SPAM_TOKENS + _WEAK_SPAM_TOKENS


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


def _hard_spam_message_decision(features: MessageFeatures) -> LocalDecision | None:
    text = features.text
    if not _looks_like_hard_spam_text(text, has_carrier=_has_message_carrier(features)):
        return None

    return LocalDecision(
        action=DecisionAction.BAN,
        reason="hard_spam_message",
        confidence=0.96,
        should_call_llm=False,
        metadata={"local_signal": "message_text"},
    )


def _hard_spam_preview_decision(features: MessageFeatures) -> LocalDecision | None:
    preview = features.metadata.get("og_preview")
    if not isinstance(preview, dict):
        return None

    preview_text = " ".join(
        str(preview.get(key) or "")
        for key in ("title", "description", "site_name", "image_alt", "text")
    )
    if not _looks_like_hard_spam_text(preview_text, has_carrier=features.has_preview_url):
        return None

    return LocalDecision(
        action=DecisionAction.BAN,
        reason="hard_spam_link_preview",
        confidence=0.96,
        should_call_llm=False,
        metadata={"local_signal": "og_preview"},
    )


def _has_message_carrier(features: MessageFeatures) -> bool:
    text = features.text
    return bool(
        features.mention_count > 0
        or features.links
        or features.has_preview_url
        or re.search(r"@\w{3,}", text, flags=re.IGNORECASE)
        or re.search(r"\b\w{3,}bot\b", text, flags=re.IGNORECASE)
        or re.search(r"https?://|t\.me/", text, flags=re.IGNORECASE)
    )


def _looks_like_hard_spam_text(value: str, *, has_carrier: bool) -> bool:
    normalized = normalize_text(value)
    if not normalized or not has_carrier:
        return False

    return any(token in normalized for token in _ALL_SPAM_TOKENS)


def _looks_like_spam_bio(value: str) -> bool:
    normalized = normalize_text(value)
    if not normalized:
        return False

    has_contact_or_link = bool(
        re.search(r"https?://|t\.me/|telegram|@\w{3,}", value, flags=re.IGNORECASE)
    )
    if not has_contact_or_link:
        return False

    # Bio path uses only STRONG tokens — weak tokens like "私聊", "客服", "加群" appear
    # in normal users' bios too often to safely auto-ban.
    return any(token in normalized for token in _STRONG_SPAM_TOKENS)
