"""Fingerprint review WebUI: authentication, authorization, stale actions, deletion,
batch idempotency, bounded/escaped output and Chinese user-facing text.

These tests use the framework-independent API (``WebUIApi.handle``) and the real
SQLite store, so they need neither aiohttp nor aiogram.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import pytest

from telegram_llm_antispam.db import Database
from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.models import FingerprintAuditResult
from telegram_llm_antispam.webui import text
from telegram_llm_antispam.webui.api import (
    CONTENT_SECURITY_POLICY, MAX_BATCH_ITEMS, MAX_BODY_BYTES, STATIC_DIR, ApiRequest, WebUIApi,
)
from telegram_llm_antispam.webui.auth import (
    InitDataError, SessionStore, sign_init_data, verify_init_data,
)
from telegram_llm_antispam.webui_store import fingerprint_version


BOT_TOKEN = "123456:TEST-not-a-real-token"
REVIEWER = 5439265612
OTHER_USER = 777


# ----------------------------------------------------------------- fixtures


def make_init_data(user_id=REVIEWER, *, auth_date=None, token=BOT_TOKEN, **extra):
    fields = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps({"id": user_id, "first_name": "审核员", "username": "reviewer"},
                           ensure_ascii=False, separators=(",", ":")),
    }
    fields.update(extra)
    return sign_init_data(fields, token)


class Env:
    def __init__(self, tmp_path: Path, *, bot_token=BOT_TOKEN, max_age=600):
        self.db = Database(tmp_path / "webui.sqlite")
        self.db.connect()
        self.db.migrate()
        self.sessions = SessionStore(idle_ttl_seconds=1800)
        self.api = WebUIApi(
            db=self.db, bot_token=bot_token, reviewer_user_id=REVIEWER,
            init_data_max_age_seconds=max_age, sessions=self.sessions,
        )
        self.token = None

    def call(self, method, path, *, body=None, query="", token="__session__", headers=None,
             raw_body=None):
        all_headers = dict(headers or {})
        token = self.token if token == "__session__" else token
        if token:
            all_headers["Authorization"] = f"Bearer {token}"
        payload = raw_body if raw_body is not None else b""
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            all_headers.setdefault("Content-Type", "application/json")
        return self.api.handle(ApiRequest(method, path, query, all_headers, payload))

    def login(self, user_id=REVIEWER):
        response = self.call("POST", "/api/session", token=None,
                             body={"init_data": make_init_data(user_id)})
        assert response.status == 200, response.json()
        self.token = response.json()["token"]
        return self.token

    def seed(self, phrase, *, weight=60, propose=True, confidence=0.97, evidence=None):
        value = hashlib.sha256(phrase.encode("utf-8")).hexdigest()[:32]
        self.db.upsert_fingerprint("phrase", value, weight, "llm_spam_phrase", pattern_text=phrase)
        fp = self.db.get_fingerprint(value)
        for index, (label, body) in enumerate(evidence or ()):
            self.add_evidence(fp.value, body, label, message_id=100 + index)
        review_id = None
        if propose:
            run_id = self.db.get_or_start_fingerprint_audit(86400, 1000)
            self.db.record_fingerprint_audit_result(
                run_id, self.db.fingerprint_audit_payload(fp.id),
                FingerprintAuditResult(fp.id, "disable", confidence, "普通词语，容易误伤正常聊天"),
            )
            with self.db._locked_conn() as conn:
                review_id = conn.execute(
                    "SELECT id FROM fingerprint_reviews WHERE fingerprint_id=? AND status='pending'",
                    (fp.id,),
                ).fetchone()[0]
            # Allow the next proposal to open a fresh run in tests.
            with self.db._locked_conn() as conn:
                conn.execute("UPDATE fingerprint_audit_runs SET finished_at=started_at, started_at=0")
                conn.commit()
        return fp, review_id

    def add_evidence(self, value, body, label, *, message_id=900):
        features = build_message_features({
            "chat": {"id": -1001}, "message_id": message_id, "from_user": {"id": 42}, "text": body,
        })
        self.db.add_fingerprint_evidence(value, features, label)

    def review(self, review_id):
        return self.call("GET", f"/api/reviews/{review_id}").json()["review"]

    def fingerprint(self, fid):
        return self.call("GET", f"/api/fingerprints/{fid}").json()["fingerprint"]

    def act(self, action, items, request_id="req-0000000001"):
        return self.call("POST", "/api/reviews/actions", body={
            "request_id": request_id, "action": action,
            "items": [{"review_id": rid, "version": ver} for rid, ver in items],
        })

    def count(self, sql, *params):
        with self.db._locked_conn() as conn:
            return conn.execute(sql, params).fetchone()[0]


@pytest.fixture
def env(tmp_path):
    environment = Env(tmp_path)
    yield environment
    environment.db.close()


CJK = re.compile(r"[一-鿿]")


def assert_chinese_error(response, status, code):
    assert response.status == status
    error = response.json()["error"]
    assert error["code"] == code
    assert CJK.search(error["message"]), error["message"]


# ----------------------------------------------------------- authentication


def test_init_data_signature_freshness_and_shape_are_verified():
    now = int(time.time())
    verified = verify_init_data(make_init_data(), BOT_TOKEN, max_age_seconds=600, now=now)
    assert verified.user.id == REVIEWER

    tampered = make_init_data().replace("%E5%AE%A1", "%E5%AE%A2")  # edit signed user JSON
    cases = {
        "bad_signature": [
            tampered,
            make_init_data(token="999:other-bot"),
            make_init_data().split("&hash=")[0],  # hash removed
            "user=%7B%22id%22%3A5439265612%7D&auth_date=1",  # initDataUnsafe-like, unsigned
        ],
        "expired": [make_init_data(auth_date=now - 601)],
        "future_auth_date": [make_init_data(auth_date=now + 3600)],
        "malformed": ["", "a" * 9000, make_init_data() + "&auth_date=1"],
    }
    for code, values in cases.items():
        for value in values:
            with pytest.raises(InitDataError) as raised:
                verify_init_data(value, BOT_TOKEN, max_age_seconds=600, now=now)
            assert raised.value.code == code, value
    with pytest.raises(InitDataError):
        verify_init_data(make_init_data(user_id=True), BOT_TOKEN, max_age_seconds=600, now=now)
    with pytest.raises(InitDataError) as raised:
        verify_init_data(make_init_data(), "", max_age_seconds=600, now=now)
    assert raised.value.code == "server_not_configured"


def test_login_requires_signed_fresh_init_data_from_the_exact_reviewer(env):
    assert_chinese_error(env.call("POST", "/api/session", token=None,
                                  body={"init_data": make_init_data(OTHER_USER)}), 403, "forbidden")
    assert_chinese_error(env.call("POST", "/api/session", token=None, body={
        "init_data": make_init_data(auth_date=int(time.time()) - 3600)}), 401, "init_data_expired")
    assert_chinese_error(env.call("POST", "/api/session", token=None, body={
        "init_data": make_init_data(token="1:wrong")}), 401, "init_data_invalid")
    # A client-supplied user object or ID is not a credential.
    assert_chinese_error(env.call("POST", "/api/session", token=None, body={
        "init_data": "", "user": {"id": REVIEWER}}), 401, "init_data_invalid")
    assert env.sessions.resolve(None) is None

    token = env.login()
    login = env.call("POST", "/api/session", token=None, body={"init_data": make_init_data()}).json()
    assert set(login) == {"token", "expires_in", "display_name"}
    assert env.call("GET", "/api/summary", token=token).status == 200


def test_login_without_bot_token_is_refused(tmp_path):
    env = Env(tmp_path, bot_token=None)
    try:
        assert_chinese_error(env.call("POST", "/api/session", token=None,
                                      body={"init_data": make_init_data()}),
                             503, "server_not_configured")
    finally:
        env.db.close()


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/meta"), ("GET", "/api/summary"), ("GET", "/api/reviews"),
    ("GET", "/api/reviews/1"), ("POST", "/api/reviews/actions"), ("GET", "/api/fingerprints"),
    ("GET", "/api/fingerprints/1"), ("POST", "/api/fingerprints/1/delete"),
])
def test_every_api_route_requires_a_reviewer_session(env, method, path):
    fp, rid = env.seed("机器人")
    body = {"request_id": "req-unauth-01", "action": "disable",
            "items": [{"review_id": rid, "version": "0" * 16}], "version": "0" * 16,
            "confirm": True}
    for token in (None, "forged-token", ""):
        response = env.call(method, path, token=token, body=body if method == "POST" else None)
        assert_chinese_error(response, 401, "unauthenticated")
    # A session that somehow belongs to another user is refused and revoked.
    stray = env.sessions.create(OTHER_USER)
    assert_chinese_error(env.call(method, path, token=stray,
                                  body=body if method == "POST" else None), 403, "forbidden")
    assert env.sessions.resolve(stray) is None
    assert env.db.get_fingerprint(fp.value).enabled
    assert env.count("SELECT COUNT(*) FROM fingerprints") == 1


def test_session_store_expires_idle_and_absolute_sessions():
    clock = [1000.0]
    store = SessionStore(idle_ttl_seconds=600, absolute_ttl_seconds=1200, max_sessions=2,
                         clock=lambda: clock[0])
    token = store.create(REVIEWER)
    clock[0] += 500
    assert store.resolve(token) is not None
    clock[0] += 500  # still within idle window because the previous use refreshed it
    assert store.resolve(token) is not None
    clock[0] += 300  # past the absolute lifetime
    assert store.resolve(token) is None
    first, second, third = store.create(1), store.create(2), store.create(3)
    assert store.resolve(first) is None and store.resolve(third) is not None
    clock[0] += 601
    assert store.resolve(second) is None


# ------------------------------------------------------ reads never mutate


def test_opening_queues_and_details_never_changes_state(env):
    fp, rid = env.seed("机器人", evidence=[("llm_spam", "点击机器人领取奖励")])
    env.login()
    with env.db._locked_conn() as conn:
        before = [tuple(r) for r in conn.execute("SELECT * FROM fingerprint_reviews")]
    for path, query in [("/api/meta", ""), ("/api/summary", ""), ("/api/reviews", "status=pending"),
                        (f"/api/reviews/{rid}", ""), ("/api/fingerprints", ""),
                        (f"/api/fingerprints/{fp.id}", "")]:
        assert env.call("GET", path, query=query).status == 200
    with env.db._locked_conn() as conn:
        after = [tuple(r) for r in conn.execute("SELECT * FROM fingerprint_reviews")]
    assert before == after
    assert env.db.get_fingerprint(fp.value).enabled
    assert env.count("SELECT COUNT(*) FROM fingerprint_review_actions") == 0


# --------------------------------------------- single actions and staleness


def test_single_action_is_idempotent_by_request_id_and_by_state(env):
    fp, rid = env.seed("机器人")
    env.login()
    version = env.review(rid)["version"]
    first = env.act("disable", [(rid, version)])
    assert first.status == 200
    result = first.json()["results"][0]
    assert result["status"] == "ok" and CJK.search(result["message"])
    assert result["review"]["status"] == "disabled"
    assert env.db.get_fingerprint(fp.value).enabled is False

    replay = env.act("disable", [(rid, version)]).json()  # network retry, same request ID
    assert replay["replayed"] is True and replay["results"][0]["status"] == "ok"
    double_click = env.act("disable", [(rid, version)], request_id="req-0000000002").json()
    assert double_click["results"][0]["status"] == "already_done"
    assert env.count("SELECT COUNT(*) FROM fingerprint_review_actions") == 1

    conflict = env.act("keep", [(rid, version)])  # same ID, different payload
    assert_chinese_error(conflict, 409, "request_id_conflict")

    restored = env.act("restore", [(rid, env.review(rid)["version"])], request_id="req-restore-1")
    assert restored.json()["results"][0]["status"] == "ok"
    assert env.db.get_fingerprint(fp.value).enabled is True


def test_changed_evidence_makes_old_page_stale_and_returns_fresh_item(env):
    fp, rid = env.seed("机器人", evidence=[("llm_spam", "点击机器人领取奖励")])
    env.login()
    seen = env.review(rid)
    env.add_evidence(fp.value, "居然机器人没头像", "manual_ham")  # new human evidence arrives
    stale = env.act("disable", [(rid, seen["version"])]).json()["results"][0]
    assert stale["status"] == "stale"
    assert stale["message"] == text.ITEM_RESULTS["stale"]
    assert env.db.get_fingerprint(fp.value).enabled is True
    fresh = stale["review"]
    assert fresh["version"] != seen["version"]
    assert any(s["label"] == "manual_ham" for s in fresh["fingerprint"]["samples"])
    retry = env.act("disable", [(rid, fresh["version"])], request_id="req-0000000003").json()
    assert retry["results"][0]["status"] == "ok"


def test_hit_count_and_weight_bumps_do_not_make_a_page_stale(env):
    fp, rid = env.seed("机器人")
    env.login()
    seen = env.review(rid)
    env.db.record_fingerprint_hit(fp.id, weight_increment=5, weight_cap=80)
    assert env.act("keep", [(rid, seen["version"])]).json()["results"][0]["status"] == "ok"


def test_bot_callbacks_and_webui_actions_cannot_both_apply(env):
    fp, rid = env.seed("机器人")
    other, other_rid = env.seed("正常讨论")
    env.login()
    seen = env.review(rid)
    # The reviewer used the old inline button first.
    assert env.db.resolve_fingerprint_review(rid, "keep", REVIEWER)
    stale = env.act("disable", [(rid, seen["version"])]).json()["results"][0]
    assert stale["status"] == "stale" and stale["review"]["status"] == "kept"
    assert env.db.get_fingerprint(fp.value).enabled is True

    # The WebUI acted first; the old inline button is then rejected.
    seen = env.review(other_rid)
    assert env.act("disable", [(other_rid, seen["version"])],
                   request_id="req-0000000004").json()["results"][0]["status"] == "ok"
    assert not env.db.resolve_fingerprint_review(other_rid, "keep", REVIEWER)
    assert env.db.get_fingerprint(other.value).enabled is False


def test_restore_on_pending_or_unknown_review_is_rejected_per_item(env):
    _, rid = env.seed("机器人")
    env.login()
    version = env.review(rid)["version"]
    result = env.act("restore", [(rid, version)]).json()["results"][0]
    assert result["status"] == "invalid"
    missing = env.act("keep", [(99999, "0" * 16)], request_id="req-0000000005").json()
    assert missing["results"][0]["status"] == "not_found"
    assert missing["results"][0]["review"] is None


# ------------------------------------------------------------------ batches


def test_batch_reports_each_item_and_only_touches_selected_reviews(env):
    a, rid_a = env.seed("机器人")
    b, rid_b = env.seed("正常讨论")
    c, rid_c = env.seed("同城交友")
    untouched, rid_untouched = env.seed("资源分享")
    env.login()
    versions = {rid: env.review(rid)["version"] for rid in (rid_a, rid_b, rid_c)}
    env.add_evidence(b.value, "大家正常讨论一下", "manual_ham")  # makes B stale
    assert env.db.resolve_fingerprint_review(rid_c, "disable", REVIEWER)  # C already disabled

    response = env.act("disable", [(rid_a, versions[rid_a]), (rid_b, versions[rid_b]),
                                   (rid_c, versions[rid_c]), (424242, "0" * 16)],
                       request_id="batch-000000001")
    data = response.json()
    assert [r["status"] for r in data["results"]] == ["ok", "stale", "already_done", "not_found"]
    assert data["summary"] == {"ok": 1, "already_done": 1, "stale": 1, "not_found": 1, "invalid": 0}
    assert all(CJK.search(r["message"]) and "review" not in r for r in data["results"])
    assert env.db.get_fingerprint(a.value).enabled is False
    assert env.db.get_fingerprint(b.value).enabled is True
    assert env.db.get_fingerprint(untouched.value).enabled is True
    assert env.review(rid_untouched)["status"] == "pending"

    replay = env.act("disable", [(rid_a, versions[rid_a]), (rid_b, versions[rid_b]),
                                 (rid_c, versions[rid_c]), (424242, "0" * 16)],
                     request_id="batch-000000001").json()
    assert replay["replayed"] is True
    assert [r["status"] for r in replay["results"]] == ["ok", "stale", "already_done", "not_found"]
    assert env.count("SELECT COUNT(*) FROM fingerprint_review_actions") == 2  # C via bot + A once


def test_batch_validation_is_explicit_and_bounded(env):
    _, rid = env.seed("机器人")
    env.login()
    version = env.review(rid)["version"]
    assert_chinese_error(env.act("disable", [(rid, version), (rid, version)]), 400, "duplicate_items")
    too_many = [(i + 1, "0" * 16) for i in range(MAX_BATCH_ITEMS + 1)]
    response = env.act("disable", too_many)
    assert_chinese_error(response, 400, "too_many_items")
    assert str(MAX_BATCH_ITEMS) in response.json()["error"]["message"]
    for body in (
        {"request_id": "short", "action": "disable", "items": [{"review_id": rid, "version": version}]},
        {"request_id": "req-0000000009", "action": "delete", "items": [{"review_id": rid, "version": version}]},
        {"request_id": "req-0000000009", "action": "disable", "items": []},
        {"request_id": "req-0000000009", "action": "disable", "items": [{"review_id": rid}]},
        {"request_id": "req-0000000009", "action": "disable", "items": [{"review_id": "1", "version": version}]},
    ):
        assert_chinese_error(env.call("POST", "/api/reviews/actions", body=body), 400, "bad_request")
    assert env.review(rid)["status"] == "pending"
    assert env.count("SELECT COUNT(*) FROM webui_action_requests") == 0


# ------------------------------------------------------------------- delete


def test_delete_requires_confirmation_and_current_version(env):
    fp, rid = env.seed("机器人", evidence=[("llm_spam", "点击机器人领取奖励"),
                                          ("manual_ham", "居然机器人没头像")])
    env.login()
    detail = env.fingerprint(fp.id)
    assert detail["delete_impact"] == {"evidence_count": 2, "review_count": 1}
    path = f"/api/fingerprints/{fp.id}/delete"
    for confirm in (None, False, "true", 1):
        body = {"request_id": "del-000000001", "version": detail["version"]}
        if confirm is not None:
            body["confirm"] = confirm
        assert_chinese_error(env.call("POST", path, body=body), 400, "confirmation_required")
    assert env.db.get_fingerprint_by_id(fp.id) is not None

    env.add_evidence(fp.value, "机器人回复很快", "llm_benign")
    stale = env.call("POST", path, body={"request_id": "del-000000002",
                                         "version": detail["version"], "confirm": True}).json()
    assert stale["result"]["status"] == "stale"
    assert stale["fingerprint"]["version"] != detail["version"]
    assert env.db.get_fingerprint_by_id(fp.id) is not None

    fresh = stale["fingerprint"]["version"]
    done = env.call("POST", path, body={"request_id": "del-000000003", "version": fresh,
                                        "confirm": True}).json()
    assert done["result"]["status"] == "ok" and done["fingerprint"] is None
    assert (done["result"]["evidence_count"], done["result"]["review_count"]) == (3, 1)
    assert env.db.get_fingerprint_by_id(fp.id) is None
    assert env.count("SELECT COUNT(*) FROM fingerprint_evidence WHERE fingerprint_id=?", fp.id) == 0
    assert env.count("SELECT COUNT(*) FROM fingerprint_reviews WHERE fingerprint_id=?", fp.id) == 0
    assert env.count("SELECT COUNT(*) FROM fingerprint_deletions WHERE fingerprint_id=? "
                     "AND user_id=?", fp.id, REVIEWER) == 1
    replay = env.call("POST", path, body={"request_id": "del-000000003", "version": fresh,
                                          "confirm": True}).json()
    assert replay["replayed"] and replay["result"]["status"] == "ok"
    again = env.call("POST", path, body={"request_id": "del-000000004", "version": fresh,
                                         "confirm": True}).json()
    assert again["result"]["status"] == "already_done"
    assert env.call("GET", f"/api/reviews/{rid}").status == 404


def test_delete_cleans_linked_rows_even_without_foreign_keys(env):
    fp, _ = env.seed("机器人", evidence=[("llm_spam", "点击机器人领取奖励")])
    with env.db._locked_conn() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
    env.login()
    version = env.fingerprint(fp.id)["version"]
    result = env.call("POST", f"/api/fingerprints/{fp.id}/delete", body={
        "request_id": "del-nofk-0001", "version": version, "confirm": True}).json()["result"]
    assert result["status"] == "ok"
    assert env.count("SELECT COUNT(*) FROM fingerprint_evidence") == 0
    assert env.count("SELECT COUNT(*) FROM fingerprint_reviews") == 0


def test_disable_and_restore_are_distinct_from_delete(env):
    fp, rid = env.seed("机器人")
    env.login()
    env.act("disable", [(rid, env.review(rid)["version"])])
    row = env.db.get_fingerprint_by_id(fp.id)
    assert row is not None and row["enabled"] == 0
    assert env.fingerprint(fp.id)["reviews"][0]["status"] == "disabled"


# ------------------------------------------------------- listing & filters


def test_library_search_filters_and_sorting(env):
    robot, _ = env.seed("机器人", weight=60, propose=False)
    percent, _ = env.seed("100%返现", weight=90, propose=False)
    under, _ = env.seed("a_b 联系", weight=20, propose=False)
    env.db.record_fingerprint_hit(robot.id)
    env.db.record_fingerprint_hit(robot.id)
    env.db.mark_fingerprint_false_positive(under.value, 5)
    env.login()

    def ids(query):
        response = env.call("GET", "/api/fingerprints", query=query)
        assert response.status == 200, response.json()
        return [item["id"] for item in response.json()["items"]]

    assert ids("q=机器") == [robot.id]
    assert ids("q=%25") == [percent.id]          # LIKE wildcards are literal
    assert ids("q=_") == [under.id]
    assert ids(f"q=%23{robot.id}") == [robot.id]  # "#id"
    assert ids(f"q={robot.value[:8]}") == [robot.id]
    assert ids("weight_min=50&weight_max=95&sort=weight") == [percent.id, robot.id]
    assert ids("hits_min=2") == [robot.id]
    assert ids("fp_min=1") == [under.id]
    assert ids("type=phrase&source=llm_spam_phrase&enabled=1&sort=hits")[0] == robot.id
    assert ids("type=skeleton") == []
    assert ids("limit=1&offset=1&sort=weight") == [robot.id]


@pytest.mark.parametrize("query,field", [
    ("weight_min=abc", "weight_min"), ("weight_max=101", "weight_max"), ("weight_min=nan", "weight_min"),
    ("hits_min=-1", "hits_min"), ("limit=51", "limit"), ("limit=0", "limit"),
    ("sort=drop%20table", "sort"), ("type=PHRASE", "type"), ("enabled=maybe", "enabled"),
    ("q=" + "长" * 101, "q"),
])
def test_invalid_filters_are_rejected_with_chinese_field_names(env, query, field):
    env.login()
    response = env.call("GET", "/api/fingerprints", query=query)
    assert_chinese_error(response, 400, "bad_request")
    assert response.json()["error"]["message"] == text.field_error(field)
    assert field not in response.json()["error"]["message"]


def test_review_views_sort_and_filter(env):
    plain, rid_plain = env.seed("普通词")
    flagged, rid_flagged = env.seed("机器人")
    env.db.mark_fingerprint_false_positive(flagged.value, 5)
    env.login()
    pending = env.call("GET", "/api/reviews", query="status=pending").json()
    assert [item["id"] for item in pending["items"]] == [rid_flagged, rid_plain]
    assert pending["items"][0]["fingerprint"]["has_manual_ham"] is True
    assert env.call("GET", "/api/reviews", query="status=pending&q=普通").json()["total"] == 1
    env.act("keep", [(rid_plain, env.review(rid_plain)["version"])])
    kept = env.call("GET", "/api/reviews", query="status=kept").json()
    assert [item["id"] for item in kept["items"]] == [rid_plain]
    assert_chinese_error(env.call("GET", "/api/reviews", query="status=not_recommended"),
                         400, "bad_request")
    summary = env.call("GET", "/api/summary").json()
    assert summary["reviews"] == {"pending": 1, "kept": 1, "disabled": 0, "restored": 0}
    assert summary["pending_with_manual_ham"] == 1


# --------------------------------------------- bounded, escaped responses


def test_untrusted_evidence_is_bounded_and_returned_as_json_text(env):
    hostile = '<script>alert(1)</script><b onclick="x">机器人</b>' + "广" * 2000
    fp, rid = env.seed("机器人", evidence=[("manual_spam", hostile), ("manual_ham", "机器人 a"),
                                          ("llm_benign", "机器人 b"), ("local_hit", "机器人 c")])
    env.db.upsert_fingerprint("content", "c" * 32, 50, "llm_spam", pattern_text="长" * 5000)
    env.login()
    response = env.call("GET", f"/api/reviews/{rid}")
    assert response.headers["Content-Type"] == "application/json; charset=utf-8"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-store"
    samples = response.json()["review"]["fingerprint"]["samples"]
    assert len(samples) == 3
    stored = next(s for s in samples if s["label"] == "manual_spam")
    assert stored["text"].startswith("<script>alert(1)</script>") and stored["truncated"] is True
    assert all(len(sample["text"]) <= 800 for sample in samples)
    assert [s["label"] for s in samples][:1] == ["manual_ham"]  # human evidence first
    library = env.call("GET", "/api/fingerprints", query="type=content").json()["items"][0]
    assert len(library["pattern_text"]) == 160 and library["pattern_truncated"] is True
    detail = env.fingerprint(library["id"])
    assert len(detail["pattern_text"]) == 4000
    page = env.call("GET", "/api/fingerprints", query="limit=50").json()
    assert page["limit"] == 50


def test_static_shell_has_strict_headers_and_no_html_injection_sinks(env):
    page = env.call("GET", "/", token=None)
    assert page.status == 200
    assert page.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert "script-src 'self' https://telegram.org" in CONTENT_SECURITY_POLICY
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY
    html = page.body.decode("utf-8")
    assert '<html lang="zh-CN">' in html
    assert re.search(r"<script>(?!</script>)", html) is None  # no inline scripts
    assert "style=" not in html
    script = env.call("GET", "/static/app.js", token=None).body.decode("utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                 "initDataUnsafe"):
        assert sink not in script, sink
    assert str(REVIEWER) not in script and str(REVIEWER) not in html
    assert env.call("GET", "/static/app.css", token=None).headers["Content-Type"].startswith("text/css")
    assert env.call("HEAD", "/", token=None).body == b""


def test_transport_errors_are_chinese(env):
    env.login()
    assert_chinese_error(env.call("GET", "/nope"), 404, "not_found")
    assert_chinese_error(env.call("DELETE", "/api/reviews/actions"), 405, "method_not_allowed")
    assert_chinese_error(env.call("POST", "/"), 405, "method_not_allowed")
    assert_chinese_error(env.call("POST", "/api/reviews/actions", raw_body=b"{}",
                                  headers={"Content-Type": "text/plain"}), 415,
                         "unsupported_media_type")
    assert_chinese_error(env.call("POST", "/api/reviews/actions", raw_body=b"{oops",
                                  headers={"Content-Type": "application/json"}), 400, "bad_json")
    assert_chinese_error(env.call("POST", "/api/reviews/actions",
                                  raw_body=b"x" * (MAX_BODY_BYTES + 1),
                                  headers={"Content-Type": "application/json"}), 413,
                         "payload_too_large")
    assert_chinese_error(env.call("GET", "/api/reviews/999"), 404, "review_not_found")
    assert_chinese_error(env.call("GET", "/api/fingerprints/999"), 404, "fingerprint_not_found")
    assert env.call("GET", "/healthz", token=None).json() == {"status": "ok"}


def test_internal_errors_do_not_leak_details(env):
    env.login()

    def explode(*args, **kwargs):
        raise RuntimeError("database path /secret/bot.db is broken")

    env.db.webui_summary = explode
    response = env.call("GET", "/api/summary")
    assert_chinese_error(response, 500, "internal")
    assert b"secret" not in response.body


# ------------------------------------------------------------ Chinese text


ALLOWED_ASCII_WORDS = {"Telegram", "JSON"}


def _assert_user_text(value):
    assert CJK.search(value), value
    words = set(re.findall(r"[A-Za-z_]{2,}", value))
    assert words <= ALLOWED_ASCII_WORDS, (value, words)


def test_all_user_facing_api_text_is_simplified_chinese():
    for code in text.ERRORS:
        _assert_user_text(text.error_message(code, limit=MAX_BATCH_ITEMS))
    for value in text.ITEM_RESULTS.values():
        for message in value.values() if isinstance(value, dict) else [value]:
            _assert_user_text(message)
    for field in text.FIELD_LABELS:
        _assert_user_text(text.field_error(field))
    for group in text.LABELS.values():
        for message in group.values():
            _assert_user_text(message)


def test_labels_cover_every_code_the_bot_writes():
    labels = text.LABELS
    assert {"phrase", "skeleton", "content"} <= set(labels["fingerprint_types"])
    assert {"llm_spam", "llm_spam_phrase", "vote_confirmed", "manual",
            "disabled_false_positive"} <= set(labels["sources"])
    assert {"manual_ham", "manual_spam", "llm_benign", "llm_spam", "local_hit"} <= set(
        labels["evidence_labels"])
    assert {"pending", "kept", "disabled", "restored"} <= set(labels["review_statuses"])
    assert {"disable", "keep", "insufficient_evidence"} <= set(labels["decisions"])


def test_frontend_copy_is_chinese():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for phrase in ("待复核", "已保留", "已停用", "指纹库", "停用并下一条", "保留并下一条",
                   "稍后处理", "确认删除", "无法撤销", "加载更多", "请在 Telegram 中"):
        assert phrase in script, phrase
    assert "指纹复核台" in html
    # Every string literal shown to people (passed as text/title/label/toast) has CJK.
    for literal in re.findall(r'(?:text|title|label|placeholder): "([^"]+)"', script):
        assert CJK.search(literal) or re.fullmatch(r"[\s：，。（）#]*", literal), literal


def test_store_version_ignores_traffic_but_tracks_decision_inputs(env):
    fp, _ = env.seed("机器人")
    with env.db._locked_conn() as conn:
        row = conn.execute("SELECT * FROM fingerprints WHERE id=?", (fp.id,)).fetchone()
        base = fingerprint_version(row, [])
        changed = dict(row)
        changed["hit_count"] += 10
        changed["weight"] += 5
        assert fingerprint_version(changed, []) == base
        changed["enabled"] = 0
        assert fingerprint_version(changed, []) != base
