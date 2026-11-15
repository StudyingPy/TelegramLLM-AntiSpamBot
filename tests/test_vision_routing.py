from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.models import DecisionAction, LocalDecision
from telegram_llm_antispam.vision_collect import PreparedVisuals, VisualImage
from telegram_llm_antispam.vision_media import Frame
from telegram_llm_antispam.vision_routing import profile_inspection_reason
from telegram_llm_antispam.vision_shadow import VisionShadow
from test_vision_enforce import config


def message(text, **kwargs):
    return SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(id=42, is_premium=True),
        text=text,
        bot=object(),
        **kwargs,
    )


class Collector:
    def __init__(self):
        self.requests = []

    async def prepare(self, bot, request):
        self.requests.append(request)
        source = "avatar" if request.inspect_profile else "photo"
        return PreparedVisuals((VisualImage(source, Frame(b"synthetic")),))


@pytest.mark.parametrize(
    "text",
    [
        "你好",
        "1",
        "😀✨🌟",
        "谢谢大家",
        "微信帮我收资 一天5ooo",
        "这个网购平台有用吗？",
        "怎么更换头像？",
    ],
)
def test_plain_text_unicode_emoji_and_new_premium_user_skip_visual_network(tmp_path, text):
    async def run():
        collector = Collector()
        service = VisionShadow(config(tmp_path), collector=collector)
        msg = message(text)
        current = build_message_features(msg)
        current.metadata["sender_profile"] = {"is_premium": True, "bio": "https://t.me/example"}
        local = LocalDecision(DecisionAction.REVIEW, "unmatched", 0, True)
        try:
            assert await service.bind(msg, current, local) is None
            assert collector.requests == [] and "visual_context" not in current.metadata
            assert (
                service.store.conn.execute("SELECT count(*) FROM visual_budgets").fetchone()[0] == 0
            )
        finally:
            await service.close()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["custom_emoji", "photo", "sticker", "animation", "video"])
def test_real_visual_material_is_reviewed_without_unnecessary_profile_fetch(tmp_path, kind):
    async def run():
        collector = Collector()
        service = VisionShadow(config(tmp_path), collector=collector)
        kwargs = (
            {
                "entities": [
                    {"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "emoji"}
                ]
            }
            if kind == "custom_emoji"
            else {
                kind: [SimpleNamespace(file_id="id")]
                if kind == "photo"
                else SimpleNamespace(file_id="id")
            }
        )
        msg = message("😀", **kwargs)
        current = build_message_features(msg)
        try:
            binding = await service.bind(msg, current, LocalDecision(DecisionAction.REVIEW, "x", 0))
            assert binding is not None and len(collector.requests) == 1
            assert collector.requests[0].inspect_profile is False
            assert current.metadata["visual_routing"]["body_media"] is True
            binding.release()
        finally:
            await service.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "text,profile,reason",
    [
        ("看我简介", {}, "message_points_to_profile"),
        ("点击我的头像进群", {}, "message_points_to_profile"),
        ("会网购的来", {}, "short_recruitment_needs_profile"),
        ("1", {"display_name": "飞机群发"}, "short_message_with_profile_ad_signal"),
        ("😀", {"display_name": "代⚓发"}, "short_message_with_profile_ad_signal"),
    ],
)
def test_specific_profile_signals_keep_avatar_and_status_inspection(
    tmp_path, text, profile, reason
):
    async def run():
        collector = Collector()
        service = VisionShadow(config(tmp_path), collector=collector)
        msg = message(text)
        current = build_message_features(msg)
        current.metadata["sender_profile"] = profile
        try:
            assert profile_inspection_reason(current) == reason
            binding = await service.bind(msg, current, LocalDecision(DecisionAction.REVIEW, "x", 0))
            assert binding is not None and collector.requests[0].inspect_profile
            assert current.metadata["visual_routing"]["profile_reason"] == reason
            binding.release()
        finally:
            await service.close()

    asyncio.run(run())


@pytest.mark.parametrize("protection", ["giveaway_reply", "quoted_or_code_message", "trusted"])
def test_giveaways_quotes_and_trusted_recruitment_do_not_trigger_profile_only_checks(protection):
    current = build_message_features(message("会网购的来"))
    if protection == "trusted":
        current.metadata["user_trust"] = {"is_trusted": True}
    else:
        current.metadata[protection] = True
    assert profile_inspection_reason(current) is None


def test_shadow_plain_text_routing_matches_enforcement(tmp_path):
    service = VisionShadow(config(tmp_path))
    msg = message("普通聊天😀")
    current = build_message_features(msg)
    from dataclasses import replace

    service.settings = replace(service.settings, vision_mode="shadow")
    try:
        assert not service.submit(msg, current, LocalDecision(DecisionAction.ALLOW, "x", 0))
        assert service.queue.empty()
    finally:
        service.store.close()
