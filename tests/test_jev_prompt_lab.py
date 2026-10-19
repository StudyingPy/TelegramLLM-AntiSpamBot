import asyncio
import json

import pytest

from telegram_llm_antispam.jev_prompt_lab import VARIANTS, compact_state, report, request_for, run


def row(text="真实\u200b消息"):
    return {
        "id": "test",
        "state": {"raw_text": text},
        "human_label": "spam",
        "baseline": {"judgement": {"is_spam": True}},
    }


def test_inputs_preserve_raw_zero_width_and_do_not_read_outcomes():
    value = row()
    value["state"].update(
        {
            "content_hash": "hash",
            "sender_reputation": 0,
            "sender_profile": {"bio": "当时Bio", "user_id": 123, "bio_fetched_at": 1},
        }
    )
    compact = compact_state(value["state"])
    assert compact["raw_text"] == "真实\u200b消息"
    assert compact["sender_profile"] == {"bio": "当时Bio"}
    assert "sender_reputation" not in compact and "content_hash" not in compact
    for name in VARIANTS:
        request = request_for(value, name, "jev-1.13.0")
        assert "baseline" not in request and "human_label" not in request


def test_call_cap_checked_before_requests_or_partial_spend(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", "fake")
    with pytest.raises(ValueError, match="exceeds"):
        asyncio.run(run([row()], tmp_path, model="jev-1.13.0", call_api=True, max_calls=0))
    assert not (tmp_path / "predictions.jsonl").exists()


def test_duplicate_requests_are_coalesced_and_cache_rebuild_never_calls(tmp_path, monkeypatch):
    import telegram_llm_antispam.jev_prompt_lab as module

    calls = []

    async def fake(request, key, timeout, proxy):
        calls.append(request)
        await asyncio.sleep(0.01)
        return {"status": "ok", "noul": 0.8}

    monkeypatch.setattr(module, "evaluate", fake)
    monkeypatch.setenv("JEV_API_KEY", "test")
    variants = ["production_replay", "long_compact"]
    # A raw-text-only input produces the same request under both input variants.
    results = asyncio.run(
        run([row()], tmp_path, model="jev-1.13.0", call_api=True, max_calls=1, variants=variants)
    )
    assert len(results) == 2 and len(calls) == 1
    monkeypatch.delenv("JEV_API_KEY")
    rebuilt = asyncio.run(run([row()], tmp_path, model="jev-1.13.0", variants=variants))
    assert len(rebuilt) == 2 and len(calls) == 1
    assert all(r["cached"] and r["result"]["noul"] == 0.8 for r in rebuilt)
    assert len((tmp_path / "predictions.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_default_mode_is_dry_run(tmp_path, monkeypatch):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    results = asyncio.run(run([row()], tmp_path, model="jev-1.13.0"))
    assert all(p["result"]["status"] == "not_called" for p in results)
    assert not (tmp_path / "predictions.jsonl").exists()


def test_request_hash_covers_questions_model_and_zero_width(tmp_path, monkeypatch):
    import telegram_llm_antispam.jev_prompt_lab as module

    requests = []

    async def fake(request, *args):
        requests.append(request)
        return {"status": "ok", "noul": 0.5}

    monkeypatch.setattr(module, "evaluate", fake)
    monkeypatch.setenv("JEV_API_KEY", "test")
    for text, model in [
        ("正文", "jev-1.13.0"),
        ("正\u200b文", "jev-1.13.0"),
        ("正文", "jev-1.14.0"),
    ]:
        asyncio.run(
            run([row(text)], tmp_path, model=model, call_api=True, variants=["short_zh_compact"])
        )
    assert len(requests) == 3
    stored = [
        json.loads(line)
        for line in (tmp_path / "predictions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len({r["request_hash"] for r in stored}) == 3


def test_failed_variant_cannot_change_comparison_denominator_or_create_gold(tmp_path):
    rows = []
    for oid in ("a", "b"):
        value = row(oid)
        value.update(
            id=oid,
            source="live_frozen",
            update_type="message",
            truncated=False,
            baseline={"status": "ok", "judgement": {"is_spam": True}},
        )
        rows.append(value)
    predictions = [
        {
            "id": oid,
            "variant": variant,
            "result": {"status": "failed"}
            if (oid, variant) == ("b", "two")
            else {"status": "ok", "noul": 0.8, "usage": {"input_tokens": 1}},
        }
        for oid in ("a", "b")
        for variant in ("one", "two")
    ]
    stats = report(rows, predictions, tmp_path)
    assert stats["one"]["mimo_positive_n"] == stats["two"]["mimo_positive_n"] == 1
    assert stats["one"]["gold_accuracy"] is None
