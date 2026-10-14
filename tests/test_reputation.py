from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from telegram_llm_antispam.admin import ChatAdminCache
from telegram_llm_antispam.db import Database, SCHEMA_SQL
from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.feedback import phrase_fingerprint_value
from telegram_llm_antispam.handlers import create_router
from telegram_llm_antispam.models import (
    ActionResult, DecisionAction, LLMJudgement, LLMOutcome, LLMOutcomeStatus, UserContext,
)
from telegram_llm_antispam.reputation import trust_context
from test_llm import _settings


@pytest.mark.parametrize("hours,count,score,expected", [
    (71, 30, 100, False), (72, 30, 80, True), (73, 29, 100, False), (73, 30, 79, False),
])
def test_protection_requires_age_normal_history_and_reputation(hours, count, score, expected):
    now = 1_000_000
    context = UserContext(-1001, 42, score, 100, first_seen_at=now-hours*3600,
                          normal_messages_seen=count)
    assert trust_context(context, _settings(), now=now)["is_trusted"] is expected
    assert trust_context(replace(context, joined_at=now), _settings(), now=now)["is_trusted"] is False


def test_message_and_normal_counts_are_deduplicated_and_rejoin_preserves_history(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.connect()
    db.migrate()
    try:
        message = SimpleNamespace(chat=SimpleNamespace(id=-1001), message_id=1,
                                  from_user=SimpleNamespace(id=42), text="正常讨论")
        features = build_message_features(message)
        db.record_message_seen(features, reputation_delta=2, confirmed_normal=True)
        db.record_message_seen(features, reputation_delta=2, confirmed_normal=True)
        context = db.get_user_context(-1001, 42)
        assert (context.messages_seen, context.normal_messages_seen, context.reputation_score) == (1, 1, 52)
        # A model-benign message can still fall back to a vote for a new account:
        # it counts as normal history without receiving the ALLOW-only +2 reward.
        db.record_message_seen(replace(features, message_id=2), confirmed_normal=True)
        db.record_user_join(-1001, 42, joined_at=123456)
        context = db.get_user_context(-1001, 42)
        assert (context.normal_messages_seen, context.reputation_score, context.joined_at) == (2, 52, 123456)
    finally:
        db.close()


def test_legacy_backfill_is_conservative_and_does_not_reward_again(tmp_path):
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO user_reputation VALUES (-1001,42,100,1,2,179,0,0)")
    for meta in ({"reputation_reward": 2}, {"reputation_reward": 2}, {}):
        conn.execute(
            "INSERT INTO action_log(chat_id,message_id,user_id,action,reason,confidence,metadata_json,created_at) "
            "VALUES (-1001,1,42,'allow','llm_not_spam',0.99,?,3)", (json.dumps(meta),)
        )
    conn.commit()
    conn.close()
    db = Database(path)
    db.connect()
    try:
        db.migrate()
        db.migrate()
        context = db.get_user_context(-1001, 42)
        assert context.normal_messages_seen == 1
        assert context.reputation_score == 100
        assert context.messages_seen == 179
    finally:
        db.close()


@pytest.mark.parametrize("source,outcome_kind,expected", [
    ("phrase", "normal", DecisionAction.ALLOW),
    ("content", "normal", DecisionAction.ALLOW),
    ("skeleton", "normal", DecisionAction.ALLOW),
    ("hard", "normal", DecisionAction.ALLOW),
    ("hard", "spam", DecisionAction.WITHDRAW_VOTE),
    ("hard", "failed", DecisionAction.REVIEW),
    ("hard", "disabled", DecisionAction.REVIEW),
])
def test_trusted_user_regression_uses_model_instead_of_local_vote_or_ban(
    tmp_path, monkeypatch, source, outcome_kind, expected
):
    db = Database(tmp_path / "db.sqlite")
    db.connect()
    db.migrate()
    captured = []

    async def apply(_self, _message, features, decision):
        captured.append(decision)
        return ActionResult(action_log_id=db.record_action(features, decision))

    monkeypatch.setattr("telegram_llm_antispam.handlers.ModerationActions.apply", apply)

    class Judge:
        async def judge(self, features):
            if outcome_kind in {"failed", "disabled"}:
                return LLMOutcome(
                    LLMOutcomeStatus.FAILED if outcome_kind == "failed" else LLMOutcomeStatus.DISABLED,
                    error="timeout",
                )
            return LLMOutcome(LLMOutcomeStatus.OK, judgement=LLMJudgement(
                is_spam=outcome_kind == "spam", confidence=0.99, category="benign"
            ))

    async def get_me():
        return SimpleNamespace(id=7777)

    async def get_chat(_user_id):
        return SimpleNamespace(bio=None)

    async def get_admins(_chat_id):
        return []

    try:
        db.allow_chat(-1001, "group", None)
        db.adjust_reputation(-1001, 42, 50)
        with db._locked_conn() as conn:
            conn.execute("UPDATE user_reputation SET first_seen_at=1,normal_messages_seen=30")
            conn.commit()
        db.upsert_fingerprint("phrase", phrase_fingerprint_value("机器人"), 60,
                              "llm_spam_phrase", pattern_text="机器人")
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-1001, type="supergroup"), message_id=1,
            from_user=SimpleNamespace(id=42, is_bot=False, username="member", first_name="Member"),
            text="@example 兼职日赚过万" if source == "hard" else "居然机器人没头像",
            bot=SimpleNamespace(get_me=get_me, get_chat=get_chat, get_chat_administrators=get_admins),
        )
        if source in {"content", "skeleton"}:
            features = build_message_features(message)
            db.upsert_fingerprint(
                source, features.content_hash if source == "content" else features.skeleton_hash,
                90, "vote_confirmed",
            )
        router = create_router(_settings(), db, Judge())
        handler = next(item for item in router.message.handlers if not item.filters)
        asyncio.run(handler.callback(message))
        assert captured[0].action == expected
        assert captured[0].metadata["user_trust"]["is_trusted"] is True
        assert captured[0].metadata["local_rule_reason"] in {
            "known_fingerprint", "hard_spam_message", "known_high_weight_fingerprint_generalized"
        }
        assert db.get_fingerprint(phrase_fingerprint_value("机器人")).weight == 60
    finally:
        db.close()


