from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import httpx
import pytest

from telegram_llm_antispam.models import ActionResult, DecisionAction, LocalDecision
from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.notifications import build_moderation_detail_text
from telegram_llm_antispam.vision_collect import PreparedVisuals, VisualImage
from telegram_llm_antispam.vision_media import Frame
from telegram_llm_antispam.vision_shadow import VisionShadow, VisualBinding
from test_llm import _settings
from test_vision_responses import VALUE
from test_vision_enforce import message


@pytest.mark.parametrize(
    "failure",
    [
        "json",
        "truncated",
        "empty",
        "schema",
        "envelope",
        "message",
        "transport",
        429,
        503,
    ],
)
def test_transient_visual_failure_recovers_once_with_same_images(tmp_path, monkeypatch, failure):
    requests = []
    client_class = httpx.AsyncClient

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            if failure == "transport":
                raise httpx.ConnectError("private-endpoint-secret", request=request)
            if isinstance(failure, int):
                return httpx.Response(failure, text="private response")
            if failure == "envelope":
                return httpx.Response(200, json={"choices": []})
            if failure == "message":
                return httpx.Response(200, json={"choices": [{"message": None}]})
            content = {
                "json": "bad JSON secret",
                "truncated": '{"confidence":',
                "empty": None,
                "schema": '{"confidence":0.9}',
            }[failure]
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "length" if failure == "truncated" else "stop",
                            "message": {"content": content},
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(VALUE)}}]
            },
        )

    monkeypatch.setattr(
        "telegram_llm_antispam.vision_shadow.httpx.AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs),
    )

    async def run():
        service = VisionShadow(
            replace(
                _settings(),
                vision_observation_path=tmp_path / "v.db",
                newapi_base_url="https://example.test/v1",
                newapi_api_key="synthetic-key",
            )
        )
        try:
            result = await service._judge(
                {}, PreparedVisuals((VisualImage("photo", Frame(b"PNG")),))
            )
            assert result["status"] == "ok" and result["attempt_count"] == 2
            assert result["attempts"][0]["status"] == "failed"
            assert "secret" not in json.dumps(result) and "private" not in json.dumps(result)
            assert service.store.conn.execute("SELECT calls FROM visual_budgets").fetchone()[0] == 1
        finally:
            await service.close()

    asyncio.run(run())
    assert len(requests) == 2
    assert requests[0]["messages"] == requests[1]["messages"]
    assert requests[1]["max_tokens"] == (3600 if failure == "truncated" else 2400)


@pytest.mark.parametrize("failure", ["refusal", "content_filter", 400, 401, 402])
def test_permanent_failures_do_not_retry(tmp_path, failure):
    async def run():
        service = VisionShadow(replace(_settings(), vision_observation_path=tmp_path / "v.db"))
        calls = []

        async def fail(*args, **kwargs):
            calls.append(1)
            return {
                "status": "failed",
                **(
                    {"http_status": failure}
                    if isinstance(failure, int)
                    else {"error_type": failure}
                ),
            }

        service._judge_once = fail
        try:
            result = await service._judge({}, PreparedVisuals(()))
            assert result["attempt_count"] == 1 and len(calls) == 1
        finally:
            await service.close()

    asyncio.run(run())


def test_exhausted_retry_keeps_review_and_explains_failure(tmp_path):
    async def run():
        service = VisionShadow(
            replace(
                _settings(),
                vision_mode="enforce",
                vision_max_calls_per_day=0,
                vision_observation_path=tmp_path / "v.db",
            )
        )

        async def fail(*args, **kwargs):
            return {"status": "failed", "error_type": "json_parse_error"}

        service._judge_once = fail
        prepared = PreparedVisuals((VisualImage("photo", Frame(b"PNG")),))
        service.store.begin("test", {"status": "prepared"}, 1)
        binding = VisualBinding(service, "test", prepared, {"status": "prepared"})
        await service.live_slots.acquire()
        try:
            outcome = await binding.judge_payload({})
            assert outcome.error == "visual_review_unavailable"
            decision = binding.annotate(
                LocalDecision(
                    DecisionAction.ALLOW,
                    "llm_not_spam",
                    0.15,
                    metadata={
                        "model_chain": {
                            "mode": "review",
                            "decision_source": "jev_only",
                            "primary_result": {"status": "ok", "noul": 0.15, "category": "unknown"},
                        }
                    },
                )
            )
            assert decision.action == DecisionAction.REVIEW and binding.released
            text = build_moderation_detail_text(
                build_message_features(message()), decision, ActionResult()
            )
            assert "json_parse_error" in text and "已尝试 2 次" in text
            assert "Jev仅为文字初审" in text and "采用Jev单独判定" not in text
        finally:
            await service.close()

    asyncio.run(run())


def test_retry_respects_daily_budget(tmp_path):
    async def run():
        service = VisionShadow(
            replace(
                _settings(), vision_max_calls_per_day=1, vision_observation_path=tmp_path / "v.db"
            )
        )
        assert service.store.reserve(1)

        async def fail(*args, **kwargs):
            return {"status": "failed", "error_type": "empty_content"}

        service._judge_once = fail
        try:
            result = await service._judge({}, PreparedVisuals(()))
            assert result["attempt_count"] == 1 and result["retry_skipped"] == "daily_budget"
        finally:
            await service.close()

    asyncio.run(run())


def test_timeout_retry_shares_deadline_and_cancellation_propagates(tmp_path):
    async def run():
        service = VisionShadow(
            replace(
                _settings(), vision_timeout_seconds=0.3, vision_observation_path=tmp_path / "v.db"
            )
        )
        calls = []

        async def slow(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                await asyncio.sleep(10)
            return {"status": "ok"}

        service._judge_once = slow
        try:
            started = time.monotonic()
            result = await service._judge({}, PreparedVisuals(()))
            assert result["status"] == "ok" and result["attempts"][0]["error_type"] == "timeout"
            assert time.monotonic() - started < 1

            async def cancelled(*args, **kwargs):
                raise asyncio.CancelledError()

            service._judge_once = cancelled
            with pytest.raises(asyncio.CancelledError):
                await service._judge({}, PreparedVisuals(()))
        finally:
            await service.close()

    asyncio.run(run())
