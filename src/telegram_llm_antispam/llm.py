from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
)

from .config import Settings
from .models import (
    DecisionAction,
    LLMJudgement,
    LLMOutcome,
    LLMOutcomeStatus,
    LocalDecision,
    MessageFeatures,
)


logger = logging.getLogger(__name__)


class LLMJudge(Protocol):
    async def judge(self, features: MessageFeatures) -> LLMOutcome:
        """Return an always-on outcome describing the LLM hop.

        Implementations must never raise — transport, timeout, and parse errors are
        captured into LLMOutcome(status=FAILED, error=...). Callers can rely on the
        outcome to annotate decisions for observability even when no judgement was
        produced.
        """


class NullLLMJudge:
    async def judge(self, features: MessageFeatures) -> LLMOutcome:
        logger.debug("LLM judge is not configured; falling back to local decision only")
        return LLMOutcome(status=LLMOutcomeStatus.DISABLED, provider_count=0)


@dataclass(frozen=True, slots=True)
class NewAPIProvider:
    """A single OpenAI-compatible NewAPI endpoint configuration.

    `base_url` is the OpenAI-style root (e.g. "https://api.example/v1"). The SDK
    handles the "/chat/completions" suffix internally — we pass base_url straight
    through to AsyncOpenAI without our old urljoin-based normalization.
    """

    base_url: str
    api_key: str
    model: str


class NewAPIJudge:
    """OpenAI-SDK-backed implementation. Sibling project Telegram-PresenceD/vision.py
    proved this exact shape (AsyncOpenAI + base_url + per-call client + close in
    finally) is rock-solid against the same NewAPI host that our old urllib path
    was timing out against. Confirmed root cause was urllib's blocking getaddrinfo
    plus asyncio.to_thread + wait_for layering: the SDK uses httpx + async DNS
    natively and avoids both pitfalls.
    """

    def __init__(self, settings: Settings) -> None:
        providers = _newapi_providers_from_settings(settings)
        if not providers:
            raise ValueError("At least one NewAPI base URL and API key are required")

        self._providers = providers
        self._timeout = settings.newapi_timeout_seconds
        self._temperature = settings.newapi_temperature
        self._max_tokens = settings.newapi_max_tokens

    async def judge(self, features: MessageFeatures) -> LLMOutcome:
        feature_payload = _feature_payload(features)
        last_error: str | None = None
        for index, provider in enumerate(self._providers, start=1):
            judgement, error = await self._judge_with_provider(provider, feature_payload, index)
            if judgement is not None:
                return LLMOutcome(
                    status=LLMOutcomeStatus.OK,
                    provider_count=len(self._providers),
                    judgement=judgement,
                )
            if error is not None:
                last_error = error

        logger.warning(
            "All %s NewAPI provider(s) failed; using local fallback", len(self._providers)
        )
        return LLMOutcome(
            status=LLMOutcomeStatus.FAILED,
            provider_count=len(self._providers),
            error=last_error,
        )

    async def _judge_with_provider(
        self,
        provider: NewAPIProvider,
        feature_payload: dict[str, object],
        index: int,
    ) -> tuple[LLMJudgement | None, str | None]:
        # Per-call client mirrors Telegram-PresenceD vision.py: small create+close cost
        # in exchange for sidestepping every long-lived-session pitfall (stale
        # connections, DNS cache poisoning, pool exhaustion). Reverse-cost-benefit
        # for our QPS is wildly in favor of per-call.
        client = self._build_client(provider)
        messages = _chat_messages(feature_payload)
        start = time.monotonic()
        try:
            response = await client.chat.completions.create(
                model=provider.model,
                messages=messages,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                response_format={"type": "json_object"},
                timeout=self._timeout,
            )
        except APITimeoutError as exc:
            elapsed = time.monotonic() - start
            logger.warning(
                "NewAPI provider %s timed out after %.1fs: %s",
                index,
                elapsed,
                provider.base_url,
            )
            await _safe_close(client)
            return None, f"timeout after {elapsed:.1f}s (limit {self._timeout:.1f}s)"
        except APIConnectionError as exc:
            elapsed = time.monotonic() - start
            logger.warning(
                "NewAPI provider %s connection error after %.1fs (%s): %s",
                index,
                elapsed,
                provider.base_url,
                exc,
            )
            await _safe_close(client)
            return None, f"connection_error after {elapsed:.1f}s: {exc}"
        except OpenAIError as exc:
            elapsed = time.monotonic() - start
            logger.warning(
                "NewAPI provider %s API error after %.1fs (%s): %s",
                index,
                elapsed,
                provider.base_url,
                exc,
            )
            await _safe_close(client)
            return None, f"api_error after {elapsed:.1f}s: {type(exc).__name__}: {exc}"
        except Exception as exc:  # defense in depth: SDK must never crash the bot loop
            elapsed = time.monotonic() - start
            logger.warning(
                "NewAPI provider %s unexpected error after %.1fs (%s): %s",
                index,
                elapsed,
                provider.base_url,
                exc,
            )
            await _safe_close(client)
            return None, f"unexpected_error after {elapsed:.1f}s: {type(exc).__name__}: {exc}"

        content = _extract_message_content(response)
        await _safe_close(client)

        if not content:
            logger.warning(
                "NewAPI provider %s returned no message content: %s",
                index,
                provider.base_url,
            )
            return None, "empty content"

        try:
            data = _loads_json_object(content)
            return _parse_judgement(data), None
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "NewAPI provider %s returned invalid judgement JSON (%s): %s",
                index,
                provider.base_url,
                exc,
            )
            return None, f"parse_error: {type(exc).__name__}: {exc}"

    def _build_client(self, provider: NewAPIProvider) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=provider.api_key,
            base_url=provider.base_url,
            timeout=self._timeout,
            max_retries=0,  # we do our own retry across providers; SDK retry adds latency we'd misattribute
        )


