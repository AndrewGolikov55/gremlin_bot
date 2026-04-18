from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.roulette import RouletteParticipant
from app.services.roulette import (
    DEFAULT_GENERATED_TITLE,
    RouletteService,
    _coerce_float,
    _coerce_int,
)


class DummySettings:
    def __init__(self, values: dict[str, object] | None = None) -> None:
        self._values = values or {}

    async def get_all(self, chat_id: int) -> dict[str, object]:
        return dict(self._values)


class DummyAppConfig:
    def __init__(self, values: dict[str, object] | None = None) -> None:
        self._values = values or {}

    async def get_all(self) -> dict[str, object]:
        return dict(self._values)


class DummyContext:
    async def get_recent_turns(self, session: Any, chat_id: int, limit: int) -> list[Any]:
        return []


class DummyPersonas:
    async def get_all(self) -> dict[str, str]:
        return {}


class DummyMemory:
    pass


def build_service(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    settings_values: dict[str, object] | None = None,
    app_config_values: dict[str, object] | None = None,
) -> RouletteService:
    return RouletteService(
        bot=cast(Bot, object()),
        sessionmaker=sessionmaker,
        settings=cast(Any, DummySettings(settings_values)),
        app_config=cast(Any, DummyAppConfig(app_config_values)),
        context=cast(Any, DummyContext()),
        personas=cast(Any, DummyPersonas()),
        memory=cast(Any, DummyMemory()),
    )


async def test_register_participant_rejects_bot_like_username_with_whitespace(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    service = build_service(sessionmaker)

    created, count = await service.register_participant(1, 101, "HelperBot ")

    assert (created, count) == (False, 0)
    async with sessionmaker() as session:
        participants = (await session.execute(RouletteParticipant.__table__.select())).all()
    assert participants == []


async def test_pick_title_prefers_custom_title_without_llm(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    service = build_service(sessionmaker)

    async with sessionmaker() as session:
        with patch.object(
            service,
            "_generate_title",
            new=AsyncMock(side_effect=AssertionError("_generate_title should not be called")),
        ) as mocked_generate:
            title_code, title_display = await service._pick_title(
                session,
                chat_id=1,
                conf={"roulette_custom_title": "  Свой титул  "},
                app_conf={},
            )

    assert (title_code, title_display) == ("custom", "Свой титул")
    mocked_generate.assert_not_awaited()


async def test_participant_count_ignores_legacy_bot_like_usernames_with_whitespace(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    service = build_service(sessionmaker)

    async with sessionmaker() as session:
        session.add_all(
            [
                RouletteParticipant(chat_id=1, user_id=1, username="real_user"),
                RouletteParticipant(chat_id=1, user_id=2, username="legacyhelperbot "),
            ]
        )
        await session.commit()

    assert await service.participant_count(1) == 1


async def test_pick_title_returns_default_when_generator_returns_none(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    service = build_service(sessionmaker)

    async with sessionmaker() as session:
        with patch.object(service, "_generate_title", new=AsyncMock(return_value=None)):
            title_code, title_display = await service._pick_title(
                session,
                chat_id=1,
                conf={},
                app_conf={},
            )

    assert (title_code, title_display) == ("generated", DEFAULT_GENERATED_TITLE)


def test_coerce_int_returns_default_for_unsupported_and_invalid_values() -> None:
    assert _coerce_int(object(), 7) == 7
    assert _coerce_int("oops", 7) == 7


def test_coerce_int_accepts_bool_int_and_numeric_strings() -> None:
    assert _coerce_int(True, 7) == 1
    assert _coerce_int(12, 7) == 12
    assert _coerce_int("42", 7) == 42
    assert _coerce_int(b"15", 7) == 15


def test_coerce_float_returns_default_for_unsupported_and_invalid_values() -> None:
    assert _coerce_float(object(), 0.9) == 0.9
    assert _coerce_float("oops", 0.9) == 0.9


async def test_announce_roulette_instructions_reach_llm_as_closing_text(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The roulette focus instructions must arrive as closing_text so the LLM
    responds to them, not to the last unrelated chat message."""
    from app.services import context as ctx_module
    from app.services import roulette as roulette_module

    service = build_service(sessionmaker)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    service.bot = cast(Any, bot)

    captured_calls: list[dict[str, Any]] = []
    original_build_messages = ctx_module.build_messages

    def capturing_build_messages(system_prompt: str, turns: Any, **kwargs: Any) -> Any:
        captured_calls.append(kwargs)
        return original_build_messages(system_prompt, turns, **kwargs)

    fake_llm_response = "Скоро всё узнаете [[winner]]"
    with (
        patch.object(service, "_build_winner_memory_block", new=AsyncMock(return_value=None)),
        patch.object(service.context, "get_recent_turns", new=AsyncMock(return_value=[])),
        patch.object(service.personas, "get_all", new=AsyncMock(return_value={})),
        patch.object(service.settings, "get_all", new=AsyncMock(return_value={})),
        patch.object(service.app_config, "get_all", new=AsyncMock(return_value={})),
        patch.object(roulette_module, "llm_generate", new=AsyncMock(return_value=fake_llm_response)),
        patch(f"{roulette_module.__name__}.build_messages", side_effect=capturing_build_messages),
    ):
        await service._announce(chat_id=1, user_id=42, username="testuser", title_display="Стоячий мастер")

    assert len(captured_calls) == 2, f"Expected 2 build_messages calls, got {len(captured_calls)}"
    intrigue_kwargs = captured_calls[0]
    winner_kwargs = captured_calls[1]

    assert "closing_text" in intrigue_kwargs, "Intrigue call must use closing_text"
    assert intrigue_kwargs["closing_text"] is not None
    assert "Стоячий мастер" in intrigue_kwargs["closing_text"]
    assert "Подогрей интригу" in intrigue_kwargs["closing_text"]

    assert "closing_text" in winner_kwargs, "Winner call must use closing_text"
    assert winner_kwargs["closing_text"] is not None
    assert "Стоячий мастер" in winner_kwargs["closing_text"]


def test_coerce_float_accepts_numeric_inputs_and_strings() -> None:
    assert _coerce_float(2, 0.9) == 2.0
    assert _coerce_float(1.5, 0.9) == 1.5
    assert _coerce_float("0.25", 0.9) == 0.25
    assert _coerce_float(b"0.75", 0.9) == 0.75
