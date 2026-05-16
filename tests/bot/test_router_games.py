from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Chat, PollAnswer
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.router_games import (
    _start_round,
    build_games_menu_markup,
    format_first_winner_message,
    on_poll_answer,
)
from app.models import GuessRound, Message, RouletteScoreAdjustment
from app.services.guess_game import GuessGameService, LLMPick


def test_build_games_menu_returns_inline_keyboard_with_guess() -> None:
    markup = build_games_menu_markup()
    flat = [btn for row in markup.inline_keyboard for btn in row]
    assert any(btn.callback_data == "games:guess" for btn in flat)
    assert any("Угадай" in btn.text for btn in flat)


def test_format_first_winner_message_mentions_user_and_penalty() -> None:
    msg = format_first_winner_message(display_name="Андрей", username="andrey")
    assert "Андрей" in msg or "@andrey" in msg
    assert "1 очко" in msg


def test_format_first_winner_message_falls_back_to_display_name_without_username() -> None:
    msg = format_first_winner_message(display_name="Bob", username=None)
    assert "Bob" in msg
    assert "@" not in msg


# ---------------------------------------------------------------------------
# Integration test helpers
# ---------------------------------------------------------------------------

async def _seed_messages(sessionmaker_: async_sessionmaker[AsyncSession], chat_id: int) -> None:
    base = "это какой-то длинный тестовый текст для опроса " * 2
    async with sessionmaker_() as session:
        for uid in (1, 2, 3, 4):
            for i in range(5):
                session.add(Message(
                    chat_id=chat_id, message_id=uid * 100 + i, user_id=uid,
                    text=base + str(uid) + "-" + str(i),
                    reply_to_id=None,
                    date=datetime.utcnow() - timedelta(days=2),
                    is_bot=False, tg_file_id=None, media_group_id=None,
                ))
        await session.commit()


def _make_svc(sessionmaker_: async_sessionmaker[AsyncSession], *, llm_user_id: int = 2, llm_message_id: int = 200) -> GuessGameService:
    app_config = MagicMock()
    app_config.get_all = AsyncMock(return_value={})

    async def fake_llm_pick(*args, **kwargs):
        return LLMPick(author_user_id=llm_user_id, message_id=llm_message_id, reason="ok")

    async def fake_display_name(chat_id_, user_id_):
        return f"User{user_id_}"

    return GuessGameService(
        sessionmaker=sessionmaker_,
        app_config=app_config,
        bot=None,
        display_name=fake_display_name,
        llm_pick=fake_llm_pick,
    )


