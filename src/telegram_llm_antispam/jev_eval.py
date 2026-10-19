"""Offline Jev evaluation for historical Telegram moderation records.

The module deliberately treats historical state as incomplete.  It exports only
facts that were persisted at moderation time, never the current profile or
reputation as a substitute for historical values.  The Jev request is kept in
one fixed question map for every sample and the original LLM decision is stored
only as comparison metadata, never in the Jev state.
"""

from __future__ import annotations

import argparse
import csv
import concurrent.futures
import hashlib
import html
import json
import os
import random
import re
import sqlite3
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from .text import normalize_text


JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL_DEFAULT = "jev-latest"
DATASET_VERSION = "jev-eval-2"

# This object is immutable by convention and is serialized into the prompt hash.
# Do not alter it per sample: reproducibility depends on one Questions map.
JEV_QUESTIONS: dict[str, dict[str, Any]] = {
    "is_spam": {
        "type": "noul",
        "instructions": (
            "Does this Telegram group message itself have proactive advertising, commercial "
            "lead-generation, scam, gambling/porn promotion, recruitment-for-profit, or "
            "traffic-diversion intent? Judge the message and contemporaneous context only. "
            "Normal discussion, quoted/reporting of an advertisement, technical links, jokes, "
            "and ordinary product recommendations are not spam. A weak bio, username, link, "
            "or reputation signal alone is not enough. A yes is spam intent; a no is benign or "
            "insufficient evidence."
        ),
        "criteria": {
            "true": "The message is an active ad, solicitation, scam, or commercial diversion.",
            "false": "The message is normal discussion or evidence is insufficient.",
        },
    },
    "category": {
        "type": "choice",
        "instructions": "Select the primary category of the message if it is advertising-like.",
        "criteria": {
            "ads": "General commercial promotion or paid service advertising.",
            "scam": "Fraud, impersonation, fake investment, or deceptive scheme.",
            "porn": "Sexual or adult-content promotion.",
            "gambling": "Betting, casino, lottery, or gambling promotion.",
            "crypto": "Crypto, token, mining, exchange, or trading promotion.",
            "traffic_diversion": "Inviting users to another group, channel, bot, or private contact.",
            "mass_mention": "Mass unsolicited mentions used for promotion or disruption.",
            "benign": "Normal non-advertising discussion.",
            "unknown": "Insufficient evidence to choose another category.",
        },
    },
}
JEV_PROMPT_HASH = hashlib.sha256(
    json.dumps(JEV_QUESTIONS, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


def _json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple, int, float, bool, str)):
        return value
    return str(value)


def _parse_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _body_from_detail(detail: str | None) -> str | None:
    if not detail:
        return None
    match = re.search(r"正文：\s*\n<blockquote>(.*?)</blockquote>", detail, re.S)
    if not match:
        return None
    body = html.unescape(match.group(1))
    # The builder escapes HTML but does not rewrite zero-width characters.
    return body if body != "(empty)" else ""


def _profile_from_detail(detail: str | None) -> dict[str, str | None] | None:
    if not detail:
        return None
    profile = re.search(r"用户资料：(?P<name>.*?)\s+@(?P<username>[^\n]*)\nBio：(?P<bio>[^\n]*)", detail)
    if not profile:
        return None
    name = html.unescape(profile.group("name")).strip() or None
    username = html.unescape(profile.group("username")).strip()
    bio = html.unescape(profile.group("bio")).strip()
    return {
        "display_name": name,
        "username": username if username != "-" else None,
        "bio": bio if bio != "-" else None,
    }


def _historical_text(meta: dict[str, Any], detail: str | None) -> str | None:
    snapshot = meta.get("text_snapshot")
    if isinstance(snapshot, str):
        return snapshot
    return _body_from_detail(detail)


