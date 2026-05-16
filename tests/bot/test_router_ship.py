from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Chat, MessageEntity
from aiogram.types import User as TgUser

from app.bot.router_games import _parse_ship_args, cmd_ship
from app.services.ship import ShipOutcome


def _make_entity(type_: str, offset: int, length: int, user: TgUser | None = None) -> MessageEntity:
    return MessageEntity(type=type_, offset=offset, length=length, user=user)


def test_parse_ship_args_two_at_usernames():
    text = "/ship @alice @bob"
    entities = [
        _make_entity("bot_command", 0, 5),
        _make_entity("mention", 6, 6),  # @alice
        _make_entity("mention", 13, 4),  # @bob
    ]
    candidates = _parse_ship_args(text=text, entities=entities)
    assert candidates == [("username", "@alice"), ("username", "@bob")]


def test_parse_ship_args_text_mention_uses_user_id():
    user = TgUser(id=100, is_bot=False, first_name="Алиса")
    text = "/ship Алиса @bob"
    entities = [
        _make_entity("bot_command", 0, 5),
        _make_entity("text_mention", 6, 5, user=user),
        _make_entity("mention", 12, 4),
    ]
    candidates = _parse_ship_args(text=text, entities=entities)
    assert candidates == [("id", 100), ("username", "@bob")]


def test_parse_ship_args_returns_empty_when_no_mentions():
    candidates = _parse_ship_args(text="/ship hello", entities=[_make_entity("bot_command", 0, 5)])
    assert candidates == []


def test_parse_ship_args_returns_empty_when_one_mention():
    text = "/ship @alice"
    entities = [_make_entity("bot_command", 0, 5), _make_entity("mention", 6, 6)]
    assert _parse_ship_args(text=text, entities=entities) == []


def test_parse_ship_args_returns_empty_when_three_mentions():
    text = "/ship @a @b @c"
    entities = [
        _make_entity("bot_command", 0, 5),
        _make_entity("mention", 6, 2),
        _make_entity("mention", 9, 2),
        _make_entity("mention", 12, 2),
    ]
    assert _parse_ship_args(text=text, entities=entities) == []


@pytest.mark.asyncio
async def test_cmd_ship_private_chat_returns_group_only():
    bot = AsyncMock()
    bot.id = 7
    ship = AsyncMock()
    message = MagicMock()
    message.chat = Chat(id=100, type="private")
    message.text = "/ship @a @b"
    message.entities = []
    message.reply = AsyncMock()

    await cmd_ship(message, bot, ship)

    message.reply.assert_awaited_once()
    args, kwargs = message.reply.call_args
    assert "групп" in (args[0] if args else kwargs.get("text", "")).lower()
    ship.compute_or_cached.assert_not_called()


@pytest.mark.asyncio
async def test_cmd_ship_no_args_returns_usage_hint():
    bot = AsyncMock()
    bot.id = 7
    ship = AsyncMock()
    message = MagicMock()
    message.chat = Chat(id=-100, type="supergroup")
    message.text = "/ship"
    message.entities = []
    message.reply = AsyncMock()

    await cmd_ship(message, bot, ship)

    message.reply.assert_awaited_once()
    hint = message.reply.call_args.args[0]
    assert "Использование" in hint
    ship.compute_or_cached.assert_not_called()


@pytest.mark.asyncio
async def test_cmd_ship_unknown_username_says_who_not_found():
    bot = AsyncMock()
    bot.id = 7
    ship = AsyncMock()
    ship.resolve_candidate = AsyncMock(side_effect=[(100, "alice"), None])

    message = MagicMock()
    message.chat = Chat(id=-100, type="supergroup")
    message.text = "/ship @alice @ghost"
    message.entities = [
        _make_entity("bot_command", 0, 5),
        _make_entity("mention", 6, 6),
        _make_entity("mention", 13, 6),
    ]
    message.reply = AsyncMock()

    await cmd_ship(message, bot, ship)

    message.reply.assert_awaited_once()
    text = message.reply.call_args.args[0]
    assert "@ghost" in text or "ghost" in text
    ship.compute_or_cached.assert_not_called()


@pytest.mark.asyncio
async def test_cmd_ship_happy_path_runs_pipeline_and_sends_text():
    bot = AsyncMock()
    bot.id = 7
    bot.send_message = AsyncMock()
    ship = AsyncMock()
    ship.resolve_candidate = AsyncMock(side_effect=[(100, "alice"), (200, "bob")])
    ship.compute_or_cached = AsyncMock(return_value=ShipOutcome(
        score=73, rendered_text="💞 73/100", cached=False,
    ))

    message = MagicMock()
    message.chat = Chat(id=-100, type="supergroup")
    message.text = "/ship @alice @bob"
    message.entities = [
        _make_entity("bot_command", 0, 5),
        _make_entity("mention", 6, 6),
        _make_entity("mention", 13, 4),
    ]
    message.reply = AsyncMock()

    await cmd_ship(message, bot, ship)

    ship.compute_or_cached.assert_awaited_once_with(
        chat_id=-100,
        a=(100, "alice"),
        b=(200, "bob"),
        bot_id=7,
    )
    message.reply.assert_awaited_once()
    assert "💞" in message.reply.call_args.args[0]