def test_admin_cache_invalidation_and_lookup_failure():
    cache = ChatAdminCache()
    members = [42]
    calls = []

    async def get_admins(chat_id):
        calls.append(chat_id)
        return [SimpleNamespace(user=SimpleNamespace(id=uid)) for uid in members]

    async def run():
        bot = SimpleNamespace(get_chat_administrators=get_admins)
        assert await cache.is_admin(bot, -1001, 42) is True
        members.clear()
        assert await cache.is_admin(bot, -1001, 42) is True
        cache.invalidate(-1001)
        assert await cache.is_admin(bot, -1001, 42) is False
        assert len(calls) == 2
        cache.invalidate(-1001)

        async def failure(_chat_id):
            raise RuntimeError("Telegram unavailable")

        assert await cache.is_admin(SimpleNamespace(get_chat_administrators=failure), -1001, 42) is None

    asyncio.run(run())


def test_bot_administrator_is_checked_by_member_status():
    cache = ChatAdminCache()

    async def get_admins(_chat_id):
        return []

    async def get_member(_chat_id, _user_id):
        return SimpleNamespace(status="administrator")

    bot = SimpleNamespace(get_chat_administrators=get_admins, get_chat_member=get_member)
    assert asyncio.run(cache.is_admin(bot, -1001, 42, is_bot=True)) is True


def test_chat_member_join_update_resets_observation(tmp_path):
    from datetime import datetime, timezone

    db = Database(tmp_path / "db.sqlite")
    db.connect()
    db.migrate()
    try:
        db.allow_chat(-1001, "group", None)
        db.adjust_reputation(-1001, 42, 50)
        router = create_router(_settings(), db)
        event = SimpleNamespace(
            chat=SimpleNamespace(id=-1001), date=datetime.fromtimestamp(123456, timezone.utc),
            old_chat_member=SimpleNamespace(status="left"),
            new_chat_member=SimpleNamespace(status="member", user=SimpleNamespace(id=42)),
        )
        asyncio.run(router.chat_member.handlers[0].callback(event))
        assert db.get_user_context(-1001, 42).joined_at == 123456
        assert db.get_user_context(-1001, 42).reputation_score == 100
    finally:
        db.close()


def test_admin_lookup_failure_prevents_local_punishment(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    db.connect()
    db.migrate()
    decisions = []

    async def apply(_self, _message, _features, decision):
        decisions.append(decision)
        return ActionResult()

    async def get_me():
        return SimpleNamespace(id=7777)

    async def get_chat(_user_id):
        return SimpleNamespace(bio=None)

    async def failure(_chat_id):
        raise RuntimeError("API failure")

    monkeypatch.setattr("telegram_llm_antispam.handlers.ModerationActions.apply", apply)
    try:
        db.allow_chat(-1001, "group", None)
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-1001, type="supergroup"), message_id=1,
            from_user=SimpleNamespace(id=42, is_bot=False, first_name="Member"),
            text="@example 兼职日赚过万",
            bot=SimpleNamespace(get_me=get_me, get_chat=get_chat, get_chat_administrators=failure),
        )
        router = create_router(_settings(), db)
        handler = next(item for item in router.message.handlers if not item.filters)
        asyncio.run(handler.callback(message))
        assert decisions[0].action == DecisionAction.REVIEW
        assert decisions[0].reason == "admin_identity_unverified"
    finally:
        db.close()


@pytest.mark.parametrize("anonymous,new_member", [(False, False), (True, False), (False, True)])
def test_admin_immunity_does_not_skip_invited_new_member(tmp_path, monkeypatch, anonymous, new_member):
    db = Database(tmp_path / "db.sqlite")
    db.connect()
    db.migrate()
    judged = []

    class Judge:
        async def judge(self, features):
            judged.append(features.user_id)
            return LLMOutcome(LLMOutcomeStatus.OK, judgement=LLMJudgement(False, 0.99, "benign"))

    async def get_me():
        return SimpleNamespace(id=7777)

    async def get_chat(_user_id):
        return SimpleNamespace(bio=None)

    async def get_admins(_chat_id):
        return [SimpleNamespace(user=SimpleNamespace(id=42))]

    try:
        db.allow_chat(-1001, "group", None)
        user = SimpleNamespace(id=42, is_bot=False, first_name="Admin")
        invited = SimpleNamespace(id=43, is_bot=False, first_name="Member")
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-1001, type="supergroup"), message_id=1, from_user=user,
            text="@example 兼职日赚过万" if not new_member else None,
            sender_chat=SimpleNamespace(id=-1001) if anonymous else None,
            new_chat_members=[invited] if new_member else None,
            bot=SimpleNamespace(get_me=get_me, get_chat=get_chat, get_chat_administrators=get_admins),
        )
        router = create_router(_settings(), db, Judge())
        handler = next(item for item in router.message.handlers if not item.filters)
        asyncio.run(handler.callback(message))
        assert judged == ([43] if new_member else [])
        if new_member:
            assert db.get_user_context(-1001, 43).joined_at is not None
            assert db.get_user_context(-1001, 43).normal_messages_seen == 0
    finally:
        db.close()
