from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import Dict, Literal

from aiogram import Bot, F, Router, types
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command

from ..services.dice_game import AlreadyPlayedTodayError, DiceGameService
from ..services.guess_game import GuessGameService, NoCandidatesError, PreparedRound
from ..services.ship import ShipService

router = Router(name="games")
logger = logging.getLogger("bot.games")

_GUESS_LOCKS: Dict[int, asyncio.Lock] = {}


def _get_guess_lock(chat_id: int) -> asyncio.Lock:
    lock = _GUESS_LOCKS.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _GUESS_LOCKS[chat_id] = lock
    return lock

QUESTION_LIMIT = 300


def build_games_menu_markup() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="🎭 Угадай кто сказал", callback_data="games:guess")],
            [types.InlineKeyboardButton(text="🎲 Кости", callback_data="games:dice")],
            [types.InlineKeyboardButton(text="💞 Шипперинг (рандом)", callback_data="games:ship_random")],
        ]
    )


# --- Dice game --------------------------------------------------------------

DICE_MAX_PICKS = 2
DICE_FACES = (1, 2, 3, 4, 5, 6)


@dataclass(frozen=True)
class DiceCallback:
    action: Literal["pick", "roll", "cancel"]
    owner_id: int
    picks: list[int]
    number: int | None


def _parse_picks_csv(raw: str) -> list[int] | None:
    if not raw:
        return []
    try:
        out = [int(x) for x in raw.split(",")]
    except ValueError:
        return None
    if any(n not in DICE_FACES for n in out):
        return None
    if len(out) > DICE_MAX_PICKS:
        return None
    if len(set(out)) != len(out):
        return None
    return out


def parse_dice_callback(data: str) -> DiceCallback | None:
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "dice":
        return None
    action = parts[1]
    try:
        owner_id = int(parts[2])
    except ValueError:
        return None
    if action == "cancel" and len(parts) == 3:
        return DiceCallback(action="cancel", owner_id=owner_id, picks=[], number=None)
    if action == "roll" and len(parts) == 4:
        picks = _parse_picks_csv(parts[3])
        if picks is None:
            return None
        return DiceCallback(action="roll", owner_id=owner_id, picks=picks, number=None)
    if action == "pick" and len(parts) == 5:
        picks = _parse_picks_csv(parts[3])
        if picks is None:
            return None
        try:
            number = int(parts[4])
        except ValueError:
            return None
        if number not in DICE_FACES:
            return None
        return DiceCallback(action="pick", owner_id=owner_id, picks=picks, number=number)
    return None


def _picks_to_csv(picks: list[int]) -> str:
    return ",".join(str(n) for n in picks)


def build_dice_keyboard(owner_id: int, picks: list[int]) -> types.InlineKeyboardMarkup:
    csv = _picks_to_csv(picks)
    selected = set(picks)
    rows: list[list[types.InlineKeyboardButton]] = []
    for row_nums in ((1, 2, 3), (4, 5, 6)):
        rows.append([
            types.InlineKeyboardButton(
                text=(f"✓ {n}" if n in selected else str(n)),
                callback_data=f"dice:pick:{owner_id}:{csv}:{n}",
            )
            for n in row_nums
        ])
    rows.append([
        types.InlineKeyboardButton(
            text="🎲 Бросать",
            callback_data=f"dice:roll:{owner_id}:{csv}",
        ),
        types.InlineKeyboardButton(
            text="Отмена",
            callback_data=f"dice:cancel:{owner_id}",
        ),
    ])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def format_dice_picks_text(picks: list[int]) -> str:
    return ", ".join(str(n) for n in picks)


def format_dice_intro_text() -> str:
    return (
        "🎲 Кости — ставка на сегодня\n\n"
        "🎯 1 число → −2 / +2  (шанс 1/6)\n"
        "🎯 2 числа → −1 / +1  (шанс 1/3)"
    )