def _llm_from_meta(meta: dict[str, Any]) -> dict[str, Any] | None:
    outcome = meta.get("llm_outcome")
    if isinstance(outcome, dict):
        return {
            "status": outcome.get("status"),
            "is_spam": outcome.get("is_spam"),
            "confidence": outcome.get("confidence"),
            "category": outcome.get("category"),
            "error": outcome.get("error"),
        }
    if "llm_is_spam" in meta or "llm_confidence" in meta:
        return {
            "status": "ok",
            "is_spam": meta.get("llm_is_spam"),
            "confidence": meta.get("llm_confidence"),
            "category": meta.get("llm_category"),
            "error": None,
        }
    return None


def _reference_for_status(status: str) -> tuple[str | None, str, bool]:
    if status in {"confirmed_spam", "admin_banned"}:
        return None, f"session_{status}_requires_event_audit", False
    if status == "released":
        return None, "session_released_requires_event_audit", False
    if status == "expired_released":
        return None, "timeout_default_release_unknown", False
    return None, "unlabeled", False


def _connect_readonly(path: Path) -> sqlite3.Connection:
    # URI mode=ro plus query_only protects an accidentally pointed-at live DB.
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def build_dataset(db_path: Path, *, limit: int | None = None, seed: int = 0) -> list[dict[str, Any]]:
    """Build a conservative dataset from persisted snapshots and vote outcomes."""
    conn = _connect_readonly(db_path)
    try:
        actions = conn.execute(
            "SELECT id, chat_id, message_id, user_id, action, reason, confidence, "
            "metadata_json, created_at FROM action_log ORDER BY id"
        ).fetchall()
        sessions = conn.execute(
            "SELECT id, chat_id, original_message_id, status, spam_votes, ham_votes, "
            "detail_text, created_at FROM vote_sessions ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    by_message: dict[tuple[int, int], list[sqlite3.Row]] = {}
    for row in actions:
        by_message.setdefault((row["chat_id"], row["message_id"]), []).append(row)

    from .jev_audit import classify_events

    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for session in sessions:
        key = (session["chat_id"], session["original_message_id"])
        related = by_message.get(key, [])
        source_action = next(
            (row for row in related if row["action"] in {"allow", "review", "withdraw_vote", "ban"}
             and _historical_text(_parse_json(row["metadata_json"]), session["detail_text"]) is not None),
            None,
        )
        meta = _parse_json(source_action["metadata_json"] if source_action else None)
        text = _historical_text(meta, session["detail_text"])
        if text is None or key in seen:
            continue
        label, source, human = _reference_for_status(session["status"])
        provenance = classify_events([dict(row) for row in related], [dict(session)])
        # Even direct bans/vote triggers are only human-event candidates: the
        # human may have assessed the profile/account rather than this message.
        if provenance["candidate_message_label"] == "ham":
            label, source, human = "ham", "verified_human_message_release", True
        original = _llm_from_meta(meta)
        rows.append(_make_record(
            key=key,
            text=text,
            user_id=(source_action["user_id"] if source_action else None),
            profile=_profile_from_detail(session["detail_text"]),
            original=original,
            action={
                "action": source_action["action"] if source_action else "vote_session",
                "reason": source_action["reason"] if source_action else session["status"],
                "confidence": source_action["confidence"] if source_action else None,
                "vote_status": session["status"],
                "spam_votes": session["spam_votes"],
                "ham_votes": session["ham_votes"],
            },
            reference={"label": label, "source": source, "human_confirmed": human,
                       "event_scope": provenance["scope"],
                       "candidate_message_label": provenance["candidate_message_label"],
                       "user_label": provenance["human_account_label"]},
            created_at=session["created_at"],
        ))
        seen.add(key)

    # Direct bans and LLM snapshots not represented by a vote session.
    for row in actions:
        if row["action"] not in {"allow", "review", "withdraw_vote", "ban"}:
            # Finalization snapshots can contain a DIFFERENT triggering message
            # copied onto a sibling session's action. Never use them as its body.
            continue
        key = (row["chat_id"], row["message_id"])
        if key in seen:
            continue
        meta = _parse_json(row["metadata_json"])
        text = _historical_text(meta, None)
        if text is None:
            continue
        human = None
        source = "unlabeled_action"
        if row["reason"].startswith("hard_") or row["reason"].startswith("known_"):
            source = "hard_rule_unreviewed"
        rows.append(_make_record(
            key=key,
            text=text,
            user_id=row["user_id"],
            profile=None,
            original=_llm_from_meta(meta),
            action={"action": row["action"], "reason": row["reason"], "confidence": row["confidence"]},
            reference={"label": None, "source": source, "human_confirmed": False},
            created_at=row["created_at"],
        ))
        seen.add(key)

    rows.sort(key=lambda item: (item["created_at"] or 0, item["sample_id"]))
    if limit is not None and limit >= 0 and len(rows) > limit:
        rng = random.Random(seed)
        rows = sorted(rng.sample(rows, limit), key=lambda item: item["sample_id"])
    return rows


def _make_record(
    *, key: tuple[int, int], text: str, user_id: int | None, profile: dict[str, Any] | None,
    original: dict[str, Any] | None, action: dict[str, Any], reference: dict[str, Any], created_at: int,
) -> dict[str, Any]:
    return {
        "dataset_version": DATASET_VERSION,
        "sample_id": f"{key[0]}:{key[1]}",
        "chat_id": key[0],
        "message_id": key[1],
        "user_id": user_id,
        "raw_text": text,
        "clean_text": normalize_text(text),
        # Historical reputation, OG, links and personal-channel posts are not persisted
        # as a time-versioned feature snapshot. Null means missing, never current state.
        "historical_context": {
            "sender_profile_at_moderation": profile,
            "reputation_at_moderation": None,
            "og_preview_at_moderation": None,
            "personal_chat_at_moderation": None,
            "missing": ["historical_reputation", "og_preview", "personal_chat"]
            + ([] if profile is not None else ["sender_profile"]),
        },
        "reference": reference,
        "original_llm": original,
        "original_action": action,
        "created_at": created_at,
        "difficulty": {
            "is_human_labeled": bool(reference.get("human_confirmed")),
            "needs_manual_review": reference.get("label") is None,
        },
    }


class JevClient:
    def __init__(self, api_key: str, *, endpoint: str = JEV_ENDPOINT, model: str = JEV_MODEL_DEFAULT,
                 proxy: str | None = None, timeout: float = 30.0) -> None:
        self.api_key = api_key
        self.endpoint = endpoint
        self.model = model
        self.proxy = proxy
        self.timeout = timeout

    def decide(self, state: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({"model": self.model, "state": state, "questions": JEV_QUESTIONS}, ensure_ascii=False).encode()
        request = urllib.request.Request(
            self.endpoint, data=body, method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}) if self.proxy else urllib.request.ProxyHandler({})
        )
        started = time.perf_counter()
        try:
            with opener.open(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return {"status": "ok", "elapsed_ms": round((time.perf_counter() - started) * 1000, 2), "response": payload}
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            detail = getattr(exc, "read", lambda: b"")()
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", "replace")[:1000]
            return {"status": "failed", "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                    "error": f"{type(exc).__name__}: {exc}", "response_excerpt": detail}


def _jev_state(sample: dict[str, Any]) -> dict[str, Any]:
    # Deliberately omit reference labels, prior LLM/action fields and difficulty annotations.
    context = sample.get("historical_context") or {}
    return {
        "message": {"raw_text": sample.get("raw_text", ""), "clean_text": sample.get("clean_text", "")},
        "sender_profile_at_moderation": context.get("sender_profile_at_moderation"),
        "reputation_at_moderation": context.get("reputation_at_moderation"),
        "og_preview_at_moderation": context.get("og_preview_at_moderation"),
        "personal_chat_at_moderation": context.get("personal_chat_at_moderation"),
        "message_link": f"https://t.me/c/{str(sample['chat_id']).removeprefix('-100')}/{sample['message_id']}",
    }


def run_predictions(dataset: Iterable[dict[str, Any]], output: Path, *, api_key: str | None,
                    call_api: bool, use_cache: bool, model: str, endpoint: str, proxy: str | None,
                    timeout: float, concurrency: int = 1) -> list[dict[str, Any]]:
    existing: dict[str, dict[str, Any]] = {}
    if use_cache and output.exists():
        for line in output.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                if item.get("sample_id"):
                    existing[item["sample_id"]] = item
            except json.JSONDecodeError:
                continue
    client = JevClient(api_key or "", endpoint=endpoint, model=model, proxy=proxy, timeout=timeout)
    predictions: list[dict[str, Any]] = []
    samples = list(dataset)
    pending: list[dict[str, Any]] = []
    results: dict[str, dict[str, Any]] = {}
    for sample in samples:
        cached = existing.get(sample["sample_id"])
        # When calling the API, only successful rows are cache hits; failed/not_run
        # rows must be retryable. In report-only mode every existing row is reusable.
        if cached is not None and (not call_api or (use_cache and cached.get("status") == "ok")):
            results[sample["sample_id"]] = cached
            continue
        if not call_api:
            result = {"status": "not_run"}
        elif not api_key:
            result = {"status": "failed", "error": "JEV_API_KEY is not set"}
        else:
            pending.append(sample)
            continue
        results[sample["sample_id"]] = _prediction_record(sample, result, model)
    if pending:
        workers = max(1, min(int(concurrency), len(pending)))
        if workers == 1:
            completed = ((sample, client.decide(_jev_state(sample))) for sample in pending)
            for sample, result in completed:
                results[sample["sample_id"]] = _prediction_record(sample, result, model)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                future_map = {pool.submit(client.decide, _jev_state(sample)): sample for sample in pending}
                for future in concurrent.futures.as_completed(future_map):
                    sample = future_map[future]
                    try:
                        result = future.result()
                    except Exception as exc:  # defensive; client normally captures errors
                        result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
                    results[sample["sample_id"]] = _prediction_record(sample, result, model)
    for sample in samples:
        predictions.append(results[sample["sample_id"]])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in predictions) + "\n", encoding="utf-8")
    return predictions


