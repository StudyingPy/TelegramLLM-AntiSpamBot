from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from telegram_llm_antispam.db import Database, SCHEMA_SQL
from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.feedback import phrase_fingerprint_value, record_vote_ham_feedback
from telegram_llm_antispam.fingerprint_audit import (
    FingerprintAuditor, handle_review_callback, review_detail, show_reviews,
)
from telegram_llm_antispam.llm import NewAPIJudge, _parse_fingerprint_audit
from telegram_llm_antispam.models import (
    DecisionAction, FingerprintAuditOutcome, FingerprintAuditResult, LLMOutcomeStatus, LocalDecision,
)
from test_llm import _settings


def _db(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.connect()
    db.migrate()
    return db


def _seed(db, phrase="机器人", *, pattern=True):
    value = phrase_fingerprint_value(phrase)
    db.upsert_fingerprint("phrase", value, 60, "llm_spam_phrase",
                          pattern_text=phrase if pattern else None)
    return db.get_fingerprint(value)


def _propose(db, fid):
    run_id = db.get_or_start_fingerprint_audit(86400, 1000)
    payload = db.fingerprint_audit_payload(fid)
    db.record_fingerprint_audit_result(
        run_id, payload, FingerprintAuditResult(fid, "disable", 0.99, "普通名词，容易误判")
    )
    return db.pending_fingerprint_reviews()[0]["id"]


def test_disable_blocks_matching_and_relearning_restore_is_explicit(tmp_path):
    db = _db(tmp_path)
    try:
        fp = _seed(db)
        rid = _propose(db, fp.id)
        assert db.resolve_fingerprint_review(rid, "disable", 5439265612)
        assert not db.resolve_fingerprint_review(rid, "disable", 5439265612)
        assert not db.get_strongest_fingerprint((("phrase", fp.value),))
        db.upsert_fingerprint("phrase", fp.value, 95, "llm_spam_phrase", pattern_text="机器人")
        assert db.get_fingerprint(fp.value).enabled is False
        assert db.get_fingerprint(fp.value).weight == 60
        assert db.resolve_fingerprint_review(rid, "restore", 5439265612)
        assert not db.resolve_fingerprint_review(rid, "restore", 5439265612)
        assert db.get_strongest_fingerprint((("phrase", fp.value),)).enabled
        with db._locked_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM fingerprint_review_actions").fetchone()[0] == 2
    finally:
        db.close()


def test_release_penalizes_actual_phrase_once(tmp_path):
    db = _db(tmp_path)
    try:
        fp = _seed(db)
        features = build_message_features({"chat": {"id": -1001}, "message_id": 9,
                                           "from_user": {"id": 42}, "text": "居然机器人没头像"})
        decision = LocalDecision(DecisionAction.WITHDRAW_VOTE, "known_fingerprint", 0.6,
                                 metadata={"fingerprint_id": fp.id})
        db.record_action(features, decision, {"text_snapshot": features.text})
        sid = db.create_vote_session(features, decision, 60)
        db.close_vote_session(sid, "released")
        session = db.get_vote_session(sid)
        record_vote_ham_feedback(db, session, _settings())
        record_vote_ham_feedback(db, session, _settings())
        after = db.get_fingerprint(fp.value)
        assert (after.weight, after.false_positive_count) == (30, 1)
        assert db.fingerprint_audit_payload(fp.id)["samples"][0]["label"] == "manual_ham"
    finally:
        db.close()


def test_legacy_phrase_recovery_and_missed_feedback_are_idempotent(tmp_path):
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    value = phrase_fingerprint_value("机器人")
    conn.execute("INSERT INTO fingerprints(id,fingerprint_type,value,weight,source,created_at) "
                 "VALUES (9055,'phrase',?,60,'llm_spam_phrase',1)", (value,))
    for mid, meta in [(1, {"llm_outcome": {"signal_phrases": ["机器人"], "is_spam": True},
                           "text_snapshot": "点击机器人领取广告奖励 @examplebot"}),
                      (2, {"fingerprint_id": 9055, "text_snapshot": "居然机器人没头像"})]:
        conn.execute("INSERT INTO action_log(chat_id,message_id,user_id,action,reason,confidence,metadata_json,created_at) "
                     "VALUES (-1001,?,42,'withdraw_vote','known_fingerprint',0.6,?,2)", (mid, json.dumps(meta)))
    conn.execute("INSERT INTO vote_sessions(chat_id,original_message_id,suspect_user_id,status,reason,created_at,expires_at) "
                 "VALUES (-1001,2,42,'released','known_fingerprint',2,3)")
    conn.execute("INSERT INTO vote_sessions(chat_id,original_message_id,suspect_user_id,status,reason,created_at,expires_at) "
                 "VALUES (-1001,1,42,'expired_released','known_fingerprint',2,3)")
    conn.commit()
    conn.close()
    db = Database(path)
    db.connect()
    try:
        db.migrate()
        db.migrate()
        fp = db.get_fingerprint(value)
        assert (fp.weight, fp.false_positive_count) == (30, 1)
        payload = db.fingerprint_audit_payload(9055)
        assert payload["pattern_text"] == "机器人"
        assert len(payload["samples"]) <= 3
    finally:
        db.close()


@pytest.mark.parametrize("results", [
    [{"fingerprint_id": 999, "decision": "disable", "confidence": 1, "reason": "坏"}],
    [{"fingerprint_id": True, "decision": "disable", "confidence": 1, "reason": "坏"}],
    [{"fingerprint_id": 1, "decision": "disable", "confidence": float('nan'), "reason": "坏"}],
    [],
])
def test_audit_output_rejects_foreign_ids_and_invalid_decisions(results):
    with pytest.raises(ValueError):
        _parse_fingerprint_audit({"results": results}, {1})


def test_audit_resumes_batches_and_notifies_only_designated_reviewer(tmp_path):
    db = _db(tmp_path)
    settings = replace(_settings(), fingerprint_audit_batch_size=1, fingerprint_audit_max_per_run=3)
    calls = []
    sent = []

    class Judge:
        fail = True

        async def audit_fingerprints(self, inputs):
            calls.append([item["id"] for item in inputs])
            if len(calls) == 2 and self.fail:
                self.fail = False
                return FingerprintAuditOutcome(LLMOutcomeStatus.FAILED, error="network")
            return FingerprintAuditOutcome(LLMOutcomeStatus.OK, tuple(
                FingerprintAuditResult(item["id"], "disable", 0.99, "普通名词") for item in inputs
            ))

    async def get_me():
        return SimpleNamespace(username="testbot")

    async def send_message(user_id, text, **kwargs):
        sent.append((user_id, text))

    try:
        first = _seed(db)
        second = _seed(db, "正常讨论")
        missing = _seed(db, "未知原文", pattern=False)
        judge = Judge()
        auditor = FingerprintAuditor(settings, db, judge)
        bot = SimpleNamespace(get_me=get_me, send_message=send_message)
        assert asyncio.run(auditor.run_once(bot)) is False
        # A new auditor instance resumes the persisted run, without repeating batch one.
        assert asyncio.run(FingerprintAuditor(settings, db, judge).run_once(bot)) is True
        assert calls == [[first.id], [second.id], [second.id]]
        assert all(item[0] == 5439265612 for item in sent)
        assert len(sent) == 1
        assert len(db.pending_fingerprint_reviews()) == 2
        assert db.get_fingerprint(first.value).enabled
        assert db.get_fingerprint(missing.value).enabled
        asyncio.run(auditor.run_once(bot))
        assert len(sent) == 1
    finally:
        db.close()


def test_failed_notification_leaves_proposal_pending_for_retry(tmp_path):
    db = _db(tmp_path)
    try:
        fp = _seed(db)
        _propose(db, fp.id)

        async def get_me():
            return SimpleNamespace(username="testbot")

        async def fail(*args, **kwargs):
            raise RuntimeError("Telegram unavailable")

        auditor = FingerprintAuditor(_settings(), db, None)
        asyncio.run(auditor.notify_ready(SimpleNamespace(get_me=get_me, send_message=fail)))
        assert len(db.pending_fingerprint_reviews(unnotified=True)) == 1
        assert db.get_fingerprint(fp.value).enabled
    finally:
        db.close()


def test_kept_or_pending_same_evidence_is_not_proposed_again(tmp_path):
    db = _db(tmp_path)
    try:
        fp = _seed(db)
        rid = _propose(db, fp.id)
        assert db.resolve_fingerprint_review(rid, "keep", 5439265612)
        _ = db.get_or_start_fingerprint_audit(86400, 1000)
        payload = db.fingerprint_audit_payload(fp.id)
        db.record_fingerprint_audit_result(1, payload,
                                         FingerprintAuditResult(fp.id, "disable", 0.99, "再次建议"))
        assert not db.pending_fingerprint_reviews()
    finally:
        db.close()


@pytest.mark.parametrize("user_id,private", [(999, True), (5439265612, False)])
def test_review_ui_rejects_other_users_and_group_callbacks(tmp_path, user_id, private):
    db = _db(tmp_path)
    answered = []

    async def answer(*args, **kwargs):
        answered.append(args)

    try:
        fp = _seed(db)
        rid = _propose(db, fp.id)
        message = SimpleNamespace(chat=SimpleNamespace(type="private" if private else "supergroup"),
                                  from_user=SimpleNamespace(id=user_id), answer=answer)
        asyncio.run(show_reviews(message, _settings(), db))
        callback = SimpleNamespace(message=message, from_user=message.from_user,
                                   data=f"fp:disable:{rid}", answer=answer)
        asyncio.run(handle_review_callback(callback, _settings(), db))
        assert db.get_fingerprint(fp.value).enabled
        assert len(answered) == 2
    finally:
        db.close()


def test_authorized_callback_is_idempotent_and_escapes_samples(tmp_path):
    db = _db(tmp_path)
    edited = []

    async def answer(*args, **kwargs):
        pass

    async def edit_text(text, **kwargs):
        edited.append(text)

    try:
        fp = _seed(db, "机器人<&>")
        rid = _propose(db, fp.id)
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=5439265612), data=f"fp:disable:{rid}", answer=answer,
            message=SimpleNamespace(chat=SimpleNamespace(type="private"), edit_text=edit_text),
        )
        asyncio.run(handle_review_callback(callback, _settings(), db))
        asyncio.run(handle_review_callback(callback, _settings(), db))
        assert len(edited) == 1
        assert "&lt;&amp;&gt;" in review_detail(db, rid)[0]
        assert db.get_fingerprint(fp.value).enabled is False
    finally:
        db.close()


