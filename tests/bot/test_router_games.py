from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Chat, PollAnswer
from aiogram.types import Message as TgMessage
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.router_games import (
    _start_round,
    build_games_menu_markup,
    format_first_winner_message,
    on_dice_callback,
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


from app.bot.router_games import _open_dice, format_dice_intro_text
from app.services.dice_game import DiceGameService


def test_build_games_menu_contains_dice_button() -> None:
    markup = build_games_menu_markup()
    flat = [btn for row in markup.inline_keyboard for btn in row]
    assert any(btn.callback_data == "games:dice" for btn in flat)
    assert any("Кости" in btn.text for btn in flat)


def _fake_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=555))
    bot.send_dice = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_open_dice_in_non_group_chat_refuses(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    svc = DiceGameService(sessionmaker)
    bot = _fake_bot()
    chat = Chat(id=42, type="private")
    user = TgUser(id=10, is_bot=False, first_name="A")

    await _open_dice(chat=chat, user=user, reply_to_message_id=1, bot=bot, dice_game=svc)

    bot.send_message.assert_called_once()
    text = bot.send_message.call_args.kwargs.get("text") or bot.send_message.call_args.args[1]
    assert "групповых" in text.lower()


@pytest.mark.asyncio
async def test_open_dice_sends_keyboard_first_time(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    svc = DiceGameService(sessionmaker)
    bot = _fake_bot()
    chat = Chat(id=-100, type="supergroup")
    user = TgUser(id=10, is_bot=False, first_name="A")

    await _open_dice(chat=chat, user=user, reply_to_message_id=1, bot=bot, dice_game=svc)

    bot.send_message.assert_called_once()
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == -100
    assert kwargs["reply_to_message_id"] == 1
    assert format_dice_intro_text() in (kwargs["text"] or "")
    markup = kwargs["reply_markup"]
    assert markup is not None
    flat = [btn for row in markup.inline_keyboard for btn in row]
    assert any("Бросать" in b.text for b in flat)
    # owner_id is the caller (user 10)
    assert any(b.callback_data and "dice:roll:10:" in b.callback_data for b in flat)


@pytest.mark.asyncio
async def test_open_dice_refuses_when_already_played(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    svc = DiceGameService(sessionmaker)
    # seed a round for today
    from datetime import datetime
    await svc.record_roll(
        chat_id=-100, user_id=10, picks=[3], dice_value=4,
        dice_message_id=999, now=datetime.utcnow(),
    )

    bot = _fake_bot()
    chat = Chat(id=-100, type="supergroup")
    user = TgUser(id=10, is_bot=False, first_name="A")

    await _open_dice(chat=chat, user=user, reply_to_message_id=1, bot=bot, dice_game=svc)

    bot.send_message.assert_called_once()
    text = bot.send_message.call_args.kwargs.get("text") or ""
    assert "уже бросал" in text.lower()


def _fake_callback(
    *, data: str, from_user_id: int, owner_in_msg: bool = True,
    chat_id: int = -100, message_id: int = 555,
) -> tuple[CallbackQuery, MagicMock, AsyncMock]:
    """Build a CallbackQuery with mock .answer / .message.edit_text.

    Returns ``(cb, msg, answer_mock)``. The ``answer_mock`` is returned
    separately so tests can assert on it without mypy complaining about the
    aiogram-typed ``CallbackQuery.answer`` attribute.
    """
    msg = MagicMock(spec=TgMessage)
    msg.message_id = message_id
    msg.chat = Chat(id=chat_id, type="supergroup")
    msg.edit_text = AsyncMock()
    msg.edit_reply_markup = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = TgUser(id=from_user_id, is_bot=False, first_name="U", username="u")
    cb.message = msg
    answer_mock = AsyncMock()
    cb.answer = answer_mock
    return cb, msg, answer_mock


@pytest.mark.asyncio
async def test_dice_pick_from_foreign_user_alerts_and_no_edit(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    svc = DiceGameService(sessionmaker)
    bot = _fake_bot()
    cb, msg, answer = _fake_callback(data="dice:pick:10::3", from_user_id=99)

    await on_dice_callback(cb, bot=bot, dice_game=svc)

    answer.assert_called_once()
    assert answer.call_args.kwargs.get("show_alert") is True
    msg.edit_text.assert_not_called()
    msg.edit_reply_markup.assert_not_called()


@pytest.mark.asyncio
async def test_dice_pick_toggles_number_on(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    svc = DiceGameService(sessionmaker)
    bot = _fake_bot()
    cb, msg, answer = _fake_callback(data="dice:pick:10::3", from_user_id=10)

    await on_dice_callback(cb, bot=bot, dice_game=svc)

    msg.edit_reply_markup.assert_called_once()
    markup = msg.edit_reply_markup.call_args.kwargs["reply_markup"]
    flat = [b for row in markup.inline_keyboard for b in row]
    assert any(b.text == "✓ 3" for b in flat)


@pytest.mark.asyncio
async def test_dice_pick_toggles_number_off(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    svc = DiceGameService(sessionmaker)
    bot = _fake_bot()
    # current picks already include 3 → tap again removes it
    cb, msg, answer = _fake_callback(data="dice:pick:10:3:3", from_user_id=10)

    await on_dice_callback(cb, bot=bot, dice_game=svc)

    markup = msg.edit_reply_markup.call_args.kwargs["reply_markup"]
    flat = [b for row in markup.inline_keyboard for b in row]
    assert not any(b.text.startswith("✓") for b in flat)


@pytest.mark.asyncio
async def test_dice_pick_max_two_blocks_third(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    svc = DiceGameService(sessionmaker)
    bot = _fake_bot()
    # current picks: 3, 5 → trying to add 2
    cb, msg, answer = _fake_callback(data="dice:pick:10:3,5:2", from_user_id=10)

    await on_dice_callback(cb, bot=bot, dice_game=svc)

    answer.assert_called_once()
    assert "Максимум" in answer.call_args.args[0] or "макс" in (answer.call_args.args[0] or "").lower()
    msg.edit_reply_markup.assert_not_called()


@pytest.mark.asyncio
async def test_dice_roll_without_picks_alerts(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    svc = DiceGameService(sessionmaker)
    bot = _fake_bot()
    cb, msg, answer = _fake_callback(data="dice:roll:10:", from_user_id=10)

    await on_dice_callback(cb, bot=bot, dice_game=svc)

    answer.assert_called_once()
    text = answer.call_args.args[0] if answer.call_args.args else ""
    assert "Выбери" in text or "хотя бы" in text.lower()
    bot.send_dice.assert_not_called()


@pytest.mark.asyncio
async def test_dice_cancel(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    svc = DiceGameService(sessionmaker)
    bot = _fake_bot()
    cb, msg, answer = _fake_callback(data="dice:cancel:10", from_user_id=10)

    await on_dice_callback(cb, bot=bot, dice_game=svc)

    msg.edit_text.assert_called_once()
    text_arg = msg.edit_text.call_args.args[0] if msg.edit_text.call_args.args else msg.edit_text.call_args.kwargs.get("text")
    assert "отменён" in (text_arg or "").lower()
    # No round should be recorded
    async with sessionmaker() as session:
        from app.models import DiceRound
        rounds = (await session.execute(select(DiceRound))).scalars().all()
        assert rounds == []


@pytest.mark.asyncio
async def test_dice_roll_happy_path_win(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.bot.router_games.DICE_ANIMATION_DELAY", 0.0)
    svc = DiceGameService(sessionmaker)
    bot = _fake_bot()
    sent_dice = MagicMock()
    sent_dice.message_id = 777
    sent_dice.dice = MagicMock(value=3)
    bot.send_dice = AsyncMock(return_value=sent_dice)

    cb, msg, answer = _fake_callback(data="dice:roll:10:3", from_user_id=10)
    await on_dice_callback(cb, bot=bot, dice_game=svc)

    bot.send_dice.assert_called_once()
    # Round recorded with win
    from app.models import DiceRound, RouletteScoreAdjustment
    async with sessionmaker() as session:
        rnd = (await session.execute(select(DiceRound))).scalar_one()
        assert rnd.dice_value == 3
        assert rnd.delta == -2
        assert rnd.won is True
        adj = (await session.execute(select(RouletteScoreAdjustment))).scalar_one()
        assert adj.delta == -2
        assert adj.reason == "dice_win"
    # Result message sent
    bot.send_message.assert_called()
    last_call_text = bot.send_message.call_args.kwargs.get("text") or ""
    assert "выпало 3" in last_call_text.lower()


@pytest.mark.asyncio
async def test_dice_roll_happy_path_loss(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.bot.router_games.DICE_ANIMATION_DELAY", 0.0)
    svc = DiceGameService(sessionmaker)
    bot = _fake_bot()
    sent_dice = MagicMock()
    sent_dice.message_id = 777
    sent_dice.dice = MagicMock(value=6)
    bot.send_dice = AsyncMock(return_value=sent_dice)

    cb, msg, answer = _fake_callback(data="dice:roll:10:3", from_user_id=10)
    await on_dice_callback(cb, bot=bot, dice_game=svc)

    from app.models import DiceRound, RouletteScoreAdjustment
    async with sessionmaker() as session:
        rnd = (await session.execute(select(DiceRound))).scalar_one()
        assert rnd.won is False
        assert rnd.delta == 0
        adjs = (await session.execute(select(RouletteScoreAdjustment))).scalars().all()
        assert adjs == []
    text = bot.send_message.call_args.kwargs.get("text") or ""
    assert "мимо" in text.lower()


@pytest.mark.asyncio
async def test_dice_roll_concurrent_second_attempt_loses_race(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.bot.router_games.DICE_ANIMATION_DELAY", 0.0)
    svc = DiceGameService(sessionmaker)
    # Pre-seed today's round
    from datetime import datetime
    await svc.record_roll(
        chat_id=-100, user_id=10, picks=[3], dice_value=4,
        dice_message_id=10, now=datetime.utcnow(),
    )
    bot = _fake_bot()
    sent_dice = MagicMock(message_id=777, dice=MagicMock(value=5))
    bot.send_dice = AsyncMock(return_value=sent_dice)
    cb, msg, answer = _fake_callback(data="dice:roll:10:3", from_user_id=10)

    await on_dice_callback(cb, bot=bot, dice_game=svc)

    # Second roll attempt: dice rolled but record fails → user told it didn't burn the day
    answer.assert_called()
    bot.send_message.assert_called()
    text = bot.send_message.call_args.kwargs.get("text") or ""
    assert "уже бросал" in text.lower()
