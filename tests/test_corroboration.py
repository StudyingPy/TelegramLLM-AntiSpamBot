from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from telegram_llm_antispam.corroboration import (
    corroborated_financial_recruitment,
    financial_collection_offer,
)
from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.models import DecisionAction, LocalDecision


def features(text="微信帮我收资 一天5ooo"):
    return build_message_features(
        SimpleNamespace(
            message_id=1,
            chat=SimpleNamespace(id=-1001),
            from_user=SimpleNamespace(id=42),
            text=text,
        )
    )


def decision(primary=0.91, reviewer=0.82):
    return LocalDecision(
        DecisionAction.WITHDRAW_VOTE,
        "llm_spam",
        reviewer,
        metadata={
            "model_chain": {
                "decision_source": "mimo_review",
                "reviewer_status": "ok",
                "primary_result": {"status": "ok", "category": "scam", "noul": primary},
            },
            "llm_outcome": {"status": "ok", "is_spam": True, "confidence": reviewer},
        },
    )


@pytest.mark.parametrize(
    "text,amount",
    [
        ("微信帮我收资 一天5ooo", 5000),
        ("微信帮我收资 一天5000", 5000),
        ("VX帮忙代收款，日结１ＯＯＯ元", 1000),
        ("帮我微信收款 每天3〇〇〇块", 3000),
        ("\u200b微信帮我收资\n一天5ooo" + "\u200b" * 200, 5000),
    ],
)
def test_numeric_obfuscation_is_interpreted_only_inside_collection_offer(text, amount):
    assert financial_collection_offer(text) == amount


@pytest.mark.parametrize(
    "text",
    [
        "微信今天收款5000",
        "微信收款 一天5000",
        "今天微信帮我收资一天5000",
        "微信帮我收资料 一天5000字",
        "我在微信帮别人收款，日流水5000",
        "谁遇到过‘微信帮我收资 一天5ooo’这种骗子？",
        "“微信帮我收资 一天5ooo”",
        "不要相信微信帮我收资一天5000",
        "微信帮我收资一天500",
        "想赚钱的来",
        "抽奖口令：微信帮我收资一天5000",
        "微信帮我收资一天5ooo，这个是广告",
    ],
)
def test_discussions_quotations_and_other_ads_do_not_get_policy_override(text):
    assert financial_collection_offer(text) is None
    original = decision()
    assert corroborated_financial_recruitment(features(text), original) is original


@pytest.mark.parametrize("primary", [0.90, 0.91])
def test_both_supplied_frozen_predictions_now_ban_without_inventing_confidence(primary):
    result = corroborated_financial_recruitment(features(), decision(primary))
    assert result.action == DecisionAction.BAN
    assert result.reason == "corroborated_financial_recruitment"
    assert result.confidence == 0.82
    assert result.metadata["corroborated_recruitment"]["jev_noul"] == primary
    assert result.metadata["corroborated_recruitment"]["daily_amount"] == 5000


@pytest.mark.parametrize(
    "primary,reviewer", [(0.89, 0.82), (0.91, 0.79), (float("nan"), 0.82), (0.91, True)]
)
def test_either_weak_or_invalid_score_leaves_existing_vote(primary, reviewer):
    original = decision(primary, reviewer)
    assert corroborated_financial_recruitment(features(), original) is original


@pytest.mark.parametrize(
    "case", ["jev_only", "reviewer_failed", "primary_failed", "benign", "other_category"]
)
def test_disagreement_failure_or_non_scam_primary_never_counts_as_two_model_evidence(case):
    original = decision()
    if case == "jev_only":
        original.metadata["model_chain"]["decision_source"] = "jev_only"
    if case == "reviewer_failed":
        original.metadata["model_chain"]["reviewer_status"] = "failed"
    if case == "primary_failed":
        original.metadata["model_chain"]["primary_result"]["status"] = "failed"
    if case == "benign":
        original.metadata["llm_outcome"]["is_spam"] = False
    if case == "other_category":
        original.metadata["model_chain"]["primary_result"]["category"] = "ads"
    assert corroborated_financial_recruitment(features(), original) is original


@pytest.mark.parametrize("case", ["trusted", "giveaway", "profile_only", "allow"])
def test_trust_giveaway_profile_scope_and_benign_decisions_remain_protected(case):
    current, original = features(), decision()
    if case == "trusted":
        current.metadata["user_trust"] = {"is_trusted": True}
    if case == "giveaway":
        current.metadata["giveaway_reply"] = {"verified_source": True}
    if case == "profile_only":
        original.metadata["visual_review"] = {
            "status": "ok",
            "message_is_promotional": False,
            "profile_has_advertising": True,
        }
    if case == "allow":
        original = replace(original, action=DecisionAction.ALLOW)
    assert corroborated_financial_recruitment(current, original) is original


def test_visual_body_ads_can_use_independent_primary_corroboration():
    original = decision()
    original.metadata["visual_review"] = {"status": "ok", "message_is_promotional": True}
    assert corroborated_financial_recruitment(features(), original).action == DecisionAction.BAN


def test_router_applies_corroboration_after_models_before_moderation(tmp_path, monkeypatch):
    import asyncio

    from telegram_llm_antispam.db import Database
    from telegram_llm_antispam.handlers import create_router
    from telegram_llm_antispam.models import (
        ActionResult,
        LLMJudgement,
        LLMOutcome,
        LLMOutcomeStatus,
    )
    from test_llm import _settings

    applied = []

    class Jev:
        requires_review = True

        async def compare(self, current, local, judge):
            return LLMOutcome(
                LLMOutcomeStatus.OK,
                1,
                LLMJudgement(True, 0.82, "scam"),
                metadata={"model_chain": decision().metadata["model_chain"]},
            ), None

        def record_final(self, *args):
            pass

    async def apply(_self, msg, current, verdict):
        applied.append(verdict)
        return ActionResult()

    async def get_me():
        return SimpleNamespace(id=7777)

    async def get_chat(uid):
        return SimpleNamespace(bio=None)

    monkeypatch.setattr("telegram_llm_antispam.handlers.ModerationActions.apply", apply)
    db = Database(tmp_path / "bot.db")
    db.connect()
    db.migrate()
    db.allow_chat(-1001, "test", None)
    msg = SimpleNamespace(
        message_id=1,
        text="微信帮我收资 一天5ooo",
        from_user=SimpleNamespace(id=42, is_bot=False),
        chat=SimpleNamespace(id=-1001, type="supergroup", title="test"),
        bot=SimpleNamespace(get_me=get_me, get_chat=get_chat),
    )
    router = create_router(_settings(), db, jev_observer=Jev())
    handler = next(item for item in router.message.handlers if not item.filters)
    try:
        asyncio.run(handler.callback(msg))
        assert applied[0].action == DecisionAction.BAN
        assert applied[0].confidence == 0.82
        assert applied[0].reason == "corroborated_financial_recruitment"
    finally:
        db.close()


def test_telegram_blockquote_entities_do_not_get_automatic_policy_upgrade():
    current = build_message_features(
        SimpleNamespace(
            message_id=1,
            chat=SimpleNamespace(id=-1001),
            from_user=SimpleNamespace(id=42),
            text="微信帮我收资 一天5ooo",
            entities=[{"type": "blockquote", "offset": 0, "length": 15}],
        )
    )
    original = decision()
    assert current.metadata["quoted_or_code_message"] is True
    assert corroborated_financial_recruitment(current, original) is original