def test_newapi_audit_falls_back_after_bad_id_and_closes_clients(monkeypatch):
    settings = replace(_settings(), newapi_base_url="https://one.example,https://two.example",
                       newapi_api_key="dummy", newapi_model="model")
    judge = NewAPIJudge(settings)
    closed = []

    def client(provider):
        async def create(**kwargs):
            fid = 999 if 'one.example' in provider.base_url else 1
            return {"choices": [{"message": {"content": json.dumps({"results": [
                {"fingerprint_id": fid, "decision": "keep", "confidence": 0.95, "reason": "专用广告短语"}
            ]})}}]}

        async def close():
            closed.append(provider.base_url)

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=close)

    monkeypatch.setattr(judge, '_build_client', client)
    outcome = asyncio.run(judge.audit_fingerprints([{"id": 1, "pattern_text": "拿码收钱来"}]))
    assert outcome.status == LLMOutcomeStatus.OK
    assert len(closed) == 2


def test_timeout_release_is_not_ham_feedback_and_low_confidence_is_not_a_proposal(tmp_path):
    db = _db(tmp_path)
    try:
        fp = _seed(db)
        features = build_message_features({"chat": {"id": -1001}, "message_id": 9,
                                           "from_user": {"id": 42}, "text": "机器人讨论"})
        decision = LocalDecision(DecisionAction.WITHDRAW_VOTE, "known_fingerprint", 0.6,
                                 metadata={"fingerprint_id": fp.id})
        db.record_action(features, decision)
        sid = db.create_vote_session(features, decision, -1)
        db.expire_open_vote_sessions()
        record_vote_ham_feedback(db, db.get_vote_session(sid), _settings())
        assert db.get_fingerprint(fp.value).false_positive_count == 0
        run_id = db.get_or_start_fingerprint_audit(86400, 1000)
        db.record_fingerprint_audit_result(
            run_id, db.fingerprint_audit_payload(fp.id),
            FingerprintAuditResult(fp.id, "disable", 0.89, "证据尚不确定"),
        )
        assert not db.pending_fingerprint_reviews()
        assert db.get_fingerprint(fp.value).enabled
    finally:
        db.close()
