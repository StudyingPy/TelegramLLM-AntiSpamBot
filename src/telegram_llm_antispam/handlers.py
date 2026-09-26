from __future__ import annotations

import html
import logging
import re
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .actions import ModerationActions
from .admin import can_manage_chat, is_chat_allowed, is_global_admin
from .config import Settings
from .db import Database
from .feedback import (
    _EMPTY_TEXT_HASH,
    fingerprint_lookup_values,
    record_llm_spam_feedback,
)
from .features import build_message_features
from .fingerprints import is_low_entropy_skeleton
from .llm import LLMJudge, NullLLMJudge, decision_from_llm
from .models import (
    DecisionAction,
    LLMJudgement,
    LLMOutcome,
    LLMOutcomeStatus,
    LocalDecision,
    MessageFeatures,
)
from .notifications import (
    REVIEW_DEEPLINK_PREFIX,
    notify_admins,
    review_action_keyboard,
    review_card_text,
)
from .og import fetch_og_for_features, should_fetch_og
from .profile import fetch_personal_chat_for_crosscheck, get_sender_profile
from .rules import RuleEngine, should_crosscheck_personal_channel


logger = logging.getLogger(__name__)

ADMIN_VERIFY_ACTIONS = {"status", "allow_chat", "deny_chat"}

# Telegram does not send the previous revision with edited_message.  Keep a small,
# expiring baseline only for accounts most likely to abuse edits; established users
# continue through the ordinary moderation path without per-message tracking.
EDIT_WATCH_MAX_MESSAGES = 5
EDIT_WATCH_TTL_SECONDS = 24 * 60 * 60

# Telegram's own service account. 777000 ("Telegram") is the sender of channel posts
# auto-forwarded into a linked discussion group, and of login/service notifications.
# It is never a real group member submitting spam, so it bypasses moderation
# unconditionally — the same effect as an operator whitelisting it, but built in so a
# fresh deployment never bans its own linked-channel posts.
TELEGRAM_SERVICE_USER_IDS: frozenset[int] = frozenset({777000})

_BOT_USERNAME = r"[A-Za-z0-9_]{2,29}bot"
_BOT_ONLY_MESSAGE_RE = re.compile(
    rf"^\s*(?P<mentions>@{_BOT_USERNAME}(?:\s+@{_BOT_USERNAME})*)\s*$",
    re.IGNORECASE,
)
_BOT_MENTION_RE = re.compile(rf"@({_BOT_USERNAME})", re.IGNORECASE)


