from __future__ import annotations

import asyncio
import unittest.mock as um
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest
from sqlalchemy import select

from app.models import StorychainContribution, StorychainRound
from app.services.app_config import AppConfigService
from app.services.games.storychain import MAX_ROUND_AGE, StorychainService
from app.services.persona import StylePromptService
from app.services.settings import SettingsService


def _make_bot():
    bot = MagicMock()
    sent = MagicMock(message_id=555)
    bot.send_message = AsyncMock(return_value=sent)
    return bot


def _make_svc(sessionmaker, *, bot=None):
    bot = bot or _make_bot()
    settings = create_autospec(SettingsService, instance=True)
    settings.get_all = AsyncMock(return_value={"style": "default"})
    personas = create_autospec(StylePromptService, instance=True)
    personas.get = AsyncMock(return_value="PERSONA")
    app_config = create_autospec(AppConfigService, instance=True)
    app_config.get_all = AsyncMock(return_value={})
    return StorychainService(
        sessionmaker=sessionmaker,
        bot=bot,
        personas=personas,
        settings=settings,
        app_config=app_config,
    )


@pytest.mark.asyncio
async def test_start_creates_active_round(sessionmaker):
    svc = _make_svc(sessionmaker)
    with um.patch(
        "app.services.games.storychain.llm_generate",
        AsyncMock(return_value="Жил-был кот."),
    ):
        await svc.start(chat_id=42, target_contributions=3)
    async with sessionmaker() as session:
        rounds = (await session.execute(select(StorychainRound))).scalars().all()
    assert len(rounds) == 1
    assert rounds[0].status == "active"
    assert rounds[0].target_contributions == 3
    assert rounds[0].seed == "Жил-был кот."


@pytest.mark.asyncio
async def test_start_clamps_target_into_3_12(sessionmaker):
    svc = _make_svc(sessionmaker)
    with um.patch("app.services.games.storychain.llm_generate", AsyncMock(return_value="seed")):
        await svc.start(chat_id=1, target_contributions=1)
        await svc.start(chat_id=2, target_contributions=99)
    async with sessionmaker() as session:
        rows = (await session.execute(select(StorychainRound))).scalars().all()
    targets = sorted(r.target_contributions for r in rows)
    assert targets == [3, 12]


@pytest.mark.asyncio
async def test_add_appends_contribution_and_advances(sessionmaker):
    svc = _make_svc(sessionmaker)
    with um.patch("app.services.games.storychain.llm_generate", AsyncMock(return_value="seed")):
        await svc.start(chat_id=42, target_contributions=5)
    await svc.add(chat_id=42, user_id=100, text="первое предложение")
    async with sessionmaker() as session:
        contribs = (await session.execute(select(StorychainContribution))).scalars().all()
        rounds = (await session.execute(select(StorychainRound))).scalars().all()
    assert len(contribs) == 1
    assert contribs[0].text == "первое предложение"
    assert rounds[0].status == "active"


@pytest.mark.asyncio
async def test_add_rejects_empty_text(sessionmaker):
    svc = _make_svc(sessionmaker)
    with um.patch("app.services.games.storychain.llm_generate", AsyncMock(return_value="seed")):
        await svc.start(chat_id=42)
    await svc.add(chat_id=42, user_id=100, text="   ")
    async with sessionmaker() as session:
        contribs = (await session.execute(select(StorychainContribution))).scalars().all()
    assert contribs == []


@pytest.mark.asyncio
async def test_add_rejects_too_long(sessionmaker):
    svc = _make_svc(sessionmaker)
    with um.patch("app.services.games.storychain.llm_generate", AsyncMock(return_value="seed")):
        await svc.start(chat_id=42)
    await svc.add(chat_id=42, user_id=100, text="x" * 501)
    async with sessionmaker() as session:
        contribs = (await session.execute(select(StorychainContribution))).scalars().all()
    assert contribs == []


