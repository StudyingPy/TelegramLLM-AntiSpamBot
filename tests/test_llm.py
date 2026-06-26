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
    _normalize_base_url,
    _parse_judgement,
    decision_from_llm,
)
from telegram_llm_antispam.models import DecisionAction, LLMJudgement, LLMOutcomeStatus, UserContext


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
        whitelisted_user_ids=(),
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
        fingerprint_hit_weight_increment=5,
        fingerprint_hit_weight_cap=80,
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


def test_normalize_base_url_accepts_host_or_v1_or_legacy_full_path():
    """We migrated from urllib (which needed the full /v1/chat/completions URL) to the
    OpenAI SDK (which wants the /v1 root and appends the rest itself). Operators have
    historically configured NEWAPI_BASE_URL in all three forms — accept all three.
    The legacy alias _chat_completions_url is kept pointing at the same function so
    no external import breaks.
    """

    assert _normalize_base_url("https://api.example") == "https://api.example/v1"
    assert _normalize_base_url("https://api.example/v1") == "https://api.example/v1"
    assert _normalize_base_url("https://api.example/v1/chat/completions") == (
        "https://api.example/v1"
    )
    # Legacy alias works the same — guards against external callers / older tests.
    assert _chat_completions_url("https://api.example") == "https://api.example/v1"


def test_newapi_providers_support_multiple_urls_keys_and_models():
    settings = replace(
        _settings(),
        newapi_base_url="https://api-a.example,https://api-b.example/v1",
        newapi_api_key="key-a,key-b",
        newapi_model="model-a,model-b",
    )

    providers = _newapi_providers_from_settings(settings)

    assert [(item.base_url, item.api_key, item.model) for item in providers] == [
        ("https://api-a.example/v1", "key-a", "model-a"),
        ("https://api-b.example/v1", "key-b", "model-b"),
    ]


def test_newapi_provider_parser_reuses_single_key_and_model():
    settings = replace(
        _settings(),
        newapi_base_url="https://api-a.example,https://api-b.example",
        newapi_api_key="shared-key",
        newapi_model="shared-model",
    )

    providers = _newapi_providers_from_settings(settings)

    assert [(item.base_url, item.api_key, item.model) for item in providers] == [
        ("https://api-a.example/v1", "shared-key", "shared-model"),
        ("https://api-b.example/v1", "shared-key", "shared-model"),
    ]


def _fake_completion_response(*, content: str) -> SimpleNamespace:
    """Build a stand-in for openai.types.chat.ChatCompletion.

    The SDK returns an object exposing .choices[0].message.content. Our extractor
    falls back to dict-style access, but using attribute access here exercises the
    real-shape path so we don't accidentally regress to dict-only support.
    """

    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


class _FakeCompletions:
    """Mimics `client.chat.completions` with a single async create() method."""

    def __init__(self, behavior):
        self._behavior = behavior
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return await self._behavior(kwargs)


class _FakeAsyncOpenAI:
    """Minimal AsyncOpenAI replacement: enough surface for NewAPIJudge to work."""

    def __init__(self, behavior):
        self.chat = SimpleNamespace(completions=_FakeCompletions(behavior))
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_newapi_judge_falls_back_to_next_provider_after_error():
    """When the first provider raises, the second one is consulted. The SDK's
    APIConnectionError is the canonical "transport blew up" exception we want to
    fall through; we use it here instead of OSError so the test exercises the
    actual except branch the production code will hit."""

    from openai import APIConnectionError

    settings = replace(
        _settings(),
        newapi_base_url="https://api-a.example,https://api-b.example",
        newapi_api_key="key-a,key-b",
        newapi_model="model-a,model-b",
    )

    class FallbackJudge(NewAPIJudge):
        def __init__(self, settings):
            super().__init__(settings)
            self.called_base_urls: list[str] = []
            self.fake_clients: list[_FakeAsyncOpenAI] = []

        def _build_client(self, provider):  # noqa: ANN001 - matches parent signature
            self.called_base_urls.append(provider.base_url)
            call_index = len(self.called_base_urls)

            async def behavior(_kwargs):
                if call_index == 1:
                    raise APIConnectionError(request=None)
                return _fake_completion_response(
                    content=(
                        '{"is_spam": true, "confidence": 0.93, '
                        '"category": "ads", "signal_phrases": ["赚钱"]}'
                    )
                )

            client = _FakeAsyncOpenAI(behavior)
            self.fake_clients.append(client)
            return client

    message = SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(id=42),
        text="来这里几分钟赚几百 @baurpc",
    )
    context = UserContext(chat_id=-1001, user_id=42, reputation_score=50, messages_seen=1)
    features = build_message_features(message, context)
    judge = FallbackJudge(settings)

    outcome = asyncio.run(judge.judge(features))

    assert outcome.status == LLMOutcomeStatus.OK
    assert outcome.judgement is not None
    assert outcome.judgement.is_spam is True
    assert outcome.judgement.confidence == 0.93
    assert outcome.provider_count == 2
    assert judge.called_base_urls == [
        "https://api-a.example/v1",
        "https://api-b.example/v1",
    ]
    # Both clients must have been closed — leaking httpx connections in a long-
    # running bot adds up.
    assert all(c.closed for c in judge.fake_clients)


def test_newapi_judge_returns_failed_outcome_when_all_providers_fail():
    """When every provider raises, the outcome carries FAILED + the last error
    string, and never raises out of judge()."""

    from openai import APIConnectionError

    settings = replace(
        _settings(),
        newapi_base_url="https://api-a.example,https://api-b.example",
        newapi_api_key="key-a,key-b",
        newapi_model="model-a,model-b",
    )

    class AlwaysFailJudge(NewAPIJudge):
        def _build_client(self, provider):  # noqa: ANN001
            async def behavior(_kwargs):
                raise APIConnectionError(request=None)

            return _FakeAsyncOpenAI(behavior)

    message = SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(id=42),
        text="hi",
    )
    context = UserContext(chat_id=-1001, user_id=42, reputation_score=50, messages_seen=1)
    features = build_message_features(message, context)
    judge = AlwaysFailJudge(settings)

    outcome = asyncio.run(judge.judge(features))

    assert outcome.status == LLMOutcomeStatus.FAILED
    assert outcome.provider_count == 2
    assert outcome.judgement is None
    assert outcome.error is not None
    assert "connection_error" in outcome.error


def test_newapi_judge_reports_sdk_timeout_with_elapsed():
    """APITimeoutError gets its own branch so admin notifications can distinguish
    'we hit the configured limit' from 'connection blew up immediately'."""

    from openai import APITimeoutError

    settings = replace(
        _settings(),
        newapi_base_url="https://api-a.example",
        newapi_api_key="key-a",
        newapi_model="model-a",
        newapi_timeout_seconds=2,
    )

    class TimingOutJudge(NewAPIJudge):
        def _build_client(self, provider):  # noqa: ANN001
            async def behavior(_kwargs):
                raise APITimeoutError(request=None)

            return _FakeAsyncOpenAI(behavior)

    message = SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(id=42),
        text="hi",
    )
    context = UserContext(chat_id=-1001, user_id=42, reputation_score=50, messages_seen=1)
    features = build_message_features(message, context)
    judge = TimingOutJudge(settings)

    outcome = asyncio.run(judge.judge(features))

    assert outcome.status == LLMOutcomeStatus.FAILED
    assert outcome.error is not None
    assert "timeout after" in outcome.error
    assert "limit 2.0s" in outcome.error


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
