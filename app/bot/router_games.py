from __future__ import annotations

import logging
from datetime import datetime
from html import escape

from aiogram import Bot, F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command

from ..services.guess_game import GuessGameService, NoCandidatesError, PreparedRound

router = Router(name="games")
logger = logging.getLogger("bot.games")

QUESTION_LIMIT = 300


def build_games_menu_markup() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="🎭 Угадай кто сказал", callback_data="games:guess")],
        ]
    )


def format_first_winner_message(*, display_name: str, username: str | None) -> str:
    mention = f"@{username}" if username else escape(display_name)
    return f"🎯 Первым угадал {mention} — минус 1 очко в месячной рулетке."


def _build_poll_question(text: str) -> str:
    base = f"Кто это написал?\n\n«{text}»"
    if len(base) <= QUESTION_LIMIT:
        return base
    return base[: QUESTION_LIMIT - 1] + "…"


async def _start_round(
    chat: types.Chat,
    bot: Bot,
    guess_game: GuessGameService,
) -> None:
    if chat.type not in {"group", "supergroup"}:
        await bot.send_message(chat.id, "Игра доступна только в групповых чатах.")
        return

    now = datetime.utcnow()
    if not await guess_game.can_start_today(chat_id=chat.id, now=now):
        await bot.send_message(chat.id, "На сегодня уже играли, приходите завтра.")
        return

    try:
        prepared: PreparedRound = await guess_game.prepare_round(chat_id=chat.id, now=now)
    except NoCandidatesError:
        await bot.send_message(chat.id, "Слишком тихо у вас, не из кого выбирать.")
        return

    try:
        poll_msg = await bot.send_poll(
            chat_id=chat.id,
            question=_build_poll_question(prepared.text),
            options=prepared.option_labels,
            type="quiz",
            correct_option_id=prepared.correct_option_id,
            is_anonymous=False,
            allows_multiple_answers=False,
        )
    except TelegramBadRequest as exc:
        logger.warning("guess.send_poll failed chat=%s: %s", chat.id, exc)
        await bot.send_message(chat.id, "Не могу запустить опрос — нужны права в чате.")
        return

    if poll_msg.poll is None:
        logger.warning("guess.send_poll returned no poll for chat=%s", chat.id)
        return

    await guess_game.persist_round(
        prepared,
        poll_id=poll_msg.poll.id,
        chat_message_id=poll_msg.message_id,
    )
    logger.info(
        "guess.round.started chat=%s mode=%s n_options=%s",
        chat.id, prepared.selection_mode, len(prepared.option_user_ids),
    )


@router.message(Command("games"))
async def cmd_games(message: types.Message) -> None:
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Меню игр доступно только в групповых чатах.")
        return
    await message.reply("🎮 Выбери игру:", reply_markup=build_games_menu_markup())


@router.message(Command("guess"))
async def cmd_guess(message: types.Message, bot: Bot, guess_game: GuessGameService) -> None:
    await _start_round(message.chat, bot, guess_game)


@router.callback_query(F.data == "games:guess")
async def cb_games_guess(query: types.CallbackQuery, bot: Bot, guess_game: GuessGameService) -> None:
    await query.answer()
    if query.message is None:
        return
    if isinstance(query.message, types.InaccessibleMessage):
        return
    await _start_round(query.message.chat, bot, guess_game)


@router.poll_answer()
async def on_poll_answer(poll_answer: types.PollAnswer, bot: Bot, guess_game: GuessGameService) -> None:
    round_ = await guess_game.find_round_by_poll(poll_answer.poll_id)
    if round_ is None:
        return
    if poll_answer.option_ids != [round_.correct_option_id]:
        return
    if poll_answer.user.id == round_.author_user_id:
        return
    won = await guess_game.record_first_winner(
        round_id=round_.id,
        user_id=poll_answer.user.id,
        now=datetime.utcnow(),
    )
    if not won:
        return
    msg = format_first_winner_message(
        display_name=poll_answer.user.full_name,
        username=poll_answer.user.username,
    )
    try:
        await bot.send_message(
            chat_id=round_.chat_id,
            text=msg,
            reply_to_message_id=round_.chat_message_id,
        )
    except TelegramBadRequest:
        await bot.send_message(chat_id=round_.chat_id, text=msg)
    logger.info(
        "guess.round.first_winner chat=%s round=%s user=%s",
        round_.chat_id, round_.id, poll_answer.user.id,
    )