def _fake_bot_with_poll(poll_id: str = "POLL-XYZ") -> MagicMock:
    bot = MagicMock()
    sent_poll = MagicMock()
    sent_poll.message_id = 999
    sent_poll.poll = MagicMock(id=poll_id)
    bot.send_poll = AsyncMock(return_value=sent_poll)
    bot.send_message = AsyncMock()
    return bot


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_flow_first_winner_gets_adjustment(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -400
    await _seed_messages(sessionmaker, chat_id)
    svc = _make_svc(sessionmaker)
    bot = _fake_bot_with_poll("POLL-1")

    chat = Chat(id=chat_id, type="supergroup")
    await _start_round(chat, bot, svc)

    async with sessionmaker() as session:
        rounds = (await session.execute(select(GuessRound))).scalars().all()
        assert len(rounds) == 1
        assert rounds[0].author_user_id == 2
        assert rounds[0].poll_id == "POLL-1"
        round_id = rounds[0].id
        correct_id = rounds[0].correct_option_id

    answer = PollAnswer(
        poll_id="POLL-1",
        user=TgUser(id=42, is_bot=False, first_name="Hero", username="hero"),
        option_ids=[correct_id],
        option_persistent_ids=[],
    )
    await on_poll_answer(answer, bot, svc)

    async with sessionmaker() as session:
        adj = (await session.execute(
            select(RouletteScoreAdjustment).where(RouletteScoreAdjustment.user_id == 42)
        )).scalar_one()
        assert adj.delta == -1
        assert adj.source_id == round_id
        rnd = (await session.execute(select(GuessRound).where(GuessRound.id == round_id))).scalar_one()
        assert rnd.first_winner_user_id == 42

    bot.send_message.assert_called()


@pytest.mark.asyncio
async def test_second_correct_vote_no_extra_adjustment(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -401
    await _seed_messages(sessionmaker, chat_id)
    svc = _make_svc(sessionmaker)
    bot = _fake_bot_with_poll("POLL-2")

    await _start_round(Chat(id=chat_id, type="supergroup"), bot, svc)

    async with sessionmaker() as session:
        rnd = (await session.execute(select(GuessRound))).scalar_one()
        correct_id = rnd.correct_option_id

    a1 = PollAnswer(poll_id="POLL-2", user=TgUser(id=42, is_bot=False, first_name="A"), option_ids=[correct_id], option_persistent_ids=[])
    a2 = PollAnswer(poll_id="POLL-2", user=TgUser(id=43, is_bot=False, first_name="B"), option_ids=[correct_id], option_persistent_ids=[])
    await on_poll_answer(a1, bot, svc)
    await on_poll_answer(a2, bot, svc)

    async with sessionmaker() as session:
        adjs = (await session.execute(select(RouletteScoreAdjustment))).scalars().all()
        assert len(adjs) == 1
        assert adjs[0].user_id == 42


@pytest.mark.asyncio
async def test_daily_limit_blocks_second_start(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -402
    await _seed_messages(sessionmaker, chat_id)
    svc = _make_svc(sessionmaker)
    bot = _fake_bot_with_poll("POLL-3")

    chat = Chat(id=chat_id, type="supergroup")
    await _start_round(chat, bot, svc)
    bot.send_message.reset_mock()
    bot.send_poll.reset_mock()

    await _start_round(chat, bot, svc)

    bot.send_poll.assert_not_called()
    bot.send_message.assert_called_once()
    args, kwargs = bot.send_message.call_args
    text = kwargs.get("text") or (args[1] if len(args) > 1 else "")
    assert "уже играли" in text.lower()


from app.bot.router_games import (
    DiceCallback,
    build_dice_keyboard,
    format_dice_result,
    parse_dice_callback,
)


class TestParseDiceCallback:
    def test_pick_with_empty_picks(self) -> None:
        cb = parse_dice_callback("dice:pick:42::3")
        assert cb == DiceCallback(action="pick", owner_id=42, picks=[], number=3)

    def test_pick_with_one_existing_pick(self) -> None:
        cb = parse_dice_callback("dice:pick:42:3:5")
        assert cb == DiceCallback(action="pick", owner_id=42, picks=[3], number=5)

    def test_pick_with_two_existing_picks(self) -> None:
        cb = parse_dice_callback("dice:pick:42:3,5:2")
        assert cb == DiceCallback(action="pick", owner_id=42, picks=[3, 5], number=2)

    def test_roll(self) -> None:
        cb = parse_dice_callback("dice:roll:42:3,5")
        assert cb == DiceCallback(action="roll", owner_id=42, picks=[3, 5], number=None)

    def test_cancel(self) -> None:
        cb = parse_dice_callback("dice:cancel:42")
        assert cb == DiceCallback(action="cancel", owner_id=42, picks=[], number=None)

    @pytest.mark.parametrize("bad", [
        "dice:bogus:1:2:3",
        "not-dice:pick:1::3",
        "dice:pick:abc::3",
        "dice:pick:1:7:3",      # 7 not in 1..6
        "dice:pick:1::7",
        "dice:pick:1:3,3,3:5",  # too many picks
    ])
    def test_invalid_returns_none(self, bad: str) -> None:
        assert parse_dice_callback(bad) is None


class TestBuildDiceKeyboard:
    def test_six_number_buttons_plus_roll_and_cancel(self) -> None:
        markup = build_dice_keyboard(owner_id=42, picks=[])
        flat = [btn for row in markup.inline_keyboard for btn in row]
        # 6 numbers + Бросать + Отмена
        assert len(flat) == 8
        labels = [btn.text for btn in flat]
        for n in "123456":
            assert n in labels
        assert any("Бросать" in t for t in labels)
        assert any("Отмена" in t for t in labels)

    def test_selected_number_shown_with_check(self) -> None:
        markup = build_dice_keyboard(owner_id=42, picks=[3])
        flat = [btn for row in markup.inline_keyboard for btn in row]
        labels = [btn.text for btn in flat]
        assert "✓ 3" in labels
        assert "5" in labels  # other digits unchecked

    def test_callback_data_includes_owner_and_current_picks(self) -> None:
        markup = build_dice_keyboard(owner_id=42, picks=[3, 5])
        flat = [btn for row in markup.inline_keyboard for btn in row]
        # one of the number buttons (e.g. for 2) should carry picks=3,5
        two_btn = next(b for b in flat if b.text == "2")
        assert two_btn.callback_data == "dice:pick:42:3,5:2"
        roll_btn = next(b for b in flat if "Бросать" in b.text)
        assert roll_btn.callback_data == "dice:roll:42:3,5"
        cancel_btn = next(b for b in flat if "Отмена" in b.text)
        assert cancel_btn.callback_data == "dice:cancel:42"


class TestFormatDiceResult:
    def test_win_single_pick(self) -> None:
        msg = format_dice_result(picks=[5], dice_value=5, delta=-2, mention="@andrey")
        assert "@andrey" in msg
        assert "5" in msg
        assert "2 очка" in msg or "минус 2" in msg.lower()

    def test_win_double_pick(self) -> None:
        msg = format_dice_result(picks=[3, 5], dice_value=5, delta=-1, mention="@andrey")
        assert "@andrey" in msg
        assert "3" in msg and "5" in msg
        assert "1 очко" in msg or "минус 1" in msg.lower()

    def test_loss(self) -> None:
        msg = format_dice_result(picks=[3], dice_value=4, delta=0, mention="@andrey")
        assert "@andrey" in msg
        assert "Мимо" in msg or "мимо" in msg
