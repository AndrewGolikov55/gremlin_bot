from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...models import AkinatorQuestion, AkinatorRound, Message, User, UserMemoryProfile
from ...utils.locks import get_chat_lock
from ..app_config import AppConfigService
from ..llm.client import LLMError, LLMRateLimitError, resolve_llm_options
from ..llm.client import generate as llm_generate
from .common import RoundStatus

logger = logging.getLogger(__name__)

MAX_QUESTIONS = 20

SYSTEM_PROMPT = (
    "Ты ведущий игры «Акинатор». Бот загадал участника чата по его профилю и сообщениям.\n"
    "Игрок задаёт вопрос «да/нет» о загаданном участнике. Ответь ОДНИМ словом:\n"
    "- 'yes' если по фактам/профилю/сообщениям загаданного это правда\n"
    "- 'no' если это неправда\n"
    "- 'maybe' если есть и за, и против, или контекст неоднозначен\n"
    "- 'unknown' если в профиле и сообщениях нет данных для ответа\n"
    "Никаких лишних слов, только одно из четырёх значений."
)


class AkinatorService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        bot: Bot,
        app_config: AppConfigService,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.bot = bot
        self.app_config = app_config
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, chat_id: int) -> asyncio.Lock:
        return get_chat_lock(chat_id, self._locks)

    async def _resolve_display(self, *, chat_id: int, user_id: int) -> tuple[str, str | None]:
        try:
            member = await self.bot.get_chat_member(chat_id, user_id)
            if member.status in {
                ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED,
            }:
                user = getattr(member, "user", None)
                if user is not None:
                    name = user.first_name or user.username or f"id{user_id}"
                    return str(name), (user.username or None)
        except TelegramBadRequest:
            pass
        except Exception:
            logger.exception("akinator: get_chat_member failed chat=%s user=%s", chat_id, user_id)
        return f"id{user_id}", None

    async def _pick_target(self, *, chat_id: int, exclude_user_id: int) -> int | None:
        """Pick a chat member with a non-empty profile (identity OR summary OR preferences)."""
        async with self.sessionmaker() as session:
            stmt = select(UserMemoryProfile).where(UserMemoryProfile.chat_id == chat_id)
            profiles = (await session.execute(stmt)).scalars().all()
        candidates: list[int] = []
        for p in profiles:
            uid = int(p.user_id)
            if uid == exclude_user_id:
                continue
            has_data = (
                (p.identity and len(p.identity) > 0)
                or (p.preferences and len(p.preferences) > 0)
                or (p.summary and p.summary.strip())
            )
            if has_data:
                candidates.append(uid)
        if not candidates:
            return None
        return random.choice(candidates)

    async def start(self, *, chat_id: int, initiator_id: int) -> None:
        async with self._lock(chat_id):
            target = await self._pick_target(chat_id=chat_id, exclude_user_id=initiator_id)
            if target is None:
                await self.bot.send_message(
                    chat_id,
                    "Не из кого выбирать — ни у кого нет наполненного профиля.",
                )
                return
            try:
                async with self.sessionmaker() as session:
                    async with session.begin():
                        round_ = AkinatorRound(
                            chat_id=chat_id,
                            initiator_user_id=initiator_id,
                            target_user_id=target,
                            status=RoundStatus.ACTIVE.value,
                        )
                        session.add(round_)
            except IntegrityError:
                await self.bot.send_message(chat_id, "В чате уже идёт раунд Акинатора.")
                return
        await self.bot.send_message(
            chat_id,
            f"🤔 Я загадал участника этого чата.\n"
            f"Задавайте вопросы yes/no через /akinator_ask <вопрос>.\n"
            f"Угадывайте через /akinator_guess @username.\n"
            f"Лимит — {MAX_QUESTIONS} вопросов.",
        )

    async def _llm_answer(self, *, system: str, user: str) -> str:
        conf = await self.app_config.get_all()
        provider = resolve_llm_options(conf)
        try:
            text = await llm_generate(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.1,
                max_tokens=10,
                provider=provider,
            )
        except (LLMError, LLMRateLimitError):
            logger.exception("akinator: LLM failed")
            return "unknown"
        normalised = (text or "").strip().lower()
        for token in ("yes", "no", "maybe", "unknown"):
            if token in normalised:
                return token
        return "unknown"

    async def ask(self, *, chat_id: int, asker_id: int, question: str) -> None:
        question = question.strip()
        if not question:
            await self.bot.send_message(chat_id, "Задай вопрос текстом после команды.")
            return

        async with self._lock(chat_id):
            async with self.sessionmaker() as session:
                round_ = await self._fetch_active(session, chat_id)
                if round_ is None:
                    await self.bot.send_message(chat_id, "Раунд Акинатора не идёт. /akinator чтобы запустить.")
                    return
                if round_.questions_asked >= MAX_QUESTIONS:
                    return  # Should not happen; handled by closer
                # Build LLM user prompt with target profile + messages
                target_uid = round_.target_user_id
                stmt = (
                    select(Message.text)
                    .where(
                        Message.chat_id == chat_id,
                        Message.user_id == target_uid,
                        Message.is_bot.is_(False),
                        func.length(Message.text) > 0,
                    )
                    .order_by(Message.date.desc())
                    .limit(25)
                )
                rows = (await session.execute(stmt)).all()
                profile = await session.get(UserMemoryProfile, (chat_id, target_uid))

        messages_block = "\n".join(f"- {row[0]}" for row in rows) or "(нет сообщений)"
        identity = ", ".join(profile.identity or []) if profile else ""
        prefs = ", ".join(profile.preferences or []) if profile else ""
        projects = ", ".join(profile.projects or []) if profile else ""
        summary = (profile.summary if profile else None) or "—"

        user_prompt = (
            f"Профиль загаданного:\n"
            f"identity: {identity or '—'}\n"
            f"preferences: {prefs or '—'}\n"
            f"projects: {projects or '—'}\n"
            f"summary: {summary}\n\n"
            f"Сообщения загаданного:\n{messages_block}\n\n"
            f"Вопрос игрока: {question}\n"
            f"Ответ (одно слово: yes/no/maybe/unknown):"
        )
        answer = await self._llm_answer(system=SYSTEM_PROMPT, user=user_prompt)

        async with self.sessionmaker() as session:
            async with session.begin():
                round_ = await self._fetch_active(session, chat_id)
                if round_ is None:
                    return
                session.add(AkinatorQuestion(
                    round_id=round_.id,
                    asker_user_id=asker_id,
                    question=question,
                    answer=answer,
                ))
                round_.questions_asked = round_.questions_asked + 1
                await session.flush()
                exhausted = round_.questions_asked >= MAX_QUESTIONS

        emoji = {"yes": "✅", "no": "❌", "maybe": "🤷", "unknown": "❓"}.get(answer, "❓")
        await self.bot.send_message(chat_id, f"{emoji} {answer} (вопросов: {round_.questions_asked}/{MAX_QUESTIONS})")

        if exhausted:
            await self._finish_lost(chat_id=chat_id)

    async def guess(self, *, chat_id: int, asker_id: int, target_username: str | None) -> None:
        if not target_username:
            await self.bot.send_message(chat_id, "Укажи кого угадываешь: /akinator_guess @username.")
            return
        raw = target_username.strip()
        if raw.startswith("@"):
            raw = raw[1:]
        async with self.sessionmaker() as session:
            target_uid = (
                await session.execute(
                    select(User.tg_id).where(func.lower(User.username) == raw.lower())
                )
            ).scalar_one_or_none()
        if target_uid is None:
            await self.bot.send_message(chat_id, f"Не знаю @{raw}.")
            return

        async with self._lock(chat_id):
            async with self.sessionmaker() as session:
                round_ = await self._fetch_active(session, chat_id)
                if round_ is None:
                    await self.bot.send_message(chat_id, "Раунд не идёт.")
                    return
                correct = round_.target_user_id == target_uid
                if correct:
                    await session.execute(
                        update(AkinatorRound)
                        .where(AkinatorRound.id == round_.id)
                        .values(
                            status=RoundStatus.WON.value,
                            winner_user_id=asker_id,
                            finished_at=datetime.utcnow(),
                        )
                    )
                    await session.commit()
                else:
                    await session.commit()
        if correct:
            display, _ = await self._resolve_display(chat_id=chat_id, user_id=target_uid)
            await self.bot.send_message(chat_id, f"🎉 В точку! Был загадан {display}. Угадал — {asker_id}.")
        else:
            await self.bot.send_message(chat_id, f"❌ Нет, не @{raw}.")

    async def _finish_lost(self, *, chat_id: int) -> None:
        async with self.sessionmaker() as session:
            round_ = await self._fetch_active(session, chat_id)
            if round_ is None:
                return
            await session.execute(
                update(AkinatorRound)
                .where(AkinatorRound.id == round_.id)
                .values(status=RoundStatus.LOST.value, finished_at=datetime.utcnow())
            )
            await session.commit()
            target_uid = round_.target_user_id
        display, _ = await self._resolve_display(chat_id=chat_id, user_id=target_uid)
        await self.bot.send_message(
            chat_id,
            f"⏱ Вопросы кончились. Был загадан {display}. Команда проиграла.",
        )

    @staticmethod
    async def _fetch_active(session: AsyncSession, chat_id: int) -> AkinatorRound | None:
        stmt = (
            select(AkinatorRound)
            .where(
                AkinatorRound.chat_id == chat_id,
                AkinatorRound.status == RoundStatus.ACTIVE.value,
            )
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()
