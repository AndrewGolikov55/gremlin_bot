from datetime import datetime, timezone
import logging

from aiogram import Bot, F, Router, types
from aiogram.enums import ChatType, MessageEntityType
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.chat import Chat
from ..models.message import Message
from ..models.user import User
from ..services.context import ContextService, build_messages, build_system_prompt
from ..services.interjector import InterjectorService
from ..services.llm.ollama import generate as llm_generate
from ..services.moderation import apply_moderation
from ..services.settings import SettingsService


logger = logging.getLogger(__name__)


router = Router(name="triggers")


@router.message(F.text)
async def collect_messages(
    message: types.Message,
    session: AsyncSession,
    settings: SettingsService,
    context: ContextService,
    interjector: InterjectorService,
    bot: Bot,
):
    bot_user = await bot.get_me()

    if _is_own_message(message, bot_user.id):
        return

    await _ensure_chat(session, message)
    await _upsert_user(session, message)
    await _store_message(session, message)

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
    is_reply_to_bot = _is_reply(message, bot_user.id)

    logger.debug(
        "Trigger check chat=%s type=%s mention=%s reply=%s text=%r entities=%s",
        message.chat.id,
        message.chat.type,
        is_mention,
        is_reply_to_bot,
        message.text,
        message.entities,
    )

    max_turns = int(conf.get("context_max_turns", 100) or 100)
    turns = await context.get_recent_turns(session, message.chat.id, max_turns)

    if _should_reply(is_mention, is_reply_to_bot, message.chat.type):
        focus_text = None
        if is_reply_to_bot:
            focus_text = (message.text or message.caption or "").strip()
            if not focus_text:
                focus_text = None
        system_prompt = build_system_prompt(conf, focus_text)
        messages_for_llm = build_messages(system_prompt, turns, max_turns)

        try:
            max_length_conf = conf.get("max_length")
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
                temperature=float(conf.get("temperature", 0.8) or 0.8),
                top_p=float(conf.get("top_p", 0.9) or 0.9),
            )
        except Exception:
            await message.reply("🤖 Не удалось подготовить ответ (LLM недоступна).")
            return

        reply_text = apply_moderation(raw_reply, conf.get("profanity", "soft"))
        if not reply_text.strip():
            return

        await message.reply(reply_text.strip())
        return

    await interjector.maybe_reply_to_message(message, conf, turns)


async def _ensure_chat(session: AsyncSession, message: types.Message) -> None:
    chat = await session.get(Chat, message.chat.id)
    if chat is None:
        chat = Chat(id=message.chat.id, title=message.chat.title or str(message.chat.id), is_active=True)
        session.add(chat)
    elif message.chat.title and chat.title != message.chat.title:
        chat.title = message.chat.title


async def _upsert_user(session: AsyncSession, message: types.Message) -> None:
    if not message.from_user:
        return

    stmt = select(User).where(User.tg_id == message.from_user.id)
    res = await session.execute(stmt)
    user = res.scalar_one_or_none()

    username = message.from_user.username or message.from_user.full_name
    if user is None:
        user = User(tg_id=message.from_user.id, username=username, is_admin_cached=False)
        session.add(user)
    else:
        if username and user.username != username:
            user.username = username


async def _store_message(session: AsyncSession, message: types.Message) -> None:
    stmt = select(Message.id).where(
        Message.chat_id == message.chat.id,
        Message.message_id == message.message_id,
    )
    res = await session.execute(stmt)
    if res.scalar_one_or_none() is not None:
        return

    msg_date = message.date or datetime.utcnow()
    if msg_date.tzinfo is not None:
        msg_date = msg_date.astimezone(timezone.utc).replace(tzinfo=None)

    msg = Message(
        chat_id=message.chat.id,
        message_id=message.message_id,
        user_id=message.from_user.id if message.from_user else 0,
        text=message.text or "",
        reply_to_id=message.reply_to_message.message_id if message.reply_to_message else None,
        date=msg_date,
        is_bot=bool(message.from_user and message.from_user.is_bot),
    )
    session.add(msg)


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


def _is_reply(message: types.Message, bot_id: int) -> bool:
    if not message.reply_to_message:
        return False
    replied = message.reply_to_message.from_user
    return bool(replied and replied.id == bot_id)


def _should_reply(is_mention: bool, is_reply: bool, chat_type: ChatType | str | None) -> bool:
    if chat_type == ChatType.PRIVATE or chat_type == "private":
        return True
    if is_reply:
        return True
    return is_mention
