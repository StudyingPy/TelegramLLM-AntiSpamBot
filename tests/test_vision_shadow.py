from __future__ import annotations

import asyncio
import io
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.models import DecisionAction, LocalDecision
from telegram_llm_antispam.vision_collect import (
    LimitedBuffer, PreparedVisuals, VisualCollector, VisualImage, request_from_message,
)
from telegram_llm_antispam.vision_media import Frame
from telegram_llm_antispam.vision_shadow import (
    VisionShadow, VisionStore, create_vision_shadow, parse_visual_result,
)
from test_llm import _settings


def settings(tmp_path, **values):
    return replace(_settings(), vision_mode="shadow", vision_chat_ids=(-1001,),
                   vision_observation_path=tmp_path / "visual.db", **values)


def message(mid=1):
    return SimpleNamespace(chat=SimpleNamespace(id=-1001), message_id=mid,
                           from_user=SimpleNamespace(id=42), text="1😀\n2", bot=object(),
                           entities=[{"type": "custom_emoji", "offset": 1, "length": 2,
                                      "custom_emoji_id": "emoji"}])


def test_shadow_is_off_by_default_and_requires_explicit_groups(tmp_path):
    assert _settings().vision_mode == "off"
    assert create_vision_shadow(_settings()) is None
    assert create_vision_shadow(replace(settings(tmp_path), vision_chat_ids=())) is None
    assert not (tmp_path / "visual.db").exists()


def test_original_caption_and_premium_animation_are_preserved():
    msg = SimpleNamespace(caption="1😀", caption_entities=[{
        "type": "custom_emoji", "offset": 1, "length": 2, "custom_emoji_id": "id"}],
        sticker=SimpleNamespace(file_id="st", file_unique_id="u", premium_animation={
            "file_id": "premium", "file_unique_id": "pu"}))
    request = request_from_message(msg, 42, inspect_profile=False)
    assert request.document.text == "1😀"
    assert request.document.runs[-1].emoji_id == "id"
    assert [ref.source for ref in request.files] == ["sticker", "premium_animation"]


def test_download_stream_limit_does_not_trust_advertised_file_size():
    buffer = LimitedBuffer(4)
    buffer.write(b"1234")
    with pytest.raises(ValueError):
        buffer.write(b"5")


def test_budget_is_persistent_and_disabled_for_zero(tmp_path):
    path = tmp_path / "budget.db"
    store = VisionStore(path)
    assert store.reserve(0) is False
    assert store.reserve(1) is True
    store.close()
    store = VisionStore(path)
    assert store.reserve(1) is False
    store.close()


def test_queue_full_dedup_and_group_selection_never_enqueue_extra_jobs(tmp_path):
    service = VisionShadow(settings(tmp_path, vision_queue_size=1))
    msg = message()
    features = build_message_features(msg)
    decision = LocalDecision(DecisionAction.ALLOW, "benign", .9)
    assert service.submit(msg, features, decision)
    assert not service.submit(msg, features, decision)
    assert not service.submit(message(2), replace(features, message_id=2), decision)
    assert service.queue.qsize() == 1
    assert not service.submit(msg, replace(features, chat_id=-1002), decision)
    service.store.close()


class Collector:
    async def prepare(self, bot, request):
        return PreparedVisuals((VisualImage("status_emoji", Frame(b"SYNTHETIC_PNG"), "hash"),))


