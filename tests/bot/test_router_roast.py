from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest

from app.bot.router_fun import cmd_roast
from app.services.roast import RoastService


def _msg(
    *,
    chat_type: str = "supergroup",
    text: str = "/roast",
    entities: list[MagicMock] | None = None,
    from_user_id: int = 200,
    chat_id: int = 42,
) -> MagicMock:
    message = MagicMock()
    message.chat.id = chat_id
    message.chat.type = chat_type
    message.from_user = MagicMock()
    message.from_user.id = from_user_id
    message.from_user.is_bot = False
    message.text = text
    message.entities = entities or []
    message.reply = AsyncMock()
    return message


@pytest.mark.asyncio
async def test_cmd_roast_in_private_chat_returns_group_only(monkeypatch):
    session = AsyncMock()
    monkeypatch.setattr(
        "app.bot.router_fun._store_command_once",
        AsyncMock(return_value=True),
    )
    roast = create_autospec(RoastService, instance=True)
    roast.run = AsyncMock()

    message = _msg(chat_type="private")
    await cmd_roast(message, session=session, roast=roast)

    message.reply.assert_awaited_once()
    args, _ = message.reply.call_args
    assert "групп" in args[0].lower()
    roast.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_roast_without_args_dispatches_with_none_target(monkeypatch):
    session = AsyncMock()
    monkeypatch.setattr(
        "app.bot.router_fun._store_command_once",
        AsyncMock(return_value=True),
    )
    roast = create_autospec(RoastService, instance=True)
    roast.run = AsyncMock()

    message = _msg(text="/roast")
    await cmd_roast(message, session=session, roast=roast)

    roast.run.assert_awaited_once()
    kwargs = roast.run.await_args.kwargs
    assert kwargs["chat_id"] == 42
    assert kwargs["initiator_id"] == 200
    assert kwargs["target_arg"] is None


@pytest.mark.asyncio
async def test_cmd_roast_with_mention_extracts_username(monkeypatch):
    session = AsyncMock()
    monkeypatch.setattr(
        "app.bot.router_fun._store_command_once",
        AsyncMock(return_value=True),
    )
    roast = create_autospec(RoastService, instance=True)
    roast.run = AsyncMock()

    text = "/roast @andrew"
    entity = MagicMock()
    entity.type = "mention"
    entity.offset = 7
    entity.length = 7
    message = _msg(text=text, entities=[entity])

    await cmd_roast(message, session=session, roast=roast)

    roast.run.assert_awaited_once()
    assert roast.run.await_args.kwargs["target_arg"] == "@andrew"


@pytest.mark.asyncio
async def test_cmd_roast_with_text_mention_uses_username(monkeypatch):
    session = AsyncMock()
    monkeypatch.setattr(
        "app.bot.router_fun._store_command_once",
        AsyncMock(return_value=True),
    )
    roast = create_autospec(RoastService, instance=True)
    roast.run = AsyncMock()

    entity = MagicMock()
    entity.type = "text_mention"
    entity.offset = 7
    entity.length = 6
    entity.user = MagicMock()
    entity.user.username = "andrew"
    entity.user.id = 100
    message = _msg(text="/roast Андрей", entities=[entity])

    await cmd_roast(message, session=session, roast=roast)

    roast.run.assert_awaited_once()
    assert roast.run.await_args.kwargs["target_arg"] == "@andrew"


@pytest.mark.asyncio
async def test_cmd_roast_ignores_duplicate_command(monkeypatch):
    session = AsyncMock()
    monkeypatch.setattr(
        "app.bot.router_fun._store_command_once",
        AsyncMock(return_value=False),  # duplicate
    )
    roast = create_autospec(RoastService, instance=True)
    roast.run = AsyncMock()

    message = _msg(text="/roast")
    await cmd_roast(message, session=session, roast=roast)

    roast.run.assert_not_awaited()
    message.reply.assert_not_awaited()
