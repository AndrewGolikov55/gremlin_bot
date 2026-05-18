from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import Message, User, UserMemoryProfile
from ..utils.text import strip_markdown
from .app_config import AppConfigService
from .llm.client import LLMError, LLMRateLimitError, resolve_llm_options
from .llm.client import generate as llm_generate
from .persona import DEFAULT_STYLE_KEY, StylePromptService
from .settings import SettingsService

logger = logging.getLogger(__name__)

ACTIVE_WINDOW = timedelta(days=7)
MAX_MESSAGES = 25
MAX_MESSAGE_CHARS = 240  # per-message cap before joining into LLM prompt
LLM_MAX_TOKENS = 360
LLM_TEMPERATURE = 0.95


def _truncate(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    s = str(text)
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


COMMON_OUTPUT_RULES = (
    "Никакого markdown — ни **, ни *, ни __, ни _, ни backticks. Только plain text.\n"
    "Не пиши вступлений вроде «Окей, погнали» или «Ну что, @user». Сразу к делу.\n"
)

TRUTH_RULES = (
    "Жанр: правда-или-действие. Сгенерируй ОДИН вопрос-«правду» для цели — "
    "острый, личный, цепляющий за её паттерны из профиля и сообщений.\n"
    "Без обличений и реальных угроз. 1-3 предложения, заверши вопросительным знаком.\n"
    + COMMON_OUTPUT_RULES
)
DARE_RULES = (
    "Жанр: правда-или-действие. Сгенерируй ОДНО «действие» для цели — смешное, "
    "выполнимое в чате/в течение часа, без денег и без вреда. Должно отзеркаливать её "
    "интересы или паттерны.\n"
    "1-3 предложения, повелительное наклонение.\n"
    + COMMON_OUTPUT_RULES
)
HOROSCOPE_RULES = (
    "Сгенерируй персональный гороскоп на сегодня для цели, на основе её "
    "интересов, проектов и последних сообщений. Тон — ироничный астролог. "
    "3-5 предложений, упомяни конкретное дело/паттерн цели, а не общие штампы.\n"
    + COMMON_OUTPUT_RULES
)
FORTUNE_RULES = (
    "Сгенерируй короткое предсказание-«fortune cookie» — 1-2 предложения, "
    "афористично, абсурдно-философски, без обращения к конкретному человеку.\n"
    + COMMON_OUTPUT_RULES
)
WISDOM_RULES = (
    "Сгенерируй фейк-афоризм, который якобы произнёс заданный участник. "
    "Стиль — глубокомысленная чушь в духе пабликов «мудрость дня», но с зацепкой "
    "за конкретные интересы/паттерны участника. 1-3 предложения от ПЕРВОГО ЛИЦА "
    "(как будто он сам говорит).\n"
    + COMMON_OUTPUT_RULES
)
PREDICT_RULES = (
    "Сгенерируй абсурдное предсказание о будущем цели — что с ней произойдёт через "
    "неделю/месяц/год, опираясь на её паттерны и последние сообщения. "
    "3-5 предложений, без угроз, с конкретикой.\n"
    + COMMON_OUTPUT_RULES
)


@dataclass
class QuickGameContext:
    chat_id: int
    initiator_id: int
    target_uid: int | None
    target_display: str
    target_username: str | None
    target_messages: list[str] = field(default_factory=list)
    identity: list[str] = field(default_factory=list)
    preferences: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    summary: str | None = None
    persona_system: str = ""
    active_user_ids: list[int] = field(default_factory=list)


def _format_list(items: list[str]) -> str:
    clean = [str(i).strip() for i in items if str(i).strip()]
    return ", ".join(clean) if clean else "—"


def _format_messages(items: list[str]) -> str:
    if not items:
        return "(нет текстовых сообщений)"
    return "\n".join(f"{i}. {_truncate(text)}" for i, text in enumerate(items, start=1))


class QuickGameService:
    """Shared infrastructure for LLM single-shot games (/truth, /horoscope, /fortune, /wisdom, /predict)."""

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

    async def _active_user_ids(
        self, *, chat_id: int, now: datetime, exclude_user_id: int | None
    ) -> list[int]:
        cutoff = now - ACTIVE_WINDOW
        stmt = (
            select(Message.user_id)
            .where(
                Message.chat_id == chat_id,
                Message.is_bot.is_(False),
                Message.date >= cutoff,
                func.length(Message.text) > 0,
            )
            .group_by(Message.user_id)
        )
        async with self.sessionmaker() as session:
            rows = (await session.execute(stmt)).all()
        ids = [int(row[0]) for row in rows]
        if exclude_user_id is not None:
            ids = [uid for uid in ids if uid != exclude_user_id]
        return ids

    async def _resolve_display(
        self, *, chat_id: int, user_id: int
    ) -> tuple[str, str | None]:
        active_statuses = {
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.RESTRICTED,
        }
        try:
            member = await self.bot.get_chat_member(chat_id, user_id)
            if member.status in active_statuses:
                user = getattr(member, "user", None)
                if user is not None:
                    name = user.first_name or user.username or f"id{user_id}"
                    return str(name), (user.username or None)
        except TelegramBadRequest:
            pass
        except Exception:
            logger.exception("quick_games: get_chat_member failed chat=%s user=%s", chat_id, user_id)
        return f"id{user_id}", None

    async def _resolve_username(self, raw: str) -> int | None:
        raw = raw.strip()
        if raw.startswith("@"):
            raw = raw[1:]
        if not raw:
            return None
        async with self.sessionmaker() as session:
            stmt = select(User.tg_id).where(func.lower(User.username) == raw.lower())
            return (await session.execute(stmt)).scalar_one_or_none()

    async def _persona_system(self, chat_id: int) -> str:
        conf = await self.settings.get_all(chat_id)
        style = str(conf.get("style", DEFAULT_STYLE_KEY))
        return await self.personas.get(style)

    async def build_context(
        self,
        *,
        chat_id: int,
        initiator_id: int,
        target_arg: str | None,
        now: datetime,
        need_target: bool = True,
        load_persona: bool = True,
    ) -> QuickGameContext | str:
        """Build a QuickGameContext. Returns a refusal string if it cannot proceed."""
        active = await self._active_user_ids(chat_id=chat_id, now=now, exclude_user_id=None)

        target_uid: int | None = None
        if need_target:
            if target_arg:
                target_uid = await self._resolve_username(target_arg)
                if target_uid is None:
                    return f"Не знаю участника {target_arg.strip()}."
            else:
                candidates = [uid for uid in active if uid != initiator_id]
                if not candidates:
                    return "Не из кого выбирать, в чате тишина."
                target_uid = random.choice(candidates)

        ctx = QuickGameContext(
            chat_id=chat_id,
            initiator_id=initiator_id,
            target_uid=target_uid,
            target_display="",
            target_username=None,
            active_user_ids=active,
        )

        if target_uid is not None:
            ctx.target_display, ctx.target_username = await self._resolve_display(
                chat_id=chat_id, user_id=target_uid
            )
            async with self.sessionmaker() as session:
                stmt = (
                    select(Message.text)
                    .where(
                        Message.chat_id == chat_id,
                        Message.user_id == target_uid,
                        Message.is_bot.is_(False),
                        func.length(Message.text) > 0,
                    )
                    .order_by(desc(Message.date))
                    .limit(MAX_MESSAGES)
                )
                rows = (await session.execute(stmt)).all()
                profile = await session.get(UserMemoryProfile, (chat_id, target_uid))
            ctx.target_messages = [str(row[0]) for row in reversed(rows)]
            if profile is not None:
                ctx.identity = list(profile.identity or [])
                ctx.preferences = list(profile.preferences or [])
                ctx.projects = list(profile.projects or [])
                ctx.summary = profile.summary if profile.summary else None

        if load_persona:
            ctx.persona_system = await self._persona_system(chat_id)

        return ctx

    async def _llm_oneshot(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = LLM_MAX_TOKENS,
        temperature: float = LLM_TEMPERATURE,
    ) -> str | None:
        conf = await self.app_config.get_all()
        provider = resolve_llm_options(conf)
        try:
            text = await llm_generate(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
                provider=provider,
            )
        except (LLMError, LLMRateLimitError):
            logger.exception("quick_games: LLM provider failed")
            return None
        return strip_markdown(text).strip() if text else None

    async def _send(self, chat_id: int, text: str) -> None:
        await self.bot.send_message(chat_id=chat_id, text=text)

    def _format_target_block(self, ctx: QuickGameContext) -> str:
        username_label = f"@{ctx.target_username}" if ctx.target_username else "без юзернейма"
        return (
            f"Цель: {ctx.target_display} ({username_label})\n"
            f"\n"
            f"Профиль:\n"
            f"- identity: {_format_list(ctx.identity)}\n"
            f"- preferences: {_format_list(ctx.preferences)}\n"
            f"- projects: {_format_list(ctx.projects)}\n"
            f"- summary: {ctx.summary or '—'}\n"
            f"\n"
            f"Последние сообщения цели:\n"
            f"{_format_messages(ctx.target_messages)}\n"
        )

    def _mention(self, ctx: QuickGameContext) -> str:
        return f"@{ctx.target_username}" if ctx.target_username else ctx.target_display

    # ---------------- /truth ----------------

    async def run_truth_or_dare(
        self, *, chat_id: int, initiator_id: int, target_arg: str | None,
        now: datetime | None = None,
    ) -> None:
        if now is None:
            now = datetime.utcnow()
        ctx = await self.build_context(
            chat_id=chat_id, initiator_id=initiator_id, target_arg=target_arg, now=now,
        )
        if isinstance(ctx, str):
            await self._send(chat_id, ctx)
            return

        is_truth = random.random() < 0.5
        rules = TRUTH_RULES if is_truth else DARE_RULES
        system = f"{ctx.persona_system}\n\n{rules}"
        user = self._format_target_block(ctx) + "\nВыдай."
        body = await self._llm_oneshot(system=system, user=user) or (
            "LLM в обмороке, попробуй ещё раз."
        )
        label = "🎭 Правда" if is_truth else "🎬 Действие"
        await self._send(chat_id, f"{label} для {self._mention(ctx)}:\n\n{body}")

    # ---------------- /horoscope ----------------

    async def run_horoscope(
        self, *, chat_id: int, initiator_id: int, target_arg: str | None,
        now: datetime | None = None,
    ) -> None:
        if now is None:
            now = datetime.utcnow()
        ctx = await self.build_context(
            chat_id=chat_id, initiator_id=initiator_id, target_arg=target_arg, now=now,
        )
        if isinstance(ctx, str):
            await self._send(chat_id, ctx)
            return
        system = f"{ctx.persona_system}\n\n{HOROSCOPE_RULES}"
        user = self._format_target_block(ctx) + "\nГороскоп на сегодня."
        body = await self._llm_oneshot(system=system, user=user) or (
            "Звёзды молчат, LLM в обмороке."
        )
        await self._send(chat_id, f"🔮 Гороскоп для {self._mention(ctx)}:\n\n{body}")

    # ---------------- /fortune ----------------

    async def run_fortune(
        self, *, chat_id: int, initiator_id: int, now: datetime | None = None,
    ) -> None:
        if now is None:
            now = datetime.utcnow()
        # No target, no persona — neutral cookie voice
        system = FORTUNE_RULES
        user = "Выдай одно предсказание-fortune cookie."
        body = await self._llm_oneshot(system=system, user=user, max_tokens=120) or (
            "Печенье пустое, LLM в обмороке."
        )
        await self._send(chat_id, f"🥠 {body}")

    # ---------------- /wisdom ----------------

    async def run_wisdom(
        self, *, chat_id: int, initiator_id: int, now: datetime | None = None,
    ) -> None:
        if now is None:
            now = datetime.utcnow()
        active = await self._active_user_ids(
            chat_id=chat_id, now=now, exclude_user_id=None,
        )
        if not active:
            await self._send(chat_id, "Не из кого выбирать мудреца.")
            return
        speaker_uid = random.choice(active)
        display, username = await self._resolve_display(chat_id=chat_id, user_id=speaker_uid)
        async with self.sessionmaker() as session:
            stmt = (
                select(Message.text)
                .where(
                    Message.chat_id == chat_id,
                    Message.user_id == speaker_uid,
                    Message.is_bot.is_(False),
                    func.length(Message.text) > 0,
                )
                .order_by(desc(Message.date))
                .limit(MAX_MESSAGES)
            )
            rows = (await session.execute(stmt)).all()
            profile = await session.get(UserMemoryProfile, (chat_id, speaker_uid))

        messages = [str(row[0]) for row in reversed(rows)]
        username_label = f"@{username}" if username else "без юзернейма"
        identity = list(profile.identity or []) if profile else []
        preferences = list(profile.preferences or []) if profile else []

        system = WISDOM_RULES
        user = (
            f"Говорящий: {display} ({username_label})\n"
            f"Интересы: {_format_list(identity + preferences)}\n"
            f"Последние сообщения:\n{_format_messages(messages)}\n"
            f"\nВыдай афоризм от первого лица."
        )
        body = await self._llm_oneshot(system=system, user=user, max_tokens=180) or (
            "Мудрость растворилась, LLM в обмороке."
        )
        mention = f"@{username}" if username else display
        await self._send(chat_id, f"📜 Как однажды сказал {mention}:\n\n«{body}»")

    # ---------------- /predict ----------------

    async def run_predict(
        self, *, chat_id: int, initiator_id: int, target_arg: str | None,
        now: datetime | None = None,
    ) -> None:
        if now is None:
            now = datetime.utcnow()
        ctx = await self.build_context(
            chat_id=chat_id, initiator_id=initiator_id, target_arg=target_arg, now=now,
        )
        if isinstance(ctx, str):
            await self._send(chat_id, ctx)
            return
        system = f"{ctx.persona_system}\n\n{PREDICT_RULES}"
        user = self._format_target_block(ctx) + "\nПредскажи будущее."
        body = await self._llm_oneshot(system=system, user=user) or (
            "Будущее туманно, LLM в обмороке."
        )
        await self._send(chat_id, f"🌌 Предсказание для {self._mention(ctx)}:\n\n{body}")
