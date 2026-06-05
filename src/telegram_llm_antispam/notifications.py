from __future__ import annotations

import html
from typing import Any

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import Settings
from .models import ActionResult, DecisionAction, LocalDecision, MessageFeatures


async def notify_admins(
    bot: Any,
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
        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="跳过投票并封禁",
                        callback_data=f"admin_ban:{result.vote_session_id}",
                    )
                ]
            ]
        )

    for user_id in settings.notify_user_ids:
        try:
            await bot.send_message(user_id, text, reply_markup=reply_markup)
        except TelegramAPIError:
            continue


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

    llm_text = ""
    if "llm_confidence" in decision.metadata or "category" in decision.metadata:
        phrases = decision.metadata.get("llm_signal_phrases") or decision.metadata.get("signal_phrases")
        phrase_text = ""
        if isinstance(phrases, list | tuple) and phrases:
            phrase_text = f"\n信号：{_esc(', '.join(str(item) for item in phrases[:8]))}"
        llm_text = (
            f"\nLLM：{_esc(str(decision.metadata.get('llm_category') or decision.metadata.get('category') or '-'))}"
            f" / {decision.metadata.get('llm_confidence', decision.confidence):.0%}"
            f"{phrase_text}"
        )

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


def _esc(value: str) -> str:
    return html.escape(value, quote=False)
