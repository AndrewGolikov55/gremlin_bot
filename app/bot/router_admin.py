from __future__ import annotations

from datetime import datetime
from html import escape

from aiogram import F, Router, types
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

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
        prob = conf.get("interject_p", 0)
        cooldown = conf.get("interject_cooldown", 60)
        revive_enabled = conf.get("revive_enabled", False)
        revive_hours = int(conf.get("revive_after_hours", 48) or 48)
        revive_days = max(1, revive_hours // 24)
        return await message.reply(
            f"Статус: {'ON' if active else 'OFF'}\n"
            "Реакция: упоминания и ответы\n"
            f"Вероятность вмешательства: {prob}%\n"
            f"Кулдаун: {cooldown}с\n"
            f"Оживление: {'включено' if revive_enabled else 'выключено'} (порог {revive_days} д.)"
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
    await _send_settings(message, conf)


@router.message(Command("trigger"))
async def cmd_trigger(message: types.Message, command: CommandObject, settings: SettingsService):
    await message.reply("Бот всегда отвечает на упоминания и ответы на свои сообщения. Отдельная настройка режима не требуется.")


@router.message(Command("interject"))
async def cmd_interject(message: types.Message, command: CommandObject, settings: SettingsService):
    args = (command.args or "").strip().split()
    if len(args) != 2 or args[0].lower() not in {"p", "cooldown"}:
        return await message.reply("Использование: /interject p 0-100 или /interject cooldown секунды", parse_mode=None)

    action = args[0].lower()
    value = args[1]
    if action == "p":
        if not value.isdigit():
            return await message.reply("Вероятность должна быть числом 0-100")
        prob = int(value)
        if not 0 <= prob <= 100:
            return await message.reply("Вероятность должна быть в диапазоне 0-100")
        await settings.set(message.chat.id, "interject_p", prob)
        return await message.reply(f"Вероятность вмешательства {prob}%")

    if not value.isdigit():
        return await message.reply("Кулдаун задаётся целым числом секунд")
    cooldown = int(value)
    if cooldown < 10:
        return await message.reply("Кулдаун должен быть не меньше 10 секунд")
    await settings.set(message.chat.id, "interject_cooldown", cooldown)
    return await message.reply(f"Кулдаун вмешательства {cooldown} сек")


@router.message(Command("quiet"))
async def cmd_quiet(message: types.Message, command: CommandObject, settings: SettingsService):
    arg = (command.args or "").strip().lower()
    if not arg:
        return await message.reply("Использование: /quiet 23:00-08:00 или /quiet off")
    if arg == "off":
        await settings.set(message.chat.id, "quiet_hours", None)
        return await message.reply("Тихие часы отключены")

    try:
        start, end = _parse_time_range(arg)
    except ValueError:
        return await message.reply("Формат тихих часов: /quiet 23:00-08:00 или /quiet off")

    await settings.set(message.chat.id, "quiet_hours", f"{start}-{end}")
    return await message.reply(f"Тихие часы: {start}-{end}")


@router.message(Command("style"))
async def cmd_style(message: types.Message, command: CommandObject, settings: SettingsService):
    value = (command.args or "").strip().lower()
    allowed = {"neutral", "sarcastic", "aggressive", "dry", "friendly"}
    if value not in allowed:
        return await message.reply("Использование: /style neutral|sarcastic|aggressive|dry|friendly", parse_mode=None)
    await settings.set(message.chat.id, "style", value)
    await message.reply(f"Стиль ответа: {value}")


@router.message(Command("tone"))
async def cmd_tone(message: types.Message, command: CommandObject, settings: SettingsService):
    value = (command.args or "").strip()
    if not value.isdigit():
        return await message.reply("Использование: /tone 0-10")
    tone = int(value)
    if not 0 <= tone <= 10:
        return await message.reply("Тон должен быть в диапазоне 0-10")
    await settings.set(message.chat.id, "tone", tone)
    await message.reply(f"Тональность установлена: {tone}")


@router.message(Command("length"))
async def cmd_length(message: types.Message, command: CommandObject, settings: SettingsService):
    value = (command.args or "").strip()
    if not value.isdigit():
        return await message.reply("Использование: /length число_символов")
    length = int(value)
    if length < 50 or length > 1000:
        return await message.reply("Длина ответа должна быть в диапазоне 50-1000 символов")
    await settings.set(message.chat.id, "max_length", length)
    await message.reply(f"Максимальная длина ответа: {length}")


@router.message(Command("context"))
async def cmd_context(message: types.Message, command: CommandObject, settings: SettingsService):
    args = (command.args or "").strip().split()
    if len(args) != 2 or args[0].lower() != "max_turns" or not args[1].isdigit():
        return await message.reply("Использование: /context max_turns число", parse_mode=None)
    turns = int(args[1])
    if turns < 5 or turns > 100:
        return await message.reply("Количество сообщений в контексте должно быть между 5 и 100")
    await settings.set(message.chat.id, "context_max_turns", turns)
    await message.reply(f"Контекст: последние {turns} сообщений")


def _parse_time_range(value: str) -> tuple[str, str]:
    if "-" not in value:
        raise ValueError
    start_raw, end_raw = value.split("-", 1)
    _validate_time(start_raw)
    _validate_time(end_raw)
    return start_raw, end_raw


def _validate_time(value: str) -> None:
    datetime.strptime(value, "%H:%M")


STYLE_OPTIONS = ["neutral", "sarcastic", "aggressive", "dry", "friendly"]
STYLE_LABELS = {
    "neutral": "нейтральный",
    "sarcastic": "саркастичный",
    "aggressive": "агрессивный",
    "dry": "сухой",
    "friendly": "дружелюбный",
}
PROFANITY_OPTIONS = ["off", "soft", "hard"]
PROFANITY_LABELS = {
    "off": "запрещена",
    "soft": "мягкая",
    "hard": "разрешена",
}
QUIET_OPTIONS = ["off", "23:00-08:00", "00:00-06:00"]
QUIET_LABELS = {
    "off": "нет",
    "23:00-08:00": "23:00–08:00",
    "00:00-06:00": "00:00–06:00",
}


def _render_settings(conf: dict[str, object]) -> tuple[str, InlineKeyboardMarkup]:
    active = bool(conf.get("is_active", True))
    style_raw = str(conf.get("style", "neutral"))
    style_label = STYLE_LABELS.get(style_raw, style_raw)
    profanity_raw = str(conf.get("profanity", "soft"))
    profanity_label = PROFANITY_LABELS.get(profanity_raw, profanity_raw)
    quiet_value = conf.get("quiet_hours") or "off"
    quiet_label = QUIET_LABELS.get(quiet_value, quiet_value)
    interject_p = int(conf.get("interject_p", 0) or 0)
    revive_enabled = bool(conf.get("revive_enabled", False))
    revive_hours = int(conf.get("revive_after_hours", 48) or 48)
    revive_days = max(1, revive_hours // 24)

    text = (
        "<b>⚙️ Настройки бота ⚙️</b>\n"
        #"<i>Подберите поведение прямо в чате:</i>\n"
        #"• 🎯 Режим реакции\n"
        #"• 🌙 Тихие часы\n"
        #"• 🎭 Стиль поведения\n"
        #"• 🛡️ Политика лексики\n"
        #"• 🎲 Вероятность вмешательств\n"
        #"<i>Кулдаун и прочие тонкие параметры настраиваются в админ-панели.</i>"
    )

    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{'🟢 Включён' if active else '🔴 Выключен'}",
        callback_data="settings:toggle:is_active",
    )
    builder.adjust(1)
    builder.button(
        text=f"🌙 Тихие часы: {quiet_label}",
        callback_data="settings:cycle:quiet_hours",
    )
    builder.adjust(1)
    builder.button(
        text=f"🎭 Стиль: {style_label}",
        callback_data="settings:cycle:style",
    )
    builder.adjust(1)
    builder.button(
        text=f"🛡️ Брань: {profanity_label}",
        callback_data="settings:cycle:profanity",
    )
    builder.adjust(1)
    builder.button(
        text=f"🎲 Вероятность: {interject_p}%",
        callback_data="settings:adjust:interject_p",
    )
    builder.adjust(1)
    builder.button(
        text=("💤 Оживление: ВКЛ" if revive_enabled else "💤 Оживление: ВЫКЛ"),
        callback_data="settings:toggle:revive_enabled",
    )
    builder.adjust(1)
    builder.button(
        text=f"⏳ Порог тишины: {revive_days} д",
        callback_data="settings:adjust:revive_after_hours",
    )
    builder.adjust(1)

    return text, builder.as_markup()


async def _send_settings(message: types.Message, conf: dict[str, object]) -> None:
    text, keyboard = _render_settings(conf)
    await message.reply(text, reply_markup=keyboard)


async def _edit_settings(message: types.Message, conf: dict[str, object]) -> None:
    text, keyboard = _render_settings(conf)
    await message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("settings:"))
async def cb_settings(query: types.CallbackQuery, settings: SettingsService):
    chat_id = query.message.chat.id if query.message else None
    if chat_id is None:
        await query.answer()
        return

    parts = query.data.split(":") if query.data else []
    if len(parts) < 2:
        await query.answer()
        return

    action = parts[1]
    conf = await settings.get_all(chat_id)

    if action == "toggle" and len(parts) >= 3:
        key = parts[2]
        current = bool(conf.get(key, False))
        new_value = not current
        await settings.set(chat_id, key, new_value)
        await query.answer("Включено" if new_value else "Выключено", show_alert=False)
    elif action == "cycle" and len(parts) >= 3:
        key = parts[2]
        options = {
            "style": STYLE_OPTIONS,
            "profanity": PROFANITY_OPTIONS,
            "quiet_hours": QUIET_OPTIONS,
        }.get(key)
        if options:
            if key == "quiet_hours":
                raw_value = conf.get(key)
                current = raw_value if raw_value else "off"
            else:
                current = str(conf.get(key, options[0]))
            try:
                idx = options.index(current)
            except ValueError:
                idx = 0
            new_value = options[(idx + 1) % len(options)]
            stored = None if key == "quiet_hours" and new_value == "off" else new_value
            await settings.set(chat_id, key, stored)
            await query.answer(f"{key}: {new_value}")
    elif action == "adjust":
        if len(parts) >= 3:
            key = parts[2]
            if key == "interject_p":
                current = int(conf.get("interject_p", 0) or 0)
                if current < 5:
                    next_value = current + 1
                elif current == 5:
                    next_value = 10
                elif current < 50:
                    next_value = min(50, current + 5)
                else:
                    next_value = current + 10
                    if next_value > 100:
                        next_value = 0
                await settings.set(chat_id, "interject_p", next_value)
                await query.answer(f"Вероятность: {next_value}%")
            elif key == "revive_after_hours":
                current_hours = int(conf.get("revive_after_hours", 48) or 48)
                current_days = max(1, current_hours // 24)
                next_days = current_days + 1 if current_days < 7 else 1
                await settings.set(chat_id, "revive_after_hours", next_days * 24)
                await query.answer(f"Порог тишины: {next_days} д")
            else:
                await query.answer("Недоступно", show_alert=True)
        else:
            await query.answer("Недоступно", show_alert=True)
    elif action == "refresh":
        await query.answer("Обновлено")
    else:
        await query.answer()

    updated = await settings.get_all(chat_id)
    await _edit_settings(query.message, updated)
