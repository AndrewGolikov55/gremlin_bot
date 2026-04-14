import logging
import os
import re
from typing import Any

from aiogram import Bot, F, Router, types
from aiogram.enums import ChatType, MessageEntityType
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.message import Message as DBMessage
from ..services.context import (
    ContextService,
    build_messages,
    build_system_prompt,
    DEFAULT_CHAT_PROMPT,
    DEFAULT_FOCUS_SUFFIX,
)
from ..services.interjector import InterjectorService
from ..services.llm.client import (
    LLMError,
    LLMRateLimitError,
    generate_with_fallback,
    resolve_llm_options,
)
from ..services.llm.vision import download_file_id_as_data_url
from ..services.llm.whisper import transcribe_file_id
from ..services.message_history import persist_telegram_message, store_telegram_message
from ..services.reply_images import collect_reply_images
from ..services.reply_voice import (
    VIDEO_NOTE_MARKER,
    VOICE_MARKER,
    get_reply_voice_transcript,
)
from ..services.moderation import apply_moderation
from ..services.settings import SettingsService
from ..services.app_config import AppConfigService
from ..services.persona import StylePromptService
from ..services.reactions import ReactionService
from ..services.spontaneity import ActionKind, InterjectTrigger, SpontaneityPolicy
from ..services.usage_limits import UsageLimiter
from ..services.user_memory import UserMemoryService
from ..utils.llm import resolve_temperature
from .constants import START_PRIVATE_RESPONSE
from .typing_indicator import keep_typing


logger = logging.getLogger(__name__)


