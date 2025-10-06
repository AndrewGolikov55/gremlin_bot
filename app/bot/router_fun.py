from __future__ import annotations
from aiogram import F, Router, types
from aiogram.filters import Command

from ..services.roulette import RouletteService
from ..services.settings import SettingsService


router = Router(name="fun")

PROMPT_TEXT = "Введите новое прозвище для рулетки (или напишите 'reset' чтобы сбросить)."


@router.message(Command("roll"))
async def cmd_roll(
    message: types.Message,
    roulette: RouletteService,
):
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Команда доступна только в групповых чатах.")
        return

    result = await roulette.roll(message.chat.id, initiator=str(message.from_user.id))
    if not result.success:
        await message.reply(result.message)


@router.message(Command("rollstats_montly"))
async def cmd_rollstats_monthly(message: types.Message, roulette: RouletteService):
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Команда доступна только в групповых чатах.")
        return
    stats = await roulette.get_stats_monthly(message.chat.id)
    await message.reply(stats)


@router.message(Command("rollstats_total"))
async def cmd_rollstats_total(message: types.Message, roulette: RouletteService):
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Команда доступна только в групповых чатах.")
        return
    stats = await roulette.get_stats_total(message.chat.id)
    await message.reply(stats)


@router.message(Command("reg"))
async def cmd_reg(message: types.Message, roulette: RouletteService):
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Команда доступна только в групповых чатах.")
        return
    if not message.from_user or message.from_user.is_bot:
        await message.reply("Ботам регистрироваться не нужно 😉")
        return
    user = message.from_user
    is_new, registered = await roulette.register_participant(
        message.chat.id,
        user.id,
        user.username,
    )
    try:
        total = await message.bot.get_chat_member_count(message.chat.id)
    except Exception:
        total = None
    suffix = f" (зарегистрировано: {registered})"
    if is_new:
        await message.reply(f"Вы зарегистрированы для рулетки{suffix}.")
    else:
        await message.reply(f"Вы уже в списке участников{suffix}.")


@router.message(Command("unreg"))
async def cmd_unreg(message: types.Message, roulette: RouletteService):
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Команда доступна только в групповых чатах.")
        return
    if not message.from_user or message.from_user.is_bot:
        await message.reply("Боты и так не участвуют.")
        return
    removed, registered = await roulette.unregister_participant(message.chat.id, message.from_user.id)
    suffix = f" (зарегистрировано: {registered})"
    if removed:
        await message.reply(f"Вы исключены из рулетки{suffix}.")
    else:
        await message.reply(f"Вас не было в списке участников{suffix}.")


@router.message(
    F.reply_to_message,
    F.reply_to_message.from_user.id == F.bot.id,
    F.reply_to_message.text == PROMPT_TEXT,
)
async def handle_custom_title_reply(
    message: types.Message,
    settings: SettingsService,
):
    chat_id = message.chat.id
    text = (message.text or "").strip()
    if not text or text.lower() in {"reset", "сброс", "отмена"}:
        await settings.set(chat_id, "roulette_custom_title", None)
        await message.reply("Прозвище сброшено.")
    else:
        await settings.set(chat_id, "roulette_custom_title", text)
        await message.reply(f"Новое прозвище установлено: {text}")


@router.message(Command("rolltitle"))
async def cmd_rolltitle(message: types.Message, settings: SettingsService):
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Команда доступна только в групповых чатах.")
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.reply("Использование: /rolltitle новое_прозвище")
        return
    title = args[1].strip()
    await settings.set(message.chat.id, "roulette_custom_title", title)
    await message.reply(f"Новое прозвище установлено: {title}")
