from __future__ import annotations

import html
from typing import Any

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import Settings
from .db import Database
from .models import ActionResult, DecisionAction, LocalDecision, MessageFeatures, VoteSession, VoteTally


async def notify_admins(
    bot: Any,
    db: Database,
    settings: Settings,
    features: MessageFeatures,
    decision: LocalDecision,
    result: ActionResult,
) -> None:
    if not settings.notify_user_ids or decision.action == DecisionAction.ALLOW:
        return

    text = _notification_text(features, decision, result)
    reply_markup = None
    if result.vote_session_id is not None:
        reply_markup = admin_ban_keyboard(result.vote_session_id)

    send_text = text
    if result.vote_session_id is not None:
        session = db.get_vote_session(result.vote_session_id)
        if session is not None:
            send_text = f"{text}\n\n实时状态：\n{_esc(vote_status_text(db, session))}"

    for user_id in settings.notify_user_ids:
        try:
            sent_message = await bot.send_message(user_id, send_text, reply_markup=reply_markup)
        except TelegramAPIError:
            continue
        message_id = getattr(sent_message, "message_id", None)
        if message_id is not None:
            db.record_admin_notification(
                vote_session_id=result.vote_session_id,
                action_log_id=result.action_log_id,
                notify_user_id=user_id,
                message_id=int(message_id),
                base_text=text,
            )


async def update_vote_notifications(
    bot: Any,
    db: Database,
    session_id: int,
    status_text: str,
    *,
    is_open: bool,
) -> None:
    notifications = db.list_admin_notifications(session_id)
    if not notifications:
        return

    reply_markup = admin_ban_keyboard(session_id) if is_open else None
    live_text = f"\n\n实时状态：\n{_esc(status_text)}"
    for notification in notifications:
        try:
            await bot.edit_message_text(
                chat_id=notification.notify_user_id,
                message_id=notification.message_id,
                text=f"{notification.base_text}{live_text}",
                reply_markup=reply_markup,
            )
            db.touch_admin_notification(notification.id)
        except TelegramAPIError:
            continue


def vote_status_text(
    db: Database,
    session_or_tally: VoteSession | VoteTally,
    *,
    label: str | None = None,
) -> str:
    spam_votes = session_or_tally.spam_votes
    ham_votes = session_or_tally.ham_votes
    status = session_or_tally.status
    lines = [
        label or _status_label(status),
        f"状态：{status}",
        f"投票：广告 {spam_votes} / 放行 {ham_votes}",
    ]
    session_id = (
        session_or_tally.id
        if isinstance(session_or_tally, VoteSession)
        else session_or_tally.session_id
    )
    records = db.list_vote_records(session_id)
    if records:
        lines.append("投票记录：")
        for record in records[:10]:
            vote = "广告" if record.vote == "spam" else "放行"
            lines.append(f"- {record.voter_user_id}: {vote}")
    else:
        lines.append("投票记录：暂无")
    return "\n".join(lines)


def admin_ban_keyboard(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="跳过投票并封禁",
                    callback_data=f"admin_ban:{session_id}",
                )
            ]
        ]
    )