def create_router(settings: Settings, db: Database, llm: LLMJudge | None = None) -> Router:
    router = Router(name="moderation")
    rule_engine = RuleEngine(settings)
    llm_judge = llm or NullLLMJudge()
    actions = ModerationActions(settings, db)

    # Cache our own bot id so we can distinguish "this is OUR bot replying" (skip,
    # we don't moderate our own messages) from "this is SOME OTHER bot posting"
    # (process normally — spammers register their own bots and post promotional
    # content as bot users, which is a Telegram feature, not noise to skip). Filled
    # in lazily on first message because get_me() needs an active session.
    self_bot_id: dict[str, int] = {}

    async def _get_self_bot_id(bot: Any) -> int | None:
        if "id" in self_bot_id:
            return self_bot_id["id"]
        try:
            me = await bot.get_me()
            self_bot_id["id"] = int(me.id)
            return self_bot_id["id"]
        except Exception as exc:  # pragma: no cover - depends on Telegram API state.
            logger.warning("Failed to resolve self bot id, will retry next message: %s", exc)
            return None

    @router.message(Command("start", "help"))
    async def on_help(message: Message, command: CommandObject) -> None:
        # A `/start review_<id>` deep link (from the catch-up button on a timed-out
        # group vote) opens the private-chat review flow instead of the help text.
        payload = (command.args or "").strip()
        if payload.startswith(REVIEW_DEEPLINK_PREFIX):
            await _handle_review_deeplink(message, payload)
            return
        await message.answer(
            "Telegram 反广告机器人已运行。\n"
            "/status 查看状态\n"
            "/allow_chat 允许当前群组使用机器人\n"
            "/deny_chat 禁用当前群组\n"
            "/whitelist <user_id> [备注] 加入全局白名单（仅全局管理员）\n"
            "/unwhitelist <user_id> 移出全局白名单（仅全局管理员）\n"
            "/list_whitelist 查看全局白名单（仅全局管理员）"
        )

    async def _handle_review_deeplink(message: Message, payload: str) -> None:
        if _chat_type(message) != "private":
            # Deep links resolve to a DM; ignore a stray `/start review_x` in a group.
            return
        try:
            session_id = int(payload[len(REVIEW_DEEPLINK_PREFIX):])
        except ValueError:
            await message.answer("补审链接无效。")
            return

        session = db.get_vote_session(session_id)
        if session is None:
            await message.answer("投票会话不存在或已被清理。")
            return

        user_id = message.from_user.id if message.from_user else None
        if not await can_manage_chat(message.bot, settings, session.chat_id, user_id):
            await message.answer("只有该群组的管理员可以补审此消息。")
            return

        reply_markup = (
            review_action_keyboard(session_id)
            if session.status in {"open", "expired_released"}
            else None
        )
        await message.answer(review_card_text(db, session), reply_markup=reply_markup)

    @router.message(Command("status"))
    async def on_status(message: Message) -> None:
        if _chat_type(message) in {"group", "supergroup"}:
            if _is_anonymous_admin_message(message):
                await _ask_anonymous_admin_to_verify(message, "status")
                return
            can_manage = await can_manage_chat(
                message.bot,
                settings,
                message.chat.id,
                message.from_user.id if message.from_user else None,
            )
            if not can_manage:
                await message.answer("只有群管理员可以查看此状态。")
                return
            await message.answer(_group_status_text(settings, db, message.chat.id))
            return

        user_id = message.from_user.id if message.from_user else None
        await message.answer(
            f"全局管理员：{'是' if is_global_admin(settings, user_id) else '否'}\n"
            f"通知接收者：{', '.join(str(item) for item in settings.notify_user_ids) or '-'}"
        )

    @router.message(Command("allow_chat"))
    async def on_allow_chat(message: Message) -> None:
        if _chat_type(message) in {"group", "supergroup"}:
            if _is_anonymous_admin_message(message):
                await _ask_anonymous_admin_to_verify(message, "allow_chat")
                return
            user_id = message.from_user.id if message.from_user else None
            if not await can_manage_chat(message.bot, settings, message.chat.id, user_id):
                await message.answer("只有群管理员可以允许当前群组。")
                return
            db.allow_chat(message.chat.id, message.chat.title, user_id)
            await message.answer(f"已允许当前群组使用机器人：<code>{message.chat.id}</code>")
            return

        if not is_global_admin(settings, message.from_user.id if message.from_user else None):
            await message.answer("只有全局管理员可以在私聊中管理 allowlist。")
            return
        args = (message.text or "").split(maxsplit=1)
        if len(args) != 2:
            await message.answer("用法：/allow_chat -1001234567890")
            return
        chat_id = _parse_chat_id(args[1])
        if chat_id is None:
            await message.answer("群组 ID 必须是数字，例如：/allow_chat -1001234567890")
            return
        db.allow_chat(chat_id, None, message.from_user.id if message.from_user else None)
        await message.answer(f"已允许群组：<code>{chat_id}</code>")

    @router.message(Command("deny_chat"))
    async def on_deny_chat(message: Message) -> None:
        if _chat_type(message) in {"group", "supergroup"}:
            if _is_anonymous_admin_message(message):
                await _ask_anonymous_admin_to_verify(message, "deny_chat")
                return
            user_id = message.from_user.id if message.from_user else None
            if not await can_manage_chat(message.bot, settings, message.chat.id, user_id):
                await message.answer("只有群管理员可以禁用当前群组。")
                return
            db.disallow_chat(message.chat.id)
            await message.answer(f"已禁用当前群组：<code>{message.chat.id}</code>")
            return

        if not is_global_admin(settings, message.from_user.id if message.from_user else None):
            await message.answer("只有全局管理员可以在私聊中管理 allowlist。")
            return
        args = (message.text or "").split(maxsplit=1)
        if len(args) != 2:
            await message.answer("用法：/deny_chat -1001234567890")
            return
        chat_id = _parse_chat_id(args[1])
        if chat_id is None:
            await message.answer("群组 ID 必须是数字，例如：/deny_chat -1001234567890")
            return
        db.disallow_chat(chat_id)
        await message.answer(f"已禁用群组：<code>{chat_id}</code>")

    @router.message(Command("whitelist"))
    async def on_whitelist(message: Message, command: CommandObject) -> None:
        # Global-admin-only. The whitelist is global (not chat-scoped), so we gate on
        # is_global_admin regardless of where the command runs, in group or private.
        user_id = message.from_user.id if message.from_user else None
        if not is_global_admin(settings, user_id):
            await message.answer("只有全局管理员可以管理白名单。")
            return
        target_id, note = _parse_whitelist_target(message, command.args)
        if target_id is None:
            await message.answer(
                "用法：/whitelist <user_id> [备注]，或回复某人的消息 /whitelist [备注]。"
            )
            return
        await message.answer(_whitelist_add(db, target_id, note, added_by=user_id))

    @router.message(Command("unwhitelist"))
    async def on_unwhitelist(message: Message, command: CommandObject) -> None:
        user_id = message.from_user.id if message.from_user else None
        if not is_global_admin(settings, user_id):
            await message.answer("只有全局管理员可以管理白名单。")
            return
        target_id, _ = _parse_whitelist_target(message, command.args)
        if target_id is None:
            await message.answer(
                "用法：/unwhitelist <user_id>，或回复某人的消息 /unwhitelist。"
            )
            return
        await message.answer(_whitelist_remove(db, target_id))

    @router.message(Command("list_whitelist"))
    async def on_list_whitelist(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else None
        if not is_global_admin(settings, user_id):
            await message.answer("只有全局管理员可以查看白名单。")
            return
        await message.answer(_whitelist_list(db, settings))

    @router.message()
    async def on_message(message: Message) -> None:
        await _process_group_message(message, is_edit=False)

    @router.edited_message()
    async def on_edited_message(message: Message) -> None:
        await _process_group_message(message, is_edit=True)

    async def _process_group_message(message: Message, *, is_edit: bool) -> None:
        chat_type = _chat_type(message)
        if chat_type not in {"group", "supergroup"}:
            return
        if not is_chat_allowed(settings, db, message.chat.id):
            return

        # Channel posts auto-forwarded into a linked discussion group arrive as
        # messages from Telegram's service account (777000) with is_automatic_forward
        # set and sender_chat pointing at the source channel. They are not member
        # submissions — moderating them deletes/bans the group's own channel content
        # (the classic "bot ate the forwarded 端午 promo post" failure). Never touch them.
        if not is_edit and _is_automatic_channel_forward(message):
            return

        # Whitelisted user IDs (built-in Telegram service accounts + env
        # WHITELISTED_USER_IDS + DB whitelisted_users table) completely bypass
        # moderation. This is the escape hatch for friendly bots like nmBot / 客服酱
        # whose moderation notifications were getting auto-banned by earlier versions of
        # the body-path rule — even though commit 6cb0316 fixed the specific keyword, a
        # whitelist is the right ergonomic answer for "this user is known to be safe,
        # never touch their messages". Applied to BOTH message events and new-member
        # events; we don't want to write fingerprints off a whitelisted user's text either.
        sender_id = message.from_user.id if message.from_user else None
        if sender_id is not None and _is_whitelisted_sender(db, settings, sender_id):
            return

        new_members = _new_chat_members(message)
        bot_usernames: tuple[str, ...] = ()
        had_ad_invocation = False
        if (
            not is_edit
            and not new_members
            and message.from_user is not None
            and not getattr(message.from_user, "is_bot", False)
        ):
            # Persist before the first network await. A called bot can respond while
            # sender-profile lookup is still in flight; recording later would race
            # that response and lose the caller attribution.
            # A media attachment with an @bot-only caption is not a message whose
            # only content is @bot, so this rule deliberately reads `text` only.
            raw_text = getattr(message, "text", None)
            bot_usernames = _only_bot_mentions(raw_text) if isinstance(raw_text, str) else ()
            if bot_usernames:
                had_ad_invocation = db.has_prior_confirmed_ad_bot_invocation(
                    message.chat.id,
                    message.from_user.id,
                    message.message_id,
                )
                db.record_bot_only_message(
                    message.chat.id,
                    message.message_id,
                    message.from_user.id,
                    bot_usernames,
                )

        # Only OUR own bot's messages must be skipped — replies to /help, vote prompts,
        # ban summaries. Any OTHER bot in the group is fair game: spammers register
        # their own bots and post promotional content under bot identities, which is
        # a real Telegram pattern (e.g. AI-strip / porn / lottery promo bots replying
        # to @mentions). Treating every is_bot=True as untouchable lets that bypass
        # every rule we have.
        self_id = await _get_self_bot_id(message.bot)

        if new_members and not is_edit:
            for user in new_members:
                user_id = getattr(user, "id", None)
                # Our own bot joining is a no-op for moderation. Other bots joining
                # still get a profile check — their bio/name might already be spammy.
                if self_id is not None and user_id == self_id:
                    continue
                # Whitelisted users (e.g. friendly anti-spam bots like nmBot) bypass
                # the profile/bio check on join too.
                if user_id is not None and _is_whitelisted_sender(db, settings, user_id):
                    continue
                await _process_features_for_user(
                    message,
                    user,
                    update_type="new_chat_member",
                    is_edit=False,
                )
            return

        if not message.from_user:
            return
        # Skip only ourselves, not every bot.
        if self_id is not None and message.from_user.id == self_id:
            return
        if (message.text or "").strip().startswith("/"):
            return

        await _process_features_for_user(
            message,
            message.from_user,
            update_type="edited_message" if is_edit else "message",
            is_edit=is_edit,
            bot_usernames=bot_usernames,
            had_ad_invocation=had_ad_invocation,
        )

    async def _process_features_for_user(
        message: Message,
        user: Any,
        *,
        update_type: str,
        is_edit: bool,
        bot_usernames: tuple[str, ...] = (),
        had_ad_invocation: bool = False,
    ) -> None:
        user_context = db.get_user_context(message.chat.id, user.id)
        sender_profile = await get_sender_profile(message.bot, db, user, settings)
        features = build_message_features(
            _feature_message_for_user(message, user),
            user_context=user_context,
            sender_profile=sender_profile,
            default_reputation=settings.default_reputation,
        )
        features.metadata["update_type"] = update_type
        db.purge_expired_edit_watch_snapshots()
        edit_snapshot = None
        if is_edit:
            edit_snapshot = db.get_edit_watch_snapshot(
                features.chat_id,
                features.message_id,
            )
            if edit_snapshot is not None:
                previous_text = str(edit_snapshot.get("text_snapshot") or "")
                if previous_text != features.text[:4000]:
                    # Keep the original text bounded both for action logs and for the
                    # LLM payload.  The current revision remains features.text.
                    features.metadata["edited_from"] = {
                        "text": previous_text,
                        "content_hash": edit_snapshot.get("content_hash"),
                        "created_at": edit_snapshot.get("created_at"),
                    }
        if (
            update_type in {"message", "edited_message"}
            and not sender_profile.is_bot
            and should_crosscheck_personal_channel(features)
        ):
            personal_chat = await fetch_personal_chat_for_crosscheck(
                message.bot,
                user.id,
            )
            if personal_chat is not None:
                # Live, transient evidence only. It is deliberately not written to
                # user_profiles or a channel-history table: the rule evaluates the
                # channel currently attached when the suspicious message is handled.
                features.metadata["personal_chat"] = personal_chat
        if should_fetch_og(features, settings):
            og_preview = await fetch_og_for_features(features, settings)
            if og_preview is not None:
                features.metadata["og_preview"] = og_preview.to_payload()

        repeated_bot_only_decision: LocalDecision | None = None
        if (
            update_type == "message"
            and not is_edit
            and not sender_profile.is_bot
            and features.user_id is not None
            and bot_usernames
            and had_ad_invocation
        ):
            repeated_bot_only_decision = LocalDecision(
                action=DecisionAction.BAN,
                reason="repeated_bot_only_invocation_after_ad",
                confidence=1.0,
                should_call_llm=False,
                metadata={
                    "bot_usernames": list(bot_usernames),
                    "additional_message_ids_to_delete": list(
                        db.list_bot_only_message_ids(features.chat_id, features.user_id)
                    ),
                },
            )

        fingerprint = db.get_strongest_fingerprint(fingerprint_lookup_values(features))
        if fingerprint is not None:
            new_weight = db.record_fingerprint_hit(
                fingerprint.id,
                weight_increment=settings.fingerprint_hit_weight_increment,
                weight_cap=settings.fingerprint_hit_weight_cap,
            )
            if new_weight is not None and new_weight != fingerprint.weight:
                fingerprint = replace(fingerprint, weight=new_weight)

        same_user_repeat_decision = _same_user_open_vote_repeat_decision(settings, db, features)
        repeat_decision = _repeat_decision(settings, db, features)
        decision = (
            repeated_bot_only_decision
            or same_user_repeat_decision
            or repeat_decision
            or rule_engine.evaluate(features, fingerprint=fingerprint)
        )

        if decision.should_call_llm:
            try:
                outcome = await llm_judge.judge(features)
            except Exception as exc:  # defense-in-depth: judge() should not raise.
                logger.warning("LLM judgement raised unexpectedly, treating as failure: %s", exc)
                outcome = LLMOutcome(
                    status=LLMOutcomeStatus.FAILED,
                    provider_count=0,
                    error=f"{type(exc).__name__}: {exc}",
                )
            if outcome.status == LLMOutcomeStatus.OK and outcome.judgement is not None:
                record_llm_spam_feedback(db, features, outcome.judgement, settings)
                decision = _merge_llm_decision(decision, outcome.judgement, features, settings)
            decision = _annotate_with_llm_outcome(decision, outcome)

        if (
            update_type == "message"
            and not is_edit
            and sender_profile.is_bot
            and sender_profile.username
            and _decision_identifies_advertising_bot(decision)
        ):
            reply = getattr(message, "reply_to_message", None)
            reply_to_message_id = getattr(reply, "message_id", None)
            db.confirm_bot_only_invocation_ad(
                features.chat_id,
                sender_profile.username,
                features.message_id,
                reply_to_message_id=reply_to_message_id,
            )

        reputation_reward = _normal_message_reputation_reward(
            settings,
            decision,
            update_type=update_type,
            is_edit=is_edit,
        )
        if reputation_reward:
            metadata = dict(decision.metadata)
            metadata["reputation_reward"] = reputation_reward
            decision = replace(decision, metadata=metadata)

        result = await actions.apply(message, features, decision)
        if not is_edit:
            db.record_message_seen(features, reputation_delta=reputation_reward)
        db.record_observation(features)
        if sender_profile.is_bot:
            db.delete_edit_watch_snapshot(features.chat_id, features.message_id)
        elif is_edit and edit_snapshot is not None:
            # Keep the original baseline while a suspicious edit is under vote.  A
            # clean edit becomes the new baseline so a later revision can still be
            # compared without tracking every member of the group.
            if decision.action in {DecisionAction.BAN, DecisionAction.WITHDRAW_VOTE}:
                pass
            else:
                db.save_edit_watch_snapshot(
                    features,
                    ttl_seconds=EDIT_WATCH_TTL_SECONDS,
                )
        elif (
            not is_edit
            and _should_watch_edits(user_context, sender_profile, settings)
            and decision.action in {DecisionAction.ALLOW, DecisionAction.REVIEW}
        ):
            db.save_edit_watch_snapshot(
                features,
                ttl_seconds=EDIT_WATCH_TTL_SECONDS,
            )
        await notify_admins(message.bot, db, settings, features, decision, result)

    @router.callback_query(F.data.startswith("vote:"))
    async def on_vote(callback: CallbackQuery) -> None:
        if not callback.data or not callback.from_user:
            return

        try:
            _, session_id_raw, vote = callback.data.split(":", 2)
            session_id = int(session_id_raw)
        except ValueError:
            await callback.answer("投票数据无效", show_alert=False)
            return

        tally = db.add_vote(session_id, callback.from_user.id, vote)
        if tally is None:
            await callback.answer("投票不存在", show_alert=False)
            return
        if not tally.changed:
            answer = "投票已结束" if tally.status != "open" else "投票已记录"
            await callback.answer(answer, show_alert=False)
            return

        await callback.answer("已记录")
        if callback.message is None:
            return

        closed = await actions.close_vote_if_threshold_reached(callback.message, tally)
        if not closed:
            await actions.render_vote_result(callback.message, tally)

    @router.callback_query(F.data.startswith("admin_verify:"))
    async def on_admin_verify(callback: CallbackQuery) -> None:
        if not callback.data or not callback.from_user:
            return

        try:
            _, action, chat_id_raw = callback.data.split(":", 2)
            chat_id = int(chat_id_raw)
        except ValueError:
            await callback.answer("验证数据无效", show_alert=False)
            return

        if action not in ADMIN_VERIFY_ACTIONS:
            await callback.answer("验证操作无效", show_alert=False)
            return

        if callback.message is not None:
            callback_chat_id = _message_chat_id(callback.message)
            if callback_chat_id is not None and callback_chat_id != chat_id:
                await callback.answer("验证来源不匹配", show_alert=True)
                return

        if not await can_manage_chat(callback.bot, settings, chat_id, callback.from_user.id):
            await callback.answer("只有群管理员可以确认此操作", show_alert=True)
            return

        text = _apply_verified_admin_action(
            action,
            settings=settings,
            db=db,
            chat_id=chat_id,
            title=_message_chat_title(callback.message),
            user_id=callback.from_user.id,
        )
        await callback.answer("已确认")
        if callback.message is not None and hasattr(callback.message, "edit_text"):
            try:
                await callback.message.edit_text(text)
                return
            except Exception:
                logger.debug("Failed to edit admin verification message", exc_info=True)
        await callback.bot.send_message(chat_id, text)

    @router.callback_query(F.data.startswith("admin_ban:"))
    async def on_admin_ban(callback: CallbackQuery) -> None:
        if not callback.data or not callback.from_user:
            return

        try:
            _, session_id_raw = callback.data.split(":", 1)
            session_id = int(session_id_raw)
        except ValueError:
            await callback.answer("操作数据无效", show_alert=False)
            return

        session = db.get_vote_session(session_id)
        if session is None:
            await callback.answer("投票不存在", show_alert=False)
            return
        if not await can_manage_chat(callback.bot, settings, session.chat_id, callback.from_user.id):
            await callback.answer("只有管理员可以跳过投票封禁", show_alert=True)
            return

        ok, text = await actions.admin_ban_vote_session(callback.bot, session_id, callback.from_user.id)
        await callback.answer(text, show_alert=not ok)
        if callback.message is not None and ok:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

    @router.callback_query(F.data.startswith("admin_release:"))
    async def on_admin_release(callback: CallbackQuery) -> None:
        if not callback.data or not callback.from_user:
            return

        try:
            _, session_id_raw = callback.data.split(":", 1)
            session_id = int(session_id_raw)
        except ValueError:
            await callback.answer("操作数据无效", show_alert=False)
            return

        session = db.get_vote_session(session_id)
        if session is None:
            await callback.answer("投票不存在", show_alert=False)
            return
        if not await can_manage_chat(callback.bot, settings, session.chat_id, callback.from_user.id):
            await callback.answer("只有管理员可以跳过投票放行", show_alert=True)
            return

        ok, text = await actions.admin_release_vote_session(
            callback.bot,
            session_id,
            callback.from_user.id,
        )
        await callback.answer(text, show_alert=not ok)
        if callback.message is not None and ok:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

    @router.callback_query(F.data.startswith("review_ban:"))
    async def on_review_ban(callback: CallbackQuery) -> None:
        await _handle_review_action(callback, "review_ban:", ban=True)

    @router.callback_query(F.data.startswith("review_keep:"))
    async def on_review_keep(callback: CallbackQuery) -> None:
        await _handle_review_action(callback, "review_keep:", ban=False)

    async def _handle_review_action(callback: CallbackQuery, prefix: str, *, ban: bool) -> None:
        if not callback.data or not callback.from_user:
            return

        try:
            session_id = int(callback.data[len(prefix):])
        except ValueError:
            await callback.answer("操作数据无效", show_alert=False)
            return

        session = db.get_vote_session(session_id)
        if session is None:
            await callback.answer("投票不存在", show_alert=False)
            return
        if not await can_manage_chat(callback.bot, settings, session.chat_id, callback.from_user.id):
            await callback.answer("只有该群组的管理员可以补审", show_alert=True)
            return

        if ban:
            ok, text = await actions.catchup_ban_vote_session(
                callback.bot, session_id, callback.from_user.id
            )
            result_text = f"已封禁：{session.suspect_user_id}" if ok else text
        else:
            ok, text = await actions.catchup_keep_vote_session(
                callback.bot, session_id, callback.from_user.id
            )
            result_text = "已维持放行" if ok else text

        await callback.answer(text, show_alert=not ok)
        if callback.message is not None and ok:
            try:
                await callback.message.edit_text(f"补审完成：{result_text}")
            except Exception:
                logger.debug("Failed to edit review card after action", exc_info=True)

    return router


def _chat_type(message: Message) -> str:
    return str(getattr(message.chat.type, "value", message.chat.type))


def _is_whitelisted_sender(db: Database, settings: Settings, user_id: int) -> bool:
    """A sender bypasses moderation if it is a built-in Telegram service account
    (e.g. 777000, the linked-channel auto-forwarder) or appears in the operator's
    whitelist (env WHITELISTED_USER_IDS or the whitelisted_users table)."""
    if user_id in TELEGRAM_SERVICE_USER_IDS:
        return True
    return db.is_user_whitelisted(user_id, settings.whitelisted_user_ids)


def _should_watch_edits(
    user_context: Any,
    sender_profile: Any,
    settings: Settings,
) -> bool:
    """Limit revision baselines to new or low-reputation human accounts."""
    if getattr(sender_profile, "is_bot", False):
        return False
    return bool(
        getattr(user_context, "messages_seen", 0) < EDIT_WATCH_MAX_MESSAGES
        or getattr(user_context, "reputation_score", 100)
        <= settings.low_reputation_threshold
    )


def _is_automatic_channel_forward(message: Message) -> bool:
    """True when Telegram itself forwarded a linked channel's post into the discussion
    group. These carry is_automatic_forward=True and a sender_chat for the source
    channel (distinct from the group's own chat id, which would be an anonymous admin).
    """
    if not getattr(message, "is_automatic_forward", False):
        return False
    sender_chat = getattr(message, "sender_chat", None)
    sender_chat_id = getattr(sender_chat, "id", None)
    if sender_chat_id is None:
        # is_automatic_forward without a sender_chat shouldn't happen, but if it does
        # the flag alone already tells us Telegram, not a member, posted this.
        return True
    chat_id = getattr(message.chat, "id", None)
    return chat_id is None or int(sender_chat_id) != int(chat_id)


def _is_anonymous_admin_message(message: Message) -> bool:
    sender_chat = getattr(message, "sender_chat", None)
    sender_chat_id = getattr(sender_chat, "id", None)
    chat_id = getattr(message.chat, "id", None)
    return (
        _chat_type(message) in {"group", "supergroup"}
        and sender_chat_id is not None
        and chat_id is not None
        and int(sender_chat_id) == int(chat_id)
    )


async def _ask_anonymous_admin_to_verify(message: Message, action: str) -> None:
    await message.answer(
        "匿名管理员身份无法直接校验，请点击按钮确认真实管理员身份。",
        reply_markup=_admin_verify_keyboard(action, message.chat.id),
    )


def _admin_verify_keyboard(action: str, chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="确认管理员身份",
                    callback_data=f"admin_verify:{action}:{chat_id}",
                )
            ]
        ]
    )


