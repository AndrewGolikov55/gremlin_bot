from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    # "дятел" — не в SEED_WORDS, начинается на «д», должно быть отвергнуто
    # (last_word="кот", ждём «т...»). Если бы взяли слово из SEED_WORDS — могли
    # бы попасть на тот же seed, и тест бы упал из-за совпадения, а не из-за
    # настоящего mismatch'а.
    await svc.play(chat_id=42, user_id=200, raw_word="дятел")
    await svc.stop(chat_id=42)
    async with sessionmaker() as session:
        words = [w.word for w in (await session.execute(select(WordchainWord))).scalars().all()]
    assert "дятел" not in words


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


from app.services.games.wordchain import is_valid_noun


class TestIsValidNoun:
    def test_accepts_simple_noun(self) -> None:
        ok, refusal = is_valid_noun("стол")
        assert ok is True
        assert refusal is None

    def test_rejects_unknown_word(self) -> None:
        ok, refusal = is_valid_noun("рокинокичу")
        assert ok is False
        assert refusal == "Не знаю такого слова."

    def test_rejects_plural(self) -> None:
        ok, refusal = is_valid_noun("столы")
        assert ok is False
        assert "ед. число" in (refusal or "")

    def test_rejects_genitive(self) -> None:
        ok, refusal = is_valid_noun("стола")
        assert ok is False
        assert "им. падеже" in (refusal or "")

    def test_rejects_verb(self) -> None:
        ok, refusal = is_valid_noun("бегать")
        assert ok is False
        assert "существительным" in (refusal or "")

    def test_accepts_normalised_yo_form(self) -> None:
        # ё→е already done by caller; pymorphy treats them as equivalent anyway
        ok, refusal = is_valid_noun("еж")
        assert ok is True
        assert refusal is None


class TestPlayValidation:
    @pytest.mark.asyncio
    async def test_rejects_unknown_word(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        bot = AsyncMock()
        bot.send_message = AsyncMock()
        svc = WordchainService(sessionmaker=sessionmaker, bot=bot)
        await svc.start(chat_id=42)
        bot.send_message.reset_mock()
        await svc.play(chat_id=42, user_id=200, raw_word="рокинокичу")
        await svc.stop(chat_id=42)
        async with sessionmaker() as session:
            words = [w.word for w in (await session.execute(select(WordchainWord))).scalars().all()]
        assert "рокинокичу" not in words
        sent = [c.args[1] for c in bot.send_message.await_args_list]
        assert any("Не знаю такого слова" in s for s in sent)

    @pytest.mark.asyncio
    async def test_rejects_plural_form(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        bot = AsyncMock()
        bot.send_message = AsyncMock()
        svc = WordchainService(sessionmaker=sessionmaker, bot=bot)
        await svc.start(chat_id=42)
        bot.send_message.reset_mock()
        await svc.play(chat_id=42, user_id=200, raw_word="столы")
        await svc.stop(chat_id=42)
        async with sessionmaker() as session:
            words = [w.word for w in (await session.execute(select(WordchainWord))).scalars().all()]
        assert "столы" not in words

    @pytest.mark.asyncio
    async def test_rejects_verb(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        bot = AsyncMock()
        bot.send_message = AsyncMock()
        svc = WordchainService(sessionmaker=sessionmaker, bot=bot)
        await svc.start(chat_id=42)
        bot.send_message.reset_mock()
        await svc.play(chat_id=42, user_id=200, raw_word="бегать")
        await svc.stop(chat_id=42)
        async with sessionmaker() as session:
            words = [w.word for w in (await session.execute(select(WordchainWord))).scalars().all()]
        assert "бегать" not in words


class TestPlayAlternating:
    @pytest.mark.asyncio
    async def test_same_user_twice_in_row_rejected(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        from sqlalchemy import update as sa_update
        bot = AsyncMock()
        bot.send_message = AsyncMock()
        svc = WordchainService(sessionmaker=sessionmaker, bot=bot)
        await svc.start(chat_id=42)
        # Force last_word=поле so we can play валидное слово на «е»
        async with sessionmaker() as session:
            await session.execute(
                sa_update(WordchainRound).values(last_word="поле")
            )
            await session.commit()
        # First play by user 200 succeeds
        await svc.play(chat_id=42, user_id=200, raw_word="енот")
        bot.send_message.reset_mock()
        # Second play by SAME user 200 must be rejected even if word is valid
        await svc.play(chat_id=42, user_id=200, raw_word="трактор")
        await svc.stop(chat_id=42)
        async with sessionmaker() as session:
            words = [w.word for w in (await session.execute(select(WordchainWord))).scalars().all()]
        assert "енот" in words
        assert "трактор" not in words
        sent = [c.args[1] for c in bot.send_message.await_args_list]
        assert any("Жду другого игрока" in s for s in sent)

    @pytest.mark.asyncio
    async def test_different_users_alternate_ok(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        from sqlalchemy import update as sa_update
        bot = AsyncMock()
        bot.send_message = AsyncMock()
        svc = WordchainService(sessionmaker=sessionmaker, bot=bot)
        await svc.start(chat_id=42)
        async with sessionmaker() as session:
            await session.execute(
                sa_update(WordchainRound).values(last_word="поле")
            )
            await session.commit()
        await svc.play(chat_id=42, user_id=200, raw_word="енот")
        await svc.play(chat_id=42, user_id=201, raw_word="трактор")
        await svc.stop(chat_id=42)
        async with sessionmaker() as session:
            words = [w.word for w in (await session.execute(select(WordchainWord))).scalars().all()]
        assert "енот" in words
        assert "трактор" in words

    @pytest.mark.asyncio
    async def test_first_player_after_seed_not_blocked(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        from sqlalchemy import update as sa_update
        bot = AsyncMock()
        bot.send_message = AsyncMock()
        svc = WordchainService(sessionmaker=sessionmaker, bot=bot)
        await svc.start(chat_id=42)
        # Seed user_id=0; первый реальный игрок (любой uid) проходит alternating
        async with sessionmaker() as session:
            await session.execute(
                sa_update(WordchainRound).values(last_word="поле")
            )
            await session.commit()
        await svc.play(chat_id=42, user_id=200, raw_word="енот")
        await svc.stop(chat_id=42)
        async with sessionmaker() as session:
            words = [w.word for w in (await session.execute(select(WordchainWord))).scalars().all()]
        assert "енот" in words
