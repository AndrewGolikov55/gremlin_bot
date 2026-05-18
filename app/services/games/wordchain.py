from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime

from aiogram import Bot
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...models import WordchainRound, WordchainWord
from ...utils.locks import get_chat_lock
from .common import RoundStatus

logger = logging.getLogger(__name__)

WORD_RE = re.compile(r"^[а-яё]{2,30}$")
TURN_TIMEOUT_SECONDS = 60

# Hardcoded Russian noun seeds (nominative case, singular). Keep simple/common.
SEED_WORDS: tuple[str, ...] = (
    "стол", "лампа", "дом", "кот", "море", "лес", "сахар", "книга",
    "облако", "молоко", "трава", "окно", "ручка", "снег", "звезда",
    "перо", "город", "машина", "велосипед", "пирог",
    "озеро", "ракета", "башня", "грибы", "ладонь", "север", "юг",
    "торт", "телефон", "паутина", "костёр", "мост", "плита",
    "верёвка", "корабль", "ботинок", "карандаш", "кофе", "сахарница",
    "дорога", "поле", "ветер", "капля", "солнце", "ночь", "утро",
    "вечер", "обед", "ужин",
)

_SKIP_TRAILING = set("ьъы")


def _meaningful_last_letter(word: str) -> str:
    for ch in reversed(word):
        if ch not in _SKIP_TRAILING:
            return ch
    return word[-1] if word else ""


class WordchainService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        bot: Bot,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = {}
        self._timers: dict[int, asyncio.Task] = {}

    def _lock(self, chat_id: int) -> asyncio.Lock:
        return get_chat_lock(chat_id, self._locks)

    async def start(self, *, chat_id: int) -> None:
        async with self._lock(chat_id):
            existing = await self._fetch_active(chat_id)
            if existing is not None:
                await self.bot.send_message(chat_id, "Цепочка уже идёт. /wordchain_stop чтобы закрыть.")
                return
            seed = random.choice(SEED_WORDS)
            try:
                async with self.sessionmaker() as session:
                    async with session.begin():
                        round_ = WordchainRound(
                            chat_id=chat_id,
                            status=RoundStatus.ACTIVE.value,
                            last_word=seed,
                            last_word_at=datetime.utcnow(),
                        )
                        session.add(round_)
                        await session.flush()
                        session.add(WordchainWord(round_id=round_.id, user_id=0, word=seed))
                    round_id = round_.id
            except IntegrityError:
                await self.bot.send_message(chat_id, "Цепочка уже идёт.")
                return
        await self.bot.send_message(
            chat_id,
            f"🔗 Цепочка слов начата!\n"
            f"Стартовое слово: <b>{seed}</b>\n"
            f"Каждое следующее слово — существительное в им. падеже, начинается на «{_meaningful_last_letter(seed).upper()}», "
            f"без повторов. Играйте: /wordchain_play <слово>.\n"
            f"На ход {TURN_TIMEOUT_SECONDS} сек.",
        )
        self._reset_timer(chat_id=chat_id, round_id=round_id)

    async def play(self, *, chat_id: int, user_id: int, raw_word: str) -> None:
        word = raw_word.strip().lower().replace("ё", "ё")
        if not word:
            await self.bot.send_message(chat_id, "Пусто, скажи слово.")
            return
        if not WORD_RE.match(word):
            await self.bot.send_message(chat_id, "❌ Слово только из русских букв (2–30 символов).")
            return

        async with self._lock(chat_id):
            async with self.sessionmaker() as session:
                async with session.begin():
                    round_ = await self._fetch_active(chat_id, session=session)
                    if round_ is None:
                        await self.bot.send_message(chat_id, "Цепочка не идёт. /wordchain чтобы стартануть.")
                        return
                    expected_letter = _meaningful_last_letter(round_.last_word or "")
                    if expected_letter and not word.startswith(expected_letter):
                        await self.bot.send_message(
                            chat_id,
                            f"❌ Слово должно начинаться на «{expected_letter.upper()}», "
                            f"а у тебя — «{word[0].upper()}».",
                        )
                        return
                    # Try insert; UNIQUE (round_id, word) catches repeats
                    session.add(WordchainWord(round_id=round_.id, user_id=user_id, word=word))
                    try:
                        await session.flush()
                    except IntegrityError:
                        await self.bot.send_message(chat_id, "❌ Это слово уже было в этой цепочке.")
                        return
                    await session.execute(
                        update(WordchainRound)
                        .where(WordchainRound.id == round_.id)
                        .values(
                            last_word=word,
                            last_user_id=user_id,
                            last_word_at=datetime.utcnow(),
                        )
                    )
                    round_id = round_.id
        await self.bot.send_message(
            chat_id,
            f"✅ <b>{word}</b> — следующее на «{_meaningful_last_letter(word).upper()}».",
        )
        self._reset_timer(chat_id=chat_id, round_id=round_id)

    async def stop(self, *, chat_id: int) -> None:
        async with self._lock(chat_id):
            async with self.sessionmaker() as session:
                round_ = await self._fetch_active(chat_id, session=session)
                if round_ is None:
                    await self.bot.send_message(chat_id, "Цепочки нет.")
                    return
                await session.execute(
                    update(WordchainRound)
                    .where(WordchainRound.id == round_.id)
                    .values(status=RoundStatus.FINISHED.value, finished_at=datetime.utcnow())
                )
                await session.commit()
            self._cancel_timer(chat_id)
        await self.bot.send_message(chat_id, "🔗 Цепочка закрыта.")

    def _reset_timer(self, *, chat_id: int, round_id: int) -> None:
        self._cancel_timer(chat_id)
        self._timers[chat_id] = asyncio.create_task(self._timeout_after(chat_id, round_id))

    def _cancel_timer(self, chat_id: int) -> None:
        task = self._timers.pop(chat_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _timeout_after(self, chat_id: int, round_id: int) -> None:
        try:
            await asyncio.sleep(TURN_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            return
        async with self._lock(chat_id):
            async with self.sessionmaker() as session:
                round_ = await session.get(WordchainRound, round_id)
                if round_ is None or round_.status != RoundStatus.ACTIVE.value:
                    return
                await session.execute(
                    update(WordchainRound)
                    .where(WordchainRound.id == round_id)
                    .values(
                        status=RoundStatus.EXPIRED.value,
                        finished_at=datetime.utcnow(),
                        loser_user_id=round_.last_user_id,
                    )
                )
                await session.commit()
                last_word = round_.last_word
        await self.bot.send_message(
            chat_id,
            f"⌛ Время вышло. Последнее слово: <b>{last_word}</b>. Цепочка закрыта.",
        )

    async def _fetch_active(
        self, chat_id: int, *, session: AsyncSession | None = None,
    ) -> WordchainRound | None:
        stmt = (
            select(WordchainRound)
            .where(
                WordchainRound.chat_id == chat_id,
                WordchainRound.status == RoundStatus.ACTIVE.value,
            )
            .limit(1)
        )
        if session is None:
            async with self.sessionmaker() as s:
                return (await s.execute(stmt)).scalar_one_or_none()
        return (await session.execute(stmt)).scalar_one_or_none()