def _prediction_record(sample: dict[str, Any], result: dict[str, Any], model: str) -> dict[str, Any]:
        response = result.get("response") if isinstance(result, dict) else None
        answers = response.get("answers", {}) if isinstance(response, dict) else {}
        spam = answers.get("is_spam", {}) if isinstance(answers, dict) else {}
        category = answers.get("category", {}) if isinstance(answers, dict) else {}
        return {
            "dataset_version": DATASET_VERSION,
            "sample_id": sample["sample_id"],
            "model": response.get("model", model) if isinstance(response, dict) else model,
            "prompt_hash": JEV_PROMPT_HASH,
            "status": result.get("status"),
            "elapsed_ms": result.get("elapsed_ms"),
            "error": result.get("error"),
            "is_spam_noul": spam.get("noul"),
            "category_choice": category.get("choice"),
            "category_probabilities": category.get("probabilities"),
            "category_confidence": category.get("confidence"),
            "usage": response.get("usage") if isinstance(response, dict) else None,
            "raw_response": response,
        }


def _simulated_action(pred: dict[str, Any], review_threshold: float, ban_threshold: float) -> str | None:
    if pred.get("status") != "ok" or not isinstance(pred.get("is_spam_noul"), (int, float)):
        return None
    probability = float(pred["is_spam_noul"])
    if probability < 0.5:
        return "allow"
    if probability >= ban_threshold:
        return "ban"
    if probability >= review_threshold:
        return "withdraw_vote"
    return "review"


