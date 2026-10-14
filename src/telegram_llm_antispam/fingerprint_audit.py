from __future__ import annotations

import asyncio
import html
import json
import logging

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .models import FingerprintAuditResult, LLMOutcomeStatus


logger = logging.getLogger(__name__)
REVIEW_PREFIX = "fp_review"


class FingerprintAuditor:
    def __init__(self, settings, db, judge):
        self.settings = settings
        self.db = db
        self.judge = judge

    async def run_once(self, bot):
        if not self.settings.fingerprint_audit_enabled:
            return False
        audit = getattr(self.judge, "audit_fingerprints", None)
        if audit is None:
            return False
        run_id = self.db.get_or_start_fingerprint_audit(
            max(1, self.settings.fingerprint_audit_interval_hours) * 3600,
            max(1, self.settings.fingerprint_audit_max_per_run),
        )
        if run_id is None:
            await self.notify_ready(bot)
            return False
        batch_size = max(1, min(10, self.settings.fingerprint_audit_batch_size))
        while ids := self.db.pending_fingerprint_audit_items(run_id, batch_size):
            inputs = []
            input_chars = 0
            for fid in ids:
                payload = self.db.fingerprint_audit_payload(fid)
                if payload is None or not payload["enabled"] or not payload["pattern_text"]:
                    self.db.record_fingerprint_audit_result(
                        run_id, payload or {"id": fid, "signature": "missing"},
                        FingerprintAuditResult(fid, "insufficient_evidence", 0, "无法恢复匹配原文"),
                    )
                else:
                    size = len(json.dumps(payload, ensure_ascii=False))
                    if inputs and input_chars + size > 8000:
                        break
                    inputs.append(payload)
                    input_chars += size
            if not inputs:
                continue
            try:
                outcome = await audit(inputs)
                if outcome.status != LLMOutcomeStatus.OK:
                    logger.warning("Fingerprint audit paused: %s", outcome.error or outcome.status)
                    await self.notify_ready(bot)
                    return False
                # Validate again at the job boundary, including custom/test adapters.
                from .llm import _parse_fingerprint_audit

                results = _parse_fingerprint_audit(
                    {"results": [
                        {"fingerprint_id": r.fingerprint_id, "decision": r.decision,
                         "confidence": r.confidence, "reason": r.reason}
                        for r in outcome.results
                    ]}, {item["id"] for item in inputs},
                )
            except Exception as exc:
                logger.warning("Fingerprint audit paused after invalid/failed batch: %s", exc)
                await self.notify_ready(bot)
                return False
            by_id = {r.fingerprint_id: r for r in results}
            for payload in inputs:
                self.db.record_fingerprint_audit_result(run_id, payload, by_id[payload["id"]])
            await asyncio.sleep(0)
        self.db.finish_fingerprint_audit(run_id)
        logger.info("Fingerprint audit run %s completed", run_id)
        await self.notify_ready(bot)
        return True

    async def notify_ready(self, bot):
        if not self.db.fingerprint_review_notification_due():
            return
        rows = self.db.pending_fingerprint_reviews(unnotified=True)
        if not rows:
            return
        text = f"指纹质量复核：新增 {len(rows)} 条待复核建议\n"
        text += "\n".join(
            f"• {html.escape(str(row['pattern_text'] or '未恢复原文')[:80])}："
            f"{html.escape(row['reason'][:100])}" for row in rows[:5]
        )
        text += "\n\n模型仅建议停用；确认后才停止匹配，可恢复。"
        try:
            me = await bot.get_me()
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="查看待复核", url=f"https://t.me/{me.username}?start={REVIEW_PREFIX}"
                )
            ]])
            await bot.send_message(self.settings.fingerprint_review_user_id, text,
                                   reply_markup=markup)
        except Exception as exc:
            logger.warning("Fingerprint review notification failed: %s", exc)
            return
        self.db.mark_fingerprint_reviews_notified([row["id"] for row in rows])
        logger.info("Fingerprint review digest sent to %s: %s candidates",
                    self.settings.fingerprint_review_user_id, len(rows))


