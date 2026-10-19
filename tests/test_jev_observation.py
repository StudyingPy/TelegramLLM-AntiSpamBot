from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from telegram_llm_antispam.config import Settings
from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.jev import CATEGORIES, QUESTIONS, JevClient, digest, parse_response
from telegram_llm_antispam.jev_live_report import export_report, moderation_events, summarize
from telegram_llm_antispam.jev_observation import JevObserver, create_jev_observer
from telegram_llm_antispam.jev_store import ObservationStore, import_annotations, read_observations
from telegram_llm_antispam.llm import _feature_payload
from telegram_llm_antispam.models import (
    ActionResult,
    DecisionAction,
    LLMJudgement,
    LLMOutcome,
    LLMOutcomeStatus,
    LocalDecision,
)
from test_llm import _settings


def response(p=0.95, choice="ads", model="jev-1.13.0"):
    return {
        "model": model,
        "answers": {
            "is_spam": {"type": "noul", "noul": p},
            "category": {
                "type": "choice",
                "choice": choice,
                "confidence": 0.91,
                "probabilities": {key: 1 if key == choice else 0 for key in CATEGORIES},
            },
        },
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def settings(tmp_path, **kwargs):
    return replace(
        _settings(),
        jev_mode="shadow",
        jev_api_key="unit-test-secret",
        jev_chat_ids=(-1001,),
        jev_sample_rate=1,
        jev_observation_path=tmp_path / "observations.db",
        **kwargs,
    )


def features(text="正常技术讨论\u200b https://example.org"):
    msg = SimpleNamespace(
        chat=SimpleNamespace(id=-1001), message_id=9, from_user=SimpleNamespace(id=42), text=text
    )
    value = build_message_features(msg)
    value.metadata.update(
        {
            "sender_profile": {"display_name": "昵称", "bio": "普通资料"},
            "personal_chat": {"title": "频道", "messages": ["当时内容"]},
            "og_preview": {"title": "文档", "description": "简介"},
            "edited_from": {"text": "原版本", "content_hash": "prior"},
            "user_trust": {"is_trusted": False},
            "update_type": "message",
            "llm_outcome": {"is_spam": True},
            "final_action": "ban",
        }
    )
    return value


LOCAL = LocalDecision(DecisionAction.REVIEW, "unmatched_message_needs_llm", 0, True)


class Baseline:
    def __init__(self, spam=False, status=LLMOutcomeStatus.OK):
        self.inputs, self.feature_calls = [], 0
        self.outcome = LLMOutcome(
            status,
            provider_count=1,
            judgement=LLMJudgement(spam, 0.96, "ads" if spam else "benign")
            if status == LLMOutcomeStatus.OK
            else None,
        )

    async def judge_payload(self, payload):
        self.inputs.append(payload)
        return self.outcome

    async def judge(self, f):
        self.feature_calls += 1
        return self.outcome


class Client:
    def __init__(self, p=0.95, fail=None):
        self.inputs, self.p, self.fail = [], p, fail

    async def decide(self, state):
        self.inputs.append(state)
        if self.fail:
            raise self.fail
        return {**parse_response(response(self.p), "jev-1.13.0"), "elapsed_ms": 1}


def observer(tmp_path, client=None, **kwargs):
    s = settings(tmp_path, **kwargs)
    return JevObserver(s, ObservationStore(s.jev_observation_path), client or Client())


def test_default_off_has_no_files_or_requests(tmp_path):
    s = replace(
        _settings(), jev_api_key="present-but-disabled", jev_observation_path=tmp_path / "never.db"
    )
    assert create_jev_observer(s) is None
    assert not s.jev_observation_path.exists()


@pytest.mark.parametrize(
    "change",
    [
        {"jev_mode": "primary"},
        {"jev_chat_ids": ()},
        {"jev_model": "jev-latest"},
        {"jev_sample_rate": float("nan")},
        {"jev_sample_rate": 0},
        {"jev_max_calls_per_day": 0},
        {"jev_timeout_seconds": 100},
        {"jev_retention_days": 31},
        {"jev_api_key": None},
    ],
)
def test_opt_in_requires_valid_bounded_configuration(tmp_path, change):
    with pytest.raises(ValueError):
        create_jev_observer(replace(settings(tmp_path), **change))


def test_separate_database_and_secret_repr(tmp_path):
    s = settings(tmp_path)
    assert "unit-test-secret" not in repr(s)
    with pytest.raises(ValueError, match="separate"):
        create_jev_observer(replace(s, jev_observation_path=s.database_path))


def test_questions_preserve_original_chinese_boundaries():
    for question in QUESTIONS.values():
        assert "personal_chat" in question["instructions"]
        assert "仅广告Bio不能把正常正文或空正文判为广告" in question["instructions"]
        assert "引用举报或玩笑不算" in question["instructions"]
        assert "JSON schema" not in question["instructions"]


def test_production_questions_match_evaluated_short_zh_full():
    from telegram_llm_antispam.jev import LEGACY_QUESTIONS, QUESTIONS_HASH
    from telegram_llm_antispam.jev_prompt_lab import VARIANTS

    assert QUESTIONS == VARIANTS["short_zh_full"]["questions"]
    assert VARIANTS["short_zh_full"]["input"] == "full"
    assert QUESTIONS_HASH != digest(LEGACY_QUESTIONS)
    assert QUESTIONS_HASH == "c6163e85797b1001f9dd2d6a1aa8fd776f31fe22ce2294093ac8c6d3d343cbdd"
    assert (
        digest(LEGACY_QUESTIONS)
        == "cc20caa12b86126ea7ee313ef3ad7f073044bec337992a7d0f3765c512f3a677"
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["answers"]["is_spam"].update(noul=True),
        lambda b: b["answers"]["is_spam"].update(noul=float("nan")),
        lambda b: b["answers"]["category"]["probabilities"].pop("benign"),
        lambda b: b["answers"]["category"].update(confidence=1.2),
        lambda b: b.update(model="jev-9.0.0"),
        lambda b: b["usage"].update(input_tokens=-1),
    ],
)
def test_malformed_provider_output_rejected(mutate):
    body = response()
    mutate(body)
    with pytest.raises(ValueError):
        parse_response(body, "jev-1.13.0")