def _metrics(pairs: list[tuple[str, str]]) -> dict[str, float | int | None]:
    tp = sum(actual == "spam" and pred == "spam" for actual, pred in pairs)
    fp = sum(actual == "ham" and pred == "spam" for actual, pred in pairs)
    fn = sum(actual == "spam" and pred == "ham" for actual, pred in pairs)
    tn = sum(actual == "ham" and pred == "ham" for actual, pred in pairs)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else None
    return {"n": len(pairs), "tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": precision, "recall": recall, "f1": f1,
            "false_positive_rate": fp / (fp + tn) if fp + tn else None, "miss_rate": fn / (fn + tp) if fn + tp else None}


def write_comparison(dataset: list[dict[str, Any]], predictions: list[dict[str, Any]], output: Path,
                     *, review_threshold: float, ban_threshold: float) -> list[dict[str, Any]]:
    pred_by_id = {item["sample_id"]: item for item in predictions}
    rows: list[dict[str, Any]] = []
    for sample in dataset:
        pred = pred_by_id.get(sample["sample_id"], {})
        noul = pred.get("is_spam_noul")
        jev_label = "spam" if isinstance(noul, (int, float)) and float(noul) >= 0.5 else ("ham" if isinstance(noul, (int, float)) else None)
        rows.append({
            "sample_id": sample["sample_id"], "reference_label": sample["reference"].get("label"),
            "reference_source": sample["reference"].get("source"), "human_confirmed": sample["reference"].get("human_confirmed"),
            "difficulty": "human_labeled" if sample["reference"].get("human_confirmed") else "unlabeled_or_hard_rule",
            "raw_text": sample.get("raw_text", ""), "original_action": sample["original_action"].get("action"),
            "original_reason": sample["original_action"].get("reason"), "original_llm_status": (sample.get("original_llm") or {}).get("status"),
            "original_llm_is_spam": (sample.get("original_llm") or {}).get("is_spam"),
            "original_llm_confidence": (sample.get("original_llm") or {}).get("confidence"),
            "jev_status": pred.get("status"), "jev_is_spam_noul": noul, "jev_label_50": jev_label,
            "jev_category": pred.get("category_choice"), "jev_category_confidence": pred.get("category_confidence"),
            "jev_simulated_action_70_85": _simulated_action(pred, review_threshold, ban_threshold),
            "elapsed_ms": pred.get("elapsed_ms"), "input_tokens": (pred.get("usage") or {}).get("input_tokens") if isinstance(pred.get("usage"), dict) else None,
            "cost_usd": (pred.get("usage") or {}).get("cost_usd") if isinstance(pred.get("usage"), dict) else None,
        })
    output.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]), extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    return rows