def _apply_verified_admin_action(
    action: str,
    *,
    settings: Settings,
    db: Database,
    chat_id: int,
    title: str | None,
    user_id: int,
) -> str:
    if action == "status":
        return _group_status_text(settings, db, chat_id)
    if action == "allow_chat":
        db.allow_chat(chat_id, title, user_id)
        return f"已允许当前群组使用机器人：<code>{chat_id}</code>"
    if action == "deny_chat":
        db.disallow_chat(chat_id)
        return f"已禁用当前群组：<code>{chat_id}</code>"
    return "验证操作无效"


def _group_status_text(settings: Settings, db: Database, chat_id: int) -> str:
    allowed = is_chat_allowed(settings, db, chat_id)
    return (
        f"群组：<code>{chat_id}</code>\n"
        f"允许使用：{'是' if allowed else '否'}\n"
        f"需要 allow：{'是' if settings.require_allowed_chat else '否'}"
    )


def _message_chat_id(message: Message | None) -> int | None:
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    return int(chat_id) if chat_id is not None else None


def _message_chat_title(message: Message | None) -> str | None:
    chat = getattr(message, "chat", None)
    title = getattr(chat, "title", None)
    return str(title) if title else None


def _parse_chat_id(value: str) -> int | None:
    return _parse_int(value)


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _parse_whitelist_target(
    message: Message,
    args: str | None,
) -> tuple[int | None, str | None]:
    """Resolve the whitelist target from a /whitelist command.

    Two forms:
      - `/whitelist <user_id> [note]` — explicit id, remaining text is the note.
      - reply to a user's message + `/whitelist [note]` — target is the replied-to
        sender (the ergonomic path, since raw user_ids are hard to get in Telegram).
    Returns (user_id, note); user_id is None when neither form yields a target.
    """
    reply = getattr(message, "reply_to_message", None)
    reply_user = getattr(reply, "from_user", None)
    reply_user_id = getattr(reply_user, "id", None)

    text = (args or "").strip()
    if reply_user_id is not None:
        # Reply form: the whole argument string (if any) is the note.
        note = text or None
        return int(reply_user_id), note

    if not text:
        return None, None
    parts = text.split(maxsplit=1)
    target_id = _parse_int(parts[0])
    if target_id is None:
        return None, None
    note = parts[1].strip() if len(parts) == 2 and parts[1].strip() else None
    return target_id, note


