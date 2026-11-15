from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from telegram_llm_antispam.vision_collect import PreparedVisuals, VisualImage
from telegram_llm_antispam.vision_media import Frame
from telegram_llm_antispam.vision_shadow import VisionShadow
from test_llm import _settings


VALUE = {
    "message_is_promotional": True,
    "profile_has_advertising": False,
    "confidence": 0.95,
    "evidence": [{"source": "photo", "text": "图片内的招揽"}],
}


@pytest.mark.parametrize(
    "content,finish,refusal,status,error",
    [
        (json.dumps(VALUE), "stop", None, "ok", None),
        ("```json\n" + json.dumps(VALUE) + "\n```", "stop", None, "ok", None),
        ("结果如下：" + json.dumps(VALUE), "stop", None, "ok", None),
        ([{"type": "text", "text": json.dumps(VALUE)}], "stop", None, "ok", None),
        ('{"message_is_promotional":', "length", None, "failed", "truncated_response"),
        (None, "stop", None, "failed", "empty_content"),
        (None, "stop", "sensitive refusal content", "failed", "refusal"),
        ("not JSON; sensitive response", "stop", None, "failed", "json_parse_error"),
        ('{"confidence":0.9}', "stop", None, "failed", "schema_error"),
        (json.dumps(VALUE), "content_filter", None, "failed", "content_filter"),
    ],
)
def test_visual_response_formats_and_failures_have_bounded_diagnostics(
    tmp_path,
    monkeypatch,
    content,
    finish,
    refusal,
    status,
    error,
):
    client_class = httpx.AsyncClient
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": finish, "message": {"content": content, "refusal": refusal}}
                ],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 1200},
            },
        )

    monkeypatch.setattr(
        "telegram_llm_antispam.vision_shadow.httpx.AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs),
    )

    async def run():
        settings = replace(
            _settings(),
            vision_observation_path=tmp_path / "visual.db",
            newapi_base_url="https://example.test/v1",
            newapi_api_key="synthetic-key",
            newapi_model="vision-test",
        )
        service = VisionShadow(settings)
        try:
            result = await service._judge(
                {}, PreparedVisuals((VisualImage("photo", Frame(b"PNG")),))
            )
            assert result["status"] == status
            if error:
                assert result["error_type"] == error
            assert result["finish_reason"] == finish
            assert result["usage"]["completion_tokens"] == 1200
            assert "sensitive" not in json.dumps(result)
            assert "synthetic-key" not in json.dumps(result)
            assert "content_sha256" in result
        finally:
            await service.close()

    asyncio.run(run())
    assert captured[0]["max_tokens"] == 2400
