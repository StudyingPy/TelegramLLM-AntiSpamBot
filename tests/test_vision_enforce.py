from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from telegram_llm_antispam.db import Database
from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.feedback import record_vote_spam_feedback
from telegram_llm_antispam.handlers import create_router, _same_user_open_vote_repeat_decision
from telegram_llm_antispam.llm import _feature_payload, decision_from_llm
from telegram_llm_antispam.models import ActionResult, DecisionAction, LocalDecision
from telegram_llm_antispam.vision_collect import PreparedVisuals, VisualImage
from telegram_llm_antispam.vision_media import Frame
from telegram_llm_antispam.vision_shadow import VisionShadow
from test_llm import _settings


def config(tmp_path):
    return replace(_settings(), vision_mode="enforce", vision_chat_ids=(-1001,),
                   vision_observation_path=tmp_path / "visual.db", vision_max_calls_per_day=0)


def message():
    return SimpleNamespace(message_id=10, chat=SimpleNamespace(id=-1001, type="supergroup", title="t"),
                           from_user=SimpleNamespace(id=42, is_bot=False, first_name="Member"),
                           text="普通说明", bot=object(), photo=[SimpleNamespace(file_id="photo")])


class Collector:
    def __init__(self, source="photo"):
        self.source = source

    async def prepare(self, bot, request):
        return PreparedVisuals((VisualImage(self.source, Frame(b"synthetic"), "hash"),))


@pytest.mark.parametrize("body,profile,confidence,trusted,action", [
    (True, False, .95, False, DecisionAction.BAN),
    (True, False, .75, False, DecisionAction.WITHDRAW_VOTE),
    (True, False, .65, False, DecisionAction.REVIEW),
    (False, True, .99, False, DecisionAction.WITHDRAW_VOTE),
    (False, True, .60, False, DecisionAction.REVIEW),
    (False, False, .99, False, DecisionAction.ALLOW),
    (True, False, .99, True, DecisionAction.WITHDRAW_VOTE),
])
def test_live_review_uses_existing_thresholds_and_profile_only_vote(
    tmp_path, body, profile, confidence, trusted, action,
):
    async def run():
        settings = config(tmp_path)
        source = "photo" if body else "status_emoji"
        service = VisionShadow(settings, collector=Collector(source))
        service.start()
        assert not service.tasks
        calls = []

        async def judge(payload, prepared):
            calls.append(payload)
            return {"status": "ok", "judgement": {
                "message_is_promotional": body, "profile_has_advertising": profile,
                "confidence": confidence, "evidence": [{"source": source, "text": "可见文字"}],
            }}

        service._judge = judge
        msg = message()
        if not body:
            msg.photo = []
        features = build_message_features(msg)
        if not body:
            features.metadata["sender_profile"] = {"display_name": "群发服务"}
        features.metadata["user_trust"] = {"is_trusted": trusted}
        local = LocalDecision(DecisionAction.REVIEW, "unmatched", 0, True)
        binding = await service.bind(msg, features, local)
        outcome = await binding.judge_payload(_feature_payload(features))
        decision = binding.annotate(decision_from_llm(outcome.judgement, features, settings))
        assert decision.action == action
        assert binding.released and len(calls) == 1
        assert decision.metadata["input_parity"] == "reviewer_has_visual_context"
        assert service.store.conn.execute("SELECT calls FROM visual_budgets").fetchone()[0] == 1
        assert "base64" not in service.store.conn.execute(
            "SELECT record_json FROM visual_observations").fetchone()[0]
        if profile:
            assert outcome.judgement.confidence < settings.llm_ban_threshold
        await service.close()

    asyncio.run(run())


def test_numeric_body_cannot_turn_status_ad_into_body_ban(tmp_path):
    async def run():
        service = VisionShadow(config(tmp_path), collector=Collector("status_emoji"))

        async def judge(*args):
            return {"status": "ok", "judgement": {"message_is_promotional": True,
                "profile_has_advertising": True, "confidence": .99,
                "evidence": [{"source": "body_text", "text": "1"},
                             {"source": "status_emoji", "text": "飞机群发"}]}}

        service._judge = judge
        msg = message()
        msg.text = "1"
        msg.photo = []
        features = build_message_features(msg)
        features.metadata["sender_profile"] = {"display_name": "飞机群发"}
        binding = await service.bind(msg, features, LocalDecision(DecisionAction.REVIEW, "x", 0))
        outcome = await binding.judge_payload(_feature_payload(features))
        result = binding.annotate(decision_from_llm(outcome.judgement, features, config(tmp_path)))
        assert result.action == DecisionAction.WITHDRAW_VOTE
        assert result.reason == "visual_profile_advertising"
        await service.close()

    asyncio.run(run())