def test_shadow_job_uses_real_image_blocks_but_persists_only_bounded_evidence(tmp_path, monkeypatch):
    captured = []
    client_class = httpx.AsyncClient

    def respond(request):
        payload = json.loads(request.content)
        captured.append(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "message_is_promotional": False, "profile_has_advertising": True,
            "confidence": .9, "evidence": [{"source": "status_emoji", "text": "看简介"}],
            "uncertainty": "正文只有数字",
        })}}]})

    monkeypatch.setattr("telegram_llm_antispam.vision_shadow.httpx.AsyncClient",
                        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs))

    async def run():
        service = VisionShadow(settings(
            tmp_path, newapi_base_url="https://example.test/v1", newapi_api_key="synthetic-key",
            newapi_model="vision-test",
        ), collector=Collector())
        service.start()
        msg = message()
        features = build_message_features(msg)
        baseline = LocalDecision(DecisionAction.ALLOW, "llm_not_spam", .95)
        assert service.submit(msg, features, baseline)
        await asyncio.wait_for(service.queue.join(), 2)
        raw = service.store.conn.execute("SELECT record_json FROM visual_observations").fetchone()[0]
        record = json.loads(raw)
        assert record["status"] == "ok"
        assert record["baseline"]["action"] == "allow"
        assert record["judgement"]["message_is_promotional"] is False
        assert "base64" not in raw and "SYNTHETIC_PNG" not in raw
        assert record["visual"]["images"][0]["source"] == "status_emoji"
        assert baseline.action == DecisionAction.ALLOW
        assert not service.submit(msg, features, baseline)
        await service.close()

    asyncio.run(run())
    blocks = captured[0]["messages"][1]["content"]
    assert any(block["type"] == "image_url" for block in blocks)
    assert "SYNTHETIC_PNG" not in blocks[0]["text"]


