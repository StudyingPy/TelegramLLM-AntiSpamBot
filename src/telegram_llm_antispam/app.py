from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .actions import ModerationActions
from .config import Settings
from .db import Database
from .handlers import create_router
from .llm import create_llm_judge
from .jev_observation import create_jev_observer
from .fingerprint_audit import set_review_menu_button, sweep_fingerprints
from .logging_config import configure_logging


logger = logging.getLogger(__name__)


async def run() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)

    if not settings.bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    db = Database.from_settings(settings)
    db.connect()
    db.migrate()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    judge = create_llm_judge(settings)
    jev_observer = create_jev_observer(settings)
    dispatcher.include_router(create_router(settings, db, llm=judge, jev_observer=jev_observer))
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="显示帮助"),
            BotCommand(command="help", description="显示帮助"),
            BotCommand(command="status", description="查看机器人状态"),
            BotCommand(command="allow_chat", description="允许当前群组使用机器人"),
            BotCommand(command="deny_chat", description="禁用当前群组"),
            BotCommand(command="whitelist", description="加入全局白名单（仅全局管理员）"),
            BotCommand(command="unwhitelist", description="移出全局白名单（仅全局管理员）"),
            BotCommand(command="list_whitelist", description="查看全局白名单（仅全局管理员）"),
            BotCommand(command="fingerprints", description="指纹复核（仅指定复核人）"),
        ]
    )
    vote_sweeper = asyncio.create_task(
        _sweep_expired_votes(settings, bot, ModerationActions(settings, db))
    )
    fingerprint_sweeper = asyncio.create_task(sweep_fingerprints(settings, bot, db, judge))
    webui_server = None

    try:
        webui_server = await _start_webui(settings, bot, db)
        await dispatcher.start_polling(bot)
    finally:
        for task in (vote_sweeper, fingerprint_sweeper):
            task.cancel()
        for task in (vote_sweeper, fingerprint_sweeper):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if webui_server is not None:
            # Stop accepting WebUI requests before the shared database closes.
            await webui_server.close()
        db.close()
        if jev_observer is not None:
            jev_observer.close()
        await bot.session.close()


def main() -> None:
    asyncio.run(run())


async def _start_webui(settings: Settings, bot: Bot, db: Database):
    """Start the optional fingerprint review Mini App; never blocks bot startup."""
    if not settings.webui_enabled:
        return None
    try:
        # Imported lazily so deployments without the WebUI never load the HTTP layer.
        from .webui.server import start_webui

        server = await start_webui(settings, db)
    except Exception:
        logger.exception("Fingerprint review WebUI failed to start; bot continues without it")
        return None
    if server is None:
        return None
    if settings.webui_public_url is None:
        logger.warning(
            "WEBUI_PUBLIC_URL is unset or not https://; Telegram entry buttons are disabled"
        )
    elif await set_review_menu_button(bot, settings):
        logger.info("Fingerprint review menu button set for reviewer chat")
    return server


async def _sweep_expired_votes(
    settings: Settings,
    bot: Bot,
    actions: ModerationActions,
) -> None:
    interval = max(5, settings.vote_sweep_interval_seconds)
    while True:
        try:
            expired_count = await actions.expire_due_vote_sessions(bot)
            if expired_count:
                logger.info("Expired %s vote session(s)", expired_count)
        except Exception as exc:  # pragma: no cover - background resilience.
            logger.warning("Failed to sweep expired vote sessions: %s", exc)
        await asyncio.sleep(interval)
