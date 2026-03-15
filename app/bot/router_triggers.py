import base64
import io
import logging
import mimetypes
import re

from aiogram import Bot, F, Router, types
from aiogram.enums import ChatType, MessageEntityType
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
    generate as llm_generate,
    resolve_llm_options,
)
from ..services.message_history import store_telegram_message
from ..services.moderation import apply_moderation
from ..services.settings import SettingsService
from ..services.app_config import AppConfigService
from ..services.persona import StylePromptService
from ..services.reactions import ReactionService
from ..services.usage_limits import UsageLimiter
from ..services.user_memory import UserMemoryService
from ..utils.llm import resolve_temperature
from .constants import START_PRIVATE_RESPONSE


logger = logging.getLogger(__name__)


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
    provider, fallback_enabled = resolve_llm_options(app_conf)
    base_prompt = str(app_conf.get("prompt_chat_base") or DEFAULT_CHAT_PROMPT)
    focus_suffix = str(app_conf.get("prompt_focus_suffix") or DEFAULT_FOCUS_SUFFIX)
    personalization_enabled = bool(conf.get("personalization_enabled", True))

    max_turns = int(app_conf.get("context_max_turns", 100) or 100)
    prompt_token_limit = _resolve_prompt_token_limit(app_conf)
    style_prompts = await personas.get_all()
    turns = await context.get_recent_turns(session, message.chat.id, max_turns)
    await reactions.maybe_react_to_message(message, conf, app_conf, turns)

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

            raw_reply = await llm_generate(
                messages_for_llm,
                max_tokens=max_tokens,
                temperature=resolve_temperature(conf),
                top_p=float(conf.get("top_p", 0.9) or 0.9),
                provider=provider,
                fallback_enabled=fallback_enabled,
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
                "Unexpected error while generating LLM reply (provider=%s fallback=%s)",
                provider,
                fallback_enabled,
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

    await interjector.maybe_reply_to_message(message, conf, turns)


@router.message(F.sticker | F.animation | F.photo | F.video | F.document)
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

    is_mention = _is_bot_mentioned(message, bot_user.id, bot_user.username)
    is_reply_to_bot = _is_reply(message, bot_user.id, bot_user.username)
    if not _should_reply(is_mention, is_reply_to_bot, message.chat.type):
        if message.photo:
            app_conf = await app_config.get_all()
            max_turns = int(app_conf.get("context_max_turns", 100) or 100)
            turns = await context.get_recent_turns(session, message.chat.id, max_turns)
            await interjector.maybe_reply_to_message(message, conf, turns)
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
            bot=bot,
            conf=conf,
        )
        return

    await message.reply(_unsupported_media_text(message), parse_mode=None)


async def _handle_photo_reply(
    *,
    message: types.Message,
    session: AsyncSession,
    context: ContextService,
    personas: StylePromptService,
    app_config: AppConfigService,
    usage_limits: UsageLimiter,
    memory: UserMemoryService,
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
    messages_for_llm = build_messages(
        system_prompt,
        turns,
        max_turns,
        prompt_token_limit,
        context_blocks=[memory_block] if memory_block else None,
    )
    messages_for_llm[-1] = {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": _build_photo_prompt_text(focus_text),
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": image_data_url,
                    "detail": _resolve_vision_detail(message),
                },
            },
        ],
    }

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

        raw_reply = await llm_generate(
            messages_for_llm,
            max_tokens=max_tokens,
            temperature=resolve_temperature(conf),
            top_p=float(conf.get("top_p", 0.9) or 0.9),
            provider="openai",
            fallback_enabled=False,
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


async def _download_photo_as_data_url(bot: Bot, message: types.Message) -> str | None:
    photo = _pick_photo_size(message)
    if photo is None:
        logger.warning("Photo message has no downloadable photo sizes message=%s", message.message_id)
        return None

    telegram_file = await bot.get_file(photo.file_id)
    if not telegram_file.file_path:
        logger.warning("Telegram returned empty file_path for message=%s file_id=%s", message.message_id, photo.file_id)
        return None

    buffer = io.BytesIO()
    await bot.download_file(telegram_file.file_path, destination=buffer)
    payload = buffer.getvalue()
    if not payload:
        logger.warning(
            "Downloaded empty image payload for message=%s file_path=%s",
            message.message_id,
            telegram_file.file_path,
        )
        return None

    mime_type, _encoding = mimetypes.guess_type(telegram_file.file_path)
    if not mime_type:
        mime_type = "image/jpeg"
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


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


def _is_reply(message: types.Message, bot_id: int, bot_username: str | None) -> bool:
    if not message.reply_to_message:
        return False
    replied = message.reply_to_message
    if replied.from_user and replied.from_user.id == bot_id:
        return True
    if replied.from_user and bot_username and replied.from_user.username:
        if replied.from_user.username.lower() == bot_username.lower():
            return True
    # Messages sent via inline mode reference the bot in via_bot
    if replied.via_bot and replied.via_bot.id == bot_id:
        return True
    if replied.via_bot and bot_username and replied.via_bot.username:
        if replied.via_bot.username.lower() == bot_username.lower():
            return True
    # Some chats can substitute sender_chat instead of from_user (e.g. topics or protected content)
    if replied.sender_chat and replied.sender_chat.id == bot_id:
        return True
    if replied.sender_chat and bot_username and replied.sender_chat.username:
        if replied.sender_chat.username.lower() == bot_username.lower():
            return True
    return False
    return False


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
    if message.sticker:
        kind = "стикеры"
    elif message.animation:
        kind = "гифки"
    elif message.photo:
        kind = "изображения"
    elif message.video:
        kind = "видео"
    elif message.document:
        kind = "файлы"
    return f"Я пока умею только читать текст. {kind.capitalize()} ещё не понимаю."
