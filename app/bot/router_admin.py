from __future__ import annotations

from datetime import datetime
from html import escape

from aiogram import F, Router, types
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardMarkup, ForceReply
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..services.settings import SettingsService
from ..services.persona import StylePromptService
from ..services.app_config import AppConfigService


router = Router(name="admin")

PROMPT_TEXT = "Введите новое прозвище для рулетки (или напишите 'reset' чтобы сбросить)."


@router.message(Command("bot"))
async def cmd_bot(
    message: types.Message,
    command: CommandObject,
    settings: SettingsService,
    app_config: AppConfigService,
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
        app_conf = await app_config.get_all()
        active = conf.get("is_active", True)
        prob = app_conf.get("interject_p", 0)
        cooldown = app_conf.get("interject_cooldown", 60)
        revive_enabled = conf.get("revive_enabled", False)
        revive_hours = int(conf.get("revive_after_hours", 48) or 48)
        revive_days = max(1, revive_hours // 24)
        quiet_value = conf.get("quiet_hours") or "off"
        quiet_label = QUIET_LABELS.get(quiet_value, quiet_value)
        return await message.reply(
            f"Статус: {'ON' if active else 'OFF'}\n"
            "Реакция: упоминания и ответы\n"
            f"Тихие часы: {quiet_label}\n"
            f"Вероятность вмешательства: {prob}%\n"
            f"Кулдаун: {cooldown}с\n"
            f"Оживление: {'включено' if revive_enabled else 'выключено'} (порог {revive_days} д.)"
        )
    else:
        return await message.reply("Использование: /bot on|off|status")


@router.message(Command("settings"))
async def cmd_settings(
    message: types.Message,
    settings: SettingsService,
    personas: StylePromptService,
    app_config: AppConfigService,
):
    conf = await settings.get_all(message.chat.id)
    app_conf = await app_config.get_all()
    await _send_settings(message, conf, app_conf, personas)


@router.message(Command("trigger"))
async def cmd_trigger(message: types.Message, command: CommandObject, settings: SettingsService):
    await message.reply("Бот всегда отвечает на упоминания и ответы на свои сообщения. Отдельная настройка режима не требуется.")


@router.message(Command("interject"))
async def cmd_interject(
    message: types.Message,
    command: CommandObject,
    _settings: SettingsService,
    app_config: AppConfigService,
):
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
        await app_config.set("interject_p", prob)
        return await message.reply(f"Вероятность вмешательства {prob}% (глобально)")

    if not value.isdigit():
        return await message.reply("Кулдаун задаётся целым числом секунд")
    cooldown = int(value)
    if cooldown < 10:
        return await message.reply("Кулдаун должен быть не меньше 10 секунд")
    await app_config.set("interject_cooldown", cooldown)
    return await message.reply(f"Кулдаун вмешательства {cooldown} сек (глобально)")


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
async def cmd_style(
    message: types.Message,
    command: CommandObject,
    settings: SettingsService,
    personas: StylePromptService,
):
    value = (command.args or "").strip().lower()
    style_options = await personas.list_styles()
    allowed = {slug for slug, _ in style_options}
    if value not in allowed:
        options_text = ", ".join(
            f"{title} ({slug})" for slug, title in style_options
        ) or "<нет доступных персон>"
        return await message.reply(f"Доступные стили: {options_text}")
    await settings.set(message.chat.id, "style", value)
    labels = {slug: title for slug, title in style_options}
    await message.reply(f"Стиль ответа: {labels.get(value, value)}")


@router.message(Command("length"))
async def cmd_length(
    message: types.Message,
    command: CommandObject,
    _settings: SettingsService,
    app_config: AppConfigService,
):
    value = (command.args or "").strip()
    if not value.isdigit():
        return await message.reply("Использование: /length число_символов")
    length = int(value)
    if length < 50 or length > 1000:
        return await message.reply("Длина ответа должна быть в диапазоне 50-1000 символов")
    await app_config.set("max_length", length)
    await message.reply(f"Максимальная длина ответа (глобально): {length}")


@router.message(Command("context"))
async def cmd_context(
    message: types.Message,
    command: CommandObject,
    _settings: SettingsService,
    app_config: AppConfigService,
):
    args = (command.args or "").strip().split()
    if len(args) != 2 or not args[1].isdigit():
        return await message.reply(
            "Использование: /context max_turns N или /context max_tokens N",
            parse_mode=None,
        )

    key = args[0].lower()
    value = int(args[1])

    if key == "max_turns":
        if value < 5 or value > 100:
            return await message.reply("Количество сообщений в контексте должно быть между 5 и 100")
        await app_config.set("context_max_turns", value)
        await message.reply(f"Контекст: последние {value} сообщений (глобально)")
        return

    if key == "max_tokens":
        if value < 2000 or value > 60000:
            return await message.reply("Окно контекста должно быть в пределах 2000-60000 токенов")
        await app_config.set("context_max_prompt_tokens", value)
        await message.reply(f"Макс. окно контекста: {value} токенов (глобально)")
        return

    await message.reply(
        "Использование: /context max_turns N или /context max_tokens N",
        parse_mode=None,
    )


def _parse_time_range(value: str) -> tuple[str, str]:
    if "-" not in value:
        raise ValueError
    start_raw, end_raw = value.split("-", 1)
    _validate_time(start_raw)
    _validate_time(end_raw)
    return start_raw, end_raw


def _validate_time(value: str) -> None:
    datetime.strptime(value, "%H:%M")


QUIET_OPTIONS = ["off", "23:00-08:00", "00:00-06:00"]
QUIET_LABELS = {
    "off": "нет",
    "23:00-08:00": "23:00–08:00",
    "00:00-06:00": "00:00–06:00",
}


def _render_settings(
    conf: dict[str, object],
    app_conf: dict[str, object],
    style_options: list[tuple[str, str]],
) -> tuple[str, InlineKeyboardMarkup]:
    active = bool(conf.get("is_active", True))
    style_raw = str(conf.get("style", style_options[0][0] if style_options else "standup"))
    labels_map = {slug: title for slug, title in style_options}
    style_label = labels_map.get(style_raw, style_raw)
    quiet_value = conf.get("quiet_hours") or "off"
    quiet_label = QUIET_LABELS.get(quiet_value, quiet_value)
    interject_p = int(app_conf.get("interject_p", 0) or 0)
    interject_cooldown = int(app_conf.get("interject_cooldown", 60) or 60)
    context_turns = int(app_conf.get("context_max_turns", 100) or 100)
    context_tokens = int(app_conf.get("context_max_prompt_tokens", 32000) or 32000)
    revive_enabled = bool(conf.get("revive_enabled", False))
    revive_hours = int(conf.get("revive_after_hours", 48) or 48)
    revive_days = max(1, revive_hours // 24)
    roulette_auto = bool(conf.get("roulette_auto_enabled", False))
    custom_title = conf.get("roulette_custom_title")
    title_label = custom_title if custom_title else "по умолчанию"

    text = (
        "<b>⚙️ Настройки бота ⚙️</b>\n"
        #f"Стиль: {style_label}\n"
        #f"Тихие часы: {quiet_label}\n"
        #f"Вмешательства: {interject_p}% (кулдаун {interject_cooldown}с)\n"
        #f"Контекст: {context_turns} сообщений, окно {context_tokens} токенов\n"
        #"<i>Глобальные параметры меняются в админ-панели.</i>"
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
        text=("💤 Оживление: ВКЛ" if revive_enabled else "💤 Оживление: ВЫКЛ"),
        callback_data="settings:toggle:revive_enabled",
    )
    builder.adjust(1)
    builder.button(
        text=f"⏳ Порог тишины: {revive_days} д",
        callback_data="settings:adjust:revive_after_hours",
    )
    builder.adjust(1)
    builder.button(
        text=f"🎲 Авто-рулетка: {'ВКЛ' if roulette_auto else 'ВЫКЛ'}",
        callback_data="settings:toggle:roulette_auto",
    )
    builder.adjust(1)
    builder.button(
        text=f"🏷️ Прозвище: {title_label}",
        callback_data="settings:prompt:roulette_title",
    )
    builder.adjust(1)
    builder.button(
        text="🧹 Сбросить прозвище",
        callback_data="settings:clear:roulette_title",
    )
    builder.adjust(1)

    return text, builder.as_markup()


async def _send_settings(
    message: types.Message,
    conf: dict[str, object],
    app_conf: dict[str, object],
    personas: StylePromptService,
) -> None:
    style_options = await personas.list_styles()
    if not style_options:
        style_options = [("standup", "standup")]
    text, keyboard = _render_settings(conf, app_conf, style_options)
    await message.reply(text, reply_markup=keyboard)


async def _edit_settings(
    message: types.Message,
    conf: dict[str, object],
    app_conf: dict[str, object],
    personas: StylePromptService,
) -> None:
    style_options = await personas.list_styles()
    if not style_options:
        style_options = [("standup", "standup")]
    text, keyboard = _render_settings(conf, app_conf, style_options)
    await message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("settings:"))
async def cb_settings(
    query: types.CallbackQuery,
    settings: SettingsService,
    personas: StylePromptService,
    app_config: AppConfigService,
):
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
    app_conf = await app_config.get_all()
    style_options = await personas.list_styles()
    if not style_options:
        style_options = [("standup", "standup")]

    if action == "toggle" and len(parts) >= 3:
        key = parts[2]
        if key == "roulette_auto":
            current = bool(conf.get("roulette_auto_enabled", False))
            new_value = not current
            await settings.set(chat_id, "roulette_auto_enabled", new_value)
            await query.answer("Авто-рулетка включена" if new_value else "Авто-рулетка выключена")
        else:
            current = bool(conf.get(key, False))
            new_value = not current
            await settings.set(chat_id, key, new_value)
            await query.answer("Включено" if new_value else "Выключено", show_alert=False)
    elif action == "cycle" and len(parts) >= 3:
        key = parts[2]
        options = {
            "quiet_hours": QUIET_OPTIONS,
        }.get(key)
        if key == "style":
            slugs = [slug for slug, _ in style_options]
            current = str(conf.get("style", slugs[0] if slugs else "standup"))
            if not slugs:
                await query.answer("Нет доступных стилей", show_alert=True)
            else:
                try:
                    idx = slugs.index(current)
                except ValueError:
                    idx = 0
                new_style = slugs[(idx + 1) % len(slugs)]
                await settings.set(chat_id, "style", new_style)
                labels_map = {slug: title for slug, title in style_options}
                await query.answer(f"Стиль: {labels_map.get(new_style, new_style)}")
        elif options:
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
    elif action == "prompt" and len(parts) >= 3 and parts[2] == "roulette_title":
        await query.answer("Жду новое прозвище", show_alert=False)
        await query.message.answer(PROMPT_TEXT, reply_markup=ForceReply(selective=True))
    elif action == "clear" and len(parts) >= 3 and parts[2] == "roulette_title":
        await settings.set(chat_id, "roulette_custom_title", None)
        await query.answer("Прозвище сброшено")
    elif action == "adjust":
        if len(parts) >= 3:
            key = parts[2]
            if key == "revive_after_hours":
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
    app_conf = await app_config.get_all()
    await _edit_settings(query.message, updated, app_conf, personas)
