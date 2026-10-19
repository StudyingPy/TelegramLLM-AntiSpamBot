"""Jev-first observation; the baseline alone returns an enforceable judgement."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from dataclasses import asdict
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
    def __init__(self, settings: Settings, store: ObservationStore, client: JevClient):
        self.settings, self.store, self.client = settings, store, client
        self._active = 0
        self._cooldown_until = 0.0
        self._disabled_auth = False
        self._last_prune = 0.0

    def selected(self, features: MessageFeatures, local: LocalDecision) -> bool:
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
            if result.get("error") in {"http_401", "http_403"}:
                self._disabled_auth = True
            if result.get("error") in {"http_429", "http_529", "http_503"}:
                self._cooldown_until = time.time() + result.get("retry_after_seconds", 10)
            return self._redact(result)
        except Exception as exc:
            return {"status": "failed", "error": type(exc).__name__}

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
            if len(canonical(payload).encode("utf-8")) > 65536:
                raise ValueError("observation input exceeds size limit")
            now = time.time()
            if now - self._last_prune > 3600:
                self.store.prune(now)
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
                "sampling_probability": self.settings.jev_sample_rate,
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
            self.store.begin(record)
        except Exception as exc:
            logger.warning(
                "Jev observation unavailable (%s); baseline continues", type(exc).__name__
            )
            return (await baseline.judge(features) if should_call else None), None

        result = await self._observe(record, json.loads(canonical(payload)))
        self._write("finish_jev", record, result)
        if not should_call:
            self._write(
                "finish_baseline", record["id"], {"status": "not_called", "reason": "local_bypass"}
            )
            return None, record["id"]
        start = time.monotonic()
        try:
            # The same content, with no Jev answer or local/final decisions added.
            outcome = await payload_judge(json.loads(canonical(payload)))
            if not isinstance(outcome, LLMOutcome):
                raise TypeError("invalid baseline outcome")
        except Exception as exc:
            outcome = LLMOutcome(LLMOutcomeStatus.FAILED, error=type(exc).__name__)
        saved = asdict(outcome)
        saved["input_hash"] = record["payload_hash"]
        saved["elapsed_ms"] = round((time.monotonic() - start) * 1000, 2)
        self._write("finish_baseline", record["id"], self._redact(saved))
        return outcome, record["id"]

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
    if settings.jev_mode != "shadow":
        raise ValueError("JEV_MODE must be off or shadow; Jev enforcement is not enabled")
    if not settings.jev_api_key or not settings.jev_chat_ids:
        raise ValueError("JEV shadow needs JEV_API_KEY and an explicit JEV_CHAT_IDS allowlist")
    if not re.fullmatch(r"jev-\d+\.\d+\.\d+", settings.jev_model):
        raise ValueError("JEV_MODEL must be pinned to a version such as jev-1.13.0")
    if (
        not math.isfinite(settings.jev_sample_rate)
        or not 0 < settings.jev_sample_rate <= 1
        or not math.isfinite(settings.jev_timeout_seconds)
        or not 0 < settings.jev_timeout_seconds <= 10
        or settings.jev_max_concurrent < 1
        or settings.jev_max_calls_per_day < 1
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
        logger.warning("Jev store unavailable (%s); observation disabled", type(exc).__name__)
        return None
    logger.info(
        "Jev shadow enabled: model=%s chats=%s sample_rate=%s daily_limit=%s timeout=%ss",
        settings.jev_model,
        settings.jev_chat_ids,
        settings.jev_sample_rate,
        settings.jev_max_calls_per_day,
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
