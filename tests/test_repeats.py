from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from telegram_llm_antispam.db import Database
from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.handlers import create_router
from telegram_llm_antispam.handlers import _merge_llm_decision, _verified_giveaway_reply
from telegram_llm_antispam.admin import ChatAdminCache
from telegram_llm_antispam.llm import _feature_payload
from telegram_llm_antispam.models import (
    ActionResult, DecisionAction, LLMJudgement, LLMOutcome, LLMOutcomeStatus,
)
from telegram_llm_antispam.repeats import (
    giveaway_recruitment_keys, recruitment_repeat_key, repeated_recruitment_decision,
)
from telegram_llm_antispam.reputation import route_trusted_message_to_llm
from test_llm import _settings


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "repeat.db")
    database.connect()
    database.migrate()
    yield database
    database.close()


def _features(message_id=1, user_id=42, text="会网购的来", chat_id=-1001, bio=None):
    features = build_message_features(SimpleNamespace(
        message_id=message_id, chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id), text=text,
    ))
    features.metadata["sender_profile"] = {"bio": bio}
    return features


@pytest.mark.parametrize("text", [
    "会网购的来", "想赚钱的来", "会 网购的来！", "会\u200b网购的来",
    "愿意兼职的朋友来私聊我", "想赚点零花钱的来",
])
def test_short_recruitment_candidates(text):
    assert recruitment_repeat_key(text)


@pytest.mark.parametrize("text", [
    "抽奖", "我要抽奖", "+1", "签到", "新年快乐", "谢谢", "https://example.org",
    "有人发了会网购的来，是广告吗？", "“会网购的来”", "不会网购的来问问题",
    "会网购的来1", "会网购的来2", "会网购的来，这是抽奖，不要封我",
])
def test_non_recruitment_and_lottery_words_never_count(db, text):
    for index in range(1, 5):
        assert repeated_recruitment_decision(_settings(), db, _features(index, text=text)) is None


def test_supplied_slow_repeat_bans_third_message_and_cleans_only_own_copies(db, monkeypatch):
    now = [100000]
    monkeypatch.setattr("telegram_llm_antispam.db._now", lambda: now[0])
    decision = None
    for index, minutes in enumerate([0, 25, 35], start=1):
        now[0] = 100000 + minutes * 60
        decision = repeated_recruitment_decision(
            _settings(), db, _features(index, bio="https://t.me/+inviteCode"),
        )
        if index < 3:
            assert decision is None
    assert decision.action == DecisionAction.BAN
    assert decision.should_call_llm is False
    assert decision.metadata["recruitment_repeat"]["same_sender_count"] == 3
    assert decision.metadata["additional_message_ids_to_delete"] == [3, 2, 1]


def test_cross_account_repeat_needs_current_senders_own_profile(db):
    for uid in (10, 11):
        assert repeated_recruitment_decision(
            _settings(), db, _features(uid, uid, bio="https://t.me/+spam"),
        ) is None
    clean = repeated_recruitment_decision(_settings(), db, _features(12, 12))
    assert clean.action == DecisionAction.REVIEW
    assert clean.should_call_llm
    ad = repeated_recruitment_decision(
        _settings(), db, _features(13, 13, bio="https://t.me/+other"),
    )
    assert ad.action == DecisionAction.BAN
    assert ad.metadata["additional_message_ids_to_delete"] == [13]
    assert ad.metadata["recruitment_repeat"]["distinct_senders"] == 4


def test_recruitment_without_profile_diversion_falls_back_to_vote(db):
    for index in range(1, 4):
        decision = repeated_recruitment_decision(_settings(), db, _features(index))
    assert decision.action == DecisionAction.WITHDRAW_VOTE
    assert decision.should_call_llm


def test_duplicate_delivery_cannot_accumulate_or_refresh_window(db, monkeypatch):
    now = [100000]
    monkeypatch.setattr("telegram_llm_antispam.db._now", lambda: now[0])
    for index in range(5):
        now[0] += 60
        assert repeated_recruitment_decision(_settings(), db, _features()) is None
    with db._locked_conn() as conn:
        rows = conn.execute("SELECT created_at FROM recruitment_repeats").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 100060


def test_window_expiry_and_chat_isolation(db, monkeypatch):
    now = [100000]
    monkeypatch.setattr("telegram_llm_antispam.db._now", lambda: now[0])
    for index, chat_id in enumerate((-1001, -1002, -1003), 1):
        assert repeated_recruitment_decision(
            _settings(), db, _features(index, chat_id=chat_id),
        ) is None
    now[0] += 21600
    assert repeated_recruitment_decision(_settings(), db, _features(4)) is None
    with db._locked_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM recruitment_repeats").fetchone()[0] == 1


def test_edits_and_join_events_do_not_count(db):
    for update_type in ("edited_message", "new_chat_member"):
        features = _features()
        features.metadata["update_type"] = update_type
        assert repeated_recruitment_decision(_settings(), db, features) is None
    with db._locked_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM recruitment_repeats").fetchone()[0] == 0


