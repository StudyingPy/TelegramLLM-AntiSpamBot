from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from telegram_llm_antispam.config import Settings
from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.models import DecisionAction, FingerprintRecord, UserContext
from telegram_llm_antispam.rules import RuleEngine


def _settings() -> Settings:
    return Settings(
        bot_token=None,
        database_path=Path(":memory:"),
        log_level="INFO",
        admin_user_ids=(),
        admin_notify_user_ids=(),
        allowed_chat_ids=(),
        require_allowed_chat=True,
        whitelist_domains=("trusted.example",),
        vote_min_confirmations=3,
        vote_timeout_seconds=1800,
        vote_sweep_interval_seconds=60,
        low_reputation_threshold=35,
        high_reputation_threshold=80,
        reputation_ban_threshold=20,
        default_reputation=50,
        spam_reputation_penalty=35,
        ham_reputation_reward=8,
        repeat_window_seconds=300,
        repeat_min_distinct_senders=3,
        fingerprint_review_weight=40,
        fingerprint_ban_weight=85,
        llm_fingerprint_initial_weight=50,
        vote_confirmed_fingerprint_weight=85,
        fingerprint_false_positive_penalty=30,
        llm_review_threshold=0.70,
        llm_ban_threshold=0.85,
        newapi_base_url=None,
        newapi_api_key=None,
        newapi_model="gpt-5.4",
        newapi_timeout_seconds=8,
        newapi_temperature=0,
        newapi_max_tokens=600,
        preview_punctuation_confidence=0.82,
        new_user_link_confidence=0.72,
        og_fetch_enabled=True,
        og_short_text_max_chars=8,
        og_fetch_timeout_seconds=3,
        og_fetch_max_bytes=65536,
        og_fetch_max_text_chars=1200,
        og_fetch_max_redirects=3,
        profile_bio_fetch_enabled=True,
        profile_bio_cache_ttl_seconds=604800,
    )


def _message(text: str, preview_url: str | None = None):
    return SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(id=42),
        text=text,
        link_preview_options=SimpleNamespace(url=preview_url) if preview_url else None,
    )


def _features(text: str, preview_url: str | None = None, messages_seen: int = 0):
    context = UserContext(
        chat_id=-1001,
        user_id=42,
        reputation_score=50,
        messages_seen=messages_seen,
    )
    return build_message_features(_message(text, preview_url), context)


def test_punctuation_preview_goes_to_withdraw_vote():
    decision = RuleEngine(_settings()).evaluate(
        _features("...", "https://spam.example/card"),
    )

    assert decision.action == DecisionAction.WITHDRAW_VOTE
    assert decision.reason == "empty_or_punctuation_with_link_preview"
    assert decision.should_call_llm is True


def test_external_link_goes_to_llm_review_without_first_message_signal():
    decision = RuleEngine(_settings()).evaluate(
        _features("hello https://spam.example/a"),
    )

    assert decision.action == DecisionAction.REVIEW
    assert decision.reason == "link_message_needs_llm"
    assert decision.should_call_llm is True


def test_first_message_whitelisted_link_goes_to_llm_review():
    decision = RuleEngine(_settings()).evaluate(
        _features("hello https://docs.trusted.example/a"),
    )

    assert decision.action == DecisionAction.REVIEW
    assert decision.reason == "link_message_needs_llm"
    assert decision.should_call_llm is True


def test_unmatched_message_goes_to_llm_review():
    decision = RuleEngine(_settings()).evaluate(_features("hello everyone", messages_seen=3))

    assert decision.action == DecisionAction.REVIEW
    assert decision.reason == "unmatched_message_needs_llm"
    assert decision.should_call_llm is True


def test_high_weight_content_fingerprint_and_low_reputation_bans():
    """Content-type fingerprints are still allowed to escalate to BAN — they capture
    full-message-hash repeat, which is the strictest signal we have."""
    features = _features("hello")
    features = replace(features, sender_reputation=10)
    fingerprint = FingerprintRecord(
        id=1,
        fingerprint_type="content",
        value=features.content_hash,
        weight=90,
        hit_count=10,
        false_positive_count=0,
        source="vote_confirmed",
    )

    decision = RuleEngine(_settings()).evaluate(features, fingerprint=fingerprint)

    assert decision.action == DecisionAction.BAN
    assert decision.metadata["fingerprint_type"] == "content"


