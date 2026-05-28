import asyncio
import contextlib
import logging
import os
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    Update,
)
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, generate_latest
from sqlalchemy import text

try:
    from telethon import TelegramClient  # type: ignore[import-not-found]
    from telethon.sessions import StringSession  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - depends on optional runtime extra
    TelegramClient = None  # type: ignore[assignment]
    StringSession = None  # type: ignore[assignment]

from .admin import create_admin_router
from .bot.middlewares import DbSessionMiddleware, ServicesMiddleware
from .bot.router_admin import router as admin_router
from .bot.router_fun import router as fun_router
from .bot.router_games import router as games_router
from .bot.router_games_extra import router as games_extra_router
from .bot.router_interjector import router as interjector_router
from .bot.router_spy import router as spy_router
from .bot.router_triggers import router as triggers_router
from .infra.db import init_engine_and_sessionmaker, shutdown_engine
from .infra.redis import init_redis, shutdown_redis
from .infra.scheduler import get_scheduler
from .services.app_config import AppConfigService
from .services.context import ContextService
from .services.dice_game import DiceGameService
from .services.games.akinator import AkinatorService
from .services.games.rapbattle import RapbattleService
from .services.games.storychain import StorychainService
from .services.games.wordchain import WordchainService
from .services.guess_game import GuessGameService
from .services.interjector import InterjectorService
from .services.monthly_champion import MonthlyChampionService
from .services.network_monitor import PROBE_INTERVAL_SECONDS, NetworkMonitorService
from .services.persona import BASE_STYLE_DATA, StylePromptService
from .services.quick_games import QuickGameService
from .services.quotebook import QuotebookService
from .services.reactions import ReactionService
from .services.release_broadcast import ReleaseBroadcaster
from .services.roast import RoastService
from .services.roulette import RouletteService
from .services.settings import SettingsService
from .services.ship import ShipService
from .services.spontaneity import SpontaneityPolicy
from .services.spy.config import SpyConfig
from .services.spy.polling_worker import SpyPollingWorker
from .services.spy.readers.telethon import TelethonChannelReader
from .services.spy.source_service import SpySourceService
from .services.spy.subscription_service import AiogramChatAdminChecker, SpySubscriptionService
from .services.spy.telegram_delivery import SpyTelegramDeliveryService
from .services.spy.types import SpyChannelInfo, SpyPostPayload
from .services.usage_limits import UsageLimiter
from .services.user_memory import UserMemoryService
from .utils.logging import ensure_trace_level
from .utils.proxy import get_proxy_url
from .utils.version import get_version

# Metrics
registry = CollectorRegistry()
METRIC_UPDATES = Counter("tg_updates_total", "Telegram updates received", registry=registry)
METRIC_MESSAGES = Counter("bot_messages_total", "Messages sent by bot", registry=registry)


def setup_logging():
    ensure_trace_level()
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = logging._nameToLevel.get(level_name, logging.INFO)
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


class DisabledSpyReader:
    async def resolve_channel(self, ref: str) -> SpyChannelInfo:
        raise RuntimeError("Gremlin Spy MTProto reader is not configured")

    async def fetch_latest_posts(self, username: str, *, limit: int) -> list[SpyPostPayload]:
        raise RuntimeError("Gremlin Spy MTProto reader is not configured")


# Infra
engine, async_sessionmaker = init_engine_and_sessionmaker()
redis = init_redis()