def test_native_http_contract_and_independent_probabilities():
    def handle(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/systemone"
        assert body["questions"] == QUESTIONS
        assert body["state"] == {"raw_text": "原文\u200b"}
        assert "messages" not in body
        return httpx.Response(200, json=response(0.2, "traffic_diversion"))

    client = JevClient("secret", "jev-1.13.0", 1, transport=httpx.MockTransport(handle))
    result = asyncio.run(client.decide({"raw_text": "原文\u200b"}))
    assert result["status"] == "ok"
    assert result["noul"] == 0.2
    assert result["category_confidence"] == 0.91
    assert result["category_noul_disagreement"] is True


def test_error_body_never_exposes_key():
    transport = httpx.MockTransport(lambda r: httpx.Response(401, text="echo secret"))
    result = asyncio.run(JevClient("secret", "jev-1.13.0", 1, transport=transport).decide({}))
    assert result["error"] == "http_401"
    assert "secret" not in json.dumps(result)


def test_same_frozen_input_without_judgement_leak_and_baseline_controls_outcome(tmp_path):
    ob = observer(tmp_path)
    base = Baseline(False)
    f = features()
    try:
        outcome, oid = asyncio.run(ob.compare(f, LOCAL, base))
        assert outcome == base.outcome
        assert outcome.judgement.is_spam is False  # Jev .95 has zero authority.
        assert ob.client.inputs == base.inputs == [_feature_payload(f)]
        assert "llm_outcome" not in base.inputs[0]
        assert "final_action" not in base.inputs[0]
        rows, _ = read_observations(ob.settings.jev_observation_path)
        assert rows[0]["state"]["raw_text"] == f.text
        assert (
            rows[0]["payload_hash"] == rows[0]["baseline"]["input_hash"] == digest(base.inputs[0])
        )
        assert oid == rows[0]["id"]
        assert "unit-test-secret" not in json.dumps(rows)
    finally:
        ob.store.close()


@pytest.mark.parametrize("failure", [RuntimeError("secret"), TimeoutError()])
def test_jev_failure_preserves_baseline(tmp_path, failure):
    ob = observer(tmp_path, Client(fail=failure))
    base = Baseline()
    try:
        outcome, _ = asyncio.run(ob.compare(features(), LOCAL, base))
        assert outcome == base.outcome and len(base.inputs) == 1
        rows, _ = read_observations(ob.settings.jev_observation_path)
        assert rows[0]["jev"]["status"] == "failed"
    finally:
        ob.store.close()


def test_timeout_is_bounded_and_baseline_still_runs(tmp_path):
    class Slow(Client):
        async def decide(self, state):
            await asyncio.sleep(1)

    ob = observer(tmp_path, Slow(), jev_timeout_seconds=0.01)
    base = Baseline()
    try:
        outcome, _ = asyncio.run(ob.compare(features(), LOCAL, base))
        assert outcome == base.outcome
        assert ob._active == 0
    finally:
        ob.store.close()


def test_storage_failure_calls_baseline_once_without_jev(tmp_path):
    ob = observer(tmp_path)
    ob.store.close()
    base = Baseline()
    outcome, oid = asyncio.run(ob.compare(features(), LOCAL, base))
    assert outcome == base.outcome and oid is None
    assert base.feature_calls == 1 and not ob.client.inputs


def test_local_bypass_never_invents_original_call(tmp_path):
    ob = observer(tmp_path, jev_observe_local_bypass=True)
    base = Baseline()
    local = LocalDecision(DecisionAction.BAN, "hard_spam_message", 0.96, False)
    try:
        outcome, oid = asyncio.run(ob.compare(features(), local, base))
        assert outcome is None and oid
        assert not base.inputs and not base.feature_calls
        rows, _ = read_observations(ob.settings.jev_observation_path)
        assert rows[0]["baseline"]["status"] == "not_called"
        assert summarize(rows, [])["common_successful_observations"] == 0
    finally:
        ob.store.close()


def test_daily_budget_and_valid_cache(tmp_path):
    ob = observer(tmp_path, jev_max_calls_per_day=1)
    base = Baseline()

    async def run():
        await ob.compare(features(), LOCAL, base)
        await ob.compare(features(), LOCAL, base)
        await ob.compare(features("另一条消息"), LOCAL, base)

    try:
        asyncio.run(run())
        rows, _ = read_observations(ob.settings.jev_observation_path)
        assert len(ob.client.inputs) == 1
        assert rows[1]["jev"]["cache_hit"] is True
        assert rows[2]["jev"]["reason"] == "daily_budget"
        stats = summarize(rows, [])
        assert stats["api_attempts"] == 1 and stats["cache_hits"] == 1
        assert len(base.inputs) == 3
    finally:
        ob.store.close()


def test_retention_expiry_does_not_touch_moderation_database(tmp_path):
    ob = observer(tmp_path)
    try:
        asyncio.run(ob.compare(features(), LOCAL, Baseline()))
        ob.store.prune(time.time() + 31 * 86400)
        assert not read_observations(ob.settings.jev_observation_path)[0]
    finally:
        ob.store.close()


def annotation(row, reviewer="reviewer-1", label="ham", task="message"):
    return {
        "observation_id": row["id"],
        "payload_hash": row["payload_hash"],
        "reviewer_id": reviewer,
        "task": task,
        "label": label,
        "evidence_sufficient": True,
        "note": "独立核查",
    }


def test_report_requires_independent_humans_and_uses_identical_ids(tmp_path):
    ob = observer(tmp_path)
    try:
        _, oid = asyncio.run(ob.compare(features(), LOCAL, Baseline()))
        ob.record_final(
            oid,
            LocalDecision(DecisionAction.ALLOW, "llm_not_spam", 0.96),
            ActionResult(action_log_id=1),
        )
        path = ob.settings.jev_observation_path
        rows, _ = read_observations(path)
        import_annotations(path, [annotation(rows[0])])
        assert summarize(*read_observations(path))["paired_message_jev"]["n"] == 0
        import_annotations(path, [annotation(rows[0], "reviewer-2")])
        stats = summarize(*read_observations(path))
        assert stats["paired_labelled_ids"] == [oid]
        assert stats["paired_message_jev"]["n"] == stats["paired_message_baseline"]["n"] == 1
        assert stats["paired_message_jev"]["fp"] == 1
        assert stats["paired_message_baseline"]["tn"] == 1
        assert stats["user_level_accuracy"] is None
        export_report(path, tmp_path / "export", sample_size=1)
        blind = json.loads((tmp_path / "export/blind_review.jsonl").read_text(encoding="utf-8"))
        assert "baseline" not in blind and "jev" not in blind and "final" not in blind
        assert blind["state"] == rows[0]["state"]
    finally:
        ob.store.close()


def test_conflicting_or_account_labels_cannot_become_message_gold(tmp_path):
    ob = observer(tmp_path)
    try:
        asyncio.run(ob.compare(features(), LOCAL, Baseline()))
        path = ob.settings.jev_observation_path
        rows, _ = read_observations(path)
        import_annotations(
            path,
            [
                annotation(rows[0], "a", "spam", "account"),
                annotation(rows[0], "b", "spam", "account"),
            ],
        )
        assert summarize(*read_observations(path))["human_consensus_labels"] == 0
        import_annotations(path, [annotation(rows[0], "a"), annotation(rows[0], "b", "spam")])
        assert summarize(*read_observations(path))["human_consensus_labels"] == 0
        bad = annotation(rows[0], "c")
        bad["payload_hash"] = "wrong revision"
        with pytest.raises(ValueError):
            import_annotations(path, [annotation(rows[0], "d"), bad])
        with sqlite3.connect(path) as conn:
            assert (
                conn.execute("SELECT COUNT(*) FROM annotations WHERE reviewer_id='d'").fetchone()[0]
                == 0
            )
    finally:
        ob.store.close()


def test_environment_opt_in_is_separate_from_presence_of_key(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", "test-env-key")
    monkeypatch.delenv("JEV_MODE", raising=False)
    s = Settings.from_env(tmp_path / "missing.env")
    assert s.jev_mode == "off" and create_jev_observer(s) is None


@pytest.mark.parametrize(
    "error,reason",
    [
        ("http_401", "auth_disabled_until_restart"),
        ("http_429", "rate_limit_cooldown"),
    ],
)
def test_auth_and_rate_failures_do_not_retry_or_block_baseline(tmp_path, error, reason):
    class Rejected(Client):
        async def decide(self, state):
            self.inputs.append(state)
            return {"status": "failed", "error": error, "retry_after_seconds": 10}

    ob = observer(tmp_path, Rejected())
    base = Baseline()

    async def run():
        await ob.compare(features(), LOCAL, base)
        await ob.compare(features("不同消息"), LOCAL, base)

    try:
        asyncio.run(run())
        rows, _ = read_observations(ob.settings.jev_observation_path)
        assert rows[0]["jev"]["api_attempted"] is True
        assert rows[1]["jev"]["reason"] == reason
        assert len(ob.client.inputs) == 1 and len(base.inputs) == 2
    finally:
        ob.store.close()


def test_concurrency_and_cancellation_release_slot(tmp_path):
    class Blocked(Client):
        async def decide(self, state):
            self.inputs.append(state)
            started.set()
            await asyncio.Event().wait()

    ob = observer(tmp_path, Blocked(), jev_max_concurrent=1)
    base = Baseline()

    async def run():
        nonlocal started
        started = asyncio.Event()
        first = asyncio.create_task(ob.compare(features(), LOCAL, base))
        await started.wait()
        await ob.compare(features("第二条"), LOCAL, base)
        assert len(base.inputs) == 1
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert ob._active == 0

    started = None
    try:
        asyncio.run(run())
        rows, _ = read_observations(ob.settings.jev_observation_path)
        assert rows[1]["jev"]["reason"] == "concurrency_limit"
    finally:
        ob.store.close()


def test_cache_changes_with_context_questions_and_model(tmp_path, monkeypatch):
    import telegram_llm_antispam.jev_observation as module

    ob = observer(tmp_path)
    base = Baseline()

    async def run():
        await ob.compare(features(), LOCAL, base)
        changed = features()
        changed.metadata["sender_profile"]["bio"] = "不同的当时资料"
        await ob.compare(changed, LOCAL, base)
        monkeypatch.setattr(module, "QUESTIONS_HASH", "next-question-version")
        await ob.compare(changed, LOCAL, base)
        ob.settings = replace(ob.settings, jev_model="jev-1.14.0")
        await ob.compare(changed, LOCAL, base)

    try:
        asyncio.run(run())
        rows, _ = read_observations(ob.settings.jev_observation_path)
        assert len(ob.client.inputs) == 4
        assert len({r["request_hash"] for r in rows}) == 4
    finally:
        ob.store.close()


def test_report_separates_versions_and_profile_tasks(tmp_path):
    ob = observer(tmp_path)

    async def run():
        await ob.compare(features(), LOCAL, Baseline())
        ob.settings = replace(ob.settings, jev_model="jev-1.14.0")
        await ob.compare(features("下一条完整消息"), LOCAL, Baseline())
        profile = features("")
        profile.metadata["update_type"] = "new_chat_member"
        await ob.compare(profile, LOCAL, Baseline())

    try:
        asyncio.run(run())
        path = ob.settings.jev_observation_path
        rows, _ = read_observations(path)
        import_annotations(path, [annotation(r, reviewer) for r in rows for reviewer in ("a", "b")])
        stats = export_report(path, tmp_path / "export", sample_size=3)
        assert stats["mixed_cohort_accuracy_suppressed"]
        assert stats["paired_message_jev"] is None
        assert sum(c["jev"]["n"] for c in stats["paired_cohorts"]) == 2
        blind = [
            json.loads(line)
            for line in (tmp_path / "export/blind_review.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert next(r for r in blind if r["update_type"] == "new_chat_member")["task"] == "profile"
    finally:
        ob.store.close()


@pytest.mark.parametrize("baseline_spam", [False, True])
@pytest.mark.parametrize("mode", ["shadow", "review", "review_failure"])
def test_real_handler_actions_reputation_and_learning_use_baseline_only(
    tmp_path, baseline_spam, mode, monkeypatch
):
    from telegram_llm_antispam.db import Database
    from telegram_llm_antispam.handlers import create_router

    db = Database(tmp_path / "moderation.db")
    db.connect()
    db.migrate()
    ob = observer(tmp_path, Client(p=0.01 if baseline_spam else 0.99))
    base = Baseline(spam=baseline_spam)
    if mode.startswith("review"):
        ob.settings = replace(ob.settings, jev_mode="review", jev_chat_ids=(), jev_sample_rate=0.01)
    if mode == "review_failure":
        base = Baseline(status=LLMOutcomeStatus.FAILED)
        from telegram_llm_antispam.rules import RuleEngine

        monkeypatch.setattr(
            RuleEngine,
            "evaluate",
            lambda *args, **kwargs: LocalDecision(
                DecisionAction.BAN, "local_hit_needs_model", 0.96, True
            ),
        )
    calls = []

    async def get_me():
        return SimpleNamespace(id=7777)

    async def get_chat(user_id):
        return SimpleNamespace(bio=None)

    async def delete_message(*args, **kwargs):
        calls.append("delete")

    async def ban_chat_member(*args, **kwargs):
        calls.append("ban")

    async def get_chat_member(chat_id, user_id):
        return SimpleNamespace(
            status="administrator" if user_id == 7777 else "member",
            can_delete_messages=True,
            can_restrict_members=True,
        )

    async def send_message(*args, **kwargs):
        return SimpleNamespace(message_id=200)

    bot = SimpleNamespace(
        get_me=get_me,
        get_chat=get_chat,
        delete_message=delete_message,
        ban_chat_member=ban_chat_member,
        get_chat_member=get_chat_member,
        send_message=send_message,
    )
    msg = SimpleNamespace(
        message_id=100,
        chat=SimpleNamespace(id=-1001, type="supergroup", title="test"),
        from_user=SimpleNamespace(
            id=42,
            is_bot=False,
            username="member",
            first_name="Member",
            last_name=None,
            language_code="zh-hans",
            is_premium=None,
        ),
        text="这是一条完整的技术讨论消息",
        caption=None,
        entities=None,
        caption_entities=None,
        link_preview_options=None,
        bot=bot,
        sender_chat=None,
        is_automatic_forward=False,
        new_chat_members=None,
    )
    try:
        db.allow_chat(-1001, "test", added_by_user_id=None)
        router = create_router(ob.settings, db, llm=base, jev_observer=ob)
        handler = next(h for h in router.message.handlers if not h.filters)
        asyncio.run(handler.callback(msg))
        context = db.get_user_context(-1001, 42)
        with db._locked_conn() as conn:
            actions = conn.execute(
                "SELECT action,metadata_json FROM action_log ORDER BY id"
            ).fetchall()
            learned = conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
        rows, _ = read_observations(ob.settings.jev_observation_path)
        assert len(rows) == len(base.inputs) == 1
        expected = "review" if mode == "review_failure" else "ban" if baseline_spam else "allow"
        assert rows[0]["final"]["action"] == expected
        assert json.loads(actions[0]["metadata_json"])["jev_observation_id"] == rows[0]["id"]
        if mode == "review_failure":
            assert not calls and not learned and context.reputation_score == 50
            assert rows[0]["final"]["reason"] == "mimo_review_unavailable"
        elif baseline_spam:
            assert "ban" in calls and context.reputation_score <= 50 and learned > 0
        else:
            assert calls == [] and learned == 0
            assert context.reputation_score == 50 + ob.settings.normal_message_reputation_reward
        if mode.startswith("review"):
            chain = json.loads(actions[0]["metadata_json"])["model_chain"]
            assert chain["primary"] == "jev" and chain["reviewer"] == "mimo"
            assert chain["decision_source"] == (
                "pending_review" if mode == "review_failure" else "mimo_review"
            )
            from telegram_llm_antispam.notifications import _format_llm_section

            shown = _format_llm_section(
                LocalDecision(
                    DecisionAction.REVIEW,
                    "test",
                    0,
                    metadata=json.loads(actions[0]["metadata_json"]),
                )
            )
            assert "Jev初审" in shown and "MiMo复核" in shown
            assert ("复核未完成" if mode == "review_failure" else "结论不一致") in shown
    finally:
        ob.store.close()
        db.close()


def test_human_primary_session_is_distinct_from_sibling_closure(tmp_path):
    from test_actions import FakeBot, _db, _features
    from telegram_llm_antispam.actions import ModerationActions

    db = _db(tmp_path)
    try:
        actions = ModerationActions(_settings(), db)
        actions.SUMMARY_DELETE_DELAY_SECONDS = 0
        ids = [
            db.create_vote_session(
                _features(mid),
                LocalDecision(DecisionAction.WITHDRAW_VOTE, "known_fingerprint", 0.85),
                timeout_seconds=3600,
            )
            for mid in (100, 101)
        ]
        asyncio.run(actions.admin_ban_vote_session(FakeBot(), ids[1], moderator_user_id=9001))
        with db._locked_conn() as conn:
            events = conn.execute(
                "SELECT metadata_json FROM action_log WHERE action='admin_banned_user'"
            ).fetchall()
        metadata = [json.loads(row[0]) for row in events]
        assert len(metadata) == 2
        assert {m["primary_vote_session_id"] for m in metadata} == {ids[1]}
        assert {m["vote_session_id"] for m in metadata} == set(ids)
        rows = [
            {
                "id": str(sid),
                "chat_id": -1001,
                "message_id": mid,
                "created_at": 0,
                "final": {"vote_session_id": sid},
            }
            for sid, mid in zip(ids, (100, 101))
        ]
        reported = moderation_events(tmp_path / "bot.db", rows)
        assert reported[str(ids[0])][0]["is_primary"] is False
        assert reported[str(ids[1])][0]["is_primary"] is True
        # Historical events without a click target must remain unknown.
        with db._locked_conn() as conn:
            old = conn.execute(
                "SELECT id,metadata_json FROM action_log WHERE action='admin_banned_user' LIMIT 1"
            ).fetchone()
            value = json.loads(old["metadata_json"])
            del value["primary_vote_session_id"]
            conn.execute(
                "UPDATE action_log SET metadata_json=? WHERE id=?", (json.dumps(value), old["id"])
            )
            conn.commit()
        reported = moderation_events(tmp_path / "bot.db", rows)
        assert any(event["is_primary"] is None for events in reported.values() for event in events)
    finally:
        db.close()


def test_frozen_baseline_payload_preserves_sdk_contract_and_model_usage(tmp_path):
    from test_llm import _FakeAsyncOpenAI
    from telegram_llm_antispam.llm import NewAPIJudge, _SYSTEM_PROMPT

    async def behavior(kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"is_spam":false,"confidence":0.99,"category":"benign"}'
                    )
                )
            ],
            model="actual-model-version",
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120),
        )

    client = _FakeAsyncOpenAI(behavior)

    class Judge(NewAPIJudge):
        def _build_client(self, provider):
            return client

    s = replace(
        _settings(),
        newapi_base_url="https://test.invalid",
        newapi_api_key="fake",
        newapi_model="requested-model",
    )
    payload = _feature_payload(features())
    result = asyncio.run(Judge(s).judge_payload(payload))
    request = client.chat.completions.calls[0]
    assert request["messages"][0]["content"] == _SYSTEM_PROMPT
    assert json.loads(request["messages"][1]["content"]) == payload
    assert result.metadata["requested_model"] == "requested-model"
    assert result.metadata["resolved_model"] == "actual-model-version"
    assert result.metadata["usage"]["total_tokens"] == 120
    assert client.closed