async def _safe_close(client: AsyncOpenAI) -> None:
    try:
        await client.close()
    except Exception:  # pragma: no cover - close() failures are best-effort cleanup
        logger.debug("AsyncOpenAI.close() failed", exc_info=True)


def decision_from_llm(
    judgement: LLMJudgement,
    features: MessageFeatures,
    settings: Settings,
) -> LocalDecision:
    if not judgement.is_spam:
        return LocalDecision(
            action=DecisionAction.ALLOW,
            reason="llm_not_spam",
            confidence=judgement.confidence,
            should_call_llm=False,
            metadata={"category": judgement.category},
        )

    low_rep = features.sender_reputation <= settings.reputation_ban_threshold
    high_rep = features.sender_reputation >= settings.high_reputation_threshold
    should_ban = judgement.confidence >= settings.llm_ban_threshold and not high_rep
    if should_ban:
        return LocalDecision(
            action=DecisionAction.BAN,
            reason=(
                "llm_spam_high_confidence_low_reputation"
                if low_rep
                else "llm_spam_high_confidence"
            ),
            confidence=judgement.confidence,
            should_call_llm=False,
            metadata={
                "category": judgement.category,
                "signal_phrases": list(judgement.signal_phrases),
            },
        )

    if judgement.confidence >= settings.llm_review_threshold:
        return LocalDecision(
            action=DecisionAction.WITHDRAW_VOTE,
            reason="llm_spam",
            confidence=judgement.confidence,
            should_call_llm=False,
            metadata={
                "category": judgement.category,
                "signal_phrases": list(judgement.signal_phrases),
            },
        )

    return LocalDecision(
        action=DecisionAction.REVIEW,
        reason="llm_low_confidence",
        confidence=judgement.confidence,
        should_call_llm=False,
        metadata={"category": judgement.category},
    )


def create_llm_judge(settings: Settings) -> LLMJudge:
    if settings.has_newapi:
        providers = _newapi_providers_from_settings(settings)
        if providers:
            logger.info("NewAPI LLM judge enabled with %s provider(s)", len(providers))
            return NewAPIJudge(settings)
    return NullLLMJudge()


def newapi_provider_count(settings: Settings) -> int:
    return len(_newapi_providers_from_settings(settings))


def _normalize_base_url(base_url: str) -> str:
    """Return an AsyncOpenAI-compatible base_url.

    AsyncOpenAI expects the OpenAI-style "/v1" root (it appends "/chat/completions"
    itself). Users may configure NEWAPI_BASE_URL as any of:
      - "https://api.example"                               (host only)
      - "https://api.example/v1"                            (v1 root)
      - "https://api.example/v1/chat/completions"           (legacy full path)
    Normalize all three to the /v1 form so the SDK appends correctly.
    """

    stripped = base_url.rstrip("/")
    parsed = urlparse(stripped)
    if not parsed.scheme or not parsed.netloc:
        return stripped  # let the SDK surface the error rather than guessing

    if stripped.endswith("/chat/completions"):
        stripped = stripped[: -len("/chat/completions")]
    if stripped.endswith("/v1"):
        return stripped
    return f"{stripped}/v1"


# Backwards-compatible alias so any external call sites / tests referencing the old
# helper name (which previously returned the full /v1/chat/completions URL) still
# work, but with the new semantics. We re-export under both names to keep churn small.
_chat_completions_url = _normalize_base_url


def _newapi_providers_from_settings(settings: Settings) -> tuple[NewAPIProvider, ...]:
    base_urls = _split_config_values(settings.newapi_base_url)
    api_keys = _split_config_values(settings.newapi_api_key)
    models = _split_config_values(settings.newapi_model) or ("gpt-5.4",)
    if not base_urls or not api_keys:
        return ()

    provider_count = max(len(base_urls), len(api_keys), len(models))
    providers: list[NewAPIProvider] = []
    for index in range(provider_count):
        base_url = _value_at_or_single(base_urls, index)
        api_key = _value_at_or_single(api_keys, index)
        model = _value_at_or_single(models, index)
        if base_url is None or api_key is None or model is None:
            logger.warning(
                "Skipping incomplete NewAPI provider at position %s "
                "(base_urls=%s, api_keys=%s, models=%s)",
                index + 1,
                len(base_urls),
                len(api_keys),
                len(models),
            )
            continue
        providers.append(
            NewAPIProvider(
                base_url=_normalize_base_url(base_url),
                api_key=api_key,
                model=model,
            )
        )
    return tuple(providers)


