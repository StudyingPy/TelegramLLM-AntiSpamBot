"""Read-only paired reporting and blinded human annotation for live observations."""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path

from .jev import digest
from .jev_store import connect_readonly, import_annotations, read_observations


def gold_labels(annotations: list[dict]) -> dict[str, str]:
    grouped = defaultdict(list)
    for annotation in annotations:
        if annotation["task"] == "message":
            grouped[annotation["observation_id"]].append(annotation)
    result = {}
    for oid, values in grouped.items():
        labels = {v["label"] for v in values}
        if (
            len({v["reviewer_id"] for v in values}) >= 2
            and len(labels) == 1
            and next(iter(labels)) in {"spam", "ham"}
            and all(v["sufficient"] for v in values)
        ):
            result[oid] = next(iter(labels))
    return result


def baseline_label(row: dict) -> str | None:
    baseline = row["baseline"]
    j = baseline.get("judgement") or {}
    if baseline.get("status") != "ok" or type(j.get("is_spam")) is not bool:
        return None
    return "spam" if j["is_spam"] else "ham"


def jev_label(row: dict) -> str | None:
    j = row["jev"]
    if j.get("status") != "ok" or type(j.get("noul")) not in {int, float}:
        return None
    return "spam" if j["noul"] >= 0.5 else "ham"


def message_input_sufficient(row: dict) -> bool:
    if row["update_type"] == "new_chat_member" or row.get("text_truncated_for_models"):
        return False
    state = row["state"]
    if state.get("is_empty_or_punctuation") and not state.get("og_preview"):
        return False
    return bool(state.get("raw_text") or state.get("og_preview"))