def test_review_routes_all_requests_without_sampling_cache_or_daily_skips(tmp_path):
    ob = observer(tmp_path, jev_max_calls_per_day=1)
    ob.settings = replace(ob.settings, jev_mode="review", jev_chat_ids=(), jev_sample_rate=0.00001)
    base = Baseline()

    async def run():
        for _ in range(3):
            await ob.compare(features(), LOCAL, base)

    try:
        asyncio.run(run())
        assert len(ob.client.inputs) == len(base.inputs) == 3
        rows, _ = read_observations(ob.settings.jev_observation_path)
        assert all(r["sampling_probability"] == 1 and r["mode"] == "review" for r in rows)
        assert all(r["jev"]["api_attempted"] for r in rows)
    finally:
        ob.close()


def test_review_queues_when_full_and_waits_before_mimo(tmp_path):
    sequence = []

    class Queued(Client):
        async def decide(self, state):
            sequence.append(("jev", state["raw_text"]))
            assert ob._active == 1
            await asyncio.sleep(0.01)
            return await super().decide(state)

    class Reviewer(Baseline):
        async def judge_payload(self, payload):
            sequence.append(("mimo", payload["raw_text"]))
            return await super().judge_payload(payload)

    ob = observer(tmp_path, Queued(), jev_max_concurrent=1)
    ob.settings = replace(ob.settings, jev_mode="review")
    base = Reviewer()

    async def run():
        await asyncio.gather(*(ob.compare(features(str(i)), LOCAL, base) for i in range(3)))

    try:
        asyncio.run(run())
        assert len(ob.client.inputs) == len(base.inputs) == 3
        for i in range(3):
            assert sequence.index(("jev", str(i))) < sequence.index(("mimo", str(i)))
        rows, _ = read_observations(ob.settings.jev_observation_path)
        assert all(r["jev"]["status"] == "ok" and r["jev"]["queue_ms"] >= 0 for r in rows)
    finally:
        ob.close()