def _whitelist_add(
    db: Database,
    user_id: int,
    note: str | None,
    *,
    added_by: int | None,
) -> str:
    db.whitelist_user(user_id, note, added_by_user_id=added_by)
    note_text = f"（备注：{html.escape(note)}）" if note else ""
    return f"已加入白名单：<code>{user_id}</code>{note_text}"


def _whitelist_remove(db: Database, user_id: int) -> str:
    removed = db.unwhitelist_user(user_id)
    if removed:
        return f"已移出白名单：<code>{user_id}</code>"
    return f"该用户不在白名单表中：<code>{user_id}</code>（环境变量配置的 ID 不受影响）"


def _whitelist_list(db: Database, settings: Settings) -> str:
    lines: list[str] = ["全局白名单"]
    env_ids = settings.whitelisted_user_ids
    if env_ids:
        lines.append(
            "环境变量 WHITELISTED_USER_IDS：" + ", ".join(str(i) for i in env_ids)
        )
    rows = db.list_whitelisted_users()
    if not rows:
        lines.append("白名单表：暂无")
        return "\n".join(lines)
    lines.append("白名单表：")
    for row in rows[:50]:
        note = row.get("note")
        note_text = f" — {html.escape(str(note))}" if note else ""
        lines.append(f"- <code>{row['user_id']}</code>{note_text}")
    return "\n".join(lines)


