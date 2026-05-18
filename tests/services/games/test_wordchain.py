from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models import WordchainRound, WordchainWord
from app.services.games.wordchain import WordchainService


def _make_svc(sessionmaker, *, bot=None):
    return WordchainService(sessionmaker=sessionmaker, bot=bot or AsyncMock())


@pytest.mark.asyncio
async def test_start_creates_active_round_with_seed(sessionmaker):
    svc = _make_svc(sessionmaker)
    await svc.start(chat_id=42)
    # immediately stop to cancel the timeout task (avoid lingering tasks in tests)
    await svc.stop(chat_id=42)
    async with sessionmaker() as session:
        rounds = (await session.execute(select(WordchainRound))).scalars().all()
        words = (await session.execute(select(WordchainWord))).scalars().all()
    assert len(rounds) == 1
    assert rounds[0].last_word
    assert len(words) == 1


@pytest.mark.asyncio
async def test_play_valid_word_advances(sessionmaker):
    svc = _make_svc(sessionmaker)
    await svc.start(chat_id=42)
    async with sessionmaker() as session:
        round_ = (await session.execute(select(WordchainRound))).scalars().one()
    # force a predictable seed: write "кот" via DB
    from sqlalchemy import update
    async with sessionmaker() as session:
        await session.execute(
            update(WordchainRound).where(WordchainRound.id == round_.id).values(last_word="кот")
        )
        await session.commit()
    await svc.play(chat_id=42, user_id=200, raw_word="торт")
    await svc.stop(chat_id=42)
    async with sessionmaker() as session:
        rounds = (await session.execute(select(WordchainRound))).scalars().all()
        words = [w.word for w in (await session.execute(select(WordchainWord))).scalars().all()]
    assert "торт" in words
    assert rounds[0].last_word == "торт"


@pytest.mark.asyncio
async def test_repeat_rejected(sessionmaker):
    svc = _make_svc(sessionmaker)
    await svc.start(chat_id=42)
    from sqlalchemy import update
    async with sessionmaker() as session:
        round_ = (await session.execute(select(WordchainRound))).scalars().one()
        await session.execute(
            update(WordchainRound).where(WordchainRound.id == round_.id).values(last_word="кот")
        )
        await session.commit()
    await svc.play(chat_id=42, user_id=200, raw_word="торт")
    await svc.play(chat_id=42, user_id=201, raw_word="торт")
    await svc.stop(chat_id=42)
    async with sessionmaker() as session:
        words = (await session.execute(select(WordchainWord))).scalars().all()
    # Seed + only one "торт"
    assert sum(1 for w in words if w.word == "торт") == 1


@pytest.mark.asyncio
async def test_first_letter_mismatch_rejected(sessionmaker):
    svc = _make_svc(sessionmaker)
    await svc.start(chat_id=42)
    from sqlalchemy import update
    async with sessionmaker() as session:
        round_ = (await session.execute(select(WordchainRound))).scalars().one()
        await session.execute(
            update(WordchainRound).where(WordchainRound.id == round_.id).values(last_word="кот")
        )
        await session.commit()
    await svc.play(chat_id=42, user_id=200, raw_word="дом")  # должно быть "т..."
    await svc.stop(chat_id=42)
    async with sessionmaker() as session:
        words = [w.word for w in (await session.execute(select(WordchainWord))).scalars().all()]
    assert "дом" not in words


@pytest.mark.asyncio
async def test_yo_is_normalised_to_e(sessionmaker):
    """ёж and еж must collide via UNIQUE — normalised to the same string on input."""
    from sqlalchemy import update
    svc = _make_svc(sessionmaker)
    await svc.start(chat_id=42)
    # Need a last_word whose meaningful letter is "е" so "ёлка" (→ "елка") fits.
    async with sessionmaker() as session:
        round_ = (await session.execute(select(WordchainRound))).scalars().one()
        await session.execute(
            update(WordchainRound).where(WordchainRound.id == round_.id).values(last_word="поле")
        )
        await session.commit()
    await svc.play(chat_id=42, user_id=200, raw_word="ёлка")
    await svc.play(chat_id=42, user_id=201, raw_word="ёлка")  # ё→е, дубль
    await svc.stop(chat_id=42)
    async with sessionmaker() as session:
        words = [w.word for w in (await session.execute(select(WordchainWord))).scalars().all()]
    assert words.count("елка") == 1
    assert "ёлка" not in words


@pytest.mark.asyncio
async def test_recover_stale_expires_old_active_rounds(sessionmaker):
    from datetime import datetime, timedelta

    from sqlalchemy import update

    from app.services.games.wordchain import RECOVERY_SLACK

    svc = _make_svc(sessionmaker)
    await svc.start(chat_id=42)
    async with sessionmaker() as session:
        await session.execute(
            update(WordchainRound).values(
                last_word_at=datetime.utcnow() - RECOVERY_SLACK - timedelta(seconds=10),
            )
        )
        await session.commit()
    recovered = await svc.recover_stale()
    assert recovered == 1
    async with sessionmaker() as session:
        row = (await session.execute(select(WordchainRound))).scalar_one()
    assert row.status == "expired"
