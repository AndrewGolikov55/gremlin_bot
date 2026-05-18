from __future__ import annotations

import logging

from aiogram import F, Router, types
from aiogram.filters import Command, CommandObject

from ..services.games.akinator import AkinatorService
from ..services.games.rapbattle import RapbattleService
from ..services.games.spy import SpyService
from ..services.games.storychain import StorychainService
from ..services.games.wordchain import WordchainService
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

GROUP_ONLY_REFUSAL = "Игра доступна только в групповых чатах."


def _require_group(message: types.Message) -> bool:
    return message.chat.type in {"group", "supergroup"} and message.from_user is not None


async def _refuse_private(message: types.Message) -> None:
    """Reply with a clear refusal when a group-only command is used in DM."""
    if message.chat.type not in {"group", "supergroup"}:
        await message.answer(GROUP_ONLY_REFUSAL)


@router.message(Command("spy"))
async def cmd_spy(message: types.Message, spy: SpyService) -> None:
    if not _require_group(message):
        await _refuse_private(message)
        return
    await spy.start_lobby(chat_id=message.chat.id, initiator_id=message.from_user.id)


@router.message(Command("spy_join"))
async def cmd_spy_join(message: types.Message, spy: SpyService) -> None:
    if not _require_group(message):
        await _refuse_private(message)
        return
    await spy.join(chat_id=message.chat.id, user_id=message.from_user.id)


@router.message(Command("spy_start"))
async def cmd_spy_start(message: types.Message, spy: SpyService) -> None:
    if not _require_group(message):
        await _refuse_private(message)
        return
    await spy.start_round(chat_id=message.chat.id, initiator_id=message.from_user.id)


@router.message(Command("spy_vote"))
async def cmd_spy_vote(message: types.Message, spy: SpyService) -> None:
    if not _require_group(message):
        await _refuse_private(message)
        return
    await spy.start_vote(chat_id=message.chat.id, initiator_id=message.from_user.id)


@router.message(Command("spy_abort"))
async def cmd_spy_abort(message: types.Message, spy: SpyService) -> None:
    if not _require_group(message):
        await _refuse_private(message)
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


# ---------------- /akinator ----------------

@router.message(Command("akinator"))
async def cmd_akinator(message: types.Message, akinator: AkinatorService) -> None:
    if not _require_group(message):
        await _refuse_private(message)
        return
    await akinator.start(chat_id=message.chat.id, initiator_id=message.from_user.id)


@router.message(Command("akinator_ask"))
async def cmd_akinator_ask(
    message: types.Message, command: CommandObject, akinator: AkinatorService,
) -> None:
    if not _require_group(message):
        await _refuse_private(message)
        return
    question = (command.args or "").strip()
    await akinator.ask(
        chat_id=message.chat.id, asker_id=message.from_user.id, question=question,
    )


@router.message(Command("akinator_guess"))
async def cmd_akinator_guess(
    message: types.Message, command: CommandObject, akinator: AkinatorService,
) -> None:
    if not _require_group(message):
        await _refuse_private(message)
        return
    arg = (command.args or "").strip().split()
    target = arg[0] if arg else None
    if target is None and message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        if u.username:
            target = f"@{u.username}"
    await akinator.guess(
        chat_id=message.chat.id, asker_id=message.from_user.id, target_username=target,
    )


# ---------------- /wordchain ----------------

@router.message(Command("wordchain"))
async def cmd_wordchain(message: types.Message, wordchain: WordchainService) -> None:
    if not _require_group(message):
        await _refuse_private(message)
        return
    await wordchain.start(chat_id=message.chat.id)


@router.message(Command("wordchain_play"))
async def cmd_wordchain_play(
    message: types.Message, command: CommandObject, wordchain: WordchainService,
) -> None:
    if not _require_group(message):
        await _refuse_private(message)
        return
    arg = (command.args or "").strip().split()
    if not arg:
        await message.answer("Скажи одно слово: /wordchain_play кот")
        return
    await wordchain.play(
        chat_id=message.chat.id, user_id=message.from_user.id, raw_word=arg[0],
    )


@router.message(Command("wordchain_stop"))
async def cmd_wordchain_stop(message: types.Message, wordchain: WordchainService) -> None:
    if not _require_group(message):
        await _refuse_private(message)
        return
    await wordchain.stop(chat_id=message.chat.id)


# ---------------- /rapbattle ----------------

@router.message(Command("rapbattle"))
async def cmd_rapbattle(
    message: types.Message, command: CommandObject, rapbattle: RapbattleService,
) -> None:
    if not _require_group(message):
        await _refuse_private(message)
        return
    opponent_reply_id: int | None = None
    if message.reply_to_message and message.reply_to_message.from_user:
        opponent_reply_id = message.reply_to_message.from_user.id
    opponent_arg = (command.args or "").strip().split()
    arg = opponent_arg[0] if opponent_arg else None
    await rapbattle.start(
        chat_id=message.chat.id,
        initiator_id=message.from_user.id,
        opponent_arg=arg,
        opponent_reply_id=opponent_reply_id,
    )


# ---------------- /storychain ----------------

@router.message(Command("storychain"))
async def cmd_storychain(
    message: types.Message, command: CommandObject, storychain: StorychainService,
) -> None:
    if not _require_group(message):
        await _refuse_private(message)
        return
    target: int | None = None
    if command.args:
        raw = command.args.strip().split()[0]
        try:
            target = int(raw)
        except ValueError:
            await message.answer(
                f"Ожидал число вкладов (3–12), а получил «{raw}». "
                "Запускаю без аргумента, чтобы не угадывать.",
            )
            return
    await storychain.start(chat_id=message.chat.id, target_contributions=target)


@router.message(Command("storychain_add"))
async def cmd_storychain_add(
    message: types.Message, command: CommandObject, storychain: StorychainService,
) -> None:
    if not _require_group(message):
        await _refuse_private(message)
        return
    text = (command.args or "").strip()
    await storychain.add(
        chat_id=message.chat.id, user_id=message.from_user.id, text=text,
    )


@router.message(Command("storychain_stop"))
async def cmd_storychain_stop(message: types.Message, storychain: StorychainService) -> None:
    if not _require_group(message):
        await _refuse_private(message)
        return
    await storychain.stop(chat_id=message.chat.id)