def _new_chat_members(message: Message) -> tuple[Any, ...]:
    members = getattr(message, "new_chat_members", None)
    if members:
        return tuple(members)

    action = getattr(message, "action", None)
    action_users = getattr(action, "users", None)
    if not action_users:
        return ()

    users: list[Any] = []
    for user_id in action_users:
        try:
            users.append(SimpleNamespace(id=int(user_id), is_bot=False))
        except (TypeError, ValueError):
            continue
    return tuple(users)


def _feature_message_for_user(message: Message, user: Any) -> Any:
    return SimpleNamespace(
        message_id=getattr(message, "message_id", None),
        chat=getattr(message, "chat", None),
        from_user=user,
        text=getattr(message, "text", None),
        caption=getattr(message, "caption", None),
        entities=getattr(message, "entities", None),
        caption_entities=getattr(message, "caption_entities", None),
        link_preview_options=getattr(message, "link_preview_options", None),
        rich_message=getattr(message, "rich_message", None),
        reply_markup=getattr(message, "reply_markup", None),
    )


def _same_user_open_vote_repeat_decision(
    settings: Settings,
    db: Database,
    features: MessageFeatures,
) -> LocalDecision | None:
    if features.user_id is None:
        return None
    if features.sender_reputation >= settings.high_reputation_threshold:
        return None

    sessions = db.list_vote_sessions_for_user(
        features.chat_id,
        features.user_id,
        statuses=("open",),
    )
    # Hash equality only counts when the hash actually identifies content. The empty-
    # text hash collides for every empty/whitespace/emoji-only message; matching on it
    # would BAN any user who sends a sticker or photo without caption right after some
    # earlier empty-text spam session opened. Force a real-content match.
    feature_content = (
        features.content_hash if features.content_hash != _EMPTY_TEXT_HASH else None
    )
    feature_skeleton = (
        features.skeleton_hash if features.skeleton_hash != _EMPTY_TEXT_HASH else None
    )
    # Same defense for low-entropy skeletons. A user with an open vote session on a
    # bare URL ("https://x") must not auto-ban for sharing a different URL — the
    # skeleton match would happen, but the messages are unrelated.
    if feature_skeleton is not None and is_low_entropy_skeleton(features.skeleton):
        feature_skeleton = None
    matching_session_ids = [
        session.id
        for session in sessions
        if (
            (feature_content and session.content_hash == feature_content)
            or (feature_skeleton and session.skeleton_hash == feature_skeleton)
        )
    ]
    if not matching_session_ids:
        return None

    return LocalDecision(
        action=DecisionAction.BAN,
        reason="repeated_open_vote_message_same_user",
        confidence=0.97,
        should_call_llm=False,
        metadata={"open_vote_session_ids": matching_session_ids[:10]},
    )


