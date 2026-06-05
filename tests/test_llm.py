from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from telegram_llm_antispam.config import Settings
from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.llm import (
    _chat_completions_url,
    _feature_payload,
    _loads_json_object,
    _parse_judgement,
    decision_from_llm,
)
from telegram_llm_antispam.models import DecisionAction, LLMJudgement, UserContext


def _settings() -> Settings:
    return Settings(
        bot_token=None,
        database_path=Path(":memory:"),
        log_level="INFO",
        whitelist_domains=(),
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
        llm_ban_threshold=0.92,
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


def test_chat_completions_url_accepts_host_or_v1():
    assert _chat_completions_url("https://api.example") == (
        "https://api.example/v1/chat/completions"
    )
    assert _chat_completions_url("https://api.example/v1") == (
        "https://api.example/v1/chat/completions"
    )
    assert _chat_completions_url("https://api.example/v1/chat/completions") == (
        "https://api.example/v1/chat/completions"
    )


def test_loads_json_object_handles_fenced_json():
    data = _loads_json_object(
        '```json\n{"is_spam": true, "confidence": 0.8, "signal_phrases": ["外链"]}\n```'
    )

    assert data["is_spam"] is True
    assert data["confidence"] == 0.8


def test_parse_judgement_clamps_confidence_and_phrases():
    judgement = _parse_judgement(
        {
            "is_spam": True,
            "confidence": 1.4,
            "category": "ads",
            "skeleton_hash": "abc",
            "signal_phrases": ["加群", "返利"],
        }
    )

    assert judgement.is_spam is True
    assert judgement.confidence == 1.0
    assert judgement.signal_phrases == ("加群", "返利")


def test_feature_payload_contains_link_sources():
    message = SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(id=42),
        text="hello https://spam.example/a",
    )
    context = UserContext(chat_id=-1001, user_id=42, reputation_score=50, messages_seen=1)
    features = build_message_features(message, context)

    payload = _feature_payload(features)

    assert payload["link_domains"] == ["spam.example"]
    assert payload["links"] == [
        {"source": "text", "domain": "spam.example", "url": "https://spam.example/a"}
    ]


def test_feature_payload_contains_og_preview_metadata():
    message = SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(id=42),
        text=".",
        link_preview_options=SimpleNamespace(url="https://spam.example/card"),
    )
    context = UserContext(chat_id=-1001, user_id=42, reputation_score=50, messages_seen=0)
    features = build_message_features(message, context)
    features.metadata["og_preview"] = {"title": "CRTV成人版", "text": "看片就选择CRTV"}

    payload = _feature_payload(features)

    assert payload["og_preview"] == {"title": "CRTV成人版", "text": "看片就选择CRTV"}


def test_feature_payload_contains_sender_profile_metadata():
    message = SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(id=42),
        text="hello",
    )
    context = UserContext(chat_id=-1001, user_id=42, reputation_score=50, messages_seen=1)
    features = build_message_features(message, context)
    features.metadata["sender_profile"] = {
        "username": "promo_agent",
        "display_name": "成人客服",
        "bio": "看片加群",
    }

    payload = _feature_payload(features)

    assert payload["sender_profile"] == {
        "username": "promo_agent",
        "display_name": "成人客服",
        "bio": "看片加群",
    }


def test_llm_spam_high_confidence_goes_to_withdraw_vote_for_normal_reputation():
    message = SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(id=42),
        text="join https://spam.example",
    )
    context = UserContext(chat_id=-1001, user_id=42, reputation_score=50, messages_seen=1)
    features = build_message_features(message, context)

    decision = decision_from_llm(
        LLMJudgement(is_spam=True, confidence=0.91, category="ads"),
        features,
        _settings(),
    )

    assert decision.action == DecisionAction.WITHDRAW_VOTE
    assert decision.reason == "llm_spam"