# Aiogram
_proxy_url = get_proxy_url(prefer_plain=True)
if _proxy_url:
    bot_session = AiohttpSession(proxy=_proxy_url)
    bot = Bot(token=BOT_TOKEN, session=bot_session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
else:
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Services
settings_service = SettingsService(async_sessionmaker, redis)
context_service = ContextService()
app_config_service = AppConfigService(async_sessionmaker, redis)
persona_service = StylePromptService(async_sessionmaker, redis, BASE_STYLE_DATA)
user_memory_service = UserMemoryService(async_sessionmaker)
usage_limits_service = UsageLimiter(redis, timezone=ZoneInfo("Europe/Moscow"))
spontaneity_policy = SpontaneityPolicy(
    redis=redis,
    app_config=app_config_service,
    settings=settings_service,
)
network_monitor_service = NetworkMonitorService()
reaction_service = ReactionService(
    bot=bot,
    sessionmaker=async_sessionmaker,
    usage_limits=usage_limits_service,
    memory=user_memory_service,
)
roulette_service = RouletteService(
    bot=bot,
    sessionmaker=async_sessionmaker,
    settings=settings_service,
    app_config=app_config_service,
    context=context_service,
    personas=persona_service,
    memory=user_memory_service,
)
guess_game_service = GuessGameService(
    sessionmaker=async_sessionmaker,
    app_config=app_config_service,
    bot=bot,
)
dice_game_service = DiceGameService(sessionmaker=async_sessionmaker)
monthly_champion_service = MonthlyChampionService(
    sessionmaker=async_sessionmaker,
    bot=bot,
    roulette=roulette_service,
    settings=settings_service,
    app_config=app_config_service,
)
roast_service = RoastService(
    sessionmaker=async_sessionmaker,
    bot=bot,
    personas=persona_service,
    settings=settings_service,
    app_config=app_config_service,
)
ship_service = ShipService(
    sessionmaker=async_sessionmaker,
    bot=bot,
    settings=settings_service,
    app_config=app_config_service,
    personas=persona_service,
)
quotebook_service = QuotebookService(
    sessionmaker=async_sessionmaker,
    bot=bot,
    settings=settings_service,
    app_config=app_config_service,
)
quick_games_service = QuickGameService(
    sessionmaker=async_sessionmaker,
    bot=bot,
    personas=persona_service,
    settings=settings_service,
    app_config=app_config_service,
)
akinator_service = AkinatorService(
    sessionmaker=async_sessionmaker, bot=bot, app_config=app_config_service,
)
wordchain_service = WordchainService(sessionmaker=async_sessionmaker, bot=bot)
rapbattle_service = RapbattleService(
    sessionmaker=async_sessionmaker,
    bot=bot,
    personas=persona_service,
    settings=settings_service,
    app_config=app_config_service,
)
storychain_service = StorychainService(
    sessionmaker=async_sessionmaker,
    bot=bot,
    personas=persona_service,
    settings=settings_service,
    app_config=app_config_service,
)
spy_config = SpyConfig.from_env()
spy_telegram_client = None
if (
    spy_config.enabled
    and spy_config.telegram_api_id
    and spy_config.telegram_api_hash
    and TelegramClient is not None
    and StringSession is not None
):
    spy_session = (
        StringSession(spy_config.telegram_session)
        if spy_config.telegram_session
        else spy_config.telegram_session_file
    )
    spy_telegram_client = TelegramClient(
        spy_session,
        spy_config.telegram_api_id,
        spy_config.telegram_api_hash,
    )
    spy_reader = TelethonChannelReader(spy_telegram_client)
else:
    spy_reader = DisabledSpyReader()
spy_source_service = SpySourceService(async_sessionmaker, spy_reader)
spy_subscription_service = SpySubscriptionService(
    async_sessionmaker,
    spy_source_service,
    AiogramChatAdminChecker(bot),
)
spy_polling_worker = SpyPollingWorker(
    async_sessionmaker,
    spy_reader,
    fetch_limit=spy_config.batch_limit,
)
spy_delivery_service = SpyTelegramDeliveryService(async_sessionmaker, bot)

# Routers — order matters: command routers MUST be registered before triggers_router,
# which has a catch-all @router.message(F.text) that consumes any text message.
dp.include_router(admin_router)
dp.include_router(spy_router)
dp.include_router(fun_router)
dp.include_router(games_router)
dp.include_router(games_extra_router)
dp.include_router(triggers_router)
dp.include_router(interjector_router)

# Middlewares
dp.message.middleware(DbSessionMiddleware(async_sessionmaker))
dp.callback_query.middleware(DbSessionMiddleware(async_sessionmaker))


interjector_service = InterjectorService(
    bot=bot,
    settings=settings_service,
    app_config=app_config_service,
    context=context_service,
    sessionmaker=async_sessionmaker,
    redis=redis,
    personas=persona_service,
    usage_limits=usage_limits_service,
    memory=user_memory_service,
    policy=spontaneity_policy,
)
dp.update.middleware(
    ServicesMiddleware(
        settings=settings_service,
        context=context_service,
        interjector=interjector_service,
        personas=persona_service,
        app_config=app_config_service,
        reactions=reaction_service,
        roulette=roulette_service,
        usage_limits=usage_limits_service,
        memory=user_memory_service,
        policy=spontaneity_policy,
        guess_game=guess_game_service,
        dice_game=dice_game_service,
        monthly_champion=monthly_champion_service,
        roast=roast_service,
        ship=ship_service,
        quotebook=quotebook_service,
        quick_games=quick_games_service,
        akinator=akinator_service,
        wordchain=wordchain_service,
        rapbattle=rapbattle_service,
        storychain=storychain_service,
        spy_subscriptions=spy_subscription_service,
    )
)
scheduler = get_scheduler()


app = FastAPI(title="Gremlin Bot", version=get_version())

release_broadcaster = ReleaseBroadcaster(
    bot=bot,
    sessionmaker=async_sessionmaker,
    app_config=app_config_service,
)
app.include_router(
    create_admin_router(
        async_sessionmaker,
        settings_service,
        persona_service,
        app_config_service,
        bot,
        roulette_service,
        user_memory_service,
    )
)
app.state.polling_task = None
app.state.scheduler = None
app.state.webhook_tasks = set()


def _track_background_task(task: asyncio.Task[None], *, label: str = "Background task") -> None:
    app.state.webhook_tasks.add(task)

    def _done(done_task: asyncio.Task[None]) -> None:
        app.state.webhook_tasks.discard(done_task)
        with contextlib.suppress(asyncio.CancelledError):
            exc = done_task.exception()
            if exc is not None:
                logger.error(
                    "%s failed",
                    label,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

    task.add_done_callback(_done)


async def _process_update_in_background(update_obj: Update) -> None:
    try:
        await dp.feed_update(bot, update_obj)
    except LookupError as exc:
        logger.debug("Ignoring unhandled update: %s", exc)
    except Exception:
        logger.exception("Unhandled exception while processing Telegram update")


async def _run_spy_tick() -> None:
    poll_result = await spy_polling_worker.tick()
    sent_count = await spy_delivery_service.send_pending_deliveries(limit=50)
    if poll_result.sources_checked or poll_result.posts_created or poll_result.deliveries_created or sent_count:
        logger.info(
            "Gremlin Spy tick: sources=%s posts=%s deliveries=%s sent=%s errors=%s",
            poll_result.sources_checked,
            poll_result.posts_created,
            poll_result.deliveries_created,
            sent_count,
            poll_result.errors,
        )


@app.on_event("startup")
async def on_startup():
    if spy_telegram_client is not None:
        await spy_telegram_client.start()
        logger.info("Gremlin Spy MTProto reader started")
    elif spy_config.enabled:
        logger.warning("Gremlin Spy enabled but Telegram API credentials are not configured; public channel subscriptions are unavailable")

    await persona_service.ensure_defaults()
    await configure_bot_commands(bot)
    await _recover_stale_game_rounds()

    if PUBLIC_BASE_URL and not USE_POLLING:
        # Configure webhook with secret header. allowed_updates must be passed explicitly:
        # if omitted, Telegram reuses the previous setting — which historically excluded
        # poll_answer and silently broke /guess winner detection.
        webhook_url = f"{PUBLIC_BASE_URL.rstrip('/')}/webhook/telegram"
        await bot.set_webhook(
            url=webhook_url,
            secret_token=TELEGRAM_SECRET_TOKEN or None,
            drop_pending_updates=True,
            allowed_updates=dp.resolve_used_update_types(),
        )
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
    scheduler.add_job(
        roulette_service.run_auto_roll,
        "cron",
        hour=10,
        minute=0,
        timezone=ZoneInfo("Europe/Moscow"),
        id="roulette_auto_roll",
        replace_existing=True,
    )
    scheduler.add_job(
        network_monitor_service.probe_once,
        "interval",
        seconds=PROBE_INTERVAL_SECONDS,
        id="network_probe",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        monthly_champion_service.run_monthly_summary,
        "cron",
        day=1,
        hour=12,
        minute=0,
        timezone=ZoneInfo("Europe/Moscow"),
        id="monthly_champion_tick",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        quotebook_service.tick_all_chats,
        "cron",
        day_of_week="sun",
        hour=20,
        minute=0,
        timezone=ZoneInfo("Europe/Moscow"),
        id="quotebook_tick",
        replace_existing=True,
        max_instances=1,
    )
    if spy_config.enabled:
        scheduler.add_job(
            _run_spy_tick,
            "interval",
            seconds=max(30, spy_config.poll_seconds),
            id="spy_tick",
            replace_existing=True,
            max_instances=1,
        )
    app.state.scheduler = scheduler
    _track_background_task(asyncio.create_task(network_monitor_service.probe_once()), label="Initial network probe")
    _track_background_task(
        asyncio.create_task(release_broadcaster.broadcast_if_new_version()),
        label="Release broadcast",
    )
    _track_background_task(
        asyncio.create_task(monthly_champion_service.catch_up_if_needed()),
        label="Monthly champion catch-up",
    )
    _track_background_task(
        asyncio.create_task(quotebook_service.catch_up_stale_open_rounds()),
        label="Quotebook catch-up",
    )


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

    tasks = list(getattr(app.state, "webhook_tasks", set()))
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task

    if spy_telegram_client is not None:
        await spy_telegram_client.disconnect()

    await shutdown_redis(redis)
    await shutdown_engine(engine)


@app.get("/health")
async def health():
    checks: dict[str, object] = {}

    db_ok = True
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        db_ok = False
        checks["db"] = {"ok": False, "error": str(exc)}
    else:
        checks["db"] = {"ok": True}

    redis_ok = True
    try:
        await redis.ping()
    except Exception as exc:
        redis_ok = False
        checks["redis"] = {"ok": False, "error": str(exc)}
    else:
        checks["redis"] = {"ok": True}

    proxy_state = network_monitor_service.snapshot()
    checks["network"] = proxy_state

    status = "ok"
    if not db_ok or not redis_ok:
        status = "fail"
    elif proxy_state.get("enabled") and proxy_state.get("ok") is False:
        status = "degraded"

    payload = {"status": status, "checks": checks}
    status_code = 503 if status == "fail" else 200
    return JSONResponse(payload, status_code=status_code)


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
    _track_background_task(asyncio.create_task(_process_update_in_background(update_obj)), label="Telegram update")
    return JSONResponse({"ok": True})


# Expose a small helper for handlers that want to increment metrics
def inc_messages():
    METRIC_MESSAGES.inc()


async def _recover_stale_game_rounds() -> None:
    """Sweep open game rounds left dangling by an unexpected bot restart.

    Per-process orchestration (asyncio.create_task timers) doesn't survive a
    restart, so any LOBBY/ACTIVE/VOTING/GENERATING/FINALISING row whose timer
    is gone would block new rounds via the partial unique indexes. Each
    service decides its own staleness threshold.
    """
    services = (
        ("storychain", storychain_service),
        ("wordchain", wordchain_service),
        ("akinator", akinator_service),
        ("rapbattle", rapbattle_service),
    )
    for name, svc in services:
        try:
            recovered = await svc.recover_stale()
            if recovered:
                logger.info("recover_stale.%s expired=%s", name, recovered)
        except Exception:
            logger.exception("recover_stale.%s failed", name)


async def configure_bot_commands(bot: Bot) -> None:
    # Convention (CHANGELOG v0.12.4): only /games is exposed in the autocomplete
    # popup. All individual game commands (/dice, /guess, /ship, /truth,
    # /wisdom, /akinator, /wordchain, /rapbattle, /storychain) remain invokable
    # but are reachable through the /games menu to keep the suggestion list short.
    commands = [
        BotCommand(command="settings", description="Панель настроек"),
        BotCommand(command="relationships", description="Отношения к участникам"),
        BotCommand(command="roll", description="Запустить рулетку"),
        BotCommand(command="rollstats_montly", description="Статистика рулетки за месяц"),
        BotCommand(command="rollstats_total", description="Статистика рулетки за всё время"),
        BotCommand(command="games", description="Меню игр (кости, угадайка, акинатор и др.)"),
        BotCommand(command="summary", description="Сводка обсуждения"),
        BotCommand(command="spy_list", description="Gremlin Spy источники"),
        BotCommand(command="reg", description="Зарегистрироваться в рулетке"),
        BotCommand(command="unreg", description="Выйти из рулетки"),
    ]

    await bot.set_my_commands(commands)
    await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