def _repeat_decision(
    settings: Settings,
    db: Database,
    features: MessageFeatures,
) -> LocalDecision | None:
    if not features.links:
        return None
    if features.sender_reputation >= settings.high_reputation_threshold:
        return None
    # The empty-text hash collides across every empty/whitespace-normalized message.
    # Counting "distinct senders of the empty-skeleton hash" is meaningless — N new
    # users sending stickers in a row would trip the fast-ban window. Skip.
    if features.skeleton_hash == _EMPTY_TEXT_HASH:
        return None
    # Generic skeletons (<url>, <mention>, <w>, ...) collide across every URL-only or
    # mention-only message. Three different new users sharing three different URLs
    # within the 5-minute window must NOT auto-ban the fourth innocent link-sharer.
    if is_low_entropy_skeleton(features.skeleton):
        return None

    prior_senders = db.count_recent_skeleton_senders(
        features.skeleton_hash,
        settings.repeat_window_seconds,
        exclude_user_id=features.user_id,
    )
    if prior_senders + 1 < settings.repeat_min_distinct_senders:
        return None

    return LocalDecision(
        action=DecisionAction.BAN,
        reason="repeated_skeleton_across_senders",
        confidence=0.97,
        should_call_llm=False,
        metadata={"recent_distinct_senders": prior_senders + 1},
    )


