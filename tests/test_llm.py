from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from telegram_llm_antispam.config import Settings
from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.llm import (
    NewAPIJudge,
    _chat_completions_url,
    _feature_payload,
    _loads_json_object,
    _newapi_providers_from_settings,
    _parse_judgement,
    decision_from_llm,
)
from telegram_llm_antispam.models import DecisionAction, LLMJudgement, UserContext


def _settings() -> Settings:
    return Settings(
        bot_token=None,
        database_path=Path(":memory:"),
        log_level="INFO",
        admin_user_ids=(),
        admin_notify_user_ids=(),
        allowed_chat_ids=(),
        require_allowed_chat=True,
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


def test_newapi_providers_support_multiple_urls_keys_and_models():
    settings = replace(
        _settings(),
        newapi_base_url="https://api-a.example,https://api-b.example/v1",
        newapi_api_key="key-a,key-b",
        newapi_model="model-a,model-b",
    )

    providers = _newapi_providers_from_settings(settings)

    assert [(item.endpoint, item.api_key, item.model) for item in providers] == [
        ("https://api-a.example/v1/chat/completions", "key-a", "model-a"),
        ("https://api-b.example/v1/chat/completions", "key-b", "model-b"),
    ]


def test_newapi_provider_parser_reuses_single_key_and_model():
    settings = replace(
        _settings(),
        newapi_base_url="https://api-a.example,https://api-b.example",
        newapi_api_key="shared-key",
        newapi_model="shared-model",
    )

    providers = _newapi_providers_from_settings(settings)

    assert [(item.endpoint, item.api_key, item.model) for item in providers] == [
        ("https://api-a.example/v1/chat/completions", "shared-key", "shared-model"),
        ("https://api-b.example/v1/chat/completions", "shared-key", "shared-model"),
    ]


def test_newapi_judge_falls_back_to_next_provider_after_error():
    class FallbackJudge(NewAPIJudge):
        def __init__(self, settings: Settings) -> None:
            super().__init__(settings)
            self.called_endpoints: list[str] = []

        def _post_chat_completion(self, provider, payload):  # noqa: ANN001
            self.called_endpoints.append(provider.endpoint)
            if len(self.called_endpoints) == 1:
                raise OSError("primary failed")
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"is_spam": true, "confidence": 0.93, '
                                '"category": "ads", "signal_phrases": ["赚钱"]}'
                            )
                        }
                    }
                ]
            }

    settings = replace(
        _settings(),
        newapi_base_url="https://api-a.example,https://api-b.example",
        newapi_api_key="key-a,key-b",
        newapi_model="model-a,model-b",
    )
    message = SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(id=42),
        text="来这里几分钟赚几百 @baurpc",
    )
    context = UserContext(chat_id=-1001, user_id=42, reputation_score=50, messages_seen=1)
    features = build_message_features(message, context)
    judge = FallbackJudge(settings)

    judgement = asyncio.run(judge.judge(features))

    assert judgement is not None
    assert judgement.is_spam is True
    assert judgement.confidence == 0.93
    assert judge.called_endpoints == [
        "https://api-a.example/v1/chat/completions",
        "https://api-b.example/v1/chat/completions",
    ]


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


def test_llm_spam_at_ban_threshold_bans_for_normal_reputation():
    message = SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(id=42),
        text="join https://spam.example",
    )
    context = UserContext(chat_id=-1001, user_id=42, reputation_score=50, messages_seen=1)
    features = build_message_features(message, context)

    decision = decision_from_llm(
        LLMJudgement(is_spam=True, confidence=0.87, category="ads"),
        features,
        _settings(),
    )

    assert decision.action == DecisionAction.BAN
    assert decision.reason == "llm_spam_high_confidence"


def test_llm_spam_at_ban_threshold_still_votes_for_high_reputation():
    message = SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(id=42),
        text="join https://spam.example",
    )
    context = UserContext(chat_id=-1001, user_id=42, reputation_score=85, messages_seen=30)
    features = build_message_features(message, context)

    decision = decision_from_llm(
        LLMJudgement(is_spam=True, confidence=0.87, category="ads"),
        features,
        _settings(),
    )

    assert decision.action == DecisionAction.WITHDRAW_VOTE
    assert decision.reason == "llm_spam"


def test_llm_high_confidence_with_profile_and_content_signals_bans():
    message = SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(id=42),
        text="做洗钱的来 一小时两千 @mkoplcn",
    )
    context = UserContext(chat_id=-1001, user_id=42, reputation_score=50, messages_seen=3)
    features = build_message_features(message, context)
    features.metadata["sender_profile"] = {
        "username": None,
        "display_name": "Nicole Hernandez",
        "bio": "Fill good skill ago happy message.",
    }

    decision = decision_from_llm(
        LLMJudgement(
            is_spam=True,
            confidence=0.98,
            category="scam",
            signal_phrases=("做洗钱的来", "一小时两千", "@mkoplcn"),
        ),
        features,
        _settings(),
    )

    assert decision.action == DecisionAction.BAN
    assert decision.reason == "llm_spam_high_confidence"