def metrics(pairs: list[tuple[str, str]]) -> dict:
    tp = sum(g == "spam" and p == "spam" for g, p in pairs)
    fp = sum(g == "ham" and p == "spam" for g, p in pairs)
    fn = sum(g == "spam" and p == "ham" for g, p in pairs)
    tn = sum(g == "ham" and p == "ham" for g, p in pairs)
    return {
        "n": len(pairs),
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


def paired(row: dict) -> bool:
    return bool(
        jev_label(row)
        and baseline_label(row)
        and row["baseline"].get("input_hash") == row["payload_hash"]
        and row["stage"] == "model_requested"
    )


def cohort(row: dict) -> dict:
    baseline = row["baseline"].get("metadata") or {}
    return {
        "requested_jev_model": row["requested_model"],
        "resolved_jev_model": row["jev"].get("model"),
        "questions_hash": row["questions_hash"],
        "system_prompt_hash": row["system_prompt_hash"],
        "baseline_requested_model": baseline.get("requested_model")
        or row.get("original_models_requested"),
        "baseline_resolved_model": baseline.get("resolved_model"),
        "baseline_provider_index": baseline.get("provider_index"),
    }


def summarize(rows: list[dict], annotations: list[dict], input_price: float | None = None) -> dict:
    labels = gold_labels(annotations)
    common = [r for r in rows if paired(r)]
    eligible = [r for r in common if r["id"] in labels and message_input_sufficient(r)]
    # Same exact observation IDs for both models, including identical input hashes.
    labelled_ids = [r["id"] for r in eligible]
    jm = metrics([(labels[r["id"]], jev_label(r)) for r in eligible])
    bm = metrics([(labels[r["id"]], baseline_label(r)) for r in eligible])
    cohorts = {}
    for row in common:
        identity = cohort(row)
        key = digest(identity)
        group = cohorts.setdefault(key, {"identity": identity, "rows": []})
        group["rows"].append(row)
    grouped_metrics = []
    for key, group in sorted(cohorts.items()):
        samples = group["rows"]
        gold = [r for r in samples if r["id"] in labels and message_input_sufficient(r)]
        grouped_metrics.append(
            {
                "cohort_id": key,
                **group["identity"],
                "common_successful_observations": len(samples),
                "paired_labelled_ids": [r["id"] for r in gold],
                "jev": metrics([(labels[r["id"]], jev_label(r)) for r in gold]),
                "baseline": metrics([(labels[r["id"]], baseline_label(r)) for r in gold]),
                "model_agreement_not_accuracy": sum(
                    jev_label(r) == baseline_label(r) for r in samples
                )
                / len(samples),
            }
        )
    # Do not present one accuracy figure across different models/policies/providers.
    if len(cohorts) > 1:
        jm = bm = None
    attempts = [r["jev"] for r in rows if r["jev"].get("api_attempted")]
    successful = [r for r in attempts if r.get("status") == "ok"]
    latencies = sorted(
        r["elapsed_ms"] for r in successful if type(r.get("elapsed_ms")) in {int, float}
    )
    tokens = [r.get("usage", {}).get("input_tokens") for r in successful]
    tokens = [n for n in tokens if type(n) is int]
    output_tokens = [r.get("usage", {}).get("output_tokens") for r in successful]
    output_tokens = [n for n in output_tokens if type(n) is int]

    def percentile(p):
        return latencies[max(0, math.ceil(len(latencies) * p) - 1)] if latencies else None

    bins = Counter()
    confidence_bins = Counter()
    for row in rows:
        j = row["jev"]
        if j.get("status") == "ok":
            p, c = j["noul"], j["category_confidence"]
            bins[
                "<0.5"
                if p < 0.5
                else "0.5-0.7"
                if p < 0.7
                else "0.7-0.85"
                if p < 0.85
                else ">=0.85"
            ] += 1
            confidence_bins["<0.5" if c < 0.5 else "0.5-0.85" if c < 0.85 else ">=0.85"] += 1
    return {
        "observations": len(rows),
        "stages": Counter(r["stage"] for r in rows),
        "jev_status": Counter(r["jev"].get("status") for r in rows),
        "jev_skips": Counter(
            r["jev"].get("reason") for r in rows if r["jev"].get("status") == "skipped"
        ),
        "baseline_status": Counter(r["baseline"].get("status") for r in rows),
        "common_successful_observations": len(common),
        "model_agreement_not_accuracy": sum(jev_label(r) == baseline_label(r) for r in common)
        / len(common)
        if common
        else None,
        "human_consensus_labels": len(labels),
        "paired_labelled_ids": labelled_ids,
        "paired_message_jev": jm,
        "paired_message_baseline": bm,
        "paired_cohorts": grouped_metrics,
        "mixed_cohort_accuracy_suppressed": len(cohorts) > 1,
        "annotation_tasks": Counter(a["task"] for a in annotations),
        "user_level_accuracy": None,
        "user_level_note": "No account classifier or account gold cohort; do not derive accounts from message release/ban.",
        "excluded_from_message_metrics": len(rows) - len(eligible),
        "noul_probability_bins": bins,
        "choice_confidence_bins": confidence_bins,
        "api_attempts": len(attempts),
        "api_failures": len(attempts) - len(successful),
        "api_failure_rate": (len(attempts) - len(successful)) / len(attempts) if attempts else None,
        "cache_hits": sum(bool(r["jev"].get("cache_hit")) for r in rows),
        "latency_p50_ms": percentile(0.5),
        "latency_p95_ms": percentile(0.95),
        "input_tokens": sum(tokens),
        "mean_input_tokens": sum(tokens) / len(tokens) if tokens else None,
        "output_tokens": sum(output_tokens),
        "mean_output_tokens": sum(output_tokens) / len(output_tokens) if output_tokens else None,
        "estimated_input_cost_usd": sum(tokens) * input_price / 1_000_000
        if input_price is not None
        else None,
        "input_price_usd_per_million": input_price,
        "requested_models": dict(Counter(r["requested_model"] for r in rows)),
        "resolved_models": dict(Counter(r["jev"].get("model") for r in rows if jev_label(r))),
        "questions_hashes": sorted({r["questions_hash"] for r in rows}),
        "system_prompt_hashes": sorted({r["system_prompt_hash"] for r in rows}),
    }


def moderation_events(path: Path, rows: list[dict]) -> dict[str, list[dict]]:
    """Fetch events only, never infer gold from the outcome or propagated status."""
    conn = connect_readonly(path)
    result = {}
    try:
        for row in rows:
            final = row.get("final") or {}
            sid = final.get("vote_session_id")
            if sid is None:
                result[row["id"]] = []
                continue
            events = conn.execute(
                "SELECT id, action, reason, metadata_json, created_at FROM action_log "
                "WHERE chat_id=? AND message_id=? AND created_at>=? ORDER BY id",
                (row["chat_id"], row["message_id"], int(row["created_at"])),
            ).fetchall()
            packets = []
            for e in events:
                m = json.loads(e["metadata_json"] or "{}")
                if m.get("vote_session_id") != sid:
                    continue
                human = e["action"] in {
                    "vote_confirmed_spam",
                    "admin_banned_user",
                    "catchup_banned_user",
                    "vote_released",
                    "admin_released_user",
                    "catchup_kept_released",
                }
                primary = m.get("primary_vote_session_id", m.get("trigger_vote_session_id"))
                packets.append(
                    {
                        "action_log_id": e["id"],
                        "action": e["action"],
                        "created_at": e["created_at"],
                        "vote_session_id": sid,
                        "primary_vote_session_id": primary,
                        "is_human_event": human,
                        "is_primary": primary == sid if primary is not None else None,
                        "gold_label": None,
                    }
                )
            result[row["id"]] = packets
    finally:
        conn.close()
    return result


def export_report(
    db: Path,
    out_dir: Path,
    *,
    sample_size: int = 50,
    seed: int = 0,
    moderation_db: Path | None = None,
    input_price: float | None = None,
) -> dict:
    rows, annotations = read_observations(db)
    stats = summarize(rows, annotations, input_price)
    out_dir.mkdir(parents=True, exist_ok=True)
    events = moderation_events(moderation_db, rows) if moderation_db else {}
    # Blinded selection is uniform over all observations, including model-normal
    # messages and local bypasses. No predictions or disposition in this packet.
    chosen = random.Random(seed).sample(rows, min(max(0, sample_size), len(rows)))
    blind = [
        {
            "observation_id": r["id"],
            "payload_hash": r["payload_hash"],
            "state": r["state"],
            "update_type": r["update_type"],
            "source_text_length": r["source_text_length"],
            "input_truncated": r["text_truncated_for_models"],
            "task": "profile" if r["update_type"] == "new_chat_member" else "message",
            "label": "unknown",
            "evidence_sufficient": False,
            "reviewer_id": "",
            "note": "",
        }
        for r in chosen
    ]
    for name, value in (
        ("dataset.jsonl", rows),
        ("annotations.jsonl", annotations),
        ("blind_review.jsonl", blind),
    ):
        (out_dir / name).write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in value), encoding="utf-8"
        )
    (out_dir / "events.json").write_text(
        json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "metrics.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (out_dir / "comparison.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "observation_id",
                "payload_hash",
                "stage",
                "jev_noul",
                "category",
                "category_confidence",
                "baseline_status",
                "baseline_label",
                "paired",
                "final_action",
                "human_message_label",
            ],
        )
        writer.writeheader()
        labels = gold_labels(annotations)
        for r in rows:
            writer.writerow(
                {
                    "observation_id": r["id"],
                    "payload_hash": r["payload_hash"],
                    "stage": r["stage"],
                    "jev_noul": r["jev"].get("noul"),
                    "category": r["jev"].get("category"),
                    "category_confidence": r["jev"].get("category_confidence"),
                    "baseline_status": r["baseline"].get("status"),
                    "baseline_label": baseline_label(r),
                    "paired": paired(r),
                    "final_action": r["final"].get("action"),
                    "human_message_label": labels.get(r["id"]),
                }
            )
    lines = [
        "# Jev 在线观察报告",
        "",
        "模式为shadow：原LLM决定实际审核。模型一致率不等于正确率，未标注样本不算正确或错误。",
        "只有两名独立标注者对同一输入确认一致且证据充分、两个模型均成功的消息才进入同分母指标。",
        "资料/账户/行为标签单列；空正文无OG、截断、入群资料检查不计消息指标。",
        "抽样来自已允许的群和模型审核环节；硬规则旁路是否覆盖取决于配置，不能归功于模型。",
        "人审事件仅作审核线索，不自动变成单消息真值。标注者通过blind_review.jsonl复核，隐藏模型结果。",
        "",
        f"```json\n{json.dumps(stats, ensure_ascii=False, indent=2)}\n```",
        "",
        "paired_cohorts按Questions、原提示词、Jev版本及原模型实际provider/model分别报告；混合版本时不提供汇总准确性指标。",
        "已标注集仍可能有选择偏差，正常样本少时不得声称低误杀。",
        "运行该报告不会发出模型请求；--moderation-db仅以只读连接关联事件。",
    ]
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cache-only Jev live report / explicit human labels import"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report")
    report.add_argument("--db", type=Path, required=True)
    report.add_argument("--out-dir", type=Path, default=Path("data/jev-live-export"))
    report.add_argument("--moderation-db", type=Path)
    report.add_argument("--sample-size", type=int, default=50)
    report.add_argument("--seed", type=int, default=20260921)
    report.add_argument("--input-usd-per-million", type=float)
    labels = sub.add_parser("labels")
    labels.add_argument("--db", type=Path, required=True)
    labels.add_argument("--file", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "labels":
        values = [
            json.loads(x) for x in args.file.read_text(encoding="utf-8").splitlines() if x.strip()
        ]
        print(json.dumps({"imported": import_annotations(args.db, values)}))
    else:
        if args.input_usd_per_million is not None and (
            not math.isfinite(args.input_usd_per_million) or args.input_usd_per_million < 0
        ):
            parser.error("input price must be finite and nonnegative")
        stats = export_report(
            args.db,
            args.out_dir,
            sample_size=args.sample_size,
            seed=args.seed,
            moderation_db=args.moderation_db,
            input_price=args.input_usd_per_million,
        )
        print(
            json.dumps(
                {"observations": stats["observations"], "out_dir": str(args.out_dir)},
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