def _merge_llm_decision(
    local_decision: LocalDecision,
    judgement: LLMJudgement,
    features: MessageFeatures,
    settings: Settings,
) -> LocalDecision:
    llm_decision = decision_from_llm(judgement, features, settings)
    if local_decision.reason == "message_personal_channel_crosscheck_needs_llm":
        metadata = dict(local_decision.metadata)
        metadata.update(llm_decision.metadata)
        return replace(llm_decision, metadata=metadata)
    if local_decision.action in {DecisionAction.ALLOW, DecisionAction.REVIEW}:
        return llm_decision

    if llm_decision.action in {DecisionAction.WITHDRAW_VOTE, DecisionAction.BAN}:
        return llm_decision

    metadata = dict(local_decision.metadata)
    metadata.update(
        {
            "llm_is_spam": judgement.is_spam,
            "llm_confidence": judgement.confidence,
            "llm_category": judgement.category,
            "llm_signal_phrases": list(judgement.signal_phrases),
        }
    )
    return LocalDecision(
        action=local_decision.action,
        reason=local_decision.reason,
        confidence=local_decision.confidence,
        should_call_llm=False,
        metadata=metadata,
    )


def _only_bot_mentions(text: str) -> tuple[str, ...]:
    """Return normalized usernames when text contains only one or more @...bot mentions."""

    match = _BOT_ONLY_MESSAGE_RE.fullmatch(text)
    if match is None:
        return ()
    # Keep order for audit metadata but collapse duplicates within one message.
    return tuple(
        dict.fromkeys(
            username.casefold()
            for username in _BOT_MENTION_RE.findall(match.group("mentions"))
        )
    )


