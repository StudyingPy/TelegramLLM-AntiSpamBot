"""Cache-only audit: event provenance, input sufficiency and label scope are separate.

No network client or action executor is imported. Current profile rows are audit
attachments only. Recovered context never silently replaces the cached Jev input.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

AUDIT_VERSION = "jev-label-audit-v2"
HUMAN_BANS = {"admin_banned_user", "catchup_banned_user"}
HUMAN_RELEASES = {"vote_released", "admin_released_user", "catchup_kept_released"}
INITIAL_ACTIONS = {"allow", "review", "withdraw_vote", "ban"}
NO_LLM_REASONS = {
    "hard_spam_message",
    "spam_profile_bio",
    "known_high_weight_fingerprint",
    "repeated_open_vote_message_same_user",
    "repeated_bot_only_invocation_after_ad",
    "repeated_skeleton_across_senders",
}


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows), encoding="utf-8"
    )


def meta(action: dict) -> dict:
    value = action.get("metadata_json", {})
    if isinstance(value, str):
        value = json.loads(value or "{}")
    return value if isinstance(value, dict) else {}


def index_mapping(mapping: dict) -> tuple[dict, dict]:
    actions, sessions = defaultdict(list), defaultdict(list)
    for row in mapping.get("actions", []):
        actions[(row["chat_id"], row["message_id"])].append(row)
    for row in mapping.get("sessions", []):
        sessions[(row["chat_id"], row["original_message_id"])].append(row)
    for rows in actions.values():
        rows.sort(key=lambda x: x["id"])
    return actions, sessions


def classify_events(actions: list[dict], sessions: list[dict]) -> dict:
    """Session status, tally and sibling metadata alone never establish human truth."""
    session_ids = {x["id"] for x in sessions}
    spam, ham, human, propagated, automatic, ambiguous = [], [], [], [], [], []
    related, evidence = set(), []
    for action in actions:
        m, kind = meta(action), action["action"]
        sid, trigger = m.get("vote_session_id"), m.get("trigger_vote_session_id")
        related_ids = m.get("related_vote_session_ids") or []
        related.update(related_ids)
        if kind not in HUMAN_BANS | HUMAN_RELEASES | {"vote_confirmed_spam", "auto_banned_user"}:
            continue
        evidence.append(
            {
                "action_log_id": action["id"],
                "action": kind,
                "created_at": action["created_at"],
                "vote_session_id": sid,
                "trigger_vote_session_id": trigger,
                "related_vote_session_ids": related_ids,
                "banned": m.get("banned"),
                "moderator_user_id": m.get("moderator_user_id"),
            }
        )
        if kind == "auto_banned_user":
            automatic.append(action["id"])
        elif kind == "vote_confirmed_spam":
            human.append(action["id"])
            if trigger in session_ids and trigger == sid:
                spam.append(action["id"])
            else:
                propagated.append(action["id"])
        elif kind in HUMAN_BANS:
            human.append(action["id"])
            if sid in session_ids and related_ids == [sid]:
                spam.append(action["id"])
            else:
                ambiguous.append(action["id"])
        elif kind in HUMAN_RELEASES and sid in session_ids:
            ham.append(action["id"])
    candidate = "spam" if spam else ("ham" if ham else None)
    if spam and ham:
        scope, candidate = "conflicting_human_events", None
    elif spam:
        scope = "direct_human_event_candidate"
    elif ham:
        scope = "direct_human_release"
    elif propagated:
        scope = "human_vote_propagated"
    elif ambiguous:
        scope = "admin_primary_unrecoverable"
    elif automatic:
        scope = "automatic_closure_not_human"
    else:
        scope = "no_verified_human_event"
    return {
        "scope": scope,
        "candidate_message_label": candidate,
        "human_account_label": "spam" if human else None,
        "human_account_event_ids": human,
        "direct_spam_event_ids": spam,
        "direct_ham_event_ids": ham,
        "propagated_vote_event_ids": propagated,
        "ambiguous_admin_event_ids": ambiguous,
        "automatic_event_ids": automatic,
        "related_session_ids": sorted(related),
        "events": evidence,
    }


def recover_context(detail: str | None) -> dict:
    """Read only input sections, never model output or disposition prose."""
    text, result = detail or "", {}
    p = re.search(r"用户资料：(.*?) @([^\n]*)\nBio：(.*?)(?=\n(?:个人频道：|触发：))", text, re.S)
    if p:
        result["sender_profile_at_moderation"] = {
            "display_name": html.unescape(p[1]),
            "username": None if p[2] == "-" else html.unescape(p[2]),
            "bio": None if p[3] == "-" else html.unescape(p[3]),
        }
    og = re.search(r"\nOG：(.*?)(?=\n(?:LLM：|信号：|正文：)|$)", text, re.S)
    if og:
        result["og_preview_at_moderation"] = {"rendered_title_description": html.unescape(og[1])}
    channel = re.search(r"\n个人频道：(.*?)(?=触发：)", text, re.S)
    if channel:
        result["personal_chat_at_moderation"] = {
            "rendered_bounded_snapshot": html.unescape(channel[1]).rstrip()
        }
    return result


def original_status(sample: dict, actions: list[dict]) -> str:
    original = sample.get("original_llm") or {}
    if original.get("status") in {"ok", "failed", "disabled"}:
        return original["status"]
    # Later edited-message records are different model inputs. They cannot repair
    # the status of an older frozen cache row with the same Telegram message ID.
    reason = (sample.get("original_action") or {}).get("reason")
    return "not_called_local_bypass" if reason in NO_LLM_REASONS else "missing_call_telemetry"


def context_available(context: dict) -> bool:
    profile = context.get("sender_profile_at_moderation") or {}
    return bool(
        profile.get("bio")
        or context.get("og_preview_at_moderation")
        or context.get("personal_chat_at_moderation")
    )


def stratify(
    dataset: list[dict],
    predictions: list[dict],
    mapping: dict,
    annotations: list[dict] | None = None,
) -> list[dict]:
    pp = {x["sample_id"]: x for x in predictions}
    reviews = {x["sample_id"]: x for x in annotations or []}
    actions, sessions = index_mapping(mapping)
    rows = []
    for sample in dataset:
        key, sample_id = (sample["chat_id"], sample["message_id"]), sample["sample_id"]
        aa, ss = actions.get(key, []), sessions.get(key, [])
        provenance, prediction = classify_events(aa, ss), pp.get(sample_id, {})
        cached_context = sample.get("historical_context") or {}
        recovered, detail_sources, body_sources = dict(cached_context), [], []
        for session in ss:
            detail = session.get("detail_text")
            if detail:
                detail_sources.append(
                    {"session_id": session["id"], "created_at": session["created_at"]}
                )
                recovered.update(recover_context(detail))
                match = re.search(r"正文：\s*\n<blockquote>(.*?)</blockquote>", detail, re.S)
                if match:
                    body = html.unescape(match[1])
                    body_sources.append("" if body == "(empty)" else body)
        raw = sample.get("raw_text", "")
        body_complete = len(raw) < 500 and all(body == raw for body in body_sources)
        review = reviews.get(sample_id)
        if (
            review
            and review.get("raw_text_sha256") != hashlib.sha256(raw.encode("utf-8")).hexdigest()
        ):
            raise ValueError(f"Annotation text hash mismatch: {sample_id}")
        if not raw:
            tier = (
                "body_empty_context_recoverable"
                if context_available(recovered)
                else "body_empty_context_unrecoverable"
            )
        elif not body_complete:
            tier = "body_partial_or_revision_ambiguous"
        elif review and review.get("assessment") in {"independent_spam", "no_ad_intent"}:
            tier = "body_complete_independent"
        elif review and review.get("assessment") in {"context_spam", "context_required"}:
            tier = "body_complete_context_required"
        elif review and review.get("assessment") == "behavior_required":
            tier = "body_complete_behavior_required"
        else:
            tier = "body_nonempty_semantic_review_pending"
        candidate = provenance["candidate_message_label"]
        enough = bool(body_complete and raw and review and review.get("evidence_sufficient"))
        if tier == "body_complete_context_required":
            enough = enough and context_available(cached_context)
        strict = candidate if enough and (review or {}).get("message_label") == candidate else None
        category, noul = prediction.get("category_choice"), prediction.get("is_spam_noul")
        valid = (
            prediction.get("status") == "ok"
            and isinstance(noul, (int, float))
            and not isinstance(noul, bool)
            and 0 <= noul <= 1
        )
        rows.append(
            {
                **sample,
                "audit_version": AUDIT_VERSION,
                "reference_original": sample.get("reference"),
                "reference": {
                    "label": strict,
                    "source": "verified_event_and_evidence_review"
                    if strict
                    else "pending_or_insufficient_message_evidence",
                    "human_confirmed": strict is not None,
                },
                "label_provenance": provenance,
                "user_reference": {
                    "label": provenance["human_account_label"],
                    "source": "explicit_human_account_event"
                    if provenance["human_account_label"]
                    else None,
                },
                "evidence_tier": tier,
                "body_integrity": "consistent_below_snapshot_limit"
                if body_complete
                else "truncation_or_revision_possible",
                "recovered_body_for_audit": max([raw, *body_sources], key=len),
                "recovered_context_for_audit": recovered,
                "detail_sources": detail_sources,
                "semantic_review": review,
                "message_metric_eligible": strict is not None and valid,
                "exclusion_reason": None
                if strict is not None and valid
                else "no_verified_message_label_or_insufficient_cached_input",
                "original_llm_status_audited": original_status(sample, aa),
                "original_input_context_identical": None,
                "input_comparison_note": "Original request not retained; cached Jev input frozen; recovered fields were not sent.",
                "jev_prediction_valid": valid,
                "jev_noul": noul if valid else None,
                "jev_label": ("spam" if noul >= 0.5 else "ham") if valid else None,
                "jev_category": category,
                "jev_category_confidence": prediction.get("category_confidence"),
                "category_noul_disagreement": bool(
                    valid and noul < 0.5 and category not in {None, "benign", "unknown"}
                ),
            }
        )
    return rows


def metrics(rows: list[dict], *, model: str = "jev", label_field: str = "reference") -> dict:
    tp = fp = fn = tn = 0
    for row in rows:
        label, predicted = (row.get(label_field) or {}).get("label"), row.get("jev_label")
        if model == "original":
            original = row.get("original_llm") or {}
            predicted = (
                ("spam" if original["is_spam"] else "ham")
                if original.get("status") == "ok" and isinstance(original.get("is_spam"), bool)
                else None
            )
        if label not in {"spam", "ham"} or predicted not in {"spam", "ham"}:
            continue
        tp += label == "spam" and predicted == "spam"
        fp += label == "ham" and predicted == "spam"
        fn += label == "spam" and predicted == "ham"
        tn += label == "ham" and predicted == "ham"
    return {
        "n": tp + fp + fn + tn,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "false_positive_rate": fp / (fp + tn) if fp + tn else None,
        "miss_rate": fn / (tp + fn) if tp + fn else None,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
    }


def paired_rows(rows: list[dict]) -> list[dict]:
    return [
        r
        for r in rows
        if r["jev_prediction_valid"]
        and (r.get("original_llm") or {}).get("status") == "ok"
        and isinstance(r["original_llm"].get("is_spam"), bool)
    ]


def user_comparison(rows: list[dict]) -> dict:
    """Identical message windows before the first human account event, positives only."""
    events = {}
    for row in rows:
        if row["user_reference"]["label"] != "spam" or row.get("user_id") is None:
            continue
        times = [
            e["created_at"]
            for e in row["label_provenance"]["events"]
            if e["action"] in HUMAN_BANS | {"vote_confirmed_spam"}
        ]
        key = (row["chat_id"], row["user_id"])
        if times:
            events[key] = min(events.get(key, min(times)), min(times))
    available, common = defaultdict(list), defaultdict(list)
    for row in rows:
        key = (row["chat_id"], row.get("user_id"))
        if key not in events or row.get("created_at", 0) > events[key]:
            continue
        available[key].append(row)
        if paired_rows([row]):
            common[key].append(row)
    users = []
    for key in sorted(events):
        rr = common.get(key, [])
        users.append(
            {
                "chat_id": key[0],
                "user_id": key[1],
                "human_positive_event_time": events[key],
                "available_messages": len(available.get(key, [])),
                "common_messages": len(rr),
                "jev_detected": any(r["jev_label"] == "spam" for r in rr) if rr else None,
                "original_detected": any(r["original_llm"]["is_spam"] for r in rr) if rr else None,
                "sample_ids": [r["sample_id"] for r in rr],
            }
        )
    paired = [u for u in users if u["common_messages"]]
    return {
        "human_positive_users": len(users),
        "common_users": len(paired),
        "common_messages": sum(u["common_messages"] for u in paired),
        "jev_detected_users": sum(u["jev_detected"] for u in paired),
        "original_detected_users": sum(u["original_detected"] for u in paired),
        "no_common_messages": len(users) - len(paired),
        "users": users,
    }


def audit_sample(
    dataset: list[dict],
    predictions: list[dict],
    related: dict,
    rows: list[dict],
    seed: int,
    size: int,
) -> list[dict]:
    pp, rr = {p["sample_id"]: p for p in predictions}, {r["sample_id"]: r for r in rows}
    misses = [
        r
        for r in dataset
        if r["reference"].get("label") == "spam"
        and pp.get(r["sample_id"], {}).get("status") == "ok"
        and pp[r["sample_id"]].get("is_spam_noul", 1) < 0.5
    ]
    chosen = random.Random(seed).sample(misses, min(size, len(misses)))
    aa, ss = index_mapping(related)
    result = []
    for sample in chosen:
        row = rr[sample["sample_id"]]
        key, ids = (
            (row["chat_id"], row["message_id"]),
            row["label_provenance"]["related_session_ids"],
        )
        result.append(
            {
                **row,
                "exact_actions": aa.get(key, []),
                "exact_sessions": ss.get(key, []),
                "related_sessions": [s for s in related.get("sessions", []) if s["id"] in ids],
                "vote_records": [v for v in related.get("votes", []) if v["session_id"] in ids],
                "current_profile_excluded_from_historical_input": next(
                    (p for p in related.get("profiles", []) if p["user_id"] == row.get("user_id")),
                    None,
                ),
            }
        )
    return result


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    k: json.dumps(row.get(k), ensure_ascii=False)
                    if isinstance(row.get(k), (dict, list))
                    else row.get(k)
                    for k in fields
                }
            )


def write_report(rows: list[dict], sample: list[dict], out_dir: Path, manifest: dict) -> dict:
    old = [r for r in rows if r["reference_original"].get("label") == "spam"]
    old_common = paired_rows(old)
    eligible = [r for r in rows if r["message_metric_eligible"]]
    common = paired_rows(eligible)
    scopes = Counter(r["label_provenance"]["scope"] for r in old)
    user = user_comparison(rows)
    stats = {
        "manifest": manifest,
        "legacy_positive_counts": {
            "all": len(old),
            "empty": sum(not r["raw_text"] for r in old),
            "empty_jev_negative": sum(not r["raw_text"] and r["jev_label"] == "ham" for r in old),
            "nonempty": sum(bool(r["raw_text"]) for r in old),
            "nonempty_jev_positive": sum(
                bool(r["raw_text"]) and r["jev_label"] == "spam" for r in old
            ),
        },
        "legacy_positive_scopes": scopes,
        "legacy_positive_input_tiers": Counter(r["evidence_tier"] for r in old),
        "legacy_same_positive_set": {
            "jev": metrics(old_common, label_field="reference_original"),
            "original": metrics(old_common, model="original", label_field="reference_original"),
        },
        "scope_all": Counter(r["label_provenance"]["scope"] for r in rows),
        "evidence_tiers": Counter(r["evidence_tier"] for r in rows),
        "original_llm_status_all": Counter(r["original_llm_status_audited"] for r in rows),
        "original_llm_status_legacy_positive": Counter(
            r["original_llm_status_audited"] for r in old
        ),
        "direct_human_event_candidates": sum(
            r["label_provenance"]["candidate_message_label"] is not None for r in rows
        ),
        "strict_reviewed": {
            "jev": metrics(eligible),
            "paired_jev": metrics(common),
            "paired_original": metrics(common, model="original"),
        },
        "paired_message_subsets": {
            tier: {
                "jev": metrics([r for r in common if r["evidence_tier"] == tier]),
                "original": metrics(
                    [r for r in common if r["evidence_tier"] == tier], model="original"
                ),
            }
            for tier in sorted({r["evidence_tier"] for r in rows})
        },
        "sample_scope": Counter(r["label_provenance"]["scope"] for r in sample),
        "sample_assessment": Counter(
            (r.get("semantic_review") or {}).get("assessment", "not_reviewed") for r in sample
        ),
        "sample_empty": sum(not r["raw_text"] for r in sample),
        "category_noul_disagreement_all": sum(r["category_noul_disagreement"] for r in rows),
        "category_noul_disagreement_legacy_positive_negative": Counter(
            r["jev_category"] for r in old if r["category_noul_disagreement"]
        ),
        "user_level": {k: v for k, v in user.items() if k != "users"},
        "new_annotation_candidates": manifest.get("normal_candidate_count", 0),
    }
    write_jsonl(out_dir / "user_comparison.jsonl", user["users"])
    (out_dir / "metrics.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    lines = [
        "# 第一轮 Jev 标签审计与分层报告",
        "",
        "本报告替代第一轮模型优劣结论。本轮 API 调用数为 0，原 dataset/predictions 冻结；新增审计输出，不修改生产库、规则和阈值。",
        "## 标签生成问题",
        "",
        "第一轮把 vote_sessions.status 当作单条消息人工真值存在错误。用户级处置会传播到其他会话；auto_banned_user 也会写 confirmed_spam。空消息加可疑 Bio 与主动发布广告是不同任务，原 Recall/Precision/F1 不可用于选型。",
        f"- 原 726 条‘人工广告’实际事件分层：`{dump(scopes)}`。",
        f"- 原数据复现：`{dump(stats['legacy_positive_counts'])}`。非空 410 条检出率仍只是旧标签诊断。",
        "- trigger_vote_session_id 可识别投票主消息。admin 的 related_vote_session_ids 排序不代表主会话；多会话主对象无法恢复。单会话管理员处置也可能只基于资料或行为，仍须消息证据复核。",
        "## 相同样本对比（旧标签诊断，非真实 Recall）",
        "",
        f"同一 {len(old_common)} 条原 LLM 成功的旧阳性样本：Jev 检出 {metrics(old_common, label_field='reference_original')['tp']}，原 LLM 检出 {metrics(old_common, model='original', label_field='reference_original')['tp']}。分母、标签、消息 ID 完全相同。",
        "原 LLM 的完整请求没有留档，输入上下文是否相同未知；Jev 只收到可恢复字段。不能把输入差异归因于模型能力。",
        "## 证据分层与新消息级标签",
        "",
        f"全 4,720 条证据层：`{dump(stats['evidence_tiers'])}`。",
        f"原726条旧阳性的输入分层：`{dump(stats['legacy_positive_input_tiers'])}`。",
        f"直接人审事件候选 {stats['direct_human_event_candidates']} 条不等于可信消息真值。只有直接人审事件、逐条语义复核一致、缓存输入充分、有效预测同时满足才计入消息级指标；其他保留 unknown，不算 FN。",
        "正文 <500 且与详情一致只标记 consistent_below_snapshot_limit；500/800 边界、详情更长或版本冲突保留 partial/revision。未复核正文保留 pending，不凭关键词或长度认定独立可判断。空正文保留资料/OG任务，不直接删除。",
        f"可信消息证据复核子集：`{dump(stats['strict_reviewed'])}`。",
        "子集来自定向复核 50 条旧漏判和 7 条已有正常消息，有选择偏差；这些诊断指标不可外推总体性能，需新的独立人审集。",
        "## 原 LLM 调用状态",
        "",
        f"全体：`{dump(stats['original_llm_status_all'])}`；旧阳性：`{dump(stats['original_llm_status_legacy_positive'])}`。",
        "failed=明确失败；disabled=明确停用；not_called_local_bypass=有本地硬路径证据；missing_call_telemetry=缺失遥测，不能推断未调用/失败。均不当作模型判断错误。",
        "## 独立用户级描述性覆盖率",
        "",
        f"`{dump(stats['user_level'])}`。",
        "单位为 (chat_id,user_id)，真值仅明确真人账户处置。窗口截止首次人审事件；两模型只用相同的原 LLM 成功且 Jev 有效的历史消息，任一消息阳性算覆盖。无共同消息用户单列。",
        "这是消息分数聚合，未运行用户级模型。单条消息放行不等于账号正常，因此用户级阴性、误杀率、Precision/F1 不可估计；不与消息级 Recall 混合。",
        "## 固定随机 50 条核查",
        "",
        f"seed={manifest['seed']}，从原 480 条漏判均匀无放回抽取；逐条检查原文、历史详情、会话、原始投票与最终事件，空正文 {stats['sample_empty']} 条。审计者 Codex，属于证据复核，不冒充新增真人金标。",
        f"事件：`{dump(stats['sample_scope'])}`；语义：`{dump(stats['sample_assessment'])}`。",
        "逐条结果与事件 ID 见 miss_audit.md，完整附件见 miss_audit_sample.jsonl。当前 user_profiles 行只作附件，即使 updated_at 接近发送时间也不充当历史输入。",
        "## Choice / Noul 分歧",
        "",
        f"Noul<0.5 但 Choice 为广告类共 {stats['category_noul_disagreement_all']} 条。旧阳性且 Noul 阴性类别：`{dump(stats['category_noul_disagreement_legacy_positive_negative'])}`。",
        "第一轮 category 问法是选择广告样类别，两问题语义未完全对齐，独立回答也不是联合概率。Choice confidence 或 traffic_diversion 不能覆盖 Noul，也不能生成真值。本轮保留原输出，不改 Questions、不补 API 调用。",
        "## 正常消息与真人标注集",
        "",
        "原 7 条已核查事件：3 条 @zaoanyun、2 条支付宝网址讨论、2 条‘官网’，来自 5 个用户、1 个群，共 4 个唯一正文。无法覆盖技术外链、推荐、引用广告、金融讨论等误杀风险。0/7 不是可靠误杀率。即使独立同分布，零事件的单侧95%上界仍约34.8%；这里的场景重复进一步破坏独立假设。",
        "normal_annotation_queue.jsonl 保存 7 条已确认 ham 与抽样中的正常正文候选；候选均为 pending，Codex 判断没有提升为真人真值。annotation_queue.jsonl 提供全部733条供两名真人独立标注，隐藏模型输出和最终处置。",
        f"本次额外只读导出 edit_watch_messages 的非空短期快照 {manifest.get('normal_candidate_count', 0)} 条，来自 {manifest.get('normal_candidate_users', 0)} 个用户、{manifest.get('normal_candidate_chats', 0)} 个群。它们仅为原始群聊候选，全部pending；其中有可疑招揽，不能把allow/review或‘近期仍留存’当作ham。当前窗口候选与第一轮历史集时间段不同，未调用Jev、没有预测，不纳入模型指标。",
        "当前基线仅覆盖新/低信誉用户，24小时到期，且可被后续无害编辑覆盖；没有完整媒体、回复链、Bio/OG、个人频道。该来源适合补充候选但有选择偏差，不能代替真实正常群史的随机抽样。项目目录只读盘点没有发现JSON/JSONL群史或正文日志文件。",
        "补充来源为 Telegram Desktop JSON 导出或已有审计机器人原始日志；没有正文的 action_log/message_observations 无法反推正文。历史 Bio/OG 不可得时保持缺失；当前资料不回填历史。",
        "标注字段：message_label/profile_or_account_label 分开，spam/ham/unknown、input_complete、context_required、证据跨度/原因。两人独立，分歧交第三人；按 chat/user/time 分组去重划分评测集。",
        "目标配额：普通聊天200、技术外链100、产品推荐50、可疑Bio+正常正文50、引用/举报/玩笑50、兼职/金融/加密讨论50；空正文+可恢复资料50另作资料任务。配额只用于覆盖，不代表群聊流行率。",
        "## 回放与复现",
        "",
        "第一轮‘完整系统回放’实为 noul 的70/85阈值映射，未恢复历史规则、信誉、指纹或权限，撤回完整系统回放称谓；缺状态的样本明确不可回放。",
        f"输入文件与SHA-256：`{dump(manifest['inputs'])}`。抽样不依赖当前日期。",
    ]
    (out_dir / "label_audit_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cache-only label audit: zero network calls.")
    for name in ("dataset", "predictions", "mapping", "sample-related", "annotations"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=Path("jev-audit-output"))
    parser.add_argument("--seed", type=int, default=20260921)
    parser.add_argument("--audit-size", type=int, default=50)
    parser.add_argument("--normal-candidates", type=Path)
    args = parser.parse_args(argv)
    dataset, predictions = read_jsonl(args.dataset), read_jsonl(args.predictions)
    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    related = json.loads(args.sample_related.read_text(encoding="utf-8"))
    rows = stratify(dataset, predictions, mapping, read_jsonl(args.annotations))
    sample = audit_sample(dataset, predictions, related, rows, args.seed, args.audit_size)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "audit_version": AUDIT_VERSION,
        "api_calls": 0,
        "seed": args.seed,
        "inputs": {
            name: {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for name, path in [
                ("dataset", args.dataset),
                ("predictions", args.predictions),
                ("mapping", args.mapping),
                ("related", args.sample_related),
                ("annotations", args.annotations),
            ]
        },
    }
    fresh = []
    if args.normal_candidates:
        candidate_data = json.loads(args.normal_candidates.read_text(encoding="utf-8"))
        manifest["inputs"]["normal_candidates"] = {
            "path": str(args.normal_candidates),
            "sha256": hashlib.sha256(args.normal_candidates.read_bytes()).hexdigest(),
        }
        for candidate in candidate_data["normal_candidates"]:
            fresh.append(
                {
                    "sample_id": f"{candidate['chat_id']}:{candidate['message_id']}",
                    "raw_text": candidate["text_snapshot"],
                    "user_id": candidate["user_id"],
                    "chat_id": candidate["chat_id"],
                    "snapshot_at": candidate["created_at"],
                    "expires_at": candidate["expires_at"],
                    "source": "edit_watch_messages_baseline",
                    "verified_message_label": None,
                    "annotations": [],
                    "status": "candidate_pending_human_annotation",
                    "message_label": None,
                    "profile_or_account_label": None,
                    "context_required": None,
                    "input_complete": None,
                    "evidence_spans": [],
                    "reviewer_id": None,
                    "historical_context": {
                        "missing": ["profile", "og", "personal_chat", "reply_chain", "media"]
                    },
                    "temporal_cohort": "current_retention_window_not_first_experiment",
                }
            )
    manifest["normal_candidate_count"] = len(fresh)
    manifest["normal_candidate_users"] = len({r["user_id"] for r in fresh})
    manifest["normal_candidate_chats"] = len({r["chat_id"] for r in fresh})
    write_jsonl(args.out_dir / "new_chat_annotation_queue.jsonl", fresh)
    write_jsonl(args.out_dir / "stratified_dataset.jsonl", rows)
    write_jsonl(args.out_dir / "miss_audit_sample.jsonl", sample)
    queue = [
        {
            "sample_id": r["sample_id"],
            "raw_text": r["raw_text"],
            "historical_context": r["historical_context"],
            "body_integrity": r["body_integrity"],
            "annotations": [],
            "message_label": None,
            "profile_or_account_label": None,
            "input_complete": None,
            "context_required": None,
            "evidence_spans": [],
            "status": "awaiting_two_independent_human_reviews",
        }
        for r in rows
        if r["reference_original"].get("human_confirmed")
    ]
    write_jsonl(args.out_dir / "annotation_queue.jsonl", queue)
    write_jsonl(
        args.out_dir / "normal_annotation_queue.jsonl",
        [
            {
                "sample_id": r["sample_id"],
                "raw_text": r["raw_text"],
                "historical_context": r["historical_context"],
                "verified_message_label": r["reference"]["label"]
                if r["reference_original"].get("label") == "ham"
                else None,
                "status": "existing_human_release"
                if r["reference_original"].get("label") == "ham"
                else "candidate_pending_human_annotation",
            }
            for r in rows
            if r["reference_original"].get("label") == "ham"
            or (r.get("semantic_review") or {}).get("assessment") == "no_ad_intent"
        ]
        + fresh,
    )
    write_csv(
        args.out_dir / "comparison.csv",
        rows,
        [
            "sample_id",
            "raw_text",
            "evidence_tier",
            "body_integrity",
            "reference",
            "reference_original",
            "user_reference",
            "semantic_review",
            "label_provenance",
            "original_llm_status_audited",
            "original_llm",
            "original_action",
            "jev_noul",
            "jev_label",
            "jev_category",
            "jev_category_confidence",
            "category_noul_disagreement",
            "message_metric_eligible",
            "exclusion_reason",
            "original_input_context_identical",
        ],
    )
    stats = write_report(rows, sample, args.out_dir, manifest)
    lines = ["# 固定随机50条证据审计", "", "审计者Codex；seed=20260921；非新增真人金标。", ""]
    for index, row in enumerate(sample, 1):
        review = row.get("semantic_review") or {}
        vote_summary = [
            {
                "session_id": s["id"],
                "status": s["status"],
                "session_spam_votes": s["spam_votes"],
                "session_ham_votes": s["ham_votes"],
                "raw_vote_records": len(
                    [v for v in row["vote_records"] if v["session_id"] == s["id"]]
                ),
                "self_vote_records": len(
                    [
                        v
                        for v in row["vote_records"]
                        if v["session_id"] == s["id"] and v["voter_user_id"] == row.get("user_id")
                    ]
                ),
            }
            for s in row["exact_sessions"]
        ]
        lines += [
            f"## {index}. {row['sample_id']}",
            "",
            f"正文：{json.dumps(row['raw_text'], ensure_ascii=False)}",
            "",
            f"- 事件来源：{row['label_provenance']['scope']}；相关会话：{row['label_provenance']['related_session_ids']}",
            f"- 事件证据：{json.dumps(row['label_provenance']['events'], ensure_ascii=False)}",
            f"- 历史资料快照：{json.dumps((row.get('historical_context') or {}).get('sender_profile_at_moderation'), ensure_ascii=False)}",
            f"- 会话与原始投票核对：{json.dumps(vote_summary, ensure_ascii=False)}",
            f"- 输入层：{row['evidence_tier']}；Noul={row['jev_noul']}；Choice={row['jev_category']}",
            f"- 复核：{review.get('assessment', 'pending')}；{review.get('rationale', '')}",
            f"- 可计消息指标：{row['message_metric_eligible']}；消息级人审真值：{row['reference']['label']}",
            "",
        ]
    (args.out_dir / "miss_audit.md").write_text("\n".join(lines), encoding="utf-8")
    print(
        json.dumps(
            {
                "rows": len(rows),
                "sample": len(sample),
                "api_calls": 0,
                "legacy_scopes": stats["legacy_positive_scopes"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
