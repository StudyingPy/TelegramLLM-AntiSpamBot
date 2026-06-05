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
    dispatcher.include_router(create_router(settings, db, llm=create_llm_judge(settings)))
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="显示帮助"),
            BotCommand(command="help", description="显示帮助"),
            BotCommand(command="status", description="查看机器人状态"),
            BotCommand(command="allow_chat", description="允许当前群组使用机器人"),
            BotCommand(command="deny_chat", description="禁用当前群组"),
        ]
    )
    vote_sweeper = asyncio.create_task(
        _sweep_expired_votes(settings, bot, ModerationActions(settings, db))
    )

    try:
        await dispatcher.start_polling(bot)
    finally:
        vote_sweeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await vote_sweeper
        db.close()
        await bot.session.close()


def main() -> None:
    asyncio.run(run())


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