@pytest.mark.asyncio
async def test_add_failed_send_does_not_persist_contribution(sessionmaker):
    """If Telegram send_message fails, the contribution row must NOT be inserted."""
    bot = _make_bot()
    svc = _make_svc(sessionmaker, bot=bot)
    with um.patch("app.services.games.storychain.llm_generate", AsyncMock(return_value="seed")):
        await svc.start(chat_id=42)
    # Make the per-contribution send raise. The first call (start's announce)
    # has already happened with success.
    bot.send_message = AsyncMock(side_effect=RuntimeError("telegram boom"))
    await svc.add(chat_id=42, user_id=100, text="должно потеряться")
    async with sessionmaker() as session:
        contribs = (await session.execute(select(StorychainContribution))).scalars().all()
    assert contribs == []  # nothing persisted


@pytest.mark.asyncio
async def test_concurrent_add_at_target_finalises_only_once(sessionmaker):
    """Two parallel /storychain_add at the target boundary → only one _finalise call."""
    svc = _make_svc(sessionmaker)
    with um.patch("app.services.games.storychain.llm_generate", AsyncMock(return_value="seed")):
        await svc.start(chat_id=42, target_contributions=3)
    # Pre-fill 2 contributions so the next two adds will both observe count >= 3
    await svc.add(chat_id=42, user_id=100, text="один")
    await svc.add(chat_id=42, user_id=101, text="два")

    finalise_calls = 0
    real_finalise = svc._finalise

    async def counting_finalise(*args, **kwargs):
        nonlocal finalise_calls
        finalise_calls += 1
        await real_finalise(*args, **kwargs)

    svc._finalise = counting_finalise

    with um.patch(
        "app.services.games.storychain.llm_generate",
        AsyncMock(return_value="это финал."),
    ):
        await asyncio.gather(
            svc.add(chat_id=42, user_id=102, text="три"),
            svc.add(chat_id=42, user_id=103, text="четыре"),
        )
    assert finalise_calls == 1
    async with sessionmaker() as session:
        rounds = (await session.execute(select(StorychainRound))).scalars().all()
    assert rounds[0].status == "finalised"


@pytest.mark.asyncio
async def test_start_blocked_while_finalising(sessionmaker):
    """A new /storychain while previous round is FINALISING must be rejected."""
    svc = _make_svc(sessionmaker)
    with um.patch("app.services.games.storychain.llm_generate", AsyncMock(return_value="seed")):
        await svc.start(chat_id=42, target_contributions=3)
    # Mark the round as FINALISING directly
    async with sessionmaker() as session:
        row = (await session.execute(select(StorychainRound))).scalar_one()
        row.status = "finalising"
        await session.commit()

    bot = svc.bot
    bot.send_message.reset_mock()
    with um.patch("app.services.games.storychain.llm_generate", AsyncMock(return_value="seed2")):
        await svc.start(chat_id=42)
    # No second row created
    async with sessionmaker() as session:
        rounds = (await session.execute(select(StorychainRound))).scalars().all()
    assert len(rounds) == 1
    # User got a refusal
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_recover_stale_expires_old_active_rounds(sessionmaker):
    svc = _make_svc(sessionmaker)
    with um.patch("app.services.games.storychain.llm_generate", AsyncMock(return_value="seed")):
        await svc.start(chat_id=42)
    # Backdate started_at
    async with sessionmaker() as session:
        row = (await session.execute(select(StorychainRound))).scalar_one()
        row.started_at = datetime.utcnow() - MAX_ROUND_AGE - timedelta(hours=1)
        await session.commit()

    expired = await svc.recover_stale()
    assert expired == 1
    async with sessionmaker() as session:
        row = (await session.execute(select(StorychainRound))).scalar_one()
    assert row.status == "expired"


@pytest.mark.asyncio
async def test_recover_stale_leaves_fresh_rounds(sessionmaker):
    svc = _make_svc(sessionmaker)
    with um.patch("app.services.games.storychain.llm_generate", AsyncMock(return_value="seed")):
        await svc.start(chat_id=42)
    expired = await svc.recover_stale()
    assert expired == 0
    async with sessionmaker() as session:
        row = (await session.execute(select(StorychainRound))).scalar_one()
    assert row.status == "active"


@pytest.mark.asyncio
async def test_stop_marks_round_expired(sessionmaker):
    svc = _make_svc(sessionmaker)
    with um.patch("app.services.games.storychain.llm_generate", AsyncMock(return_value="seed")):
        await svc.start(chat_id=42)
    await svc.stop(chat_id=42)
    async with sessionmaker() as session:
        row = (await session.execute(select(StorychainRound))).scalar_one()
    assert row.status == "expired"