def write_report(dataset: list[dict[str, Any]], predictions: list[dict[str, Any]], output: Path,
                 *, review_threshold: float, ban_threshold: float) -> None:
    if any(not row.get("audit_version") for row in dataset):
        output.write_text(
            "# Jev 评测输入待审计\n\n"
            "原会话终态不能作为消息级真人真值；本次不计算总体 Recall/Precision/F1。\n\n"
            "请使用 telegram_llm_antispam.jev_audit 和已有预测缓存完成事件、"
            "输入充分性、消息/用户标签审计。旧样本保持冻结，不能将不同输入集合的指标直接比较。\n\n"
            f"样本数：{len(dataset)}；预测数：{len(predictions)}。\n"
            "70%/85%映射仅为模型分数映射，不是完整历史系统回放。\n",
            encoding="utf-8",
        )
        return
    by_id = {item["sample_id"]: item for item in predictions}
    pairs: list[tuple[str, str]] = []
    normal_pairs: list[tuple[str, str]] = []
    difficult_pairs: list[tuple[str, str]] = []
    original_pairs: list[tuple[str, str]] = []
    source_pairs: dict[str, list[tuple[str, str]]] = {}
    llm_missing = 0
    llm_failed = 0
    for sample in dataset:
        actual = sample["reference"].get("label")
        pred = by_id.get(sample["sample_id"], {})
        noul = pred.get("is_spam_noul")
        if actual not in {"spam", "ham"} or not isinstance(noul, (int, float)):
            original = sample.get("original_llm") or {}
            if actual in {"spam", "ham"} and original.get("status") == "failed":
                llm_failed += 1
            continue
        predicted = "spam" if float(noul) >= 0.5 else "ham"
        pair = (actual, predicted)
        pairs.append(pair)
        source = str(sample["reference"].get("source") or "unknown")
        source_pairs.setdefault(source, []).append(pair)
        original = sample.get("original_llm") or {}
        if not original:
            llm_missing += 1
        elif original.get("status") == "failed":
            llm_failed += 1
        if original.get("status") == "ok" and isinstance(original.get("is_spam"), bool):
            original_pairs.append((actual, "spam" if original["is_spam"] else "ham"))
        if source.startswith("human_") and original.get("status") == "ok":
            target = difficult_pairs if original.get("is_spam") != (actual == "spam") else normal_pairs
        else:
            target = normal_pairs
        target.append(pair)
    latencies = [float(p["elapsed_ms"]) for p in predictions if p.get("status") == "ok" and isinstance(p.get("elapsed_ms"), (int, float))]
    usages = [p.get("usage", {}).get("input_tokens") for p in predictions if isinstance(p.get("usage"), dict) and isinstance(p["usage"].get("input_tokens"), (int, float))]
    output_usages = [p.get("usage", {}).get("output_tokens") for p in predictions if isinstance(p.get("usage"), dict) and isinstance(p["usage"].get("output_tokens"), (int, float))]
    failures = sum(p.get("status") == "failed" for p in predictions)
    simulated = {action: sum(_simulated_action(p, review_threshold, ban_threshold) == action for p in predictions) for action in ("allow", "review", "withdraw_vote", "ban")}
    buckets = {
        "<0.50": sum(isinstance(p.get("is_spam_noul"), (int, float)) and p["is_spam_noul"] < 0.5 for p in predictions),
        "0.50-0.69": sum(isinstance(p.get("is_spam_noul"), (int, float)) and 0.5 <= p["is_spam_noul"] < review_threshold for p in predictions),
        "0.70-0.84": sum(isinstance(p.get("is_spam_noul"), (int, float)) and review_threshold <= p["is_spam_noul"] < ban_threshold for p in predictions),
        ">=0.85": sum(isinstance(p.get("is_spam_noul"), (int, float)) and p["is_spam_noul"] >= ban_threshold for p in predictions),
    }
    source_counts: dict[str, int] = {}
    for sample in dataset:
        source = str(sample["reference"].get("source") or "unknown")
        source_counts[source] = source_counts.get(source, 0) + 1
    source_lines = [f"- `{source}`：`{json.dumps(_metrics(values), ensure_ascii=False)}`" for source, values in sorted(source_pairs.items())]
    lines = [
        "# Jev 离线评测报告", "", f"- 数据集版本：`{DATASET_VERSION}`", f"- Questions 哈希：`{JEV_PROMPT_HASH}`",
        f"- 样本数：{len(dataset)}（可标注 {sum(bool(x['reference'].get('human_confirmed')) for x in dataset)}，未知/未复核 {sum(not x['reference'].get('human_confirmed') for x in dataset)}）",
        f"- 来源计数：`{json.dumps(source_counts, ensure_ascii=False, sort_keys=True)}`",
        "", "## 数据完整性与防泄漏", "",
        "Jev state 只包含保存时的正文、可恢复的当时资料字段和明确标记为缺失的上下文；未包含原 LLM 输出、最终处置、投票结果或真值标签。当前信誉、当前 Bio、当前个人频道未冒充历史状态。",
        "", "## Jev 单独识别（noul >= 0.5 为广告）", "", f"```json\n{json.dumps(_metrics(pairs), ensure_ascii=False, indent=2)}\n```",
        "", "### 按人工来源与困难样本", "", *source_lines,
        f"- 普通样本（原 LLM 与人工标签一致或无可比原 LLM）：`{json.dumps(_metrics(normal_pairs), ensure_ascii=False)}`",
        f"- 困难样本（原 LLM 与人工标签不一致）：`{json.dumps(_metrics(difficult_pairs), ensure_ascii=False)}`",
        "", "## 原 LLM 单独识别（仅有成功原 LLM 且有人工标签）", "",
        f"`{json.dumps(_metrics(original_pairs), ensure_ascii=False)}`",
        "", "## 完整系统阈值回放（初步）", "", f"使用 review={review_threshold:.0%}、ban={ban_threshold:.0%}，并将 Jev noul 概率作为未校准的 confidence；高信誉例外因历史信誉缺失未强行模拟。动作计数：`{json.dumps(simulated, ensure_ascii=False)}`。这不是生产处置建议。",
        "", "## 置信度与请求质量", "", f"- Jev noul 概率区间计数：`{json.dumps(buckets, ensure_ascii=False)}`",
        f"- API 失败：{failures}/{len(predictions)} ({failures / len(predictions):.1%})" if predictions else "- API 失败：无样本",
        f"- 延迟 P50/P95：{statistics.median(latencies):.1f} ms / {sorted(latencies)[max(0, int(len(latencies)*.95)-1)]:.1f} ms" if latencies else "- 延迟：无成功请求",
        f"- 平均输入/输出 token：{statistics.mean(usages):.1f}/{statistics.mean(output_usages):.1f}；总输入/输出：{sum(usages)}/{sum(output_usages)}；按 $0.42/M 输入 token参考单价估算：${sum(usages) * 0.42 / 1_000_000:.4f}（API 未返回 cost_usd，实际账单以账户为准）" if usages else "- token：无返回 usage",
        f"- 原 LLM 在可导出样本中：缺失 {llm_missing}，调用失败 {llm_failed}；两者不计为原模型错误。",
        "", "## 主要数据缺口", "", "- 正常 `allow`/`review` 记录没有历史正文快照，不能直接当作 ham。需要从 Telegram 导出/审计日志补充并人工抽样确认。",
        "- 历史信誉、OG、个人频道内容没有按消息时间持久化；本轮标记缺失，不使用当前值。",
        "- 硬规则封禁和超时默认放行单独保留为未知来源，不能归因于 Jev 或当作绝对真值。",
    ]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and evaluate a Jev anti-spam dataset.")
    parser.add_argument("--db", type=Path, help="Read-only SQLite database used to build a dataset.")
    parser.add_argument("--dataset", type=Path, help="Reuse an existing dataset.jsonl instead of reading SQLite.")
    parser.add_argument("--out-dir", type=Path, default=Path("jev-eval-output"))
    parser.add_argument("--sample-size", type=int)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--call-api", action="store_true", help="Actually call Jev; otherwise write not_run predictions.")
    parser.add_argument("--use-cache", action="store_true")
    parser.add_argument("--api-key", default=None, help="Avoid this in shell history; prefer JEV_API_KEY.")
    parser.add_argument("--model", default=JEV_MODEL_DEFAULT)
    parser.add_argument("--endpoint", default=JEV_ENDPOINT)
    parser.add_argument("--proxy", default=None, help="Explicit HTTP(S) proxy, e.g. http://127.0.0.1:2235")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--concurrency", type=int, default=1, help="Parallel Jev requests; keep conservative for a shared key.")
    parser.add_argument("--review-threshold", type=float, default=0.70)
    parser.add_argument("--ban-threshold", type=float, default=0.85)
    args = parser.parse_args(argv)
    if not args.db and not args.dataset:
        parser.error("one of --db or --dataset is required")
    dataset = _read_jsonl(args.dataset) if args.dataset else build_dataset(args.db, limit=args.sample_size, seed=args.seed)
    if args.sample_size is not None and args.dataset and len(dataset) > args.sample_size:
        dataset = sorted(random.Random(args.seed).sample(dataset, args.sample_size), key=lambda item: item["sample_id"])
    dataset_path = args.out_dir / "dataset.jsonl"
    _write_jsonl(dataset_path, dataset)
    key = args.api_key or os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")
    predictions = run_predictions(dataset, args.out_dir / "predictions.jsonl", api_key=key, call_api=args.call_api,
                                  use_cache=args.use_cache, model=args.model, endpoint=args.endpoint, proxy=args.proxy,
                                  timeout=args.timeout, concurrency=args.concurrency)
    write_comparison(dataset, predictions, args.out_dir / "comparison.csv", review_threshold=args.review_threshold, ban_threshold=args.ban_threshold)
    write_report(dataset, predictions, args.out_dir / "report.md", review_threshold=args.review_threshold, ban_threshold=args.ban_threshold)
    print(json.dumps({"samples": len(dataset), "predictions": len(predictions), "out_dir": str(args.out_dir), "prompt_hash": JEV_PROMPT_HASH}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
