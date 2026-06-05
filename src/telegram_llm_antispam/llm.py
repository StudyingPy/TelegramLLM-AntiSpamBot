from __future__ import annotations

import asyncio
import json
import logging
import re
import string
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from .config import Settings
from .models import DecisionAction, LLMJudgement, LocalDecision, MessageFeatures


logger = logging.getLogger(__name__)


class LLMJudge(Protocol):
    async def judge(self, features: MessageFeatures) -> LLMJudgement | None:
        """Return a structured judgement, or None when the LLM path is unavailable."""


class NullLLMJudge:
    async def judge(self, features: MessageFeatures) -> LLMJudgement | None:
        logger.debug("LLM judge is not configured; falling back to local decision only")
        return None


@dataclass(frozen=True, slots=True)
class NewAPIProvider:
    endpoint: str
    api_key: str
    model: str


class NewAPIJudge:
    def __init__(self, settings: Settings) -> None:
        providers = _newapi_providers_from_settings(settings)
        if not providers:
            raise ValueError("At least one NewAPI base URL and API key are required")

        self._providers = providers
        self._timeout = settings.newapi_timeout_seconds
        self._temperature = settings.newapi_temperature
        self._max_tokens = settings.newapi_max_tokens

    async def judge(self, features: MessageFeatures) -> LLMJudgement | None:
        feature_payload = _feature_payload(features)
        for index, provider in enumerate(self._providers, start=1):
            judgement = await self._judge_with_provider(provider, feature_payload, index)
            if judgement is not None:
                return judgement

        logger.warning("All %s NewAPI provider(s) failed; using local fallback", len(self._providers))
        return None

    async def _judge_with_provider(
        self,
        provider: NewAPIProvider,
        feature_payload: dict[str, object],
        index: int,
    ) -> LLMJudgement | None:
        payload = _chat_payload(
            model=provider.model,
            feature_payload=feature_payload,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(self._post_chat_completion, provider, payload),
                timeout=self._timeout + 1,
            )
        except TimeoutError:
            logger.warning(
                "NewAPI provider %s timed out after %.1fs: %s",
                index,
                self._timeout,
                provider.endpoint,
            )
            return None
        except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("NewAPI provider %s failed (%s): %s", index, provider.endpoint, exc)
            return None

        content = _extract_message_content(response)
        if not content:
            logger.warning(
                "NewAPI provider %s returned no message content: %s",
                index,
                provider.endpoint,
            )
            return None

        try:
            data = _loads_json_object(content)
            return _parse_judgement(data)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "NewAPI provider %s returned invalid judgement JSON (%s): %s",
                index,
                provider.endpoint,
                exc,
            )
            return None

    def _post_chat_completion(
        self,
        provider: NewAPIProvider,
        payload: dict[str, object],
    ) -> dict[str, object]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            provider.endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {provider.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "TelegramLLMAntiSpamBot/0.1",
            },
            method="POST",
        )
        with urlopen(request, timeout=self._timeout) as response:
            raw = response.read(2_000_000)
        return json.loads(raw.decode("utf-8"))


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
    should_ban = judgement.confidence >= settings.llm_ban_threshold and (
        low_rep or _has_immediate_ban_context(judgement, features, settings)
    )
    if should_ban:
        return LocalDecision(
            action=DecisionAction.BAN,
            reason=(
                "llm_spam_high_confidence_low_reputation"
                if low_rep
                else "llm_spam_high_confidence_profile_and_content"
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


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/") + "/"
    if normalized.endswith("/chat/completions/"):
        return normalized[:-1]
    if normalized.endswith("/v1/"):
        return urljoin(normalized, "chat/completions")
    return urljoin(normalized, "v1/chat/completions")


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
                endpoint=_chat_completions_url(base_url),
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


def _chat_payload(
    *,
    model: str,
    feature_payload: dict[str, object],
    temperature: float,
    max_tokens: int,
) -> dict[str, object]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    feature_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }


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


def _extract_message_content(response: dict[str, object]) -> str | None:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return None

    first = choices[0]
    if not isinstance(first, dict):
        return None

    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content

    text = first.get("text")
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


def _has_immediate_ban_context(
    judgement: LLMJudgement,
    features: MessageFeatures,
    settings: Settings,
) -> bool:
    if features.sender_reputation >= settings.high_reputation_threshold:
        return False
    if judgement.confidence < max(settings.llm_ban_threshold, 0.95):
        return False
    if not _has_strong_content_signal(judgement, features):
        return False
    return _has_suspicious_profile_signal(features)


def _has_strong_content_signal(judgement: LLMJudgement, features: MessageFeatures) -> bool:
    category = judgement.category or ""
    if category not in {"ads", "scam", "porn", "gambling", "crypto", "traffic_diversion"}:
        return False

    text = f"{features.clean_text} {' '.join(judgement.signal_phrases)}".lower()
    has_contact = features.mention_count > 0 or any(
        token in text for token in ("@", "t.me", "telegram", "私聊", "客服", "联系")
    )
    has_hard_signal = any(
        token in text
        for token in (
            "洗钱",
            "博彩",
            "盘口",
            "下注",
            "成人",
            "看片",
            "裸聊",
            "刷单",
            "日结",
            "小时",
            "返利",
            "空投",
            "代币",
            "赚钱",
        )
    )
    return has_contact and has_hard_signal


def _has_suspicious_profile_signal(features: MessageFeatures) -> bool:
    profile = features.metadata.get("sender_profile")
    if not isinstance(profile, dict):
        return False

    username = str(profile.get("username") or "")
    display_name = str(profile.get("display_name") or "")
    bio = str(profile.get("bio") or "")

    score = 0
    if not username:
        score += 1
    elif re.fullmatch(r"[a-z]{5,}\d{2,}|[a-z]+_[a-z]+_\d+", username.lower()):
        score += 1
    if _looks_like_generated_english_bio(bio):
        score += 2
    if _looks_like_generic_latin_name(display_name) and score:
        score += 1
    return score >= 2


def _looks_like_generated_english_bio(value: str) -> bool:
    stripped = value.strip()
    if not stripped or any("\u4e00" <= char <= "\u9fff" for char in stripped):
        return False
    words = re.findall(r"[A-Za-z]+", stripped)
    if not (5 <= len(words) <= 14):
        return False
    alpha_chars = sum(1 for char in stripped if char in string.ascii_letters)
    if alpha_chars < max(12, len(stripped.replace(" ", "")) * 0.7):
        return False
    return not any(token in stripped.lower() for token in ("http", "t.me", "@", "github"))


def _looks_like_generic_latin_name(value: str) -> bool:
    words = re.findall(r"[A-Z][a-z]{2,}", value)
    return len(words) == 2 and " " in value.strip()


_SYSTEM_PROMPT = """你是 Telegram 群组反广告审核器。你的目标是判断单条消息是否为广告、诈骗、引流、拉人进群、色情/博彩推广或恶意营销。

判定原则：
- 误封真人的代价远高于漏放广告；只有证据明确时才给高置信度。
- 正常讨论、玩笑、引用广告文案、技术链接、开源项目链接，不应判为广告。
- 低信誉、外链、预览卡、联系方式、诱导私聊、博彩/色情/返利/空投/刷单等都是信号，但不是单独定罪理由。
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