def test_profile_status_expiry_refresh_and_source_assignment(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("telegram_llm_antispam.vision_collect.time.time", lambda: now[0])

    class Bot:
        calls = 0

        async def get_chat(self, uid):
            self.calls += 1
            return {"photo": {"big_file_id": "avatar", "big_file_unique_id": "au"},
                    "emoji_status_custom_emoji_id": "status" if self.calls == 1 else "new",
                    "emoji_status_expiration_date": 1002 if self.calls == 1 else None}

        async def get_custom_emoji_stickers(self, custom_emoji_ids):
            return [{"file_id": key, "file_unique_id": key, "custom_emoji_id": key,
                     "is_video": True} for key in custom_emoji_ids]

    async def run():
        bot = Bot()
        collector = VisualCollector(settings(tmp_path))
        refs, _ = await collector._profile_refs(bot, 42)
        assert [(ref.source, ref.emoji_id) for ref in refs] == [("avatar", None), ("status_emoji", "status")]
        await collector._profile_refs(bot, 42)
        assert bot.calls == 1
        now[0] = 1003
        refs, _ = await collector._profile_refs(bot, 42)
        assert refs[-1].emoji_id == "new"
        assert bot.calls == 2

    asyncio.run(run())


def test_collector_downloads_original_tiles_composes_dark_variant_and_reuses_bytes(tmp_path):
    image_module = pytest.importorskip("PIL.Image")
    from telegram_llm_antispam.vision_media import load_font

    try:
        load_font()
    except RuntimeError:
        pytest.skip("No CJK font installed")

    class Bot:
        downloads = 0

        async def get_custom_emoji_stickers(self, custom_emoji_ids):
            return [{"custom_emoji_id": "emoji", "file_id": "file", "file_unique_id": "unique",
                     "needs_repainting": True}]

        async def get_file(self, file_id):
            return {"file_id": file_id, "file_size": 200}

        async def download(self, info, destination, **kwargs):
            self.downloads += 1
            out = io.BytesIO()
            image_module.new("RGBA", (64, 64), "red").save(out, format="PNG")
            destination.write(out.getvalue())

    async def run():
        bot = Bot()
        collector = VisualCollector(settings(tmp_path))
        request = request_from_message(message(), 42, inspect_profile=False)
        result = await collector.prepare(bot, request)
        assert "body_layout_unavailable" not in result.limitations
        assert [image.source for image in result.images] == ["body_layout", "body_layout"]
        assert [image.background for image in result.images] == ["light", "dark"]
        assert "file_id" not in json.dumps(result.manifest())
        assert "base64" not in json.dumps(result.manifest())
        await collector.prepare(bot, request)
        assert bot.downloads == 1

    asyncio.run(run())


def test_collection_failure_is_reported_without_echoing_token(tmp_path):
    class Bot:
        async def get_custom_emoji_stickers(self, **kwargs):
            raise RuntimeError("do not persist this secret-token")

    async def run():
        collector = VisualCollector(settings(tmp_path))
        request = request_from_message(message(), 42, inspect_profile=False)
        result = await collector.prepare(Bot(), request)
        manifest = json.dumps(result.manifest())
        assert "body_emoji_lookup_unavailable" in manifest
        assert "secret-token" not in manifest
        assert any("unavailable" in reason for reason in result.limitations)

    asyncio.run(run())


def test_shadow_timeout_finishes_job_without_changing_baseline(tmp_path):
    class SlowCollector:
        async def prepare(self, bot, request):
            await asyncio.Event().wait()

    async def run():
        service = VisionShadow(settings(tmp_path, vision_timeout_seconds=.02), collector=SlowCollector())
        service.start()
        msg = message()
        baseline = LocalDecision(DecisionAction.ALLOW, "normal", .9)
        assert service.submit(msg, build_message_features(msg), baseline)
        await asyncio.wait_for(service.queue.join(), 1)
        raw = service.store.conn.execute("SELECT record_json FROM visual_observations").fetchone()[0]
        record = json.loads(raw)
        assert record["status"] == "failed" and record["error_type"] == "TimeoutError"
        assert record["baseline"]["action"] == "allow"
        await service.close()

    asyncio.run(run())


def test_zero_budget_cannot_call_visual_model(tmp_path):
    async def run():
        service = VisionShadow(settings(tmp_path, vision_max_calls_per_day=0), collector=Collector())

        async def forbidden(*args):
            pytest.fail("Visual model called without budget")

        service._judge = forbidden
        service.start()
        msg = message()
        assert service.submit(msg, build_message_features(msg), LocalDecision(DecisionAction.ALLOW, "normal", .9))
        await asyncio.wait_for(service.queue.join(), 1)
        raw = service.store.conn.execute("SELECT record_json FROM visual_observations").fetchone()[0]
        assert json.loads(raw)["reason"] == "daily_budget"
        await service.close()

    asyncio.run(run())


@pytest.mark.parametrize("raise_in_shadow", [False, True])
def test_router_only_submits_after_moderation_and_preserves_original_media(tmp_path, monkeypatch, raise_in_shadow):
    from telegram_llm_antispam.db import Database
    from telegram_llm_antispam.handlers import create_router
    from telegram_llm_antispam.models import ActionResult, LLMJudgement, LLMOutcome, LLMOutcomeStatus

    events = []

    class Shadow:
        def submit(self, msg, features, decision):
            assert events == ["moderation"]
            assert msg.photo[0].file_id == "original-photo"
            assert decision.action == DecisionAction.ALLOW
            events.append("shadow")
            if raise_in_shadow:
                raise RuntimeError("shadow failed")

    class Judge:
        async def judge(self, features):
            return LLMOutcome(LLMOutcomeStatus.OK, judgement=LLMJudgement(False, .99))

    async def apply(*args):
        events.append("moderation")
        return ActionResult()

    async def get_me():
        return SimpleNamespace(id=7777)

    async def get_chat(uid):
        return SimpleNamespace(bio=None)

    monkeypatch.setattr("telegram_llm_antispam.handlers.ModerationActions.apply", apply)
    db = Database(tmp_path / "moderation.db")
    db.connect()
    db.migrate()
    db.allow_chat(-1001, "test", None)
    msg = message()
    msg.chat.type = "supergroup"
    msg.chat.title = "test"
    msg.from_user.is_bot = False
    msg.photo = [SimpleNamespace(file_id="original-photo")]
    msg.bot = SimpleNamespace(get_me=get_me, get_chat=get_chat)
    router = create_router(_settings(), db, llm=Judge(), vision_shadow=Shadow())
    handler = next(item for item in router.message.handlers if not item.filters)
    try:
        asyncio.run(handler.callback(msg))
        assert events == ["moderation", "shadow"]
        assert db.get_user_context(-1001, 42).normal_messages_seen == 1
    finally:
        db.close()


@pytest.mark.parametrize("change", [
    {"confidence": float("nan")}, {"confidence": True}, {"message_is_promotional": "false"},
    {"evidence": [{"source": "invented", "text": "x"}]},
])
def test_invalid_model_output_does_not_become_valid_evidence(change):
    value = {"message_is_promotional": False, "profile_has_advertising": False,
             "confidence": .5, "evidence": []}
    value.update(change)
    with pytest.raises(ValueError):
        parse_visual_result(value)
