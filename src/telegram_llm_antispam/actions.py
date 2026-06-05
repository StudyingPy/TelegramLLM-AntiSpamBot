from __future__ import annotations

import html
import logging
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Settings
from .db import Database
from .feedback import record_vote_ham_feedback, record_vote_spam_feedback
from .models import ActionResult, DecisionAction, LocalDecision, MessageFeatures, VoteSession, VoteTally
from .permissions import check_permissions


logger = logging.getLogger(__name__)


class ModerationActions:
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

        permissions = await check_permissions(message.bot, features.chat_id, features.user_id)
        if decision.action == DecisionAction.WITHDRAW_VOTE:
            return await self._withdraw_and_vote(message, features, decision, permissions)

        if decision.action == DecisionAction.BAN:
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

    async def close_vote_if_threshold_reached(
        self,
        callback_message: Message,
        tally: VoteTally,
    ) -> bool:
        if tally.spam_votes >= self._settings.vote_min_confirmations:
            self._db.close_vote_session(tally.session_id, "confirmed_spam")
            session = self._db.get_vote_session(tally.session_id)
            if session is not None:
                record_vote_spam_feedback(self._db, session, self._settings)
            self._db.record_vote_session_action(
                tally.session_id,
                action="vote_confirmed_spam",
                reason="vote_threshold_spam",
                confidence=1.0,
            )
            if tally.suspect_user_id is not None:
                self._db.adjust_reputation(
                    tally.chat_id,
                    tally.suspect_user_id,
                    -self._settings.spam_reputation_penalty,
                )
            await self._safe_edit_message(
                callback_message,
                f"投票结束：确认广告。广告 {tally.spam_votes} / 放行 {tally.ham_votes}"
            )
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

        try:
            await bot.ban_chat_member(session.chat_id, session.suspect_user_id)
            metadata["banned"] = True
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            metadata["banned"] = False
            metadata["ban_error"] = str(exc)
            self._db.record_vote_session_action(
                session.id,
                action="admin_ban_failed",
                reason="admin_skip_vote_ban_failed",
                confidence=1.0,
                metadata=metadata,
            )
            return False, f"封禁失败：{exc}"

        self._db.close_vote_session(session.id, "admin_banned")
        closed_session = self._db.get_vote_session(session.id)
        if closed_session is not None:
            record_vote_spam_feedback(self._db, closed_session, self._settings)
        self._db.adjust_reputation(
            session.chat_id,
            session.suspect_user_id,
            -self._settings.spam_reputation_penalty,
        )
        self._db.record_vote_session_action(
            session.id,
            action="admin_banned_user",
            reason="admin_skip_vote_ban",
            confidence=1.0,
            metadata=metadata,
        )
        await self._edit_vote_message_text(
            bot,
            session,
            f"管理员已跳过投票并封禁用户。广告 {session.spam_votes} / 放行 {session.ham_votes}",
        )
        return True, "已封禁"

    async def _withdraw_and_vote(
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

        deleted = False
        if permissions.can_delete:
            deleted, error = await self._delete_message(message)
            metadata["deleted"] = deleted
            if error:
                metadata["delete_error"] = error
        else:
            metadata["deleted"] = False
            metadata["delete_error"] = permissions.reason or "missing_delete_permission"

        session_id = self._db.create_vote_session(
            features,
            decision,
            timeout_seconds=self._settings.vote_timeout_seconds,
        )
        vote_message = await message.answer(
            self._vote_text(decision),
            reply_markup=self._vote_keyboard(session_id),
        )
        self._db.set_vote_message_id(session_id, vote_message.message_id)
        action_log_id = self._db.record_action(features, decision, metadata)
        return ActionResult(
            action_log_id=action_log_id,
            vote_session_id=session_id,
            deleted=deleted,
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

        deleted = False
        if permissions.can_delete:
            deleted, error = await self._delete_message(message)
            metadata["deleted"] = deleted
            if error:
                metadata["delete_error"] = error

        if (
            features.user_id is not None
            and permissions.can_restrict
            and permissions.target_is_restrictable
        ):
            try:
                await message.bot.ban_chat_member(features.chat_id, features.user_id)
                metadata["banned"] = True
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                metadata["banned"] = False
                metadata["ban_error"] = str(exc)
        else:
            metadata["banned"] = False
            metadata["ban_error"] = permissions.reason or "missing_restrict_permission"

        action_log_id = self._db.record_action(features, decision, metadata)
        return ActionResult(
            action_log_id=action_log_id,
            deleted=deleted,
            banned=bool(metadata["banned"]),
            error=metadata.get("ban_error"),
        )

    async def _delete_message(self, message: Message) -> tuple[bool, str | None]:
        try:
            await message.delete()
            return True, None
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            return False, str(exc)

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
            "疑似广告已临时撤回，请投票确认。\n"
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