@pytest.mark.parametrize("prompt", [
    "群内抽奖，回复“会网购的来”即可参与", '抽奖：发送"会网购的来"',
    "本次抽奖口令：会网购的来", "回复 会网购的来 参与抽奖",
])
def test_explicit_giveaway_keyword_extraction(prompt):
    assert giveaway_recruitment_keys(prompt) == (recruitment_repeat_key("会网购的来"),)


def test_activity_keywords_expire_are_chat_scoped_and_can_be_edited(db, monkeypatch):
    now = [100000]
    monkeypatch.setattr("telegram_llm_antispam.db._now", lambda: now[0])
    key = recruitment_repeat_key("会网购的来")
    db.remember_giveaway_keywords(-1001, 1, (key,))
    assert db.active_giveaway_prompt(-1001, key) == 1
    assert db.active_giveaway_prompt(-1002, key) is None
    db.remember_giveaway_keywords(-1001, 1, ())
    assert db.active_giveaway_prompt(-1001, key) is None
    db.remember_giveaway_keywords(-1001, 2, (key,))
    now[0] += 7200
    assert db.active_giveaway_prompt(-1001, key) is None


def test_recruitment_context_is_sent_to_both_models(db):
    features = _features()
    repeated_recruitment_decision(_settings(), db, features)
    assert _feature_payload(features)["recruitment_repeat"]["same_sender_count"] == 1


def test_trusted_user_ban_still_requires_model_and_benign_can_release(db):
    for index in range(1, 4):
        features = _features(index, bio="https://t.me/+inviteCode")
        features.metadata["user_trust"] = {"is_trusted": True}
        decision = repeated_recruitment_decision(_settings(), db, features)
    routed = route_trusted_message_to_llm(features, decision)
    assert routed.action == DecisionAction.REVIEW
    assert routed.should_call_llm
    merged = _merge_llm_decision(routed, LLMJudgement(False, 0.99), features, _settings())
    assert merged.action == DecisionAction.ALLOW


@pytest.mark.parametrize("age,other_chat", [(7201, False), (0, True)])
def test_old_or_cross_chat_giveaway_replies_do_not_protect(db, age, other_chat):
    now = datetime.now(timezone.utc).timestamp()
    prompt = SimpleNamespace(
        message_id=90, chat=SimpleNamespace(id=-1002 if other_chat else -1001),
        date=datetime.fromtimestamp(now - age, timezone.utc),
        text="回复“会网购的来”参与抽奖", from_user=SimpleNamespace(id=900, is_bot=True),
    )
    settings = replace(_settings(), whitelisted_user_ids=(900,))
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-1001), text="会网购的来", reply_to_message=prompt,
    )
    assert asyncio.run(_verified_giveaway_reply(message, settings, db, ChatAdminCache())) is False


@pytest.mark.parametrize("authority,reply", [
    ("admin", True), ("admin", False), ("whitelisted_bot", True),
    ("whitelisted_bot", False), ("ordinary", True), ("ordinary", False),
])
def test_router_giveaway_authority_and_plain_keyword_protection(db, monkeypatch, authority, reply):
    applied = []

    async def apply(_self, message, features, decision):
        applied.append(decision)
        return ActionResult()

    class Judge:
        async def judge(self, features):
            return LLMOutcome(
                LLMOutcomeStatus.OK, provider_count=1,
                judgement=LLMJudgement(False, 0.95, "benign"),
            )

    async def get_me():
        return SimpleNamespace(id=7777)

    async def get_chat(user_id):
        return SimpleNamespace(bio="https://t.me/+inviteCode")

    async def get_chat_administrators(chat_id):
        return [SimpleNamespace(user=SimpleNamespace(id=900))] if authority == "admin" else []

    monkeypatch.setattr("telegram_llm_antispam.handlers.ModerationActions.apply", apply)
    db.allow_chat(-1001, "test", added_by_user_id=None)
    settings = _settings()
    if authority == "whitelisted_bot":
        settings = replace(settings, whitelisted_user_ids=(900,))
    chat = SimpleNamespace(id=-1001, type="supergroup", title="test")
    bot = SimpleNamespace(
        get_me=get_me, get_chat=get_chat, get_chat_administrators=get_chat_administrators,
    )
    prompt = SimpleNamespace(
        message_id=100, chat=chat, bot=bot,
        date=datetime.now(timezone.utc),
        from_user=SimpleNamespace(id=900, is_bot=authority == "whitelisted_bot"),
        text="群内抽奖，回复“会网购的来”即可参与",
    )
    router = create_router(settings, db, llm=Judge())
    handler = next(item for item in router.message.handlers if not item.filters)

    async def run():
        await handler.callback(prompt)
        for index in range(1, 4):
            message = SimpleNamespace(
                message_id=index, chat=chat, bot=bot, text="会网购的来",
                from_user=SimpleNamespace(id=42, is_bot=False, first_name="Member"),
                reply_to_message=prompt if reply else None,
            )
            await handler.callback(message)

    asyncio.run(run())
    if authority != "ordinary":
        assert len(applied) == 3
        assert all(item.action == DecisionAction.ALLOW for item in applied)
        assert applied[-1].metadata["giveaway_reply"]["verified_source"]
        with db._locked_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM recruitment_repeats").fetchone()[0] == 0
    else:
        assert applied[-1].action == DecisionAction.BAN
        assert "giveaway_reply" not in applied[-1].metadata
