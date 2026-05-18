from __future__ import annotations

import logging

from aiogram import Bot, Router, types
from aiogram.filters import Command, CommandObject

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
