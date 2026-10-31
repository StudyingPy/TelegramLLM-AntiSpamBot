"""aiohttp adapter and lifecycle for the embedded fingerprint review WebUI.

The WebUI runs on the bot's own asyncio loop and shares its ``Database`` instance (and
therefore its lock) with the polling handlers, so bot callbacks and WebUI actions are
serialized in-process; ``BEGIN IMMEDIATE`` in the store also guards against other
processes. aiohttp is already installed as an aiogram dependency.

It listens on plain HTTP (default 127.0.0.1:8088) and must sit behind an HTTPS reverse
proxy: Telegram only opens Mini Apps over HTTPS. A failure to bind is logged and the
bot keeps polling without the WebUI.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from .api import MAX_BODY_BYTES, ApiRequest, WebUIApi
from .auth import SessionStore


logger = logging.getLogger(__name__)


def build_api(settings: Any, db: Any) -> WebUIApi:
    return WebUIApi(
        db=db,
        bot_token=settings.bot_token,
        reviewer_user_id=settings.fingerprint_review_user_id,
        init_data_max_age_seconds=settings.webui_init_data_max_age_seconds,
        sessions=SessionStore(idle_ttl_seconds=settings.webui_session_ttl_seconds),
    )


def create_app(api: WebUIApi) -> web.Application:
    async def handle(request: web.Request) -> web.StreamResponse:
        try:
            body = await request.read()
        except web.HTTPRequestEntityTooLarge:
            response = api.error_response(413, "payload_too_large")
        else:
            response = api.handle(ApiRequest(
                method=request.method,
                path=request.path,
                query_string=request.query_string,
                headers={key.lower(): value for key, value in request.headers.items()},
                body=body,
            ))
        return web.Response(status=response.status, body=response.body,
                            headers=response.headers)

    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app.router.add_route("*", "/{tail:.*}", handle)
    return app


class WebUIServer:
    def __init__(self, runner: web.AppRunner, url: str) -> None:
        self._runner = runner
        self.url = url

    async def close(self) -> None:
        await self._runner.cleanup()
        logger.info("Fingerprint review WebUI stopped")


async def start_webui(settings: Any, db: Any) -> WebUIServer | None:
    """Start the WebUI if enabled. Returns None (and logs) instead of raising."""
    if not settings.webui_enabled:
        return None
    if not settings.bot_token:
        logger.error("WebUI disabled: TELEGRAM_BOT_TOKEN is required to verify initData")
        return None
    runner = web.AppRunner(create_app(build_api(settings, db)), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.webui_host, settings.webui_port)
    try:
        await site.start()
    except OSError as exc:
        logger.error("WebUI failed to listen on %s:%s: %s; bot continues without it",
                     settings.webui_host, settings.webui_port, exc)
        await runner.cleanup()
        return None
    url = f"http://{settings.webui_host}:{settings.webui_port}/"
    logger.info("Fingerprint review WebUI listening on %s (public URL: %s)", url,
                settings.webui_public_url or "not configured")
    return WebUIServer(runner, url)
