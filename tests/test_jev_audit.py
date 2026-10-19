import hashlib
import json

import pytest

from telegram_llm_antispam.jev_audit import (
    classify_events,
    metrics,
    paired_rows,
    recover_context,
    stratify,
    user_comparison,
)


def event(kind, sid=1, trigger=None, related=None, event_id=10, when=20):
    return {
        "id": event_id,
        "chat_id": -1001,
        "message_id": sid,
        "user_id": 7,
        "action": kind,
        "created_at": when,
        "metadata_json": json.dumps(
            {
                "vote_session_id": sid,
                "trigger_vote_session_id": trigger,
                "related_vote_session_ids": related if related is not None else [sid],
            }
        ),
    }


def fixture(text="正文", kind="admin_banned_user", profile=None, detail=None):
    sample = {
        "sample_id": "-1001:1",
        "chat_id": -1001,
        "message_id": 1,
        "user_id": 7,
        "raw_text": text,
        "created_at": 10,
        "historical_context": {"sender_profile_at_moderation": profile},
        "reference": {"label": "spam", "human_confirmed": True},
        "original_llm": {"status": "ok", "is_spam": True},
    }
    pred = {"sample_id": sample["sample_id"], "status": "ok", "is_spam_noul": 0.1}
    mapping = {
        "actions": [event(kind)],
        "sessions": [
            {
                "id": 1,
                "chat_id": -1001,
                "original_message_id": 1,
                "created_at": 10,
                "status": "confirmed_spam",
                "detail_text": detail,
            }
        ],
    }
    review = {
        "sample_id": sample["sample_id"],
        "raw_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "assessment": "independent_spam",
        "message_label": "spam",
        "evidence_sufficient": True,
    }
    return sample, pred, mapping, review


def test_auto_closure_never_becomes_human_truth():
    p = classify_events([event("auto_banned_user")], [{"id": 1, "status": "confirmed_spam"}])
    assert p["scope"] == "automatic_closure_not_human"
    assert p["candidate_message_label"] is None
    assert p["human_account_label"] is None


def test_vote_trigger_distinguishes_siblings_despite_copied_tally():
    p = classify_events(
        [event("vote_confirmed_spam", sid=2, trigger=1, related=[2, 1])],
        [{"id": 2, "spam_votes": 3}],
    )
    assert p["scope"] == "human_vote_propagated"
    assert p["candidate_message_label"] is None
    trigger = classify_events([event("vote_confirmed_spam", trigger=1)], [{"id": 1}])
    assert trigger["candidate_message_label"] == "spam"


def test_admin_multi_session_order_cannot_establish_primary():
    for related in ([1, 2], [2, 1]):
        p = classify_events([event("admin_banned_user", related=related)], [{"id": 1}])
        assert p["scope"] == "admin_primary_unrecoverable"
        assert p["candidate_message_label"] is None
        assert p["human_account_label"] == "spam"


def test_direct_human_event_requires_semantic_review():
    sample, pred, mapping, review = fixture()
    row = stratify([sample], [pred], mapping)[0]
    assert row["reference"]["label"] is None
    row = stratify([sample], [pred], mapping, [review])[0]
    assert row["reference"]["label"] == "spam"
    assert row["message_metric_eligible"] is True


@pytest.mark.parametrize(
    "profile,tier",
    [
        (None, "body_empty_context_unrecoverable"),
        ({"display_name": "普通名字"}, "body_empty_context_unrecoverable"),
        ({"bio": "长期供货，联系@somebody"}, "body_empty_context_recoverable"),
    ],
)
def test_empty_rows_retained_but_not_counted_as_message_false_negatives(profile, tier):
    sample, pred, mapping, review = fixture(text="", profile=profile)
    row = stratify([sample], [pred], mapping, [review])[0]
    assert row["evidence_tier"] == tier
    assert row["reference"]["label"] is None
    assert metrics([row])["fn"] == 0


def test_recovered_og_and_multiline_bio_do_not_leak_outcome():
    detail = (
        "用户资料：名字 @user\nBio：第一行\n第二行\n"
        "个人频道：频道\n频道最近消息：\n- 订阅\n触发：<code>spam</code>\n"
        "处理：ban\nOG：产品 / 简介\nLLM：广告 / 99%\n正文：\n<blockquote></blockquote>"
    )
    c = recover_context(detail)
    assert c["sender_profile_at_moderation"]["bio"] == "第一行\n第二行"
    assert c["og_preview_at_moderation"]["rendered_title_description"] == "产品 / 简介"
    assert "订阅" in c["personal_chat_at_moderation"]["rendered_bounded_snapshot"]
    assert "99%" not in json.dumps(c)
    sample, pred, mapping, review = fixture(text="", detail=detail)
    row = stratify([sample], [pred], mapping, [review])[0]
    assert row["evidence_tier"] == "body_empty_context_recoverable"
    assert row["historical_context"]["sender_profile_at_moderation"] is None
    assert row["reference"]["label"] is None


