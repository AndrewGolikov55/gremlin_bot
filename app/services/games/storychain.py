from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...models import StorychainContribution, StorychainRound
from ...utils.locks import get_chat_lock
from ...utils.text import strip_markdown
from ..app_config import AppConfigService
from ..llm.client import LLMError, LLMRateLimitError, resolve_llm_options
from ..llm.client import generate as llm_generate
from ..persona import DEFAULT_STYLE_KEY, StylePromptService
from ..settings import SettingsService
from .common import RoundStatus

logger = logging.getLogger(__name__)

DEFAULT_TARGET = 6

SEED_RULES = (
    "Сгенерируй ОДНО первое предложение для совместной истории в чате. "
    "Зацепка должна быть интригующей, но открытой — чтобы было что продолжать. "
    "Никакого markdown, только текст. 1-2 предложения."
)

FINALE_RULES = (
    "Перед тобой совместная история, написанная участниками чата по очереди. "
    "Напиши финал — 2-4 предложения, который красиво/неожиданно закрывает сюжет. "
    "Никакого markdown."
)


class StorychainService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        bot: Bot,
        personas: StylePromptService,
        settings: SettingsService,
        app_config: AppConfigService,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.bot = bot
        self.personas = personas
        self.settings = settings
        self.app_config = app_config
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, chat_id: int) -> asyncio.Lock:
        return get_chat_lock(chat_id, self._locks)

    async def _persona_prompt(self, chat_id: int) -> str:
        conf = await self.settings.get_all(chat_id)
        style = str(conf.get("style", DEFAULT_STYLE_KEY))
        return await self.personas.get(style)

    async def _llm(self, *, system: str, user: str, max_tokens: int = 220) -> str | None:
        conf = await self.app_config.get_all()
        provider = resolve_llm_options(conf)
        try:
            text = await llm_generate(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.95,
                max_tokens=max_tokens,
                provider=provider,
            )
        except (LLMError, LLMRateLimitError):
            logger.exception("storychain: LLM failed")
            return None
        return strip_markdown(text).strip() if text else None

    async def start(self, *, chat_id: int, target_contributions: int | None = None) -> None:
        target = target_contributions or DEFAULT_TARGET
        target = max(3, min(target, 12))

        async with self._lock(chat_id):
            existing = await self._fetch_active(chat_id)
            if existing is not None:
                await self.bot.send_message(
                    chat_id, "Сторичейн уже идёт. /storychain_stop чтобы закрыть.",
                )
                return
            persona = await self._persona_prompt(chat_id)
            seed = await self._llm(
                system=f"{persona}\n\n{SEED_RULES}",
                user="Сгенерируй первое предложение.",
                max_tokens=120,
            ) or "В пятницу вечером Алёна обнаружила, что её кот купил два билета в Хельсинки."

            try:
                async with self.sessionmaker() as session:
                    async with session.begin():
                        round_ = StorychainRound(
                            chat_id=chat_id,
                            status=RoundStatus.ACTIVE.value,
                            seed=seed,
                            target_contributions=target,
                        )
                        session.add(round_)
                        await session.flush()
                        round_id = round_.id
            except IntegrityError:
                await self.bot.send_message(chat_id, "Сторичейн уже идёт.")
                return

        msg = await self.bot.send_message(
            chat_id,
            f"📖 Сторичейн начат! Цель — {target} вкладов.\n\n"
            f"<i>{seed}</i>\n\n"
            f"Продолжайте по очереди через /storychain_add <предложение>.",
        )
        async with self.sessionmaker() as session:
            await session.execute(
                update(StorychainRound)
                .where(StorychainRound.id == round_id)
                .values(seed_message_id=msg.message_id)
            )
            await session.commit()

    async def add(self, *, chat_id: int, user_id: int, text: str) -> None:
        text = text.strip()
        if not text:
            await self.bot.send_message(chat_id, "Пусто, напиши предложение.")
            return
        if len(text) > 500:
            await self.bot.send_message(chat_id, "Слишком длинно, до 500 символов.")
            return

        async with self._lock(chat_id):
            async with self.sessionmaker() as session:
                async with session.begin():
                    round_ = await self._fetch_active(chat_id, session=session)
                    if round_ is None:
                        await self.bot.send_message(chat_id, "Сторичейн не идёт. /storychain чтобы запустить.")
                        return
                    msg = await self.bot.send_message(chat_id, f"➕ <i>{text}</i>")
                    session.add(StorychainContribution(
                        round_id=round_.id,
                        user_id=user_id,
                        text=text,
                        message_id=msg.message_id,
                    ))
                    count_stmt = select(StorychainContribution.id).where(
                        StorychainContribution.round_id == round_.id
                    )
                    contributions = (await session.execute(count_stmt)).all()
                    count = len(contributions) + 1  # включая только что добавленную? нет, она ещё во flush
                    # повторно подсчитаем после flush
                round_target = round_.target_contributions
                round_id = round_.id
            # Повторный пересчёт после коммита
            async with self.sessionmaker() as session:
                count_stmt = select(StorychainContribution.id).where(
                    StorychainContribution.round_id == round_id
                )
                count = len((await session.execute(count_stmt)).all())

        if count >= round_target:
            await self._finalise(chat_id=chat_id, round_id=round_id)

    async def _finalise(self, *, chat_id: int, round_id: int) -> None:
        async with self.sessionmaker() as session:
            round_ = await session.get(StorychainRound, round_id)
            if round_ is None or round_.status != RoundStatus.ACTIVE.value:
                return
            contributions_stmt = (
                select(StorychainContribution.text)
                .where(StorychainContribution.round_id == round_id)
                .order_by(StorychainContribution.id)
            )
            contributions = [str(row[0]) for row in (await session.execute(contributions_stmt)).all()]
            seed = round_.seed

        story = f"{seed}\n\n" + "\n".join(contributions)
        persona = await self._persona_prompt(chat_id)
        finale = await self._llm(
            system=f"{persona}\n\n{FINALE_RULES}",
            user=f"Текущая история:\n{story}\n\nНапиши финал.",
            max_tokens=260,
        ) or "(история осталась без финала: LLM в обмороке)"

        async with self.sessionmaker() as session:
            await session.execute(
                update(StorychainRound)
                .where(StorychainRound.id == round_id)
                .values(
                    status=RoundStatus.FINALISED.value,
                    finalised_at=datetime.utcnow(),
                    finale=finale,
                )
            )
            await session.commit()

        await self.bot.send_message(chat_id, f"📖 <b>Финал</b>:\n\n{finale}")

    async def stop(self, *, chat_id: int) -> None:
        async with self._lock(chat_id):
            async with self.sessionmaker() as session:
                round_ = await self._fetch_active(chat_id, session=session)
                if round_ is None:
                    await self.bot.send_message(chat_id, "Сторичейна нет.")
                    return
                await session.execute(
                    update(StorychainRound)
                    .where(StorychainRound.id == round_.id)
                    .values(status=RoundStatus.EXPIRED.value, finalised_at=datetime.utcnow())
                )
                await session.commit()
        await self.bot.send_message(chat_id, "📖 Сторичейн закрыт.")

    async def _fetch_active(
        self, chat_id: int, *, session: AsyncSession | None = None,
    ) -> StorychainRound | None:
        stmt = (
            select(StorychainRound)
            .where(
                StorychainRound.chat_id == chat_id,
                StorychainRound.status == RoundStatus.ACTIVE.value,
            )
            .limit(1)
        )
        if session is None:
            async with self.sessionmaker() as s:
                return (await s.execute(stmt)).scalar_one_or_none()
        return (await session.execute(stmt)).scalar_one_or_none()
