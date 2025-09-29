import asyncio
import contextlib
import json
import logging
import os
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, generate_latest

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    Update,
)

from .bot.router_admin import router as admin_router
from .bot.router_triggers import router as triggers_router
from .bot.router_interjector import router as interjector_router
from .bot.middlewares import DbSessionMiddleware, ServicesMiddleware
from .admin import create_admin_router

from .infra.db import init_engine_and_sessionmaker, shutdown_engine
from .infra.redis import init_redis, shutdown_redis
from .infra.scheduler import get_scheduler
from .services.context import ContextService
from .services.interjector import InterjectorService
from .services.settings import SettingsService
from .services.persona import StylePromptService, BASE_STYLE_DATA


# Metrics
registry = CollectorRegistry()
METRIC_UPDATES = Counter("tg_updates_total", "Telegram updates received", registry=registry)
METRIC_MESSAGES = Counter("bot_messages_total", "Messages sent by bot", registry=registry)


def setup_logging():
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


setup_logging()
logger = logging.getLogger("app")


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    logger.warning("BOT_TOKEN not set. The bot won't start properly without it.")

TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
USE_POLLING = os.getenv("USE_POLLING", "0") == "1"
def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


PORT = _env_int("PORT", 8080)
INTERJECT_TICK_SECONDS = _env_int("INTERJECT_TICK_SECONDS", 30)

# Infra
engine, async_sessionmaker = init_engine_and_sessionmaker()
redis = init_redis()

# Services
settings_service = SettingsService(async_sessionmaker, redis)
context_service = ContextService()
persona_service = StylePromptService(async_sessionmaker, redis, BASE_STYLE_DATA)

# Aiogram
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Routers
dp.include_router(admin_router)
dp.include_router(triggers_router)
dp.include_router(interjector_router)

# Middlewares
dp.message.middleware(DbSessionMiddleware(async_sessionmaker))
dp.callback_query.middleware(DbSessionMiddleware(async_sessionmaker))


interjector_service = InterjectorService(
    bot=bot,
    settings=settings_service,
    context=context_service,
    sessionmaker=async_sessionmaker,
    redis=redis,
    personas=persona_service,
)
dp.update.middleware(ServicesMiddleware(settings_service, context_service, interjector_service, persona_service))
scheduler = get_scheduler()


app = FastAPI(title="Gremlin Bot", version="0.1.0")
app.include_router(create_admin_router(async_sessionmaker, settings_service, persona_service))
app.state.polling_task = None
app.state.scheduler = None


@app.on_event("startup")
async def on_startup():
    await persona_service.ensure_defaults()
    await configure_bot_commands(bot)

    if PUBLIC_BASE_URL and not USE_POLLING:
        # Configure webhook with secret header
        webhook_url = f"{PUBLIC_BASE_URL.rstrip('/')}/webhook/telegram"
        await bot.set_webhook(url=webhook_url, secret_token=TELEGRAM_SECRET_TOKEN or None, drop_pending_updates=True)
        logger.info("Webhook set to %s", webhook_url)
    else:
        # Run polling in background for development
        async def _polling():
            logger.info("Starting polling mode")
            while True:
                try:
                    await bot.delete_webhook(drop_pending_updates=True)
                    await dp.start_polling(bot, handle_signals=False)
                    break
                except asyncio.CancelledError:
                    logger.info("Polling task cancelled")
                    raise
                except Exception:
                    logger.exception("Polling crashed; retrying через 5с")
                    await asyncio.sleep(5)

        task = asyncio.create_task(_polling())
        app.state.polling_task = task

    scheduler.start()
    scheduler.add_job(
        interjector_service.run_idle_checks,
        "interval",
        seconds=max(30, INTERJECT_TICK_SECONDS),
        id="interjector_tick",
        replace_existing=True,
        max_instances=1,
    )
    app.state.scheduler = scheduler


@app.on_event("shutdown")
async def on_shutdown():
    task = getattr(app.state, "polling_task", None)
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    sched = getattr(app.state, "scheduler", None)
    if sched:
        sched.shutdown(wait=False)

    await shutdown_redis(redis)
    await shutdown_engine(engine)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    data = generate_latest(registry)
    return PlainTextResponse(content=data.decode(), media_type=CONTENT_TYPE_LATEST)


def verify_telegram_secret(header_value: Optional[str]):
    if TELEGRAM_SECRET_TOKEN and header_value != TELEGRAM_SECRET_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid secret token")


@app.post("/webhook/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
):
    verify_telegram_secret(x_telegram_bot_api_secret_token)
    payload = await request.json()
    METRIC_UPDATES.inc()
    update_obj = Update.model_validate(payload)
    await dp.feed_update(bot, update_obj)
    return JSONResponse({"ok": True})


# Expose a small helper for handlers that want to increment metrics
def inc_messages():
    METRIC_MESSAGES.inc()


async def configure_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="settings", description="Панель настроек"),
    ]

    await bot.set_my_commands(commands)
    await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