def test_recovered_context_cannot_retroactively_make_cached_input_sufficient():
    sample, pred, mapping, review = fixture(
        detail="用户资料：名字 @user\nBio：进群做单\n触发：rule"
    )
    review["assessment"] = "context_spam"
    row = stratify([sample], [pred], mapping, [review])[0]
    assert row["reference"]["label"] is None


def test_truncation_and_revision_exclude_incomplete_input():
    for text, detail in [("字" * 500, None), ("旧版", "正文：\n<blockquote>更长新版</blockquote>")]:
        sample, pred, mapping, review = fixture(text=text, detail=detail)
        row = stratify([sample], [pred], mapping, [review])[0]
        assert row["body_integrity"] == "truncation_or_revision_possible"
        assert row["reference"]["label"] is None


def test_analyst_no_ad_observation_does_not_invent_human_ham_label():
    sample, pred, mapping, review = fixture(text="ok")
    review.update(assessment="no_ad_intent", message_label="ham")
    row = stratify([sample], [pred], mapping, [review])[0]
    assert row["reference"]["label"] is None
    assert row["user_reference"]["label"] == "spam"


def test_explicit_ham_release_included_and_timeout_excluded():
    sample, pred, mapping, review = fixture(kind="admin_released_user")
    sample["reference"]["label"] = "ham"
    review.update(assessment="no_ad_intent", message_label="ham")
    row = stratify([sample], [pred], mapping, [review])[0]
    assert metrics([row])["tn"] == 1
    mapping["actions"] = [event("vote_expired_released")]
    row = stratify([sample], [pred], mapping, [review])[0]
    assert metrics([row])["n"] == 0


def test_paired_cohort_excludes_failed_original_and_invalid_jev():
    sample, pred, mapping, review = fixture()
    row = stratify([sample], [pred], mapping, [review])[0]
    failed = {**row, "sample_id": "failed", "original_llm": {"status": "failed"}}
    invalid = {**row, "sample_id": "invalid", "jev_prediction_valid": False}
    common = paired_rows([row, failed, invalid])
    assert len(common) == 1
    assert metrics(common)["n"] == metrics(common, model="original")["n"] == 1
    assert metrics(common)["f1"] == 0


def test_future_message_cannot_improve_user_coverage():
    sample, pred, mapping, review = fixture()
    row = stratify([sample], [pred], mapping, [review])[0]
    later = {**row, "sample_id": "future", "created_at": 30, "jev_label": "spam"}
    result = user_comparison([row, later])
    assert result["common_messages"] == 1
    assert result["jev_detected_users"] == 0


def test_annotation_hash_prevents_reusing_review_for_new_text():
    sample, pred, mapping, review = fixture()
    sample["raw_text"] = "different revision"
    with pytest.raises(ValueError, match="hash mismatch"):
        stratify([sample], [pred], mapping, [review])


def test_later_model_call_cannot_repair_missing_frozen_status():
    sample, pred, mapping, review = fixture()
    sample["original_llm"] = None
    later = event("review", when=100)
    later["metadata_json"] = json.dumps({"llm_outcome": {"status": "ok", "is_spam": True}})
    mapping["actions"].append(later)
    row = stratify([sample], [pred], mapping, [review])[0]
    assert row["original_llm_status_audited"] == "missing_call_telemetry"
    assert not paired_rows([row])


def test_narrow_export_handles_many_ids_and_never_writes_source(tmp_path):
    import sqlite3

    from telegram_llm_antispam.jev_audit_export import export_rows

    db = tmp_path / "source.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE action_log(id INTEGER, chat_id INTEGER, message_id INTEGER,
        user_id INTEGER, action TEXT, reason TEXT, confidence REAL,
        metadata_json TEXT, created_at INTEGER);
        CREATE TABLE vote_sessions(id INTEGER, chat_id INTEGER, original_message_id INTEGER,
        suspect_user_id INTEGER,status TEXT,spam_votes INTEGER,ham_votes INTEGER,
        reason TEXT,detail_text TEXT,created_at INTEGER,closed_at INTEGER);
        INSERT INTO action_log VALUES (1,-1001,1,2,'review','rule',0,'{}',100);
        INSERT INTO action_log VALUES (2,-1001,9999,2,'review','rule',0,'{}',100);
        INSERT INTO vote_sessions VALUES (1,-1001,1,2,'open',0,0,'rule',NULL,100,NULL);
    """)
    conn.close()
    before = db.read_bytes()
    rows = export_rows(db, [(-1001, i) for i in range(1, 1201)] + [(-1001, 1)])
    assert [r["message_id"] for r in rows["actions"]] == [1]
    assert [r["original_message_id"] for r in rows["sessions"]] == [1]
    assert db.read_bytes() == before
