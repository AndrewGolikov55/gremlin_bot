from __future__ import annotations

import logging

from aiogram import F, Router, types
from aiogram.filters import Command, CommandObject

from ..services.games.spy import SpyService
from ..services.quick_games import QuickGameService

router = Router(name="games_extra")
logger = logging.getLogger("bot.games_extra")


def _target_from_reply_or_arg(message: types.Message, command: CommandObject | None) -> str | None:
    """Resolve target string from a reply (`@username` of the replied user) or command args."""
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        if u.username:
            return f"@{u.username}"
    if command is not None and command.args:
        return command.args.strip().split()[0]
    return None


@router.message(Command("truth"))
async def cmd_truth(
    message: types.Message,
    command: CommandObject,
    quick_games: QuickGameService,
) -> None:
    chat = message.chat
    user = message.from_user
    if chat.type not in {"group", "supergroup"} or user is None:
        await message.answer("Игра доступна только в групповых чатах.")
        return
    target = _target_from_reply_or_arg(message, command)
    await quick_games.run_truth_or_dare(
        chat_id=chat.id, initiator_id=user.id, target_arg=target,
    )


@router.message(Command("horoscope"))
async def cmd_horoscope(
    message: types.Message,
    command: CommandObject,
    quick_games: QuickGameService,
) -> None:
    chat = message.chat
    user = message.from_user
    if chat.type not in {"group", "supergroup"} or user is None:
        await message.answer("Игра доступна только в групповых чатах.")
        return
    target = _target_from_reply_or_arg(message, command)
    await quick_games.run_horoscope(
        chat_id=chat.id, initiator_id=user.id, target_arg=target,
    )


@router.message(Command("fortune"))
async def cmd_fortune(
    message: types.Message,
    quick_games: QuickGameService,
) -> None:
    chat = message.chat
    user = message.from_user
    if user is None:
        return
    await quick_games.run_fortune(chat_id=chat.id, initiator_id=user.id)


@router.message(Command("wisdom"))
async def cmd_wisdom(
    message: types.Message,
    quick_games: QuickGameService,
) -> None:
    chat = message.chat
    user = message.from_user
    if chat.type not in {"group", "supergroup"} or user is None:
        await message.answer("Игра доступна только в групповых чатах.")
        return
    await quick_games.run_wisdom(chat_id=chat.id, initiator_id=user.id)


@router.message(Command("predict"))
async def cmd_predict(
    message: types.Message,
    command: CommandObject,
    quick_games: QuickGameService,
) -> None:
    chat = message.chat
    user = message.from_user
    if chat.type not in {"group", "supergroup"} or user is None:
        await message.answer("Игра доступна только в групповых чатах.")
        return
    target = _target_from_reply_or_arg(message, command)
    await quick_games.run_predict(
        chat_id=chat.id, initiator_id=user.id, target_arg=target,
    )


# ---------------- /spy ----------------

def _require_group(message: types.Message) -> bool:
    return message.chat.type in {"group", "supergroup"} and message.from_user is not None


@router.message(Command("spy"))
async def cmd_spy(message: types.Message, spy: SpyService) -> None:
    if not _require_group(message):
        await message.answer("Игра доступна только в групповых чатах.")
        return
    await spy.start_lobby(chat_id=message.chat.id, initiator_id=message.from_user.id)


@router.message(Command("spy_join"))
async def cmd_spy_join(message: types.Message, spy: SpyService) -> None:
    if not _require_group(message):
        return
    await spy.join(chat_id=message.chat.id, user_id=message.from_user.id)


@router.message(Command("spy_start"))
async def cmd_spy_start(message: types.Message, spy: SpyService) -> None:
    if not _require_group(message):
        return
    await spy.start_round(chat_id=message.chat.id, initiator_id=message.from_user.id)


@router.message(Command("spy_vote"))
async def cmd_spy_vote(message: types.Message, spy: SpyService) -> None:
    if not _require_group(message):
        return
    await spy.start_vote(chat_id=message.chat.id, initiator_id=message.from_user.id)


@router.message(Command("spy_abort"))
async def cmd_spy_abort(message: types.Message, spy: SpyService) -> None:
    if not _require_group(message):
        return
    await spy.abort(chat_id=message.chat.id, initiator_id=message.from_user.id)


@router.callback_query(F.data.startswith("spy:reveal:"))
async def cb_spy_reveal(query: types.CallbackQuery, spy: SpyService) -> None:
    try:
        round_id = int(query.data.split(":", 2)[2])
    except (ValueError, IndexError):
        await query.answer("Неверная кнопка.", show_alert=True)
        return
    if query.message is None or query.from_user is None:
        await query.answer("Что-то пошло не так.", show_alert=True)
        return
    text, _found = await spy.reveal_role(
        chat_id=query.message.chat.id, user_id=query.from_user.id, round_id=round_id,
    )
    await query.answer(text, show_alert=True)