def test_high_weight_skeleton_fingerprint_only_votes_no_auto_ban():
    """Skeleton fingerprints over-generalize by construction — they capture shape, not
    content. A misclassified ad must not auto-ban every later message that shares the
    same shape; force vote escalation instead. This is the guard against the original
    skeleton-collapse incident.

    Uses a multi-token CJK message so the skeleton isn't classified as low-entropy —
    we want to test that BAN is downgraded to WITHDRAW_VOTE, not that low-entropy
    skeletons are dropped (a separate path tested below). The text is deliberately
    benign so no hard_spam_message rule fires."""
    features = _features("今天天气真不错 大家觉得呢")
    features = replace(features, sender_reputation=10)
    fingerprint = FingerprintRecord(
        id=1,
        fingerprint_type="skeleton",
        value=features.skeleton_hash,
        weight=90,
        hit_count=10,
        false_positive_count=0,
        source="vote_confirmed",
    )

    decision = RuleEngine(_settings()).evaluate(features, fingerprint=fingerprint)

    assert decision.action == DecisionAction.WITHDRAW_VOTE
    assert decision.reason == "known_high_weight_fingerprint_generalized"
    assert decision.metadata["fingerprint_type"] == "skeleton"


def test_high_weight_phrase_fingerprint_only_votes_no_auto_ban():
    """Same guard as skeleton — phrase fingerprints are extracted from n-grams and
    over-match by design."""
    features = _features("hello")
    features = replace(features, sender_reputation=10)
    fingerprint = FingerprintRecord(
        id=1,
        fingerprint_type="phrase",
        value="phrase-hash",
        weight=90,
        hit_count=10,
        false_positive_count=0,
        source="llm_spam_phrase",
    )

    decision = RuleEngine(_settings()).evaluate(features, fingerprint=fingerprint)

    assert decision.action == DecisionAction.WITHDRAW_VOTE
    assert decision.metadata["fingerprint_type"] == "phrase"


def test_high_weight_fingerprint_and_normal_reputation_bans_without_vote():
    features = _features("不稳不推 来这里几分钟赚几百 @baurpc", messages_seen=2)
    fingerprint = FingerprintRecord(
        id=1,
        fingerprint_type="content",
        value=features.content_hash,
        weight=85,
        hit_count=3,
        false_positive_count=0,
        source="vote_confirmed",
    )

    decision = RuleEngine(_settings()).evaluate(features, fingerprint=fingerprint)

    assert decision.action == DecisionAction.BAN
    assert decision.reason == "known_high_weight_fingerprint"


def test_obvious_spam_bio_bans_even_when_message_text_is_benign():
    features = _features("签到", messages_seen=0)
    features.metadata["sender_profile"] = {
        "display_name": "Snsb",
        "username": None,
        "bio": "https://t.me/+LmgOTZ_i-G00ODFk 点击进群了解详细做单教程",
    }

    decision = RuleEngine(_settings()).evaluate(features)

    assert decision.action == DecisionAction.BAN
    assert decision.reason == "spam_profile_bio"


def test_riru_guowan_bio_with_invite_link_bans_via_local_rules():
    """Regression: the bio '轻松日入过万： https://t.me/+...' was leaking through to
    the LLM hop (and then through to no decision when the LLM failed). The hard-token
    table was missing the 日入/日赚 family. Without that, '轻松日入过万' has no token
    overlap with our bio rule, so _looks_like_spam_bio() returns False even though
    the bio is a textbook引流 line.
    """
    features = _features("1", messages_seen=0)
    features.metadata["sender_profile"] = {
        "display_name": "郦天林",
        "username": None,
        "bio": "轻松日入过万： https://t.me/+WkKhZXWQHe0yM2Vk",
    }

    decision = RuleEngine(_settings()).evaluate(features)

    assert decision.action == DecisionAction.BAN
    assert decision.reason == "spam_profile_bio"


def test_weak_token_in_bio_does_not_auto_ban():
    """Regression: '私聊 @akondesebot 频道: https://t.me/zmkh5' was auto-banned by the
    local bio rule because '私聊' was in the hard-token list. But '私聊我 @xxx' is a
    common pattern in normal users' bios. Weak tokens are now restricted to the message
    body path; bio matching only uses strong tokens (日入/做单/拿码/...) where false
    positives are rare.
    """
    features = _features("(空消息)", messages_seen=0)
    features.metadata["sender_profile"] = {
        "display_name": "浮生",
        "username": "kb8567",
        "bio": "私聊 @akondesebot 频道:https://t.me/zmkh5",
    }

    decision = RuleEngine(_settings()).evaluate(features)

    # No spam_profile_bio ban. The decision should fall through to the LLM hop or
    # general review (we don't pin the exact reason — just that local rules didn't
    # auto-ban this borderline case).
    assert decision.action != DecisionAction.BAN, (
        f"weak-token bio must not auto-ban; got {decision.action.value} / {decision.reason}"
    )


def test_strong_token_in_bio_still_auto_bans():
    """Bio matching is tightened, NOT disabled. Bios that mention strong tokens
    (做单/拿码/日入/...) alongside a contact carrier still ban locally — these
    almost never appear in legitimate user bios."""

    features = _features("签到", messages_seen=0)
    features.metadata["sender_profile"] = {
        "display_name": "X",
        "username": None,
        "bio": "做单拿码 @bossbot https://t.me/+work",
    }

    decision = RuleEngine(_settings()).evaluate(features)

    assert decision.action == DecisionAction.BAN
    assert decision.reason == "spam_profile_bio"