@pytest.mark.parametrize("bad_result", [
    {"status": "failed", "http_status": 402},
    {"status": "ok", "judgement": {"message_is_promotional": True,
        "profile_has_advertising": False, "confidence": .99,
        "evidence": [{"source": "sticker", "text": "素材未提供"}]}},
])
def test_missing_or_misattributed_visual_review_cannot_allow_blindly(tmp_path, bad_result):
    async def run():
        service = VisionShadow(config(tmp_path), collector=Collector())

        async def judge(*args):
            return bad_result

        service._judge = judge
        msg = message()
        features = build_message_features(msg)
        binding = await service.bind(msg, features, LocalDecision(DecisionAction.REVIEW, "x", 0))
        await binding.judge_payload(_feature_payload(features))
        result = binding.annotate(LocalDecision(DecisionAction.ALLOW, "jev_only", .1))
        assert result.action == DecisionAction.REVIEW
        assert binding.released
        await service.close()

    asyncio.run(run())


def test_router_runs_jev_then_one_visual_mimo_before_actions(tmp_path, monkeypatch):
    events = []
    settings = config(tmp_path)
    service = VisionShadow(settings, collector=Collector())

    async def judge(payload, prepared):
        events.append("visual_mimo")
        assert payload["visual_context"]["images"][0]["source"] == "photo"
        return {"status": "ok", "judgement": {"message_is_promotional": True,
                "profile_has_advertising": False, "confidence": .96,
                "evidence": [{"source": "photo", "text": "高收益招揽"}]}}

    service._judge = judge

    class Jev:
        requires_review = True

        async def compare(self, features, local, reviewer):
            events.append("jev")
            return await reviewer.judge_payload(_feature_payload(features)), None

        def record_final(self, *args):
            pass

    class TextJudge:
        async def judge(self, *args):
            pytest.fail("A second text-only MiMo call must not be made")

    async def apply(_self, msg, features, decision):
        events.append("action")
        assert decision.action == DecisionAction.BAN
        assert decision.reason == "visual_message_advertising"
        return ActionResult()

    async def get_me(): return SimpleNamespace(id=7777)
    async def get_chat(uid): return SimpleNamespace(bio=None)
    monkeypatch.setattr("telegram_llm_antispam.handlers.ModerationActions.apply", apply)
    db = Database(tmp_path / "moderation.db")
    db.connect()
    db.migrate()
    db.allow_chat(-1001, "test", None)
    msg = message()
    msg.bot = SimpleNamespace(get_me=get_me, get_chat=get_chat)
    router = create_router(settings, db, llm=TextJudge(), jev_observer=Jev(), vision_shadow=service)
    handler = next(item for item in router.message.handlers if not item.filters)

    async def run():
        await handler.callback(msg)
        assert events == ["jev", "visual_mimo", "action"]
        assert db.count_fingerprints() == 0
        assert db.get_user_context(-1001, 42).normal_messages_seen == 0
        await service.close()

    try:
        asyncio.run(run())
    finally:
        db.close()


def test_visual_vote_confirmation_does_not_learn_caption_fingerprints(tmp_path):
    db = Database(tmp_path / "moderation.db")
    db.connect()
    db.migrate()
    features = build_message_features(message())
    try:
        session_id = db.create_vote_session(features, LocalDecision(
            DecisionAction.WITHDRAW_VOTE, "visual_profile_advertising", .84), timeout_seconds=60)
        record_vote_spam_feedback(db, db.get_vote_session(session_id), config(tmp_path))
        assert db.count_fingerprints() == 0
    finally:
        db.close()


@pytest.mark.parametrize("reason,repeat_ban", [
    ("visual_profile_advertising", False), ("visual_message_advertising", True),
])
def test_profile_only_open_vote_cannot_turn_repeated_greeting_into_ban(tmp_path, reason, repeat_ban):
    db = Database(tmp_path / "moderation.db")
    db.connect()
    db.migrate()
    features = build_message_features(message())
    try:
        db.create_vote_session(features, LocalDecision(
            DecisionAction.WITHDRAW_VOTE, reason, .8), timeout_seconds=60)
        decision = _same_user_open_vote_repeat_decision(config(tmp_path), db, replace(features, message_id=11))
        assert (decision is not None) is repeat_ban
    finally:
        db.close()
