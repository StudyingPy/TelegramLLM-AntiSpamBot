from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from telegram_llm_antispam.config import Settings
from telegram_llm_antispam.db import Database
from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.models import ActionResult, DecisionAction, LocalDecision, UserContext
from telegram_llm_antispam.notifications import notify_admins, update_vote_notifications
from test_llm import _settings


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, object]] = []
        self.edits: list[tuple[int, int, str, object]] = []
        self.next_message_id = 100

    async def send_message(self, user_id: int, text: str, reply_markup=None) -> None:
        self.messages.append((user_id, text, reply_markup))
        self.next_message_id += 1
        return SimpleNamespace(message_id=self.next_message_id)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup=None,
    ) -> None:
        self.edits.append((chat_id, message_id, text, reply_markup))


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "bot.db")
    db.connect()
    db.migrate()
    return db


def _settings_with_admins() -> Settings:
    settings = _settings()
    return Settings(
        bot_token=settings.bot_token,
        database_path=Path(":memory:"),
        log_level=settings.log_level,
        admin_user_ids=(100,),
        admin_notify_user_ids=(101,),
        allowed_chat_ids=(),
        require_allowed_chat=True,
        whitelist_domains=settings.whitelist_domains,
        vote_min_confirmations=settings.vote_min_confirmations,
        vote_timeout_seconds=settings.vote_timeout_seconds,
        vote_sweep_interval_seconds=settings.vote_sweep_interval_seconds,
        low_reputation_threshold=settings.low_reputation_threshold,
        high_reputation_threshold=settings.high_reputation_threshold,
        reputation_ban_threshold=settings.reputation_ban_threshold,
        default_reputation=settings.default_reputation,
        spam_reputation_penalty=settings.spam_reputation_penalty,
        ham_reputation_reward=settings.ham_reputation_reward,
        repeat_window_seconds=settings.repeat_window_seconds,
        repeat_min_distinct_senders=settings.repeat_min_distinct_senders,
        fingerprint_review_weight=settings.fingerprint_review_weight,
        fingerprint_ban_weight=settings.fingerprint_ban_weight,
        llm_fingerprint_initial_weight=settings.llm_fingerprint_initial_weight,
        vote_confirmed_fingerprint_weight=settings.vote_confirmed_fingerprint_weight,
        fingerprint_false_positive_penalty=settings.fingerprint_false_positive_penalty,
        llm_review_threshold=settings.llm_review_threshold,
        llm_ban_threshold=settings.llm_ban_threshold,
        newapi_base_url=settings.newapi_base_url,
        newapi_api_key=settings.newapi_api_key,
        newapi_model=settings.newapi_model,
        newapi_timeout_seconds=settings.newapi_timeout_seconds,
        newapi_temperature=settings.newapi_temperature,
        newapi_max_tokens=settings.newapi_max_tokens,
        preview_punctuation_confidence=settings.preview_punctuation_confidence,
        new_user_link_confidence=settings.new_user_link_confidence,
        og_fetch_enabled=settings.og_fetch_enabled,
        og_short_text_max_chars=settings.og_short_text_max_chars,
        og_fetch_timeout_seconds=settings.og_fetch_timeout_seconds,
        og_fetch_max_bytes=settings.og_fetch_max_bytes,
        og_fetch_max_text_chars=settings.og_fetch_max_text_chars,
        og_fetch_max_redirects=settings.og_fetch_max_redirects,
        profile_bio_fetch_enabled=settings.profile_bio_fetch_enabled,
        profile_bio_cache_ttl_seconds=settings.profile_bio_cache_ttl_seconds,
    )


def test_notify_admins_sends_one_combined_record_with_admin_ban_button(tmp_path):
    db = _db(tmp_path)
    bot = FakeBot()
    try:
        message = SimpleNamespace(
            message_id=7,
            chat=SimpleNamespace(id=-100123),
            from_user=SimpleNamespace(id=42),
            text=". https://spam.example",
        )
        context = UserContext(chat_id=-100123, user_id=42, reputation_score=20, messages_seen=0)
        features = build_message_features(message, context)
        features.metadata["sender_profile"] = {
            "username": "promo_agent",
            "display_name": "Promo Agent",
            "bio": "看片加群",
        }
        features.metadata["og_preview"] = {"title": "CRTV成人版", "description": "看片就选择CRTV"}
        decision = LocalDecision(
            DecisionAction.WITHDRAW_VOTE,
            "llm_spam",
            0.91,
            metadata={"category": "porn", "signal_phrases": ["看片"]},
        )
        result = ActionResult(action_log_id=3, vote_session_id=5, deleted=False, banned=False)

        asyncio.run(notify_admins(bot, db, _settings_with_admins(), features, decision, result))

        assert len(bot.messages) == 1
        user_id, text, reply_markup = bot.messages[0]
        assert user_id == 101
        assert "反广告处理记录" in text
        assert "触发：<code>llm_spam</code>" in text
        assert "处理：<b>withdraw_vote</b> / 91%" in text
        assert "删除：否 封禁：否" in text
        assert "日志：<code>3</code>" in text
        assert "投票会话：<code>5</code>" in text
        assert "信号：看片" in text
        assert "用户资料：Promo Agent @promo_agent" in text
        assert "OG：CRTV成人版 / 看片就选择CRTV" in text
        assert reply_markup is not None
        assert reply_markup.inline_keyboard[0][0].callback_data == "admin_ban:5"

        notifications = db.list_admin_notifications(5)
        assert len(notifications) == 1
        assert notifications[0].notify_user_id == 101
        assert notifications[0].message_id == 101
    finally:
        db.close()


def test_notify_admins_skips_allowed_messages(tmp_path):
    db = _db(tmp_path)
    bot = FakeBot()
    try:
        message = SimpleNamespace(
            message_id=7,
            chat=SimpleNamespace(id=-100123),
            from_user=SimpleNamespace(id=42),
            text="hello",
        )
        context = UserContext(chat_id=-100123, user_id=42, reputation_score=50, messages_seen=1)
        features = build_message_features(message, context)
        decision = LocalDecision(DecisionAction.ALLOW, "llm_not_spam", 0.99)

        asyncio.run(notify_admins(bot, db, _settings_with_admins(), features, decision, ActionResult()))

        assert bot.messages == []
    finally:
        db.close()


def test_update_vote_notifications_edits_existing_admin_record(tmp_path):
    db = _db(tmp_path)
    bot = FakeBot()
    try:
        db.record_admin_notification(
            vote_session_id=5,
            action_log_id=3,
            notify_user_id=101,
            message_id=202,
            base_text="反广告处理记录",
        )

        asyncio.run(
            update_vote_notifications(
                bot,
                db,
                5,
                "投票中\n投票：广告 1 / 放行 0\n投票记录：\n- 42: 广告",
                is_open=True,
            )
        )

        assert len(bot.edits) == 1
        chat_id, message_id, text, reply_markup = bot.edits[0]
        assert chat_id == 101
        assert message_id == 202
        assert "实时状态" in text
        assert "广告 1 / 放行 0" in text
        assert reply_markup is not None
    finally:
        db.close()