def test_weak_token_in_message_body_still_bans():
    """The weak/strong split applies only to the BIO path. In message body the carrier
    is the message itself, contextual signals are stronger, and our existing local rule
    still bans '加群一个20' / '客服私聊@bot 拿码' / etc."""

    decision = RuleEngine(_settings()).evaluate(_features("@qunji2bot   加群一个20"))

    assert decision.action == DecisionAction.BAN
    assert decision.reason == "hard_spam_message"


def test_empty_text_hash_fingerprint_is_ignored_even_if_present_in_db():
    """Critical regression: stable_hash('') is the same hash for every empty /
    whitespace-only / zero-width-only / emoji-only / digit-only message. Production
    saw vote_confirmed feedback ingest a spam whose normalized clean_text was empty,
    upgrading the empty-text hash to content-type weight 85. After that, any user
    posting a sticker, voice note, or photo without a caption got auto-banned at 95%.

    Defense in depth: even if a stale poisoned row remains in the DB, RuleEngine must
    discard the fingerprint and fall through to the regular hop instead of banning.
    """
    from telegram_llm_antispam.fingerprints import stable_hash

    features = _features("", messages_seen=0)  # empty body, no profile
    assert features.content_hash == stable_hash("")
    assert features.skeleton_hash == stable_hash("")

    poisoned = FingerprintRecord(
        id=999,
        fingerprint_type="content",
        value=stable_hash(""),
        weight=95,
        hit_count=100,
        false_positive_count=0,
        source="vote_confirmed",
    )

    decision = RuleEngine(_settings()).evaluate(features, fingerprint=poisoned)

    assert decision.action != DecisionAction.BAN, (
        f"empty-text fingerprint must not auto-ban; got {decision.action.value}"
    )


def test_low_entropy_skeleton_fingerprint_is_ignored_even_if_present_in_db():
    """Same class of bug as the empty-text hash, one level up: a fingerprint stored
    under stable_hash('<url>') matches every bare-URL message. A vote-confirmed
    URL-only spam would otherwise upgrade this hash to weight 85 and trigger
    WITHDRAW_VOTE on every legitimate URL share. Defense in depth: even if a stale
    poisoned row remains in DB, RuleEngine must drop the fingerprint.
    """
    from telegram_llm_antispam.fingerprints import stable_hash

    features = _features("https://github.com/torvalds/linux", messages_seen=2)
    assert features.skeleton == "<url>"

    poisoned = FingerprintRecord(
        id=1000,
        fingerprint_type="skeleton",
        value=stable_hash("<url>"),
        weight=95,
        hit_count=50,
        false_positive_count=0,
        source="vote_confirmed",
    )

    decision = RuleEngine(_settings()).evaluate(features, fingerprint=poisoned)

    assert decision.action not in {DecisionAction.BAN, DecisionAction.WITHDRAW_VOTE}, (
        f"low-entropy skeleton sentinel must not enforce; got {decision.action.value}"
    )


def test_bot_contact_plus_join_offer_bans_without_llm():
    decision = RuleEngine(_settings()).evaluate(_features("@qunji2bot   加群一个20"))

    assert decision.action == DecisionAction.BAN
    assert decision.reason == "hard_spam_message"
    assert decision.should_call_llm is False


def test_bot_contact_plus_payment_code_bans_without_llm():
    decision = RuleEngine(_settings()).evaluate(_features("@daishx1bot   拿码收钱来"))

    assert decision.action == DecisionAction.BAN
    assert decision.reason == "hard_spam_message"
    assert decision.should_call_llm is False


def test_hard_spam_message_overrides_review_weight_fingerprint():
    features = _features("@daishx1bot   拿码收钱来")
    fingerprint = FingerprintRecord(
        id=1,
        fingerprint_type="skeleton",
        value=features.skeleton_hash,
        weight=45,
        hit_count=1,
        false_positive_count=0,
        source="llm",
    )

    decision = RuleEngine(_settings()).evaluate(features, fingerprint=fingerprint)

    assert decision.action == DecisionAction.BAN
    assert decision.reason == "hard_spam_message"


def test_spam_og_preview_bans_without_waiting_for_vote():
    features = _features("! # $ % ^ & * ( ) _ +", "https://t.me/spam_preview")
    features.metadata["og_preview"] = {
        "title": "CRTV成人版",
        "description": "推特调教大神 反差女大母狗",
        "text": "看片就选择CRTV 什么片都能看都能搜 视频已更新方",
    }

    decision = RuleEngine(_settings()).evaluate(features)

    assert decision.action == DecisionAction.BAN
    assert decision.reason == "hard_spam_link_preview"
    assert decision.should_call_llm is False