@pytest.mark.parametrize("storage", ["closed", "missing"])
def test_review_storage_failure_does_not_remove_primary(tmp_path, storage):
    ob = observer(tmp_path)
    ob.settings = replace(ob.settings, jev_mode="review")
    ob.store.close()
    if storage == "missing":
        ob.store = None
    base = Baseline()
    outcome, oid = asyncio.run(ob.compare(features(), LOCAL, base))
    assert len(ob.client.inputs) == len(base.inputs) == 1 and oid is None
    assert outcome.metadata["model_chain"]["decision_source"] == "mimo_review"


def test_review_primary_failure_still_requires_independent_reviewer(tmp_path):
    ob = observer(tmp_path, Client(fail=TimeoutError()))
    ob.settings = replace(ob.settings, jev_mode="review")
    base = Baseline()
    try:
        outcome, _ = asyncio.run(ob.compare(features(), LOCAL, base))
        assert len(base.inputs) == 1
        assert outcome.judgement == base.outcome.judgement
        assert outcome.metadata["model_chain"]["primary_result"]["status"] == "failed"
        assert outcome.metadata["model_chain"]["agreement"] is None
    finally:
        ob.close()


def test_review_factory_requires_reviewer_and_accepts_no_observation_store(tmp_path, monkeypatch):
    import telegram_llm_antispam.jev_observation as module

    s = replace(settings(tmp_path), jev_mode="review", jev_chat_ids=(), jev_max_calls_per_day=0)
    with pytest.raises(ValueError, match="reviewer"):
        create_jev_observer(s)
    s = replace(
        s,
        newapi_base_url="https://test.invalid",
        newapi_api_key="fake",
        newapi_model="mimo-v2.6-flash",
    )
    monkeypatch.setattr(module, "ObservationStore", lambda *args: (_ for _ in ()).throw(OSError()))
    ob = create_jev_observer(s)
    assert ob.requires_review and ob.store is None
    ob.close()
