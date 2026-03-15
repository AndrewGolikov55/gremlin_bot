from __future__ import annotations

import base64
import io
import logging
import mimetypes
import random
from datetime import datetime, time, timedelta
from typing import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message as TgMessage
from redis.asyncio import Redis
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.chat import Chat
from ..models.message import Message as DBMessage
from ..services.context import (
    ContextService,
    ChatTurn,
    build_messages,
    build_system_prompt,
    DEFAULT_CHAT_PROMPT,
    DEFAULT_INTERJECT_SUFFIX,
    DEFAULT_FOCUS_SUFFIX,
)
from ..services.persona import StylePromptService
from ..services.llm.client import (
    LLMError,
    LLMRateLimitError,
    generate as llm_generate,
    resolve_llm_options,
)
from ..services.message_history import persist_telegram_message
from ..services.moderation import apply_moderation
from ..services.settings import SettingsService
from ..services.app_config import AppConfigService
from ..services.usage_limits import UsageLimiter
from ..services.user_memory import UserMemoryService
from ..utils.llm import resolve_temperature

logger = logging.getLogger("interjector")


DEFAULT_REVIVE_CLOSING = "В чате тихо. Напиши короткое сообщение, чтобы оживить разговор."


class InterjectorService:
    def __init__(
        self,
        *,
        bot: Bot,
        settings: SettingsService,
        app_config: AppConfigService,
        context: ContextService,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: Redis,
        personas: StylePromptService,
        usage_limits: UsageLimiter,
        memory: UserMemoryService,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.app_config = app_config
        self.context = context
        self.sessionmaker = sessionmaker
        self.redis = redis
        self.personas = personas
        self.usage_limits = usage_limits
        self.memory = memory
        self._cooldown_prefix = "interject:last:"
        self._revive_prefix = "interject:revive:last:"

    async def maybe_reply_to_message(
        self,
        message: TgMessage,
        conf: dict[str, object],
        turns: list[ChatTurn],
    ) -> None:
        app_conf = await self.app_config.get_all()

        probability = int(app_conf.get("interject_p", 0) or 0)
        if probability <= 0:
            return

        now = datetime.utcnow()
        if self._is_quiet(conf.get("quiet_hours"), now):
            return

        cooldown = int(app_conf.get("interject_cooldown", 60) or 60)
        if await self._is_on_cooldown(message.chat.id, cooldown, now):
            return

        roll = random.uniform(0, 100)
        if roll > probability:
            logger.debug(
                "Skip spontaneous reply chat=%s roll=%.2f p=%s",
                message.chat.id,
                roll,
                probability,
            )
            return

        focus_text = (message.text or message.caption or "").strip()
        if not focus_text:
            focus_text = None
        vision_content = None
        if message.photo:
            image_data_url = await self._download_photo_as_data_url(message)
            if image_data_url:
                vision_content = self._build_photo_content(focus_text, image_data_url)
                if not focus_text:
                    focus_text = "[photo]"

        memory_block = None
        if bool(conf.get("personalization_enabled", True)) and message.from_user:
            async with self.sessionmaker() as session:
                memory_block = await self.memory.build_user_memory_block(
                    session,
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    query_text=focus_text,
                    app_conf=app_conf,
                    speaker_name=message.from_user.username or message.from_user.full_name,
                    exclude_message_id=message.message_id,
                )

        generated = await self._generate_reply(
            conf,
            app_conf,
            turns,
            focus_text,
            chat_id=message.chat.id,
            context_blocks=[memory_block] if memory_block else None,
            message_content_override=vision_content,
            provider_override="openai" if vision_content else None,
            fallback_enabled_override=False if vision_content else None,
        )
        if not generated:
            return
        reply_text, sidecar = generated

        try:
            sent_reply = await self.bot.send_message(
                message.chat.id,
                reply_text,
                reply_to_message_id=message.message_id,
            )
        except Exception:
            logger.exception("Failed to send spontaneous reply to chat %s", message.chat.id)
            return
        try:
            await persist_telegram_message(
                self.sessionmaker,
                sent_reply,
                reply_to_message_id=message.message_id,
            )
        except Exception:
            logger.exception(
                "Failed to persist spontaneous reply chat=%s source_message=%s reply_message=%s",
                message.chat.id,
                message.message_id,
                sent_reply.message_id,
            )

        if sidecar is not None and message.from_user:
            try:
                await self.memory.apply_sidecar_update(
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    result=sidecar,
                )
            except Exception:
                logger.exception(
                    "Failed to apply interject sidecar chat=%s user=%s",
                    message.chat.id,
                    message.from_user.id,
                )

        await self._mark_interject(message.chat.id, now)
        logger.info("Spontaneous reply sent to chat %s", message.chat.id)

    async def run_idle_checks(self) -> None:
        now = datetime.utcnow()
        app_conf = await self.app_config.get_all()
        async with self.sessionmaker() as session:
            result = await session.execute(select(Chat).where(Chat.is_active.is_(True)))
            chats = list(result.scalars())
            for chat in chats:
                if not self._is_group_chat(chat.id):
                    logger.debug("Skip idle revival for non-group chat %s", chat.id)
                    continue
                try:
                    await self._maybe_revive_chat(session, chat, now, app_conf)
                except Exception:
                    logger.exception("Idle revival failed for chat %s", chat.id)

    async def _maybe_revive_chat(
        self,
        session: AsyncSession,
        chat: Chat,
        now: datetime,
        app_conf: dict[str, object],
    ) -> None:
        if not self._is_group_chat(chat.id):
            logger.debug("Skip revive attempt for non-group chat %s", chat.id)
            return

        conf = await self.settings.get_all(chat.id)
        if not conf.get("revive_enabled", False):
            return

        hours = int(conf.get("revive_after_hours", 48) or 48)
        threshold = timedelta(hours=max(1, hours))

        last_time = await self._get_last_human_message_time(session, chat.id)
        if last_time is None:
            last_time = datetime.min

        if now - last_time < threshold:
            return

        if await self._recently_revived(chat.id, threshold, now):
            return

        if not await self._consume_llm_budget(chat.id, app_conf):
            logger.debug("LLM limit reached for chat %s during revive check", chat.id)
            return

        turns = await self.context.get_recent_turns(session, chat.id, 50)
        style_prompts = await self.personas.get_all()
        base_prompt = str(app_conf.get("prompt_chat_base") or DEFAULT_CHAT_PROMPT)
        system_prompt = build_system_prompt(
            conf,
            style_prompts=style_prompts,
            base_prompt=base_prompt,
        )
        prompt_tokens = self._prompt_token_limit(app_conf)
        context_turns = min(int(app_conf.get("context_max_turns", 100) or 100), 20)
        revive_closing = str(app_conf.get("prompt_revive_closing") or DEFAULT_REVIVE_CLOSING)
        context_blocks = None
        if bool(conf.get("personalization_enabled", True)):
            participants = list(reversed([turn.user_id for turn in turns if turn.user_id and not turn.is_bot]))
            group_memory = await self.memory.build_group_memory_block(
                session,
                chat_id=chat.id,
                user_ids=participants,
                query_text=revive_closing,
                app_conf=app_conf,
            )
            if group_memory:
                context_blocks = [group_memory]

        provider, fallback_enabled = resolve_llm_options(app_conf)
        messages = build_messages(
            system_prompt,
            turns,
            context_turns,
            prompt_tokens,
            closing_text=revive_closing,
            context_blocks=context_blocks,
        )

        try:
            raw_reply = await llm_generate(
                messages,
                temperature=resolve_temperature(conf),
                top_p=float(conf.get("top_p", 0.9) or 0.9),
                max_tokens=self._max_tokens_from_config(app_conf),
                provider=provider,
                fallback_enabled=fallback_enabled,
            )
        except LLMRateLimitError as exc:
            logger.warning(
                "Rate limit during idle revival chat=%s retry_after=%s", chat.id, exc.retry_after
            )
            return
        except LLMError:
            logger.exception("LLM request failed during idle revival chat=%s", chat.id)
            return

        reply_text = apply_moderation(raw_reply)
        if not reply_text.strip():
            return

        try:
            sent_reply = await self.bot.send_message(chat.id, reply_text.strip())
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            if self._is_missing_chat_error(exc):
                await self._deactivate_chat(chat.id)
                logger.warning("Disabling chat %s after Telegram rejection: %s", chat.id, exc)
                return
            logger.exception("Failed to send idle revival to chat %s", chat.id)
            return
        except Exception:
            logger.exception("Failed to send idle revival to chat %s", chat.id)
            return
        try:
            await persist_telegram_message(self.sessionmaker, sent_reply)
        except Exception:
            logger.exception(
                "Failed to persist idle revival chat=%s reply_message=%s",
                chat.id,
                sent_reply.message_id,
            )

        await self._mark_revive(chat.id, now)
        logger.info("Idle revival sent to chat %s", chat.id)

    async def _deactivate_chat(self, chat_id: int) -> None:
        async with self.sessionmaker() as session:
            chat = await session.get(Chat, chat_id)
            if chat is None or not chat.is_active:
                return
            chat.is_active = False
            await session.commit()
            logger.info("Marked chat %s as inactive after Telegram error", chat_id)

    @staticmethod
    def _is_missing_chat_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            hint in text
            for hint in (
                "chat not found",
                "bot was blocked by the user",
                "bot was kicked",
                "user is deactivated",
            )
        )

    async def _get_last_human_message_time(self, session: AsyncSession, chat_id: int) -> datetime | None:
        result = await session.execute(
            select(DBMessage.date)
            .where(DBMessage.chat_id == chat_id, DBMessage.is_bot.is_(False))
            .order_by(desc(DBMessage.date))
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return row

    async def _recently_revived(self, chat_id: int, threshold: timedelta, now: datetime) -> bool:
        key = f"{self._revive_prefix}{chat_id}"
        value = await self.redis.get(key)
        if value is None:
            return False
        try:
            last_ts = float(value)
        except (TypeError, ValueError):
            return False
        return now - datetime.fromtimestamp(last_ts) < threshold

    async def _mark_interject(self, chat_id: int, when: datetime) -> None:
        key = f"{self._cooldown_prefix}{chat_id}"
        await self.redis.set(key, str(when.timestamp()), ex=86400)

    async def _mark_revive(self, chat_id: int, when: datetime) -> None:
        key = f"{self._revive_prefix}{chat_id}"
        await self.redis.set(key, str(when.timestamp()), ex=86400)

    async def _is_on_cooldown(self, chat_id: int, cooldown: int, now: datetime) -> bool:
        key = f"{self._cooldown_prefix}{chat_id}"
        value = await self.redis.get(key)
        if value is None:
            return False
        try:
            last_ts = float(value)
        except (TypeError, ValueError):
            return False
        return (now - datetime.fromtimestamp(last_ts)).total_seconds() < cooldown

    def _is_quiet(self, quiet_hours: str | None, now: datetime) -> bool:
        if not quiet_hours:
            return False
        try:
            start_s, end_s = quiet_hours.split("-", 1)
            start_t = time.fromisoformat(start_s)
            end_t = time.fromisoformat(end_s)
        except ValueError:
            logger.debug("Invalid quiet hours format: %s", quiet_hours)
            return False

        now_time = now.time()
        if start_t <= end_t:
            return start_t <= now_time < end_t
        return now_time >= start_t or now_time < end_t

    async def _generate_reply(
        self,
        conf: dict[str, object],
        app_conf: dict[str, object],
        turns: Iterable[ChatTurn],
        focus_text: str | None,
        *,
        chat_id: int | None = None,
        context_blocks: list[str] | None = None,
        message_content_override: object | None = None,
        provider_override: str | None = None,
        fallback_enabled_override: bool | None = None,
    ) -> tuple[str, object | None] | None:
        if chat_id is not None and not await self._consume_llm_budget(chat_id, app_conf):
            if chat_id is not None:
                logger.debug("LLM limit reached for chat %s during interject", chat_id)
            return None

        base_prompt = str(app_conf.get("prompt_chat_base") or DEFAULT_CHAT_PROMPT)
        interject_suffix = str(app_conf.get("prompt_chat_interject_suffix") or DEFAULT_INTERJECT_SUFFIX)
        focus_suffix = str(app_conf.get("prompt_focus_suffix") or DEFAULT_FOCUS_SUFFIX)
        style_prompts = await self.personas.get_all()
        system_prompt = build_system_prompt(
            conf,
            focus_text,
            interject=True,
            style_prompts=style_prompts,
            base_prompt=base_prompt,
            interject_suffix=interject_suffix,
            focus_suffix=focus_suffix,
        )
        if self.memory.sidecar_enabled(app_conf):
            system_prompt += "\n\n" + self.memory.get_sidecar_system_suffix()
        max_turns = int(app_conf.get("context_max_turns", 100) or 100)
        prompt_tokens = self._prompt_token_limit(app_conf)
        messages = build_messages(
            system_prompt,
            list(turns),
            max_turns,
            prompt_tokens,
            context_blocks=context_blocks,
        )
        if message_content_override is not None:
            messages[-1] = {"role": "user", "content": message_content_override}

        provider, fallback_enabled = resolve_llm_options(app_conf)
        if provider_override:
            provider = provider_override
        if fallback_enabled_override is not None:
            fallback_enabled = fallback_enabled_override

        try:
            raw_reply = await llm_generate(
                messages,
                temperature=resolve_temperature(conf),
                top_p=float(conf.get("top_p", 0.9) or 0.9),
                max_tokens=self._max_tokens_from_config(app_conf),
                provider=provider,
                fallback_enabled=fallback_enabled,
            )
        except LLMRateLimitError as exc:
            logger.warning(
                "Rate limit during interject chat=%s retry_after=%s",
                chat_id,
                exc.retry_after,
            )
            return None
        except LLMError:
            logger.exception("LLM request failed during spontaneous reply")
            return None

        if self.memory.sidecar_enabled(app_conf):
            sidecar = self.memory.parse_sidecar_response(raw_reply)
            reply_text = apply_moderation(self.memory.clamp_reply_text(sidecar.reply))
            return (reply_text.strip(), sidecar) if reply_text else None

        reply_text = apply_moderation(raw_reply)
        return (reply_text.strip(), None) if reply_text else None

    def _max_tokens_from_config(self, app_conf: dict[str, object]) -> int | None:
        max_length = app_conf.get("max_length")
        try:
            value = int(max_length)
        except (TypeError, ValueError):
            return None
        if value and value > 0:
            return value
        return None

    async def _consume_llm_budget(self, chat_id: int | None, app_conf: dict[str, object]) -> bool:
        if chat_id is None:
            return True
        limit_raw = app_conf.get("llm_daily_limit", 0) or 0
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return True
        allowed, _, _ = await self.usage_limits.consume(chat_id, [("llm", limit)])
        return allowed

    def _prompt_token_limit(self, app_conf: dict[str, object]) -> int | None:
        raw = app_conf.get("context_max_prompt_tokens", 32000)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 32000
        if value <= 0:
            return None
        return max(2000, min(60000, value))

    @staticmethod
    def _is_group_chat(chat_id: int) -> bool:
        # Telegram assigns negative ids to group, supergroup, and channel chats.
        # Private chats (users/bots) have positive ids and should be ignored here.
        return chat_id < 0

    async def _download_photo_as_data_url(self, message: TgMessage) -> str | None:
        photo = self._pick_photo_size(message)
        if photo is None:
            return None

        telegram_file = await self.bot.get_file(photo.file_id)
        if not telegram_file.file_path:
            return None

        buffer = io.BytesIO()
        await self.bot.download_file(telegram_file.file_path, destination=buffer)
        payload = buffer.getvalue()
        if not payload:
            return None

        mime_type, _encoding = mimetypes.guess_type(telegram_file.file_path)
        if not mime_type:
            mime_type = "image/jpeg"
        encoded = base64.b64encode(payload).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    @staticmethod
    def _pick_photo_size(message: TgMessage):
        if not message.photo:
            return None

        max_bytes = 8 * 1024 * 1024
        for photo in reversed(message.photo):
            size = getattr(photo, "file_size", None)
            if isinstance(size, int) and size > 0 and size <= max_bytes:
                return photo
        return message.photo[-1]

    @staticmethod
    def _build_photo_content(focus_text: str | None, image_data_url: str) -> list[dict[str, object]]:
        prompt = (
            f"Пользователь приложил изображение и написал: {focus_text}\n"
            "Ответь по сути и учти само изображение."
            if focus_text
            else "Пользователь приложил изображение. Кратко отреагируй на него одним сообщением."
        )
        detail = "high" if focus_text and any(
            marker in focus_text.lower()
            for marker in ("скрин", "screenshot", "текст", "прочитай", "что написано", "ocr")
        ) else "low"
        return [
            {
                "type": "text",
                "text": prompt,
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": image_data_url,
                    "detail": detail,
                },
            },
        ]
