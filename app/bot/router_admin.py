from aiogram import Router, types
from aiogram.filters import Command, CommandObject
from sqlalchemy.ext.asyncio import AsyncSession

from ..services.settings import SettingsService


router = Router(name="admin")


@router.message(Command("bot"))
async def cmd_bot(
    message: types.Message,
    command: CommandObject,
    settings: SettingsService,
):
    args = (command.args or "").strip().lower()
    if args == "on":
        await settings.set(message.chat.id, "is_active", True)
        return await message.reply("Бот включён в этом чате ✅")
    elif args == "off":
        await settings.set(message.chat.id, "is_active", False)
        return await message.reply("Бот выключен в этом чате ⛔")
    elif args == "status":
        conf = await settings.get_all(message.chat.id)
        active = conf.get("is_active", True)
        trigger_mode = conf.get("trigger_mode", "mention")
        prob = conf.get("interject_p", 0)
        cooldown = conf.get("interject_cooldown", 60)
        return await message.reply(
            f"Статус: {'ON' if active else 'OFF'}\n"
            f"Триггер: {trigger_mode}\n"
            f"Вероятность: {prob}%\nКулдаун: {cooldown}s"
        )
    else:
        return await message.reply("Использование: /bot on|off|status")


@router.message(Command("profanity"))
async def cmd_profanity(message: types.Message, command: CommandObject, settings: SettingsService):
    value = (command.args or "").strip().lower()
    if value not in {"off", "soft", "hard"}:
        return await message.reply("Использование: /profanity <hard|soft|off>")
    await settings.set(message.chat.id, "profanity", value)
    await message.reply(f"Установлено: profanity={value}")


@router.message(Command("settings"))
async def cmd_settings(message: types.Message, settings: SettingsService):
    conf = await settings.get_all(message.chat.id)
    # show a compact subset
    keys = [
        "is_active",
        "trigger_mode",
        "interject_p",
        "interject_cooldown",
        "quiet_hours",
        "style",
        "profanity",
        "max_length",
        "tone",
        "context_max_turns",
    ]
    text = "\n".join(f"{k}: {conf.get(k)}" for k in keys)
    await message.reply("Текущие настройки:\n" + text)