def format_dice_result(*, picks: list[int], dice_value: int, delta: int, mention: str) -> str:
    picks_str = format_dice_picks_text(picks)
    won = delta < 0
    if won and len(picks) == 1:
        return (
            f"🎉 {mention} поставил {picks_str} — выпало {dice_value}! "
            f"Сорвал джекпот, минус 2 очка в рулетке."
        )
    if won:
        return (
            f"✨ {mention} поставил {picks_str} — выпало {dice_value}! "
            f"Минус 1 очко в рулетке."
        )
    if len(picks) == 1:
        return (
            f"💀 {mention} поставил {picks_str} — выпало {dice_value}. "
            f"Жадность наказана: плюс 2 очка в рулетку."
        )
    return (
        f"😬 {mention} поставил {picks_str} — выпало {dice_value}. "
        f"Мимо. Плюс 1 очко в рулетку."
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

    async with _get_guess_lock(chat.id):
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
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.warning("guess.send_poll permission failed chat=%s: %s", chat.id, exc)
            await bot.send_message(chat.id, "Не могу запустить опрос — нужны права в чате.")
            return
        except TelegramAPIError:
            logger.exception("guess.send_poll TG API error chat=%s", chat.id)
            return

        if poll_msg.poll is None:
            logger.warning("guess.send_poll returned no poll for chat=%s", chat.id)
            return

        try:
            await guess_game.persist_round(
                prepared,
                poll_id=poll_msg.poll.id,
                chat_message_id=poll_msg.message_id,
            )
        except Exception:
            logger.exception("guess.persist_round failed after successful poll chat=%s poll=%s", chat.id, poll_msg.poll.id)
            await bot.send_message(
                chat.id,
                "Не смог сохранить раунд — голоса считаться не будут.",
            )
            return

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


def _parse_ship_args(
    *,
    text: str,
    entities: list[types.MessageEntity] | None,
) -> list[tuple[str, int] | tuple[str, str]]:
    """Extract ship candidates from a /ship message.

    Returns list of ("id", user_id) for text_mention, ("username", "@name") for mention.
    Returns [] if the number of usable mentions is not exactly 2.
    """
    if not entities:
        return []
    candidates: list[tuple[str, int] | tuple[str, str]] = []
    for ent in entities:
        if ent.type == "text_mention" and ent.user is not None:
            candidates.append(("id", ent.user.id))
        elif ent.type == "mention":
            raw = text[ent.offset : ent.offset + ent.length]
            candidates.append(("username", raw))
    if len(candidates) != 2:
        return []
    return candidates


_SHIP_USAGE = (
    "Использование: /ship @user1 @user2 — посчитаю совместимость.\n"
    "Пример: /ship @kostuk @golikov"
)


@router.message(Command("ship"))
async def cmd_ship(message: types.Message, bot: Bot, ship: ShipService) -> None:
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Игра для групповых чатов.")
        return

    candidates = _parse_ship_args(
        text=message.text or "",
        entities=list(message.entities or []),
    )
    if not candidates:
        await message.reply(_SHIP_USAGE)
        return

    resolved: list[tuple[int, str]] = []
    for cand in candidates:
        res = await ship.resolve_candidate(chat_id=message.chat.id, candidate=cand)
        if res is None:
            missing = cand[1] if isinstance(cand[1], str) else f"id{cand[1]}"
            await message.reply(f"Не нашёл такого: {missing}. " + _SHIP_USAGE)
            return
        resolved.append(res)

    outcome = await ship.compute_or_cached(
        chat_id=message.chat.id,
        a=resolved[0],
        b=resolved[1],
        bot_id=bot.id,
    )
    await message.reply(outcome.rendered_text)


@router.callback_query(F.data == "games:guess")
async def cb_games_guess(query: types.CallbackQuery, bot: Bot, guess_game: GuessGameService) -> None:
    await query.answer()
    if query.message is None:
        return
    if isinstance(query.message, types.InaccessibleMessage):
        return
    await _start_round(query.message.chat, bot, guess_game)


async def _open_dice(
    *,
    chat: types.Chat,
    user: types.User,
    reply_to_message_id: int | None,
    bot: Bot,
    dice_game: DiceGameService,
) -> None:
    if chat.type not in {"group", "supergroup"}:
        await bot.send_message(
            chat_id=chat.id,
            text="⛔ Игра только в групповых чатах.",
        )
        return

    now = datetime.utcnow()
    if not await dice_game.can_play_today(chat_id=chat.id, user_id=user.id, now=now):
        await bot.send_message(
            chat_id=chat.id,
            text="⏳ Ты уже бросал сегодня, приходи завтра.",
            reply_to_message_id=reply_to_message_id,
        )
        return

    await bot.send_message(
        chat_id=chat.id,
        text=format_dice_intro_text(),
        reply_to_message_id=reply_to_message_id,
        reply_markup=build_dice_keyboard(owner_id=user.id, picks=[]),
    )


@router.message(Command("dice"))
async def cmd_dice(message: types.Message, bot: Bot, dice_game: DiceGameService) -> None:
    if message.from_user is None:
        return
    await _open_dice(
        chat=message.chat, user=message.from_user,
        reply_to_message_id=message.message_id, bot=bot, dice_game=dice_game,
    )


@router.callback_query(F.data == "games:dice")
async def cb_games_dice(query: types.CallbackQuery, bot: Bot, dice_game: DiceGameService) -> None:
    await query.answer()
    if query.message is None or isinstance(query.message, types.InaccessibleMessage):
        return
    await _open_dice(
        chat=query.message.chat, user=query.from_user,
        reply_to_message_id=query.message.message_id, bot=bot, dice_game=dice_game,
    )


DICE_ANIMATION_DELAY = 2.0
_DICE_ROLL_LOCKS: Dict[tuple[int, int], asyncio.Lock] = {}


def _get_dice_lock(chat_id: int, user_id: int) -> asyncio.Lock:
    key = (chat_id, user_id)
    lock = _DICE_ROLL_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _DICE_ROLL_LOCKS[key] = lock
    return lock


def _mention_for(user: types.User) -> str:
    if user.username:
        return f"@{user.username}"
    return escape(user.full_name or f"user{user.id}")


@router.callback_query(F.data.startswith("dice:"))
async def on_dice_callback(
    query: types.CallbackQuery, bot: Bot, dice_game: DiceGameService,
) -> None:
    if query.data is None:
        await query.answer()
        return
    parsed = parse_dice_callback(query.data)
    if parsed is None:
        await query.answer()
        return
    if query.message is None or isinstance(query.message, types.InaccessibleMessage):
        await query.answer()
        return
    if query.from_user.id != parsed.owner_id:
        await query.answer("🚫 Не твоя игра — позови /dice сам.", show_alert=True)
        return

    if parsed.action == "cancel":
        try:
            await query.message.edit_text("❌ Бросок отменён.")
        except TelegramBadRequest:
            pass
        await query.answer()
        return

    if parsed.action == "pick":
        assert parsed.number is not None
        picks = list(parsed.picks)
        if parsed.number in picks:
            picks.remove(parsed.number)
        else:
            if len(picks) >= DICE_MAX_PICKS:
                await query.answer(f"🛑 Максимум {DICE_MAX_PICKS} числа.")
                return
            picks.append(parsed.number)
            picks.sort()
        try:
            await query.message.edit_reply_markup(
                reply_markup=build_dice_keyboard(owner_id=parsed.owner_id, picks=picks),
            )
        except TelegramBadRequest:
            pass
        await query.answer()
        return

    # parsed.action == "roll"
    if not parsed.picks:
        await query.answer("👇 Выбери хотя бы одно число.")
        return

    chat_id = query.message.chat.id
    user_id = parsed.owner_id

    async with _get_dice_lock(chat_id, user_id):
        await query.answer()
        # Strip keyboard, show the stake
        try:
            await query.message.edit_text(f"🎰 Ставка: {format_dice_picks_text(parsed.picks)}")
        except TelegramBadRequest:
            pass

        try:
            dice_msg = await bot.send_dice(
                chat_id=chat_id,
                emoji="🎲",
                reply_to_message_id=query.message.message_id,
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            logger.exception("dice.send_dice failed chat=%s user=%s", chat_id, user_id)
            await bot.send_message(
                chat_id=chat_id,
                text="⚠️ Не смог бросить кубик, попробуй ещё раз.",
            )
            return

        if dice_msg.dice is None:
            logger.warning("dice.send_dice returned no dice chat=%s", chat_id)
            return
        value = dice_msg.dice.value

        try:
            round_, delta = await dice_game.record_roll(
                chat_id=chat_id, user_id=user_id, picks=parsed.picks,
                dice_value=value, dice_message_id=dice_msg.message_id,
                now=datetime.utcnow(),
            )
        except AlreadyPlayedTodayError:
            await bot.send_message(
                chat_id=chat_id,
                text="⏳ Ты уже бросал сегодня, приходи завтра.",
                reply_to_message_id=dice_msg.message_id,
            )
            return
        except Exception:
            logger.exception("dice.record_roll failed chat=%s user=%s", chat_id, user_id)
            await bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ Кубик показал {value}, но не смог записать — день не сгорел.",
                reply_to_message_id=dice_msg.message_id,
            )
            return

        if DICE_ANIMATION_DELAY > 0:
            await asyncio.sleep(DICE_ANIMATION_DELAY)

        text = format_dice_result(
            picks=parsed.picks, dice_value=value, delta=delta,
            mention=_mention_for(query.from_user),
        )
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=dice_msg.message_id,
            )
        except TelegramBadRequest:
            await bot.send_message(chat_id=chat_id, text=text)

        logger.info(
            "dice.round chat=%s user=%s picks=%s value=%s delta=%s",
            chat_id, user_id, parsed.picks, value, delta,
        )


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


@router.callback_query(F.data == "games:ship_random")
async def cb_games_ship_random(query: types.CallbackQuery, bot: Bot, ship: ShipService) -> None:
    await query.answer()
    if query.message is None:
        return
    if isinstance(query.message, types.InaccessibleMessage):
        return
    chat = query.message.chat
    if chat.type not in {"group", "supergroup"}:
        await bot.send_message(chat_id=chat.id, text="Игра для групповых чатов.")
        return

    pair = await ship.pick_random_pair(chat_id=chat.id, bot_id=bot.id)
    if pair is None:
        await bot.send_message(chat_id=chat.id, text="Слишком тихо у вас, не из кого пары собрать.")
        return

    outcome = await ship.compute_or_cached(
        chat_id=chat.id,
        a=pair[0],
        b=pair[1],
        bot_id=bot.id,
    )
    await bot.send_message(chat_id=chat.id, text=outcome.rendered_text)
