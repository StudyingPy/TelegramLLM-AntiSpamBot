from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from .actions import ModerationActions
from .config import Settings
from .db import Database
from .feedback import fingerprint_lookup_values, record_llm_spam_feedback
from .features import build_message_features
from .llm import LLMJudge, NullLLMJudge, decision_from_llm
from .models import DecisionAction, LLMJudgement, LocalDecision, MessageFeatures
from .og import fetch_og_for_features, should_fetch_og
from .profile import get_sender_profile
from .rules import RuleEngine


logger = logging.getLogger(__name__)


def create_router(settings: Settings, db: Database, llm: LLMJudge | None = None) -> Router:
    router = Router(name="moderation")
    rule_engine = RuleEngine(settings)
    llm_judge = llm or NullLLMJudge()
    actions = ModerationActions(settings, db)

    @router.message()
    async def on_message(message: Message) -> None:
        if not message.from_user or message.from_user.is_bot:
            return

        chat_type = getattr(message.chat.type, "value", str(message.chat.type))
        if chat_type not in {"group", "supergroup"}:
            return

        user_context = db.get_user_context(message.chat.id, message.from_user.id)
        sender_profile = await get_sender_profile(message.bot, db, message.from_user, settings)
        features = build_message_features(
            message,
            user_context=user_context,
            sender_profile=sender_profile,
            default_reputation=settings.default_reputation,
        )
        if should_fetch_og(features, settings):
            og_preview = await fetch_og_for_features(features, settings)
            if og_preview is not None:
                features.metadata["og_preview"] = og_preview.to_payload()

        fingerprint = db.get_strongest_fingerprint(fingerprint_lookup_values(features))
        if fingerprint is not None:
            db.record_fingerprint_hit(fingerprint.id)

        repeat_decision = _repeat_decision(settings, db, features)
        decision = repeat_decision or rule_engine.evaluate(features, fingerprint=fingerprint)

        if decision.should_call_llm:
            try:
                judgement = await llm_judge.judge(features)
            except Exception as exc:  # pragma: no cover - external integration hook.
                logger.warning("LLM judgement failed, using local fallback: %s", exc)
                judgement = None
            if judgement is not None:
                record_llm_spam_feedback(db, features, judgement, settings)
                decision = _merge_llm_decision(decision, judgement, features, settings)

        await actions.apply(message, features, decision)
        db.record_message_seen(features)
        db.record_observation(features)

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

    return router


def _repeat_decision(
    settings: Settings,
    db: Database,
    features: MessageFeatures,
) -> LocalDecision | None:
    if not features.links:
        return None
    if not (features.is_first_message or features.sender_reputation <= settings.low_reputation_threshold):
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
