from datetime import datetime

from aiogram import F, Router, types
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.chat import Chat
from ..models.message import Message
from ..services.settings import SettingsService


router = Router(name="triggers")


@router.message(F.text)
async def collect_messages(message: types.Message, session: AsyncSession, settings: SettingsService):
    # Ensure chat exists
    res = await session.execute(select(Chat).where(Chat.id == message.chat.id))
    chat = res.scalar_one_or_none()
    if chat is None:
        chat = Chat(id=message.chat.id, title=message.chat.title or str(message.chat.id), is_active=True)
        session.add(chat)

    # Store message for context/analytics
    msg = Message(
        id=message.message_id,
        chat_id=message.chat.id,
        user_id=message.from_user.id if message.from_user else 0,
        text=message.text or "",
        reply_to_id=message.reply_to_message.message_id if message.reply_to_message else None,
        date=message.date or datetime.utcnow(),
        is_bot=bool(message.from_user and message.from_user.is_bot),
    )
    session.add(msg)

    # Commit in background (handler context)
    try:
        await session.commit()
    except Exception:
        await session.rollback()

    # Simple placeholder: if bot is mentioned and active, acknowledge
    conf = await settings.get_all(message.chat.id)
    if not conf.get("is_active", True):
        return

    if message.entities:
        if any(ent.type == "mention" and message.text and "@" in message.text for ent in message.entities):
            await message.reply("Я тут. Продолжайте. 🫡")

