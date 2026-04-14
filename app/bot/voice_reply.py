"""Voice-or-text reply helper shared by router and interjector.

Lives in app/bot/ to avoid a circular import with services/interjector
(interjector imports this; this imports only services/llm/tts + spontaneity).
"""
from __future__ import annotations

import logging

from aiogram import Bot, types
from aiogram.types import BufferedInputFile

from ..services.llm.tts import PERSONA_TTS_INSTRUCTIONS, synthesize_speech
from ..services.spontaneity import SpontaneityPolicy
from ..services.usage_limits import UsageLimiter

logger = logging.getLogger(__name__)


async def send_reply_maybe_voice(
    *,
    bot: Bot,
    message: types.Message,
    text: str,
    conf: dict[str, object],
    app_conf: dict[str, object],
    policy: SpontaneityPolicy,
    usage_limits: UsageLimiter,
    incoming_is_voice_reply_to_bot: bool,
) -> types.Message | None:
    """Send reply as voice if policy + budget allow; fall back to text on any failure.

    Single entry point for bot replies that could be voice. NOT used for
    error-text (rate-limit, generation failures) or voice excuses — those
    stay as plain message.reply(text).
    """
    chat_id = message.chat.id

    if not bool(app_conf.get("tts_enabled", True)):
        return await message.reply(text)

    if not await policy.should_reply_with_voice(
        chat_id, incoming_is_voice_reply_to_bot=incoming_is_voice_reply_to_bot,
    ):
        return await message.reply(text)

    tts_limit = int(app_conf.get("tts_daily_limit", 0) or 0)
    if tts_limit > 0:
        allowed, _, _ = await usage_limits.consume(chat_id, [("tts", tts_limit)])
        if not allowed:
            return await message.reply(text)

    persona = str(conf.get("style", "gopnik"))
    voice = str(app_conf.get(f"tts_voice_{persona}", "onyx"))
    instructions = PERSONA_TTS_INSTRUCTIONS.get(persona)

    audio_bytes = await synthesize_speech(text, voice=voice, instructions=instructions)
    if audio_bytes is None:
        return await message.reply(text)

    try:
        return await bot.send_voice(
            chat_id,
            voice=BufferedInputFile(audio_bytes, filename="reply.ogg"),
            reply_to_message_id=message.message_id,
        )
    except Exception:
        logger.exception(
            "bot.send_voice failed, falling back to text chat=%s", chat_id,
        )
        return await message.reply(text)


async def send_chat_maybe_voice(
    *,
    bot: Bot,
    chat_id: int,
    text: str,
    conf: dict[str, object],
    app_conf: dict[str, object],
    policy: SpontaneityPolicy,
    usage_limits: UsageLimiter,
) -> types.Message | None:
    """Like send_reply_maybe_voice but for chat-targeted messages without an incoming reference.

    Used by revive (no incoming user message). Always uses tts_reply_p
    (revive never has voice-reply boost signal). Falls back to bot.send_message
    on any TTS failure.
    """
    if not bool(app_conf.get("tts_enabled", True)):
        return await bot.send_message(chat_id, text)

    if not await policy.should_reply_with_voice(
        chat_id, incoming_is_voice_reply_to_bot=False,
    ):
        return await bot.send_message(chat_id, text)

    tts_limit = int(app_conf.get("tts_daily_limit", 0) or 0)
    if tts_limit > 0:
        allowed, _, _ = await usage_limits.consume(chat_id, [("tts", tts_limit)])
        if not allowed:
            return await bot.send_message(chat_id, text)

    persona = str(conf.get("style", "gopnik"))
    voice = str(app_conf.get(f"tts_voice_{persona}", "onyx"))
    instructions = PERSONA_TTS_INSTRUCTIONS.get(persona)

    audio_bytes = await synthesize_speech(text, voice=voice, instructions=instructions)
    if audio_bytes is None:
        return await bot.send_message(chat_id, text)

    try:
        return await bot.send_voice(
            chat_id,
            voice=BufferedInputFile(audio_bytes, filename="reply.ogg"),
        )
    except Exception:
        logger.exception(
            "bot.send_voice failed for chat-targeted message, falling back chat=%s", chat_id,
        )
        return await bot.send_message(chat_id, text)