def _split_config_values(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _value_at_or_single(values: tuple[str, ...], index: int) -> str | None:
    if len(values) == 1:
        return values[0]
    if index < len(values):
        return values[index]
    return None


def _chat_messages(feature_payload: dict[str, object]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                feature_payload, ensure_ascii=False, separators=(",", ":")
            ),
        },
    ]


def _feature_payload(features: MessageFeatures) -> dict[str, object]:
    return {
        "task": "judge_telegram_group_spam",
        "raw_text": features.text[:4000],
        "clean_text": features.clean_text[:4000],
        "skeleton": features.skeleton[:1000],
        "skeleton_hash": features.skeleton_hash,
        "content_hash": features.content_hash,
        "sender_reputation": features.sender_reputation,
        "mention_count": features.mention_count,
        "is_empty_or_punctuation": features.is_empty_or_punctuation,
        "has_preview_url": features.has_preview_url,
        "link_domains": list(features.link_domains),
        "links": [
            {"source": link.source, "domain": link.domain, "url": link.url[:500]}
            for link in features.links
        ],
        "sender_profile": features.metadata.get("sender_profile"),
        "og_preview": features.metadata.get("og_preview"),
    }


def _extract_message_content(response: object) -> str | None:
    """Pull `choices[0].message.content` from either an SDK response object or a
    plain dict (the dict form keeps the existing dict-based tests working).
    """

    # SDK path: ChatCompletion has .choices[0].message.content
    choices = getattr(response, "choices", None)
    if choices is None and isinstance(response, dict):
        choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return None

    first = choices[0]
    message = getattr(first, "message", None)
    if message is None and isinstance(first, dict):
        message = first.get("message")

    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = getattr(message, "content", None)

    if isinstance(content, str):
        return content

    # Some legacy non-chat completions endpoints put text at the top of the choice.
    text = getattr(first, "text", None) if not isinstance(first, dict) else first.get("text")
    if isinstance(text, str):
        return text

    return None


def _loads_json_object(content: str) -> dict[str, object]:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group(0))

    if not isinstance(data, dict):
        raise TypeError("LLM output must be a JSON object")
    return data


def _parse_judgement(data: dict[str, object]) -> LLMJudgement:
    is_spam = data.get("is_spam")
    if not isinstance(is_spam, bool):
        raise TypeError("is_spam must be boolean")

    confidence_raw = data.get("confidence")
    if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, int | float):
        raise TypeError("confidence must be numeric")
    confidence = max(0.0, min(1.0, float(confidence_raw)))

    category_raw = data.get("category")
    category = category_raw if isinstance(category_raw, str) else None

    skeleton_hash_raw = data.get("skeleton_hash")
    skeleton_hash = skeleton_hash_raw if isinstance(skeleton_hash_raw, str) else None

    phrases_raw = data.get("signal_phrases")
    phrases: tuple[str, ...]
    if isinstance(phrases_raw, list):
        phrases = tuple(str(item)[:80] for item in phrases_raw if str(item).strip())
    else:
        phrases = ()

    return LLMJudgement(
        is_spam=is_spam,
        confidence=confidence,
        category=category,
        skeleton_hash=skeleton_hash,
        signal_phrases=phrases[:10],
    )


_SYSTEM_PROMPT = """你是 Telegram 群组反广告审核器。你的目标是判断单条消息是否为广告、诈骗、引流、拉人进群、色情/博彩推广或恶意营销。

判定原则：
- 误封真人的代价远高于漏放广告；只有证据明确时才给高置信度。
- 正常讨论、玩笑、引用广告文案、技术链接、开源项目链接，不应判为广告。
- 低信誉、外链、预览卡、联系方式、诱导私聊、博彩/色情/返利/空投/刷单等都是信号，但不是单独定罪理由。
- @xxx、@xxxbot、t.me、telegram 这类联系方式同时搭配“加群、拿码、收钱、赚钱、做单、刷单、看片、成人、调教、博彩”等词时，应判为明确广告。
- 正文为空或只有标点但 preview/OG 文案含色情、博彩、刷单、导流等内容时，应按预览内容判为广告。
- 用户名、昵称、bio 都是用户可控的弱信号；只能与消息内容、链接、行为信号合并判断。
- 如果正文为空或只有标点但有 preview URL，通常更可疑。
- 你只输出 JSON，不输出解释文本。

JSON schema：
{
  "is_spam": boolean,
  "confidence": number,
  "category": "ads|scam|porn|gambling|crypto|traffic_diversion|mass_mention|benign|unknown",
  "skeleton_hash": string,
  "signal_phrases": [string]
}

confidence 必须在 0 到 1 之间。signal_phrases 只放实际触发判断的短语或载体信号，最多 10 个。"""
