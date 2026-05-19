from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape

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
MAX_ROUND_AGE = timedelta(hours=24)
MAX_MESSAGE_CHARS = 240


def _truncate(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    s = str(text)
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


@dataclass(frozen=True)
class TargetMeta:
    """Telegram + activity metadata for the target user. Cached per akinator round."""
    display: str           # tg first_name, fallback к username/id
    username: str | None
    member_status: str | None
    message_count_week: int


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
        self._meta_cache: dict[int, TargetMeta] = {}  # key: round_id

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

    async def _fetch_target_meta(self, *, chat_id: int, user_id: int) -> TargetMeta:
        """ONE get_chat_member + ONE COUNT() — без двойного API-вызова."""
        display = f"id{user_id}"
        username: str | None = None
        member_status: str | None = None
        try:
            member = await self.bot.get_chat_member(chat_id, user_id)
            user = getattr(member, "user", None)
            if user is not None:
                display = str(user.first_name or user.username or f"id{user_id}")
                username = user.username or None
            # ChatMemberStatus in this aiogram is a plain Enum, not StrEnum,
            # so str(enum) returns "ChatMemberStatus.MEMBER"; use .value when available.
            status = member.status
            member_status = str(getattr(status, "value", status))
        except TelegramBadRequest:
            pass
        except Exception:
            logger.exception("akinator: get_chat_member meta failed chat=%s user=%s", chat_id, user_id)
        async with self.sessionmaker() as session:
            cutoff = datetime.utcnow() - timedelta(days=7)
            stmt = (
                select(func.count())
                .select_from(Message)
                .where(
                    Message.chat_id == chat_id,
                    Message.user_id == user_id,
                    Message.is_bot.is_(False),
                    Message.date >= cutoff,
                )
            )
            count = int((await session.execute(stmt)).scalar_one())
        return TargetMeta(
            display=display,
            username=username,
            member_status=member_status,
            message_count_week=count,
        )

    async def _target_meta(
        self, *, round_id: int, chat_id: int, user_id: int,
    ) -> TargetMeta:
        """Round-scoped cache around _fetch_target_meta."""
        cached = self._meta_cache.get(round_id)
        if cached is not None:
            return cached
        meta = await self._fetch_target_meta(chat_id=chat_id, user_id=user_id)
        self._meta_cache[round_id] = meta
        return meta

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
                await self._report_active_round(chat_id)
                return
        await self.bot.send_message(
            chat_id,
            f"🤔 Я загадал участника этого чата.\n"
            f"Задавайте вопросы yes/no через /akinator_ask «вопрос».\n"
            f"Угадывайте через /akinator_guess @username.\n"
            f"Закрыть раунд — /akinator_stop. Лимит — {MAX_QUESTIONS} вопросов.",
        )

    async def _report_active_round(self, chat_id: int) -> None:
        """Send a helpful message when /akinator is called but a round is already running."""
        async with self.sessionmaker() as session:
            active = await self._fetch_active(session, chat_id)
        if active is None:
            # Race: round was just closed between the failed insert and our select.
            await self.bot.send_message(
                chat_id, "🤔 Раунд только что закрылся, попробуйте /akinator ещё раз.",
            )
            return
        await self.bot.send_message(
            chat_id,
            f"🤔 Раунд Акинатора уже идёт — задано "
            f"{active.questions_asked}/{MAX_QUESTIONS} вопросов.\n"
            f"Спрашивайте через /akinator_ask «вопрос», угадывайте через "
            f"/akinator_guess @username или закройте через /akinator_stop.",
        )

    async def stop(self, *, chat_id: int) -> None:
        async with self._lock(chat_id):
            async with self.sessionmaker() as session:
                round_ = await self._fetch_active(session, chat_id)
                if round_ is None:
                    await self.bot.send_message(chat_id, "Раунд Акинатора не идёт.")
                    return
                target_uid = round_.target_user_id
                questions_asked = round_.questions_asked
                await session.execute(
                    update(AkinatorRound)
                    .where(AkinatorRound.id == round_.id)
                    .values(status=RoundStatus.ABORTED.value, finished_at=datetime.utcnow())
                )
                await session.commit()
        display, _ = await self._resolve_display(chat_id=chat_id, user_id=target_uid)
        await self.bot.send_message(
            chat_id,
            f"🤔 Раунд закрыт после {questions_asked}/{MAX_QUESTIONS} вопросов. "
            f"Был загадан {escape(display)}.",
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
        # Take the first alphabetic run, match exactly. Avoids substring traps:
        # "not really" → first token "not" → unknown (instead of false "no");
        # "approximately yes" → "approximately" → unknown (instead of false "yes").
        normalised = (text or "").strip().lower()
        first = ""
        for ch in normalised:
            if ch.isalpha():
                first += ch
            elif first:
                break
        if first in {"yes", "no", "maybe", "unknown"}:
            return first
        return "unknown"

    async def ask(self, *, chat_id: int, asker_id: int, question: str) -> None:
        question = question.strip()
        if not question:
            await self.bot.send_message(chat_id, "Задай вопрос текстом после команды.")
            return

        # Atomically claim a question slot: increment counter only if there's
        # capacity left. Returns the round + new counter value, or None if the
        # round is gone / already at MAX_QUESTIONS.
        claim = await self._claim_question_slot(chat_id)
        if claim is None:
            await self.bot.send_message(
                chat_id, "Раунд Акинатора не идёт или вопросы кончились.",
            )
            return
        round_id, target_uid, new_count = claim

        # Load target context outside any lock (read-only, can be slow)
        async with self.sessionmaker() as session:
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

        messages_block = "\n".join(f"- {_truncate(row[0])}" for row in rows) or "(нет сообщений)"
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
            session.add(AkinatorQuestion(
                round_id=round_id,
                asker_user_id=asker_id,
                question=question,
                answer=answer,
            ))
            await session.commit()

        emoji = {"yes": "✅", "no": "❌", "maybe": "🤷", "unknown": "❓"}.get(answer, "❓")
        await self.bot.send_message(
            chat_id, f"{emoji} {answer} (вопросов: {new_count}/{MAX_QUESTIONS})",
        )

        if new_count >= MAX_QUESTIONS:
            await self._finish_lost(chat_id=chat_id)

    async def _claim_question_slot(
        self, chat_id: int,
    ) -> tuple[int, int, int] | None:
        """Atomically increment questions_asked if room is left.

        Returns (round_id, target_user_id, new_counter_value) on success, None otherwise.
        Single SQL statement → safe under concurrent /akinator_ask.
        """
        async with self.sessionmaker() as session:
            async with session.begin():
                result = await session.execute(
                    update(AkinatorRound)
                    .where(
                        AkinatorRound.chat_id == chat_id,
                        AkinatorRound.status == RoundStatus.ACTIVE.value,
                        AkinatorRound.questions_asked < MAX_QUESTIONS,
                    )
                    .values(questions_asked=AkinatorRound.questions_asked + 1)
                    .returning(AkinatorRound.id, AkinatorRound.target_user_id, AkinatorRound.questions_asked)
                )
                row = result.first()
        if row is None:
            return None
        return int(row[0]), int(row[1]), int(row[2])

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
            await self.bot.send_message(chat_id, f"Не знаю @{escape(raw)}.")
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
            asker_display, asker_username = await self._resolve_display(
                chat_id=chat_id, user_id=asker_id,
            )
            mention = (
                f"@{escape(asker_username)}" if asker_username else escape(asker_display)
            )
            await self.bot.send_message(
                chat_id,
                f"🎉 В точку! Был загадан {escape(display)}. Угадал — {mention}.",
            )
        else:
            await self.bot.send_message(chat_id, f"❌ Нет, не @{escape(raw)}.")

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
            f"⏱ Вопросы кончились. Был загадан {escape(display)}. Команда проиграла.",
        )

    async def recover_stale(self, *, now: datetime | None = None) -> int:
        """Expire ACTIVE rounds older than MAX_ROUND_AGE (called at startup)."""
        if now is None:
            now = datetime.utcnow()
        cutoff = now - MAX_ROUND_AGE
        async with self.sessionmaker() as session:
            result = await session.execute(
                update(AkinatorRound)
                .where(
                    AkinatorRound.status == RoundStatus.ACTIVE.value,
                    AkinatorRound.started_at < cutoff,
                )
                .values(status=RoundStatus.EXPIRED.value, finished_at=now)
            )
            await session.commit()
            return int(result.rowcount or 0)  # type: ignore[attr-defined]

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