def _notification_text(
    features: MessageFeatures,
    decision: LocalDecision,
    result: ActionResult,
) -> str:
    profile = features.metadata.get("sender_profile")
    profile_text = ""
    if isinstance(profile, dict):
        display_name = profile.get("display_name") or ""
        username = profile.get("username") or ""
        bio = profile.get("bio") or ""
        profile_text = (
            f"\n用户资料：{_esc(str(display_name))}"
            f" @{_esc(str(username)) if username else '-'}"
            f"\nBio：{_esc(str(bio)) if bio else '-'}"
        )

    og_preview = features.metadata.get("og_preview")
    og_text = ""
    if isinstance(og_preview, dict):
        og_text = (
            "\nOG："
            f"{_esc(str(og_preview.get('title') or '-'))} / "
            f"{_esc(str(og_preview.get('description') or '-'))}"
        )

    llm_text = _format_llm_section(decision)

    links = ", ".join(link.url for link in features.links) or "-"
    snapshot = features.text[:800] or "(empty)"
    message_link = _message_link(features.chat_id, features.message_id)
    return (
        "反广告处理记录\n"
        f"群组：<code>{features.chat_id}</code>\n"
        f"消息：{message_link}\n"
        f"用户：<code>{features.user_id or '-'}</code>"
        f"{profile_text}\n"
        f"触发：<code>{_esc(decision.reason)}</code>\n"
        f"处理：<b>{decision.action.value}</b> / {decision.confidence:.0%}\n"
        f"删除：{_fmt_bool(result.deleted)} 封禁：{_fmt_bool(result.banned)}\n"
        f"日志：<code>{result.action_log_id or '-'}</code>\n"
        f"投票会话：<code>{result.vote_session_id or '-'}</code>\n"
        f"链接：{_esc(links)}"
        f"{og_text}"
        f"{llm_text}\n"
        f"正文：\n<blockquote>{_esc(snapshot)}</blockquote>"
    )


def _message_link(chat_id: int, message_id: int) -> str:
    chat_id_text = str(chat_id)
    if chat_id_text.startswith("-100"):
        internal_id = chat_id_text[4:]
        return f'<a href="https://t.me/c/{internal_id}/{message_id}">{message_id}</a>'
    return f"<code>{message_id}</code>"


def _fmt_bool(value: bool | None) -> str:
    if value is None:
        return "-"
    return "是" if value else "否"


def _status_label(status: str) -> str:
    return {
        "open": "投票中",
        "confirmed_spam": "投票结束：确认广告",
        "released": "投票结束：放行",
        "expired_released": "投票超时：默认放行",
        "admin_banned": "管理员已跳过投票并封禁",
    }.get(status, f"状态更新：{status}")


def _format_llm_section(decision: LocalDecision) -> str:
    """Render the LLM hop section. Always emits a line when the LLM was attempted.

    Three shapes:
    - outcome=disabled              → "LLM：未配置"
    - outcome=failed                → "LLM：调用失败（N 个 provider） / <error>"
    - outcome=ok                    → "LLM：<category> / <confidence>" + 可选信号短语
    Legacy code paths that wrote llm_confidence/category without an outcome still render.
    """

    outcome = decision.metadata.get("llm_outcome")
    if isinstance(outcome, dict):
        status = str(outcome.get("status") or "")
        provider_count = outcome.get("provider_count") or 0
        if status == "disabled":
            return "\nLLM：未配置"
        if status == "failed":
            error = str(outcome.get("error") or "未知错误")
            return f"\nLLM：调用失败（{provider_count} 个 provider） / {_esc(error)}"
        if status == "ok":
            category = str(outcome.get("category") or "-")
            confidence = float(outcome.get("confidence") or 0.0)
            verdict = "广告" if outcome.get("is_spam") else "正常"
            phrases = outcome.get("signal_phrases")
            phrase_text = ""
            if isinstance(phrases, list | tuple) and phrases:
                phrase_text = (
                    f"\n信号：{_esc(', '.join(str(item) for item in phrases[:8]))}"
                )
            return (
                f"\nLLM：{verdict} / {_esc(category)} / {confidence:.0%}"
                f"{phrase_text}"
            )

    if "llm_confidence" in decision.metadata or "category" in decision.metadata:
        phrases = decision.metadata.get("llm_signal_phrases") or decision.metadata.get(
            "signal_phrases"
        )
        phrase_text = ""
        if isinstance(phrases, list | tuple) and phrases:
            phrase_text = f"\n信号：{_esc(', '.join(str(item) for item in phrases[:8]))}"
        category = (
            decision.metadata.get("llm_category") or decision.metadata.get("category") or "-"
        )
        confidence = decision.metadata.get("llm_confidence", decision.confidence)
        return f"\nLLM：{_esc(str(category))} / {float(confidence):.0%}{phrase_text}"

    return ""


def _esc(value: str) -> str:
    return html.escape(value, quote=False)