def _decision_identifies_advertising_bot(decision: LocalDecision) -> bool:
    """Whether the bot message reached a final automatic advertising decision."""

    # A WITHDRAW_VOTE result is still awaiting human confirmation. Flagging its
    # caller now could turn a later released false positive into a direct user ban.
    # Vote/admin-confirmed bot ads are correlated in the action finalization path.
    return decision.action == DecisionAction.BAN


def _normal_message_reputation_reward(
    settings: Settings,
    decision: LocalDecision,
    *,
    update_type: str,
    is_edit: bool,
) -> float:
    """Reward only a new message that the LLM explicitly judged non-spam.

    Edits and join-service events must not be farmable reputation events. Likewise,
    a local fallback after an LLM failure/disablement is not affirmative evidence
    of normal behavior. Vote-confirmed releases keep their larger, existing
    ``ham_reputation_reward`` in the action layer.
    """

    if is_edit or update_type != "message" or decision.action != DecisionAction.ALLOW:
        return 0

    outcome = decision.metadata.get("llm_outcome")
    if not isinstance(outcome, dict):
        return 0
    if outcome.get("status") != LLMOutcomeStatus.OK.value or outcome.get("is_spam") is not False:
        return 0
    return max(0, settings.normal_message_reputation_reward)


def _annotate_with_llm_outcome(decision: LocalDecision, outcome: LLMOutcome) -> LocalDecision:
    """Attach the LLM hop outcome to decision metadata regardless of final action.

    This keeps notifications and the action_log honest: "review / 0%" with no LLM
    section used to mean any of {LLM disabled, all providers failed, LLM returned
    not_spam}. After this pass, the notification always shows what actually happened.
    """

    metadata = dict(decision.metadata)
    metadata["llm_outcome"] = {
        "status": outcome.status.value,
        "provider_count": outcome.provider_count,
        "error": outcome.error,
    }
    if outcome.judgement is not None:
        metadata["llm_outcome"].update(
            {
                "is_spam": outcome.judgement.is_spam,
                "confidence": outcome.judgement.confidence,
                "category": outcome.judgement.category,
                "signal_phrases": list(outcome.judgement.signal_phrases),
            }
        )
    return LocalDecision(
        action=decision.action,
        reason=decision.reason,
        confidence=decision.confidence,
        should_call_llm=False,
        metadata=metadata,
    )