def build_vision_messages(
    *,
    system_prompt: str,
    turns,
    max_turns: int,
    prompt_token_limit: int,
    focus_text: str | None,
    image_data_urls: list[str],
    vision_detail: str,
    context_blocks: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Assemble a multimodal messages list with the final user turn carrying image_urls."""
    messages = build_messages(
        system_prompt,
        turns,
        max_turns,
        prompt_token_limit,
        context_blocks=context_blocks,
    )
    prompt_text = _build_photo_prompt_text(focus_text)
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    for url in image_data_urls:
        content.append({
            "type": "image_url",
            "image_url": {"url": url, "detail": vision_detail},
        })
    messages[-1] = {"role": "user", "content": content}
    return messages


router = Router(name="triggers")


@router.message(F.text)
async def collect_messages(
    message: types.Message,
    session: AsyncSession,
    settings: SettingsService,
    context: ContextService,
    interjector: InterjectorService,
    personas: StylePromptService,
    app_config: AppConfigService,
    reactions: ReactionService,
    usage_limits: UsageLimiter,
    memory: UserMemoryService,
    policy: SpontaneityPolicy,
    bot: Bot,
):
    bot_user = await bot.get_me()

    if _is_own_message(message, bot_user.id):
        return

    if message.chat.type == ChatType.PRIVATE or message.chat.type == "private":
        text = (message.text or "").strip()
        if text.startswith("/start"):
            return
        await message.answer(START_PRIVATE_RESPONSE)
        return

    await store_telegram_message(session, message)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        logger.debug(
            "Integrity error while persisting message chat=%s message_id=%s: %s",
            message.chat.id,
            message.message_id,
            exc,
        )
        # message already stored or race condition, continue without responding
        return
    except Exception:
        await session.rollback()
        raise

    conf = await settings.get_all(message.chat.id)
    if not conf.get("is_active", True):
        return

    if _is_command(message):
        logger.debug("Skip command message chat=%s text=%r", message.chat.id, message.text)
        return

    is_mention = _is_bot_mentioned(message, bot_user.id, bot_user.username)
    is_reply_to_bot = _is_reply(message, bot_user.id, bot_user.username)

    logger.debug(
        "Trigger check chat=%s type=%s mention=%s reply=%s text=%r entities=%s",
        message.chat.id,
        message.chat.type,
        is_mention,
        is_reply_to_bot,
        message.text,
        message.entities,
    )

    app_conf = await app_config.get_all()
    provider = resolve_llm_options(app_conf)
    base_prompt = str(app_conf.get("prompt_chat_base") or DEFAULT_CHAT_PROMPT)
    focus_suffix = str(app_conf.get("prompt_focus_suffix") or DEFAULT_FOCUS_SUFFIX)
    personalization_enabled = bool(conf.get("personalization_enabled", True))

    max_turns = int(app_conf.get("context_max_turns", 100) or 100)
    prompt_token_limit = _resolve_prompt_token_limit(app_conf)
    style_prompts = await personas.get_all()
    turns = await context.get_recent_turns(session, message.chat.id, max_turns)
    if await policy.can_react(message.chat.id):
        reacted = await reactions.generate_reaction(message, conf, app_conf, turns)
        if reacted:
            await policy.mark_acted(chat_id=message.chat.id, action=ActionKind.REACTION)

    if _should_reply(is_mention, is_reply_to_bot, message.chat.type):
        llm_limit_raw = app_conf.get("llm_daily_limit", 0) or 0
        try:
            llm_limit = int(llm_limit_raw)
        except (TypeError, ValueError):
            llm_limit = 0
        if llm_limit > 0:
            allowed, counts, _ = await usage_limits.consume(message.chat.id, [("llm", llm_limit)])
            if not allowed:
                used = counts.get("llm", llm_limit)
                await message.reply(
                    f"🤖 Лимит ответов модели на сегодня исчерпан ({used}/{llm_limit}). Попробуй завтра."
                )
                return
        raw_focus = (message.text or message.caption or "").strip()
        focus_text = None
        if raw_focus and (is_reply_to_bot or is_mention):
            cleaned = raw_focus
            if bot_user.username:
                pattern = re.compile(rf"@{re.escape(bot_user.username)}", re.IGNORECASE)
                cleaned = pattern.sub("", cleaned)
            focus_text = " ".join(cleaned.split()) or None
        system_prompt = build_system_prompt(
            conf,
            focus_text,
            style_prompts=style_prompts,
            base_prompt=base_prompt,
            focus_suffix=focus_suffix,
        )
        memory_block = None
        if personalization_enabled and message.from_user:
            speaker_name = message.from_user.username or message.from_user.full_name
            memory_block = await memory.build_user_memory_block(
                session,
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                query_text=focus_text or raw_focus,
                app_conf=app_conf,
                speaker_name=speaker_name,
                exclude_message_id=message.message_id,
            )
        if personalization_enabled and message.from_user and memory.sidecar_enabled(app_conf):
            system_prompt += "\n\n" + memory.get_sidecar_system_suffix()
        messages_for_llm = build_messages(
            system_prompt,
            turns,
            max_turns,
            prompt_token_limit,
            context_blocks=[memory_block] if memory_block else None,
        )

        reply_images: list[str] = []
        replied = message.reply_to_message
        if replied is not None and not _is_reply_to_bot(replied, bot_user.id, bot_user.username):
            reply_images = await collect_reply_images(
                bot=bot,
                message=message,
                session=session,
            )

        if reply_images:
            vision_messages = build_vision_messages(
                system_prompt=system_prompt,
                turns=turns,
                max_turns=max_turns,
                prompt_token_limit=prompt_token_limit,
                focus_text=focus_text,
                image_data_urls=reply_images,
                vision_detail="low",
                context_blocks=[memory_block] if memory_block else None,
            )
            vision_primary = "openai"
        else:
            vision_messages = None
            vision_primary = None

        async with keep_typing(bot, message.chat.id):
            try:
                max_length_conf = app_conf.get("max_length")
                max_tokens = None
                if isinstance(max_length_conf, (int, float, str)):
                    try:
                        max_len_value = int(float(max_length_conf))
                    except (TypeError, ValueError):
                        max_len_value = None
                    if max_len_value and max_len_value > 0:
                        max_tokens = max_len_value

                if vision_messages is not None and vision_primary is not None:
                    raw_reply = await generate_with_fallback(
                        vision_messages,
                        max_tokens=max_tokens,
                        temperature=resolve_temperature(conf),
                        top_p=float(conf.get("top_p", 0.9) or 0.9),
                        primary=vision_primary,
                    )
                else:
                    raw_reply = await generate_with_fallback(
                        messages_for_llm,
                        max_tokens=max_tokens,
                        temperature=resolve_temperature(conf),
                        top_p=float(conf.get("top_p", 0.9) or 0.9),
                        primary=provider,
                    )
            except LLMRateLimitError as exc:
                wait_hint = ""
                if exc.retry_after and exc.retry_after > 0:
                    wait_hint = f" Попробуй через ~{int(exc.retry_after)} с."
                await message.reply("🤖 Модель перегружена." + wait_hint)
                return
            except LLMError:
                await message.reply("🤖 LLM вернула ошибку. Попробуй позже.")
                return
            except Exception:
                logger.exception(
                    "Unexpected error while generating LLM reply (provider=%s)",
                    provider,
                )
                await message.reply("🤖 Не удалось подготовить ответ (LLM недоступна).")
                return

            sidecar = None
            if personalization_enabled and message.from_user and memory.sidecar_enabled(app_conf):
                sidecar = memory.parse_sidecar_response(raw_reply)
                reply_text = apply_moderation(memory.clamp_reply_text(sidecar.reply))
            else:
                reply_text = apply_moderation(raw_reply)
            if not reply_text.strip():
                return

            sent_reply = await message.reply(reply_text.strip())
        await policy.mark_acted(chat_id=message.chat.id, action=ActionKind.DIRECT_REPLY)
        try:
            await store_telegram_message(
                session,
                sent_reply,
                reply_to_message_id=message.message_id,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception(
                "Failed to persist bot reply chat=%s source_message=%s reply_message=%s",
                message.chat.id,
                message.message_id,
                sent_reply.message_id,
            )
        if sidecar is not None and message.from_user:
            try:
                await memory.apply_sidecar_update(
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    result=sidecar,
                )
            except Exception:
                logger.exception(
                    "Failed to apply user memory sidecar chat=%s user=%s",
                    message.chat.id,
                    message.from_user.id,
                )
        return

    if await policy.can_interject(message.chat.id, trigger=InterjectTrigger.NEW_MESSAGE):
        sent = await interjector.generate_spontaneous_reply(message, conf, turns)
        if sent:
            await policy.mark_acted(chat_id=message.chat.id, action=ActionKind.INTERJECT)


@router.message(F.sticker | F.animation | F.photo | F.video | F.document | F.voice | F.video_note)
async def handle_media_messages(
    message: types.Message,
    session: AsyncSession,
    settings: SettingsService,
    context: ContextService,
    interjector: InterjectorService,
    personas: StylePromptService,
    app_config: AppConfigService,
    usage_limits: UsageLimiter,
    memory: UserMemoryService,
    policy: SpontaneityPolicy,
    bot: Bot,
):
    bot_user = await bot.get_me()

    if _is_own_message(message, bot_user.id):
        return

    if message.chat.type != ChatType.PRIVATE and message.chat.type != "private":
        await store_telegram_message(session, message)

        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            logger.debug(
                "Integrity error while persisting media chat=%s message_id=%s: %s",
                message.chat.id,
                message.message_id,
                exc,
            )
            return
        except Exception:
            await session.rollback()
            raise

    conf = await settings.get_all(message.chat.id)
    if not conf.get("is_active", True):
        return

    if message.chat.type == ChatType.PRIVATE or message.chat.type == "private":
        await message.answer(_unsupported_media_text(message), parse_mode=None)
        return

    # Voice / video_note — dedicated handler that may transcribe and reply.
    if message.voice is not None or message.video_note is not None:
        await _handle_voice_message(
            message,
            session=session,
            bot=bot,
            settings=settings,
            app_config=app_config,
            context=context,
            personas=personas,
            memory=memory,
            usage_limits=usage_limits,
            policy=policy,
            interjector=interjector,
            conf=conf,
        )
        return

    is_mention = _is_bot_mentioned(message, bot_user.id, bot_user.username)
    is_reply_to_bot = _is_reply(message, bot_user.id, bot_user.username)
    if not _should_reply(is_mention, is_reply_to_bot, message.chat.type):
        if message.photo:
            app_conf = await app_config.get_all()
            max_turns = int(app_conf.get("context_max_turns", 100) or 100)
            turns = await context.get_recent_turns(session, message.chat.id, max_turns)
            if await policy.can_interject(
                message.chat.id, trigger=InterjectTrigger.NEW_MESSAGE
            ):
                sent = await interjector.generate_spontaneous_reply(message, conf, turns)
                if sent:
                    await policy.mark_acted(
                        chat_id=message.chat.id, action=ActionKind.INTERJECT
                    )
        return

    if message.photo:
        await _handle_photo_reply(
            message=message,
            session=session,
            context=context,
            personas=personas,
            app_config=app_config,
            usage_limits=usage_limits,
            memory=memory,
            policy=policy,
            bot=bot,
            conf=conf,
        )
        return

    await message.reply(_unsupported_media_text(message), parse_mode=None)
    await policy.mark_acted(chat_id=message.chat.id, action=ActionKind.DIRECT_REPLY)


async def _handle_photo_reply(
    *,
    message: types.Message,
    session: AsyncSession,
    context: ContextService,
    personas: StylePromptService,
    app_config: AppConfigService,
    usage_limits: UsageLimiter,
    memory: UserMemoryService,
    policy: SpontaneityPolicy,
    bot: Bot,
    conf: dict[str, object],
) -> None:
    app_conf = await app_config.get_all()
    llm_limit_raw = app_conf.get("llm_daily_limit", 0) or 0
    try:
        llm_limit = int(llm_limit_raw)
    except (TypeError, ValueError):
        llm_limit = 0
    if llm_limit > 0:
        allowed, counts, _ = await usage_limits.consume(message.chat.id, [("llm", llm_limit)])
        if not allowed:
            used = counts.get("llm", llm_limit)
            await message.reply(
                f"🤖 Лимит ответов модели на сегодня исчерпан ({used}/{llm_limit}). Попробуй завтра."
            )
            return

    image_data_url = await _download_photo_as_data_url(bot, message)
    if not image_data_url:
        logger.warning(
            "Failed to prepare image payload for chat=%s message=%s",
            message.chat.id,
            message.message_id,
        )
        await message.reply("🤖 Не получилось скачать изображение из Telegram. Попробуй отправить его ещё раз.")
        return

    raw_focus = (message.caption or "").strip()
    focus_text = None
    if raw_focus:
        cleaned = raw_focus
        bot_user = await bot.get_me()
        if bot_user.username:
            pattern = re.compile(rf"@{re.escape(bot_user.username)}", re.IGNORECASE)
            cleaned = pattern.sub("", cleaned)
        focus_text = " ".join(cleaned.split()) or None

    base_prompt = str(app_conf.get("prompt_chat_base") or DEFAULT_CHAT_PROMPT)
    focus_suffix = str(app_conf.get("prompt_focus_suffix") or DEFAULT_FOCUS_SUFFIX)
    style_prompts = await personas.get_all()
    system_prompt = build_system_prompt(
        conf,
        focus_text,
        style_prompts=style_prompts,
        base_prompt=base_prompt,
        focus_suffix=focus_suffix,
    )

    personalization_enabled = bool(conf.get("personalization_enabled", True))
    memory_block = None
    if personalization_enabled and message.from_user:
        speaker_name = message.from_user.username or message.from_user.full_name
        memory_block = await memory.build_user_memory_block(
            session,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            query_text=focus_text or raw_focus or _message_storage_text(message),
            app_conf=app_conf,
            speaker_name=speaker_name,
            exclude_message_id=message.message_id,
        )
    if personalization_enabled and message.from_user and memory.sidecar_enabled(app_conf):
        system_prompt += "\n\n" + memory.get_sidecar_system_suffix()

    max_turns = int(app_conf.get("context_max_turns", 100) or 100)
    prompt_token_limit = _resolve_prompt_token_limit(app_conf)
    turns = await context.get_recent_turns(session, message.chat.id, max_turns)
    messages_for_llm = build_vision_messages(
        system_prompt=system_prompt,
        turns=turns,
        max_turns=max_turns,
        prompt_token_limit=prompt_token_limit,
        focus_text=focus_text,
        image_data_urls=[image_data_url],
        vision_detail=_resolve_vision_detail(message),
        context_blocks=[memory_block] if memory_block else None,
    )

    async with keep_typing(bot, message.chat.id):
        try:
            max_length_conf = app_conf.get("max_length")
            max_tokens = None
            if isinstance(max_length_conf, (int, float, str)):
                try:
                    max_len_value = int(float(max_length_conf))
                except (TypeError, ValueError):
                    max_len_value = None
                if max_len_value and max_len_value > 0:
                    max_tokens = max_len_value

            raw_reply = await generate_with_fallback(
                messages_for_llm,
                max_tokens=max_tokens,
                temperature=resolve_temperature(conf),
                top_p=float(conf.get("top_p", 0.9) or 0.9),
                primary="openai",
            )
        except LLMRateLimitError as exc:
            wait_hint = ""
            if exc.retry_after and exc.retry_after > 0:
                wait_hint = f" Попробуй через ~{int(exc.retry_after)} с."
            await message.reply("🤖 Модель перегружена." + wait_hint)
            return
        except LLMError as exc:
            logger.warning("Vision request failed for chat=%s message=%s: %s", message.chat.id, message.message_id, exc)
            await message.reply("🤖 Не удалось прочитать изображение.")
            return
        except Exception:
            logger.exception("Unexpected error while generating vision reply")
            await message.reply("🤖 Не удалось прочитать изображение.")
            return

        sidecar = None
        if personalization_enabled and message.from_user and memory.sidecar_enabled(app_conf):
            sidecar = memory.parse_sidecar_response(raw_reply)
            reply_text = apply_moderation(memory.clamp_reply_text(sidecar.reply))
        else:
            reply_text = apply_moderation(raw_reply)
        if not reply_text.strip():
            return

        sent_reply = await message.reply(reply_text.strip())
    await policy.mark_acted(chat_id=message.chat.id, action=ActionKind.DIRECT_REPLY)
    try:
        await store_telegram_message(
            session,
            sent_reply,
            reply_to_message_id=message.message_id,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception(
            "Failed to persist bot photo reply chat=%s source_message=%s reply_message=%s",
            message.chat.id,
            message.message_id,
            sent_reply.message_id,
        )
    if sidecar is not None and message.from_user:
        try:
            await memory.apply_sidecar_update(
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                result=sidecar,
            )
        except Exception:
            logger.exception(
                "Failed to apply user memory sidecar after photo reply chat=%s user=%s",
                message.chat.id,
                message.from_user.id,
            )


async def _handle_voice_message(
    message: types.Message,
    *,
    session: AsyncSession,
    bot: Bot,
    settings: SettingsService,
    app_config: AppConfigService,
    context: ContextService,
    personas: StylePromptService,
    memory: UserMemoryService,
    usage_limits: UsageLimiter,
    policy: SpontaneityPolicy,
    interjector: InterjectorService,
    conf: dict[str, object],
) -> None:
    """Voice / video_note pipeline: transcribe and respond or excuse in persona."""
    chat_id = message.chat.id
    app_conf = await app_config.get_all()

    voice_obj = message.voice or message.video_note
    if voice_obj is None:
        return
    file_id = getattr(voice_obj, "file_id", None)
    if not file_id:
        return
    duration_raw = getattr(voice_obj, "duration", 0) or 0
    try:
        duration_hint = float(duration_raw)
    except (TypeError, ValueError):
        duration_hint = 0.0

    bot_user = await bot.get_me()
    is_mention = _is_bot_mentioned(message, bot_user.id, bot_user.username)
    is_reply_to_bot = _is_reply(message, bot_user.id, bot_user.username)
    is_addressed = _should_reply(is_mention, is_reply_to_bot, message.chat.type)

    if not bool(app_conf.get("voice_enabled", True)):
        # Only answer when directly addressed; silently drop unaddressed voices in groups.
        if is_addressed:
            await message.answer(_unsupported_media_text(message), parse_mode=None)
        return

    max_seconds = int(app_conf.get("voice_max_seconds", 0) or 0)
    whisper_limit = int(app_conf.get("whisper_daily_limit", 0) or 0)
    whisper_language = os.getenv("WHISPER_LANGUAGE")

    # ---- Interject path: unaddressed, policy-gated. ----
    if not is_addressed:
        if not await policy.can_interject(chat_id, trigger=InterjectTrigger.NEW_MESSAGE):
            return

        if whisper_limit > 0:
            allowed, _counts, _ = await usage_limits.consume(chat_id, [("whisper", whisper_limit)])
            if not allowed:
                return  # silent on interject path

        sent = None
        async with keep_typing(bot, message.chat.id):
            result = await transcribe_file_id(
                bot,
                file_id,
                max_seconds=max_seconds,
                duration_hint=duration_hint,
                language=whisper_language,
            )
            if result is None:
                return  # silent on interject path

            await _cache_voice_transcript(session, chat_id, message.message_id, result.text, message)

            # Refresh turns so the new transcript is visible to the LLM.
            max_turns = int(app_conf.get("context_max_turns", 100) or 100)
            turns = await context.get_recent_turns(session, chat_id, max_turns)

            sent = await interjector.generate_spontaneous_reply(
                message, conf, turns, focus_text_override=result.text,
            )
        if sent:
            await policy.mark_acted(chat_id=chat_id, action=ActionKind.INTERJECT)
        return

    # ---- Direct address path ----
    if whisper_limit > 0:
        allowed, _counts, _ = await usage_limits.consume(chat_id, [("whisper", whisper_limit)])
        if not allowed:
            await _generate_voice_excuse(
                bot=bot,
                message=message,
                session=session,
                conf=conf,
                app_conf=app_conf,
                context=context,
                personas=personas,
                memory=memory,
                situation=(
                    "На сегодня твой лимит распознавания голосовых закончился. "
                    "Скажи об этом одной-двумя фразами и попроси продублировать текстом."
                ),
            )
            await policy.mark_acted(chat_id=chat_id, action=ActionKind.DIRECT_REPLY)
            return

    direct_result = None
    sent_ok = False
    async with keep_typing(bot, message.chat.id):
        direct_result = await transcribe_file_id(
            bot,
            file_id,
            max_seconds=max_seconds,
            duration_hint=duration_hint,
            language=whisper_language,
        )
        if direct_result is not None:
            # Success path: cache transcript and reply via LLM.
            await _cache_voice_transcript(
                session, chat_id, message.message_id, direct_result.text, message,
            )
            sent_ok = await _generate_voice_direct_reply(
                message=message,
                focus_text=direct_result.text,
                bot=bot,
                session=session,
                context=context,
                personas=personas,
                memory=memory,
                usage_limits=usage_limits,
                conf=conf,
                app_conf=app_conf,
            )

    if direct_result is None:
        if max_seconds > 0 and duration_hint > max_seconds:
            situation = (
                f"Пользователь прислал голосовое длиной {int(duration_hint)} секунд, "
                f"твой лимит — {max_seconds} секунд. Скажи, что не будешь слушать такое длинное, "
                f"и попроси короче или текстом."
            )
        else:
            situation = (
                "Пользователь прислал тебе голосовое, но распознавание не сработало. "
                "Скажи об этом одной-двумя фразами и попроси продублировать текстом."
            )
        # _generate_voice_excuse manages its own keep_typing.
        await _generate_voice_excuse(
            bot=bot,
            message=message,
            session=session,
            conf=conf,
            app_conf=app_conf,
            context=context,
            personas=personas,
            memory=memory,
            situation=situation,
        )
        await policy.mark_acted(chat_id=chat_id, action=ActionKind.DIRECT_REPLY)
        return

    if sent_ok:
        await policy.mark_acted(chat_id=chat_id, action=ActionKind.DIRECT_REPLY)


async def _cache_voice_transcript(
    session: AsyncSession,
    chat_id: int,
    message_id: int,
    transcript: str,
    message: types.Message,
) -> None:
    """Write the transcript back into the persisted row, replacing the marker."""
    marker = VOICE_MARKER if message.voice is not None else VIDEO_NOTE_MARKER
    new_text = f"{marker} {transcript}"
    try:
        await session.execute(
            update(DBMessage)
            .where(
                DBMessage.chat_id == chat_id,
                DBMessage.message_id == message_id,
                DBMessage.text == marker,
            )
            .values(text=new_text)
        )
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception(
            "Failed to cache voice transcript chat=%s message=%s",
            chat_id,
            message_id,
        )


async def _generate_voice_excuse(
    *,
    bot: Bot,
    message: types.Message,
    session: AsyncSession,
    conf: dict[str, object],
    app_conf: dict[str, object],
    context: ContextService,
    personas: StylePromptService,
    memory: UserMemoryService,
    situation: str,
) -> None:
    """Send an in-persona "excuse" message explaining the voice-handling failure."""
    base_prompt = str(app_conf.get("prompt_chat_base") or DEFAULT_CHAT_PROMPT)
    focus_suffix = str(app_conf.get("prompt_focus_suffix") or DEFAULT_FOCUS_SUFFIX)
    style_prompts = await personas.get_all()
    system_prompt = build_system_prompt(
        conf,
        None,
        style_prompts=style_prompts,
        base_prompt=base_prompt,
        focus_suffix=focus_suffix,
    )

    personalization_enabled = bool(conf.get("personalization_enabled", True))
    memory_block = None
    if personalization_enabled and message.from_user:
        speaker_name = message.from_user.username or message.from_user.full_name
        memory_block = await memory.build_user_memory_block(
            session,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            query_text=situation,
            app_conf=app_conf,
            speaker_name=speaker_name,
            exclude_message_id=message.message_id,
        )

    context_blocks: list[str] = [situation]
    if memory_block:
        context_blocks.append(memory_block)

    max_turns = int(app_conf.get("context_max_turns", 100) or 100)
    prompt_token_limit = _resolve_prompt_token_limit(app_conf)
    turns = await context.get_recent_turns(session, message.chat.id, max_turns)
    messages_for_llm = build_messages(
        system_prompt,
        turns,
        max_turns,
        prompt_token_limit,
        context_blocks=context_blocks,
    )

    async with keep_typing(bot, message.chat.id):
        provider = resolve_llm_options(app_conf)
        try:
            max_length_conf = app_conf.get("max_length")
            max_tokens: int | None = None
            if isinstance(max_length_conf, (int, float, str)):
                try:
                    max_len_value = int(float(max_length_conf))
                except (TypeError, ValueError):
                    max_len_value = None
                if max_len_value and max_len_value > 0:
                    max_tokens = max_len_value
            raw_reply = await generate_with_fallback(
                messages_for_llm,
                max_tokens=max_tokens,
                temperature=resolve_temperature(conf),
                top_p=float(conf.get("top_p", 0.9) or 0.9),
                primary=provider,
            )
        except (LLMError, LLMRateLimitError):
            logger.warning("Voice excuse LLM call failed chat=%s", message.chat.id)
            return
        except Exception:
            logger.exception("Unexpected error while generating voice excuse chat=%s", message.chat.id)
            return

        reply_text = apply_moderation(raw_reply or "").strip()
        if not reply_text:
            return

        try:
            sent_reply = await message.reply(reply_text)
        except Exception:
            logger.exception("Failed to send voice excuse chat=%s", message.chat.id)
            return

    try:
        await store_telegram_message(
            session,
            sent_reply,
            reply_to_message_id=message.message_id,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception(
            "Failed to persist voice excuse chat=%s source_message=%s",
            message.chat.id,
            message.message_id,
        )


async def _generate_voice_direct_reply(
    *,
    message: types.Message,
    focus_text: str,
    bot: Bot,
    session: AsyncSession,
    context: ContextService,
    personas: StylePromptService,
    memory: UserMemoryService,
    usage_limits: UsageLimiter,
    conf: dict[str, object],
    app_conf: dict[str, object],
) -> bool:
    """Build and send an LLM reply using the transcript as focus_text. Returns True if sent."""
    chat_id = message.chat.id

    # LLM daily limit gate (separate from whisper limit).
    llm_limit_raw = app_conf.get("llm_daily_limit", 0) or 0
    try:
        llm_limit = int(llm_limit_raw)
    except (TypeError, ValueError):
        llm_limit = 0
    if llm_limit > 0:
        allowed, counts, _ = await usage_limits.consume(chat_id, [("llm", llm_limit)])
        if not allowed:
            used = counts.get("llm", llm_limit)
            try:
                await message.reply(
                    f"🤖 Лимит ответов модели на сегодня исчерпан ({used}/{llm_limit}). Попробуй завтра."
                )
            except Exception:
                logger.exception("Failed to notify about LLM limit chat=%s", chat_id)
            return False

    base_prompt = str(app_conf.get("prompt_chat_base") or DEFAULT_CHAT_PROMPT)
    focus_suffix = str(app_conf.get("prompt_focus_suffix") or DEFAULT_FOCUS_SUFFIX)
    style_prompts = await personas.get_all()
    system_prompt = build_system_prompt(
        conf,
        focus_text,
        style_prompts=style_prompts,
        base_prompt=base_prompt,
        focus_suffix=focus_suffix,
    )

    personalization_enabled = bool(conf.get("personalization_enabled", True))
    memory_block = None
    if personalization_enabled and message.from_user:
        speaker_name = message.from_user.username or message.from_user.full_name
        memory_block = await memory.build_user_memory_block(
            session,
            chat_id=chat_id,
            user_id=message.from_user.id,
            query_text=focus_text,
            app_conf=app_conf,
            speaker_name=speaker_name,
            exclude_message_id=message.message_id,
        )
    if personalization_enabled and message.from_user and memory.sidecar_enabled(app_conf):
        system_prompt += "\n\n" + memory.get_sidecar_system_suffix()

    context_blocks: list[str] = []
    if memory_block:
        context_blocks.append(memory_block)

    # Reply-chain: if user replies to an OLD voice (not the current one) of a non-bot,
    # include that transcript for context.
    replied = getattr(message, "reply_to_message", None)
    bot_user = await bot.get_me()
    if replied is not None and not _is_reply_to_bot(replied, bot_user.id, bot_user.username):
        try:
            reply_transcript = await get_reply_voice_transcript(
                bot=bot,
                message=message,
                session=session,
                max_seconds=int(app_conf.get("voice_max_seconds", 0) or 0),
            )
        except Exception:
            logger.exception(
                "reply-chain voice transcript failed chat=%s message=%s",
                chat_id,
                message.message_id,
            )
            reply_transcript = None
        if reply_transcript:
            context_blocks.append(f"[Голосовое из треда]: {reply_transcript}")

    max_turns = int(app_conf.get("context_max_turns", 100) or 100)
    prompt_token_limit = _resolve_prompt_token_limit(app_conf)
    turns = await context.get_recent_turns(session, chat_id, max_turns)
    messages_for_llm = build_messages(
        system_prompt,
        turns,
        max_turns,
        prompt_token_limit,
        context_blocks=context_blocks or None,
    )

    provider = resolve_llm_options(app_conf)
    try:
        max_length_conf = app_conf.get("max_length")
        max_tokens: int | None = None
        if isinstance(max_length_conf, (int, float, str)):
            try:
                max_len_value = int(float(max_length_conf))
            except (TypeError, ValueError):
                max_len_value = None
            if max_len_value and max_len_value > 0:
                max_tokens = max_len_value
        raw_reply = await generate_with_fallback(
            messages_for_llm,
            max_tokens=max_tokens,
            temperature=resolve_temperature(conf),
            top_p=float(conf.get("top_p", 0.9) or 0.9),
            primary=provider,
        )
    except LLMRateLimitError as exc:
        wait_hint = ""
        if exc.retry_after and exc.retry_after > 0:
            wait_hint = f" Попробуй через ~{int(exc.retry_after)} с."
        try:
            await message.reply("🤖 Модель перегружена." + wait_hint)
        except Exception:
            logger.exception("Failed to notify about rate limit chat=%s", chat_id)
        return False
    except LLMError:
        try:
            await message.reply("🤖 LLM вернула ошибку. Попробуй позже.")
        except Exception:
            logger.exception("Failed to notify about LLM error chat=%s", chat_id)
        return False
    except Exception:
        logger.exception("Unexpected error in voice direct-reply chat=%s", chat_id)
        try:
            await message.reply("🤖 Не удалось подготовить ответ.")
        except Exception:
            logger.exception("Failed to notify about unexpected error chat=%s", chat_id)
        return False

    sidecar = None
    if personalization_enabled and message.from_user and memory.sidecar_enabled(app_conf):
        sidecar = memory.parse_sidecar_response(raw_reply)
        reply_text = apply_moderation(memory.clamp_reply_text(sidecar.reply))
    else:
        reply_text = apply_moderation(raw_reply)
    reply_text = reply_text.strip()
    if not reply_text:
        return False

    try:
        sent_reply = await message.reply(reply_text)
    except Exception:
        logger.exception("Failed to send voice direct reply chat=%s", chat_id)
        return False

    try:
        await store_telegram_message(
            session,
            sent_reply,
            reply_to_message_id=message.message_id,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception(
            "Failed to persist voice direct reply chat=%s source_message=%s",
            chat_id,
            message.message_id,
        )

    if sidecar is not None and message.from_user:
        try:
            await memory.apply_sidecar_update(
                chat_id=chat_id,
                user_id=message.from_user.id,
                result=sidecar,
            )
        except Exception:
            logger.exception(
                "Failed to apply user memory sidecar after voice reply chat=%s user=%s",
                chat_id,
                message.from_user.id,
            )

    return True


async def _download_photo_as_data_url(bot: Bot, message: types.Message) -> str | None:
    photo = _pick_photo_size(message)
    if photo is None:
        logger.warning("Photo message has no downloadable photo sizes message=%s", message.message_id)
        return None
    return await download_file_id_as_data_url(bot, photo.file_id)


def _pick_photo_size(message: types.Message) -> types.PhotoSize | None:
    if not message.photo:
        return None

    max_bytes = 8 * 1024 * 1024
    for photo in reversed(message.photo):
        size = getattr(photo, "file_size", None)
        if isinstance(size, int) and size > 0 and size <= max_bytes:
            return photo
    return message.photo[-1]


def _build_photo_prompt_text(focus_text: str | None) -> str:
    if focus_text:
        return (
            f"Пользователь приложил изображение и написал: {focus_text}\n"
            "Ответь по сути запроса, опираясь и на текст, и на само изображение."
        )
    return "Пользователь приложил изображение. Кратко опиши, что на нём, и ответь одним сообщением."


def _resolve_vision_detail(message: types.Message) -> str:
    caption = (message.caption or "").lower()
    detail_markers = ("скрин", "screenshot", "текст", "прочитай", "что написано", "ocr")
    if any(marker in caption for marker in detail_markers):
        return "high"
    return "low"


def _message_storage_text(message: types.Message) -> str:
    if message.text:
        return message.text
    if message.photo:
        caption = (message.caption or "").strip()
        return f"[photo] {caption}" if caption else "[photo]"
    if message.sticker:
        return "[sticker]"
    if message.animation:
        caption = (message.caption or "").strip()
        return f"[animation] {caption}" if caption else "[animation]"
    if message.video:
        caption = (message.caption or "").strip()
        return f"[video] {caption}" if caption else "[video]"
    if message.document:
        caption = (message.caption or "").strip()
        return f"[document] {caption}" if caption else "[document]"
    return message.caption or ""


def _is_command(message: types.Message) -> bool:
    return bool(message.text and message.text.startswith("/"))


def _is_own_message(message: types.Message, bot_id: int) -> bool:
    if message.from_user:
        return message.from_user.id == bot_id
    return False


def _is_bot_mentioned(
    message: types.Message,
    bot_id: int,
    bot_username: str | None,
) -> bool:
    text = message.text or message.caption
    if not text or not bot_username:
        return False

    bot_username = bot_username.lower()
    entities = list(message.entities or []) + list(message.caption_entities or [])
    for entity in entities:
        if entity.type == MessageEntityType.TEXT_MENTION and getattr(entity, "user", None):
            if entity.user.id == bot_id:
                return True
        if entity.type == MessageEntityType.MENTION:
            mention_text = text[entity.offset : entity.offset + entity.length].lower()
            if mention_text == f"@{bot_username}":
                    return True

    return f"@{bot_username}" in text.lower()


def _is_reply_to_bot(replied: types.Message, bot_id: int, bot_username: str | None) -> bool:
    if replied.from_user and replied.from_user.id == bot_id:
        return True
    if replied.from_user and bot_username and replied.from_user.username:
        if replied.from_user.username.lower() == bot_username.lower():
            return True
    if replied.via_bot and replied.via_bot.id == bot_id:
        return True
    if replied.via_bot and bot_username and replied.via_bot.username:
        if replied.via_bot.username.lower() == bot_username.lower():
            return True
    if replied.sender_chat and replied.sender_chat.id == bot_id:
        return True
    if replied.sender_chat and bot_username and replied.sender_chat.username:
        if replied.sender_chat.username.lower() == bot_username.lower():
            return True
    return False


def _is_reply(message: types.Message, bot_id: int, bot_username: str | None) -> bool:
    if not message.reply_to_message:
        return False
    return _is_reply_to_bot(message.reply_to_message, bot_id, bot_username)


def _should_reply(is_mention: bool, is_reply: bool, chat_type: ChatType | str | None) -> bool:
    if chat_type == ChatType.PRIVATE or chat_type == "private":
        return True
    if is_reply:
        return True
    return is_mention


def _resolve_prompt_token_limit(conf: dict[str, object]) -> int:
    raw = conf.get("context_max_prompt_tokens", 32000)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 32000
    return max(2000, min(60000, value))


def _unsupported_media_text(message: types.Message) -> str:
    kind = "это"
    if getattr(message, "sticker", None):
        kind = "стикеры"
    elif getattr(message, "animation", None):
        kind = "гифки"
    elif getattr(message, "photo", None):
        kind = "изображения"
    elif getattr(message, "video", None):
        kind = "видео"
    elif getattr(message, "document", None):
        kind = "файлы"
    elif getattr(message, "voice", None):
        kind = "голосовые"
    elif getattr(message, "video_note", None):
        kind = "круглые видео"
    return f"Я пока умею только читать текст. {kind.capitalize()} ещё не понимаю."
