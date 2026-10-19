"""Jev-first routing with mandatory independent reviewer before model enforcement."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone

from .config import Settings
from .jev import (
    POLICY_VERSION,
    QUESTIONS,
    QUESTIONS_HASH,
    SYSTEM_PROMPT_HASH,
    JevClient,
    canonical,
    digest,
)
from .jev_store import ObservationStore
from .llm import LLMJudge, _feature_payload
from .models import ActionResult, LLMOutcome, LLMOutcomeStatus, LocalDecision, MessageFeatures

logger = logging.getLogger(__name__)


class JevObserver:
    def __init__(self, settings: Settings, store: ObservationStore | None, client: JevClient):
        self.settings, self.store, self.client = settings, store, client
        self._active = 0
        self._cooldown_until = 0.0
        self._disabled_auth = False
        self._last_prune = 0.0
        self._slots = asyncio.Semaphore(settings.jev_max_concurrent)

    @property
    def requires_review(self) -> bool:
        return self.settings.jev_mode == "review"

    def close(self) -> None:
        if self.store is not None:
            self.store.close()

    def selected(self, features: MessageFeatures, local: LocalDecision) -> bool:
        if self.requires_review:
            # The router already applies the application's group/admin/whitelist gates.
            return local.should_call_llm
        if features.chat_id not in self.settings.jev_chat_ids:
            return False
        if not local.should_call_llm and not self.settings.jev_observe_local_bypass:
            return False
        identity = [
            features.chat_id,
            features.message_id,
            features.content_hash,
            features.metadata.get("update_type", "message"),
        ]
        bucket = int(digest(identity)[:16], 16) / 2**64
        return bucket < self.settings.jev_sample_rate

    def _write(self, method: str, *args) -> bool:
        try:
            getattr(self.store, method)(*args)
            return True
        except Exception as exc:
            logger.warning("Jev observation storage failed (%s, %s)", method, type(exc).__name__)
            return False

    def _redact(self, value):
        if isinstance(value, dict):
            return {k: self._redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        if isinstance(value, str):
            for secret in (
                self.settings.jev_api_key,
                *(self.settings.newapi_api_key or "").split(","),
            ):
                if secret:
                    value = value.replace(secret.strip(), "[redacted]") if secret.strip() else value
        return value

    async def _observe(self, record: dict, payload: dict) -> dict:
        if self.requires_review:
            # Mandatory primary requests queue instead of being sampled or skipped.
            # No cache/counter/storage dependency may suppress a primary call.
            queued = time.monotonic()
            async with self._slots:
                queue_ms = round((time.monotonic() - queued) * 1000, 2)
                result = await self._request(payload)
                result["queue_ms"] = queue_ms
                return result
        now = time.time()
        try:
            cached = self.store.cached(record["request_hash"], now)
            if cached:
                return {**cached, "cache_hit": True, "api_attempted": False}
            if self._disabled_auth:
                return {"status": "skipped", "reason": "auth_disabled_until_restart"}
            if now < self._cooldown_until:
                return {"status": "skipped", "reason": "rate_limit_cooldown"}
            if self._active >= self.settings.jev_max_concurrent:
                return {"status": "skipped", "reason": "concurrency_limit"}
            day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
            if not self.store.reserve(day, self.settings.jev_max_calls_per_day):
                return {"status": "skipped", "reason": "daily_budget"}
            result = await self._request(payload)
            if result.get("error") in {"http_401", "http_403"}:
                self._disabled_auth = True
            if result.get("error") in {"http_429", "http_529", "http_503"}:
                self._cooldown_until = time.time() + result.get("retry_after_seconds", 10)
            return self._redact(result)
        except Exception as exc:
            return {"status": "failed", "error": type(exc).__name__}

    async def _request(self, payload: dict) -> dict:
        self._active += 1
        started = time.monotonic()
        try:
            try:
                result = await asyncio.wait_for(
                    self.client.decide(payload),
                    timeout=self.settings.jev_timeout_seconds,
                )
            except TimeoutError:
                result = {"status": "failed", "error": "deadline_exceeded"}
            except Exception as exc:
                result = {"status": "failed", "error": type(exc).__name__}
        finally:
            self._active -= 1
        result.setdefault("elapsed_ms", round((time.monotonic() - started) * 1000, 2))
        result["api_attempted"] = True
        return self._redact(result)

    async def compare(
        self, features: MessageFeatures, local: LocalDecision, baseline: LLMJudge
    ) -> tuple[LLMOutcome | None, str | None]:
        """Only call baseline when the original local decision requested it."""
        should_call = local.should_call_llm
        payload_judge = getattr(baseline, "judge_payload", None)
        if not self.selected(features, local) or (should_call and not callable(payload_judge)):
            return (await baseline.judge(features) if should_call else None), None
        try:
            # Canonical roundtrip detaches nested mutable metadata before any await.
            payload = json.loads(canonical(_feature_payload(features)))
            if not self.requires_review and len(canonical(payload).encode("utf-8")) > 65536:
                raise ValueError("observation input exceeds size limit")
            now = time.time()
            if self.store is not None and now - self._last_prune > 3600:
                self._write("prune", now)
                self._last_prune = now
            record = {
                "id": uuid.uuid4().hex,
                "created_at": now,
                "expires_at": now + self.settings.jev_retention_days * 86400,
                "chat_id": features.chat_id,
                "message_id": features.message_id,
                "user_id": features.user_id,
                "update_type": features.metadata.get("update_type", "message"),
                "stage": "model_requested" if should_call else "local_bypass",
                "mode": self.settings.jev_mode,
                "sampling_probability": 1
                if self.requires_review
                else self.settings.jev_sample_rate,
                "payload_hash": digest(payload),
                "request_hash": digest(
                    {
                        "state": payload,
                        "questions_hash": QUESTIONS_HASH,
                        "model": self.settings.jev_model,
                    }
                ),
                "state": payload,
                "questions": QUESTIONS,
                "questions_hash": QUESTIONS_HASH,
                "policy_version": POLICY_VERSION,
                "system_prompt_hash": SYSTEM_PROMPT_HASH,
                "requested_model": self.settings.jev_model,
                "source_text_length": len(features.text),
                "text_truncated_for_models": len(features.text) > 4000,
                "context_availability": {
                    key: payload.get(key) is not None
                    for key in ("sender_profile", "og_preview", "personal_chat", "edited_from")
                },
                "original_models_requested": self.settings.newapi_model,
                "local_decision": {
                    "action": local.action.value,
                    "reason": local.reason,
                    "confidence": local.confidence,
                    "should_call_llm": should_call,
                },
            }
        except Exception as exc:
            logger.warning(
                "Jev observation unavailable (%s); baseline continues", type(exc).__name__
            )
            return (await baseline.judge(features) if should_call else None), None

        stored = self._write("begin", record)
        if not stored and not self.requires_review:
            return (await baseline.judge(features) if should_call else None), None
        observation_id = record["id"] if stored else None
        result = await self._observe(record, json.loads(canonical(payload)))
        if stored:
            self._write("finish_jev", record, result)
        if not should_call:
            self._write(
                "finish_baseline", record["id"], {"status": "not_called", "reason": "local_bypass"}
            )
            return None, observation_id
        start = time.monotonic()
        try:
            # The same content, with no Jev answer or local/final decisions added.
            outcome = await payload_judge(json.loads(canonical(payload)))
            if not isinstance(outcome, LLMOutcome):
                raise TypeError("invalid baseline outcome")
        except Exception as exc:
            outcome = LLMOutcome(LLMOutcomeStatus.FAILED, error=type(exc).__name__)
        if self.requires_review:
            primary = {
                k: result.get(k)
                for k in (
                    "status",
                    "error",
                    "model",
                    "noul",
                    "category",
                    "category_confidence",
                    "elapsed_ms",
                )
            }
            primary_spam = result.get("noul", 0) >= 0.5 if result.get("status") == "ok" else None
            outcome = replace(
                outcome,
                metadata={
                    **outcome.metadata,
                    "model_chain": {
                        "mode": "review",
                        "primary": "jev",
                        "reviewer": "mimo",
                        "primary_result": primary,
                        "reviewer_status": outcome.status.value,
                        "agreement": primary_spam == outcome.judgement.is_spam
                        if primary_spam is not None and outcome.judgement is not None
                        else None,
                        "decision_source": "mimo_review"
                        if outcome.status == LLMOutcomeStatus.OK and outcome.judgement is not None
                        else "pending_review",
                    },
                },
            )
        saved = asdict(outcome)
        saved["input_hash"] = record["payload_hash"]
        saved["elapsed_ms"] = round((time.monotonic() - start) * 1000, 2)
        if stored:
            self._write("finish_baseline", record["id"], self._redact(saved))
        return outcome, observation_id

    def record_final(
        self, observation_id: str | None, decision: LocalDecision, result: ActionResult
    ) -> None:
        if observation_id:
            self._write(
                "finish_action",
                observation_id,
                {
                    "status": "recorded",
                    "action": decision.action.value,
                    "reason": decision.reason,
                    "confidence": decision.confidence,
                    "action_log_id": result.action_log_id,
                    "vote_session_id": result.vote_session_id,
                    "deleted": result.deleted,
                    "banned": result.banned,
                },
            )


def create_jev_observer(settings: Settings) -> JevObserver | None:
    if settings.jev_mode == "off":
        return None  # No validation, DB creation, API or changed baseline input.
    if settings.jev_mode not in {"shadow", "review"}:
        raise ValueError("JEV_MODE must be off, shadow or review")
    if not settings.jev_api_key or (settings.jev_mode == "shadow" and not settings.jev_chat_ids):
        raise ValueError("JEV shadow needs JEV_API_KEY and an explicit JEV_CHAT_IDS allowlist")
    if settings.jev_mode == "review" and not settings.has_newapi:
        raise ValueError("JEV review requires a configured reviewer")
    if not re.fullmatch(r"jev-\d+\.\d+\.\d+", settings.jev_model):
        raise ValueError("JEV_MODEL must be pinned to a version such as jev-1.13.0")
    if (
        not math.isfinite(settings.jev_sample_rate)
        or not 0 < settings.jev_sample_rate <= 1
        or not math.isfinite(settings.jev_timeout_seconds)
        or not 0 < settings.jev_timeout_seconds <= 10
        or settings.jev_max_concurrent < 1
        or settings.jev_max_calls_per_day < (0 if settings.jev_mode == "review" else 1)
        or not 1 <= settings.jev_retention_days <= 30
    ):
        raise ValueError("invalid Jev observation limits")
    if settings.jev_observation_path.resolve() == settings.database_path.resolve():
        raise ValueError("Jev observations must use a separate database")
    if settings.jev_proxy_url and not settings.jev_proxy_url.startswith(("http://", "https://")):
        raise ValueError("JEV_PROXY_URL must be an explicit HTTP(S) proxy")
    try:
        store = ObservationStore(settings.jev_observation_path)
    except Exception as exc:
        if settings.jev_mode != "review":
            logger.warning("Jev store unavailable (%s); observation disabled", type(exc).__name__)
            return None
        logger.warning(
            "Jev store unavailable (%s); primary/reviewer routing continues", type(exc).__name__
        )
        store = None
    logger.info(
        "Jev %s enabled: model=%s chats=%s sample_rate=%s daily_limit=%s timeout=%ss",
        settings.jev_mode,
        settings.jev_model,
        "all_moderated_model_requests" if settings.jev_mode == "review" else settings.jev_chat_ids,
        1 if settings.jev_mode == "review" else settings.jev_sample_rate,
        "none" if settings.jev_mode == "review" else settings.jev_max_calls_per_day,
        settings.jev_timeout_seconds,
    )
    return JevObserver(
        settings,
        store,
        JevClient(
            settings.jev_api_key,
            settings.jev_model,
            settings.jev_timeout_seconds,
            settings.jev_proxy_url,
        ),
    )
