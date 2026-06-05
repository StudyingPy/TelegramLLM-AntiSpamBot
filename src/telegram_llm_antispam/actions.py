from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Settings
from .db import Database
from .feedback import record_vote_ham_feedback, record_vote_spam_feedback
from .models import ActionResult, DecisionAction, LocalDecision, MessageFeatures, VoteSession, VoteTally
from .notifications import update_vote_notifications, vote_status_text
from .permissions import check_permissions


logger = logging.getLogger(__name__)


class ModerationActions:
    SUMMARY_DELETE_DELAY_SECONDS = 120

    def __init__(self, settings: Settings, db: Database) -> None:
        self._settings = settings
        self._db = db

    async def apply(
        self,
        message: Message,
        features: MessageFeatures,
        decision: LocalDecision,
    ) -> ActionResult:
        if decision.action in {DecisionAction.ALLOW, DecisionAction.REVIEW}:
            action_log_id = self._db.record_action(features, decision)
            return ActionResult(action_log_id=action_log_id)

        if decision.action == DecisionAction.WITHDRAW_VOTE:
            return await self._withdraw_and_vote(message, features, decision)

        if decision.action == DecisionAction.BAN:
            permissions = await check_permissions(message.bot, features.chat_id, features.user_id)
            return await self._ban(message, features, decision, permissions)

        return ActionResult(error="unknown_action")

    async def render_vote_result(self, callback_message: Message, tally: VoteTally) -> None:
        text = (
            f"投票中：广告 {tally.spam_votes} / 放行 {tally.ham_votes}\n"
            f"最低确认票数：{self._settings.vote_min_confirmations}"
        )
        try:
            await callback_message.edit_text(text, reply_markup=self._vote_keyboard(tally.session_id))
        except TelegramBadRequest:
            logger.debug("Vote message was not modified")
        await update_vote_notifications(
            callback_message.bot,
            self._db,
            tally.session_id,
            vote_status_text(self._db, tally),
            is_open=True,
        )

    async def close_vote_if_threshold_reached(
        self,
        callback_message: Message,
        tally: VoteTally,
    ) -> bool:
        if tally.spam_votes >= self._settings.vote_min_confirmations:
            session = self._db.get_vote_session(tally.session_id)
            if session is None:
                return False

            permissions = await check_permissions(
                callback_message.bot,
                session.chat_id,
                session.suspect_user_id,
            )
            metadata = await self._finalize_spam_user(
                callback_message.bot,
                session.chat_id,
                session.suspect_user_id,
                permissions,
                final_status="confirmed_spam",
                action="vote_confirmed_spam",
                reason="vote_threshold_spam",
                confidence=1.0,
                summary_reason="投票确认广告",
                primary_session_id=session.id,
                extra_metadata={
                    "trigger_vote_session_id": session.id,
                    "spam_votes": tally.spam_votes,
                    "ham_votes": tally.ham_votes,
                },
            )
            if session.suspect_user_id is not None:
                self._db.adjust_reputation(
                    session.chat_id,
                    session.suspect_user_id,
                    -self._settings.spam_reputation_penalty,
                )
            if metadata.get("banned"):
                return True
            return True

        if tally.ham_votes >= self._settings.vote_min_confirmations:
            self._db.close_vote_session(tally.session_id, "released")
            session = self._db.get_vote_session(tally.session_id)
            if session is not None:
                record_vote_ham_feedback(self._db, session, self._settings)
            self._db.record_vote_session_action(
                tally.session_id,
                action="vote_released",
                reason="vote_threshold_ham",
                confidence=1.0,
            )
            if tally.suspect_user_id is not None:
                self._db.adjust_reputation(
                    tally.chat_id,
                    tally.suspect_user_id,
                    self._settings.ham_reputation_reward,
                )
            await self._safe_edit_message(
                callback_message,
                f"投票结束：放行。广告 {tally.spam_votes} / 放行 {tally.ham_votes}"
            )
            if session is not None:
                await update_vote_notifications(
                    callback_message.bot,
                    self._db,
                    tally.session_id,
                    vote_status_text(self._db, session),
                    is_open=False,
                )
            return True

        return False

    async def expire_due_vote_sessions(self, bot: Any, limit: int = 100) -> int:
        sessions = self._db.expire_open_vote_sessions(limit=limit)
        for session in sessions:
            self._db.record_vote_session_action(
                session.id,
                action="vote_expired_released",
                reason="vote_timeout_default_release",
                confidence=0.0,
                metadata={"expires_at": session.expires_at},
            )
            await self._edit_expired_vote_message(bot, session)
            await update_vote_notifications(
                bot,
                self._db,
                session.id,
                vote_status_text(self._db, session),
                is_open=False,
            )
        return len(sessions)

    async def admin_ban_vote_session(
        self,
        bot: Any,
        session_id: int,
        moderator_user_id: int,
    ) -> tuple[bool, str]:
        session = self._db.get_vote_session(session_id)
        if session is None:
            return False, "投票会话不存在"
        if session.status != "open":
            return False, "投票已经结束"
        if session.suspect_user_id is None:
            return False, "没有可封禁的用户"

        permissions = await check_permissions(bot, session.chat_id, session.suspect_user_id)
        metadata: dict[str, Any] = {"moderator_user_id": moderator_user_id}
        if not (permissions.can_restrict and permissions.target_is_restrictable):
            metadata["banned"] = False
            metadata["ban_error"] = permissions.reason or "missing_restrict_permission"
            self._db.record_vote_session_action(
                session.id,
                action="admin_ban_failed",
                reason="admin_skip_vote_ban_failed",
                confidence=1.0,
                metadata=metadata,
            )
            return False, "Bot 没有封禁权限，或目标不可封禁"

        metadata = await self._finalize_spam_user(
            bot,
            session.chat_id,
            session.suspect_user_id,
            permissions,
            final_status="admin_banned",
            action="admin_banned_user",
            reason="admin_skip_vote_ban",
            confidence=1.0,
            summary_reason="管理员跳过投票",
            primary_session_id=session.id,
            extra_metadata=metadata,
        )
        if not metadata.get("banned"):
            return False, f"封禁失败：{metadata.get('ban_error') or 'unknown_error'}"
        self._db.adjust_reputation(
            session.chat_id,
            session.suspect_user_id,
            -self._settings.spam_reputation_penalty,
        )
        return True, "已封禁"

    async def _withdraw_and_vote(
        self,
        message: Message,
        features: MessageFeatures,
        decision: LocalDecision,
    ) -> ActionResult:
        metadata: dict[str, Any] = {
            "text_snapshot": features.text[:500],
            "links": [link.url for link in features.links],
            "deleted": False,
        }

        session_id = self._db.create_vote_session(
            features,
            decision,
            timeout_seconds=self._settings.vote_timeout_seconds,
        )
        vote_message = await message.answer(
            self._vote_text(decision),
            reply_markup=self._vote_keyboard(session_id),
            reply_to_message_id=message.message_id,
            allow_sending_without_reply=True,
        )
        self._db.set_vote_message_id(session_id, vote_message.message_id)
        action_log_id = self._db.record_action(features, decision, metadata)
        return ActionResult(
            action_log_id=action_log_id,
            vote_session_id=session_id,
            deleted=False,
        )

    async def _ban(
        self,
        message: Message,
        features: MessageFeatures,
        decision: LocalDecision,
        permissions: Any,
    ) -> ActionResult:
        metadata: dict[str, Any] = {
            "text_snapshot": features.text[:500],
            "links": [link.url for link in features.links],
        }

        final_metadata = await self._finalize_spam_user(
            message.bot,
            features.chat_id,
            features.user_id,
            permissions,
            final_status="confirmed_spam",
            action="auto_banned_user",
            reason=decision.reason,
            confidence=decision.confidence,
            summary_reason=decision.reason,
            current_message_id=features.message_id,
            extra_metadata=metadata,
        )

        action_log_id = self._db.record_action(features, decision, final_metadata)
        return ActionResult(
            action_log_id=action_log_id,
            deleted=bool(final_metadata.get("deleted")),
            banned=bool(final_metadata.get("banned")),
            error=final_metadata.get("ban_error"),
        )

    async def _delete_message(self, message: Message) -> tuple[bool, str | None]:
        try:
            await message.delete()
            return True, None
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            return False, str(exc)

    async def _finalize_spam_user(
        self,
        bot: Any,
        chat_id: int,
        user_id: int | None,
        permissions: Any,
        *,
        final_status: str,
        action: str,
        reason: str,
        confidence: float,
        summary_reason: str,
        current_message_id: int | None = None,
        primary_session_id: int | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = dict(extra_metadata or {})
        sessions = (
            self._db.list_vote_sessions_for_user(
                chat_id,
                user_id,
                statuses=("open",),
            )
            if user_id is not None
            else ()
        )
        if primary_session_id is not None and all(session.id != primary_session_id for session in sessions):
            primary_session = self._db.get_vote_session(primary_session_id)
            if primary_session is not None:
                sessions = (primary_session, *sessions)

        original_message_ids = _unique_ints(
            session.original_message_id for session in sessions if session.original_message_id
        )
        vote_message_ids = _unique_ints(
            session.vote_message_id for session in sessions if session.vote_message_id
        )
        if current_message_id is not None:
            original_message_ids = _unique_ints((*original_message_ids, current_message_id))

        metadata["related_vote_session_ids"] = [session.id for session in sessions]
        metadata["deleted_original_message_ids"] = []
        metadata["deleted_vote_message_ids"] = []
        metadata["delete_errors"] = []

        if permissions.can_delete:
            deleted_original, original_errors = await self._delete_chat_messages(
                bot,
                chat_id,
                original_message_ids,
            )
            deleted_vote, vote_errors = await self._delete_chat_messages(
                bot,
                chat_id,
                vote_message_ids,
            )
            metadata["deleted_original_message_ids"] = deleted_original
            metadata["deleted_vote_message_ids"] = deleted_vote
            metadata["delete_errors"] = original_errors + vote_errors
        elif original_message_ids or vote_message_ids:
            metadata["delete_errors"] = [
                {
                    "message_id": message_id,
                    "error": permissions.reason or "missing_delete_permission",
                }
                for message_id in (*original_message_ids, *vote_message_ids)
            ]

        metadata["deleted"] = bool(
            current_message_id is not None
            and current_message_id in metadata["deleted_original_message_ids"]
        )
        metadata["deleted_count"] = len(metadata["deleted_original_message_ids"]) + len(
            metadata["deleted_vote_message_ids"]
        )

        if user_id is not None and permissions.can_restrict and permissions.target_is_restrictable:
            try:
                await bot.ban_chat_member(chat_id, user_id)
                metadata["banned"] = True
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                metadata["banned"] = False
                metadata["ban_error"] = str(exc)
        else:
            metadata["banned"] = False
            metadata["ban_error"] = permissions.reason or "missing_restrict_permission"

        for session in sessions:
            self._db.close_vote_session(session.id, final_status)
            closed_session = self._db.get_vote_session(session.id)
            if closed_session is not None:
                record_vote_spam_feedback(self._db, closed_session, self._settings)
            self._db.record_vote_session_action(
                session.id,
                action=action,
                reason=reason,
                confidence=confidence,
                metadata=metadata,
            )
            if primary_session_id is None or session.id != primary_session_id:
                await update_vote_notifications(
                    bot,
                    self._db,
                    session.id,
                    vote_status_text(self._db, closed_session or session),
                    is_open=False,
                )

        if primary_session_id is not None:
            primary_session = self._db.get_vote_session(primary_session_id)
            if primary_session is not None:
                await update_vote_notifications(
                    bot,
                    self._db,
                    primary_session.id,
                    vote_status_text(self._db, primary_session),
                    is_open=False,
                )

        summary_message_id, summary_error = await self._send_ban_summary(
            bot,
            chat_id,
            user_id,
            summary_reason,
            metadata,
        )
        metadata["summary_message_id"] = summary_message_id
        if summary_error:
            metadata["summary_error"] = summary_error
        return metadata

    async def _delete_chat_messages(
        self,
        bot: Any,
        chat_id: int,
        message_ids: tuple[int, ...],
    ) -> tuple[list[int], list[dict[str, object]]]:
        deleted: list[int] = []
        errors: list[dict[str, object]] = []
        for message_id in message_ids:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
                deleted.append(message_id)
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                errors.append({"message_id": message_id, "error": str(exc)})
        return deleted, errors

    async def _send_ban_summary(
        self,
        bot: Any,
        chat_id: int,
        user_id: int | None,
        reason: str,
        metadata: dict[str, Any],
    ) -> tuple[int | None, str | None]:
        user_text = f"<code>{user_id}</code>" if user_id is not None else "-"
        status = "已封禁" if metadata.get("banned") else "封禁失败"
        text = (
            "反广告处理完成\n"
            f"用户：{user_text}\n"
            f"处理：{status}\n"
            f"原因：<code>{html.escape(reason)}</code>\n"
            f"已清理：广告消息 {len(metadata.get('deleted_original_message_ids', []))} 条，"
            f"投票消息 {len(metadata.get('deleted_vote_message_ids', []))} 条"
        )
        if metadata.get("ban_error"):
            text += f"\n错误：<code>{html.escape(str(metadata['ban_error']))}</code>"

        try:
            sent_message = await bot.send_message(chat_id, text)
        except TelegramAPIError as exc:
            return None, str(exc)

        message_id = getattr(sent_message, "message_id", None)
        if message_id is None:
            return None, None
        if self.SUMMARY_DELETE_DELAY_SECONDS > 0:
            asyncio.create_task(
                self._delete_summary_later(
                    bot,
                    chat_id,
                    int(message_id),
                    self.SUMMARY_DELETE_DELAY_SECONDS,
                )
            )
        return int(message_id), None

    async def _delete_summary_later(
        self,
        bot: Any,
        chat_id: int,
        message_id: int,
        delay_seconds: int,
    ) -> None:
        await asyncio.sleep(delay_seconds)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError:
            logger.debug("Failed to delete ban summary message %s", message_id, exc_info=True)

    async def _edit_expired_vote_message(self, bot: Any, session: VoteSession) -> None:
        if session.vote_message_id is None:
            return

        text = (
            "投票超时：默认放行并标记。\n"
            f"广告 {session.spam_votes} / 放行 {session.ham_votes}"
        )
        try:
            await self._edit_vote_message_text(bot, session, text)
        except TelegramAPIError as exc:  # pragma: no cover - depends on Telegram API state.
            logger.warning("Failed to edit expired vote message %s: %s", session.id, exc)

    async def _edit_vote_message_text(self, bot: Any, session: VoteSession, text: str) -> None:
        if session.vote_message_id is None:
            return
        await bot.edit_message_text(
            text=text,
            chat_id=session.chat_id,
            message_id=session.vote_message_id,
        )

    async def _safe_edit_message(self, message: Message, text: str) -> None:
        try:
            await message.edit_text(text)
        except TelegramAPIError as exc:  # pragma: no cover - depends on Telegram API state.
            logger.warning("Failed to edit vote message: %s", exc)

    def _vote_text(self, decision: LocalDecision) -> str:
        reason = html.escape(decision.reason)
        confidence = f"{decision.confidence:.0%}"
        return (
            "疑似广告，请根据被回复的原消息投票确认。\n"
            f"原因：<code>{reason}</code>\n"
            f"置信度：{confidence}"
        )

    def _vote_keyboard(self, session_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="确认广告",
                        callback_data=f"vote:{session_id}:spam",
                    ),
                    InlineKeyboardButton(
                        text="放行",
                        callback_data=f"vote:{session_id}:ham",
                    ),
                    InlineKeyboardButton(
                        text="管理员封禁",
                        callback_data=f"admin_ban:{session_id}",
                    ),
                ]
            ]
        )


def _unique_ints(values: object) -> tuple[int, ...]:
    result: list[int] = []
    for value in values:
        if value is None:
            continue
        int_value = int(value)
        if int_value not in result:
            result.append(int_value)
    return tuple(result)