async def sweep_fingerprints(settings, bot, db, judge):
    auditor = FingerprintAuditor(settings, db, judge)
    while True:
        try:
            await auditor.run_once(bot)
        except Exception as exc:
            logger.warning("Fingerprint maintenance failed: %s", exc)
        await asyncio.sleep(300)


def review_list(db, *, disabled=False, offset=0):
    rows = db.pending_fingerprint_reviews(disabled=disabled, offset=offset, limit=10)
    count = db.count_fingerprint_reviews(disabled=disabled)
    title = "已停用指纹" if disabled else "待复核指纹"
    lines = [f"{title}：{count} 条"]
    buttons = []
    for row in rows:
        name = str(row["pattern_text"] or "无法恢复原文")[:80]
        lines.append(f"• {html.escape(name)} / {row['confidence']:.0%}")
        buttons.append([InlineKeyboardButton(text=f"查看：{name[:30]}",
                                             callback_data=f"fp:detail:{row['id']}")])
    if offset + 10 < count:
        buttons.append([InlineKeyboardButton(
            text="下一页", callback_data=f"fp:list:{int(disabled)}:{offset + 10}"
        )])
    buttons.append([InlineKeyboardButton(
        text="待复核" if disabled else "已停用 / 恢复",
        callback_data=f"fp:list:{int(not disabled)}:0",
    )])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def review_detail(db, review_id):
    row = db.get_fingerprint_review(review_id)
    if row is None:
        return "复核记录不存在。", None
    fp = db.fingerprint_audit_payload(row["fingerprint_id"])
    if fp is None:
        return "指纹已被清理。", None
    text = (
        f"指纹复核 #{row['id']}\n"
        f"原文：<code>{html.escape(str(fp['pattern_text'] or '无法恢复')[:1000])}</code>\n"
        f"类型：{html.escape(fp['fingerprint_type'])} / 权重 {fp['weight']:.0f}\n"
        f"来源：{html.escape(fp['source'])} / 人工误判记录 {fp['false_positive_count']}\n"
        f"模型建议：{html.escape(row['decision'])} / {row['confidence']:.0%}\n"
        f"理由：{html.escape(row['reason'])}\n"
        f"状态：{html.escape(row['status'])}"
    )
    for sample in fp["samples"]:
        # Raw bounded text is escaped before rendering; samples cannot create buttons.
        body = html.escape(sample["text"][:400])
        text += f"\n样本（{sample['label']}）：<blockquote>{body}</blockquote>"
    actions = []
    if row["status"] == "pending":
        actions = [InlineKeyboardButton(text="停用", callback_data=f"fp:disable:{review_id}"),
                   InlineKeyboardButton(text="保留", callback_data=f"fp:keep:{review_id}")]
    elif row["status"] == "disabled":
        actions = [InlineKeyboardButton(text="恢复", callback_data=f"fp:restore:{review_id}")]
    keyboard = [actions] if actions else []
    keyboard.append([InlineKeyboardButton(text="返回列表", callback_data="fp:list:0:0")])
    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)


async def show_reviews(message, settings, db):
    if (getattr(message.chat.type, "value", message.chat.type) != "private"
            or not message.from_user
            or message.from_user.id != settings.fingerprint_review_user_id):
        await message.answer("仅指定复核人可在私聊中查看指纹复核。")
        return
    text, markup = review_list(db)
    await message.answer(text, reply_markup=markup)


async def handle_review_callback(callback, settings, db):
    if (not callback.from_user
            or callback.from_user.id != settings.fingerprint_review_user_id
            or not callback.message
            or getattr(callback.message.chat.type, "value", callback.message.chat.type) != "private"):
        await callback.answer("仅指定复核人可在私聊中操作。", show_alert=True)
        return
    try:
        parts = callback.data.split(":")
        action = parts[1]
        if action == "list":
            disabled = int(parts[2]) == 1
            offset = max(0, int(parts[3]))
            text, markup = review_list(db, disabled=disabled, offset=offset)
        else:
            rid = int(parts[2])
            if action != "detail":
                if not db.resolve_fingerprint_review(rid, action, callback.from_user.id):
                    await callback.answer("该建议已处理或指纹已变化。", show_alert=True)
                    return
            text, markup = review_detail(db, rid)
    except (ValueError, IndexError):
        await callback.answer("复核操作无效。", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=markup)
