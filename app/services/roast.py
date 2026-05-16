from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import Message, RoastRun, User, UserMemoryProfile
from .app_config import AppConfigService
from .llm.client import LLMError, LLMRateLimitError, resolve_llm_options
from .llm.client import generate as llm_generate
from .persona import DEFAULT_STYLE_KEY, StylePromptService
from .settings import SettingsService

logger = logging.getLogger(__name__)

COOLDOWN = timedelta(hours=24)
ACTIVE_WINDOW = timedelta(days=7)
MAX_MESSAGES = 30
LLM_MAX_TOKENS = 280
LLM_TEMPERATURE = 0.95

ROAST_RULES = (
    "Ты делаешь прожарку. Правила:\n"
    "- Остро, метко, без банальщин и сюсюканья.\n"
    "- Маты уместны если они работают на удар, а не для галочки.\n"
    "- Опирайся на КОНКРЕТНЫЕ сообщения цели и факты из её профиля, а не общие штампы.\n"
    "- Бей по противоречиям, странным паттернам, повторяющимся темам.\n"
    "- Открытая площадка: религия, семья, болезни, нацпринадлежность — всё в игре, если это смешно.\n"
    "- Не призывай к насилию и не выдавай личной информации, которую цель явно прятала.\n"
    "- 3-5 строк plain text, без приветствий и пояснений."
)

# Single-process deployment assumed: per-chat asyncio.Lock guards concurrent
# /roast invocations inside one Python process. With multi-replica deployment
# this lock does not synchronise — two replicas could simultaneously pass the
# cooldown check, run two LLM calls and insert two roast_runs rows. If we ever
# move to multi-replica, switch to a Postgres advisory lock or Redis-based
# per-chat lock here.


@dataclass(frozen=True)
class _TargetContext:
    user_id: int
    display_name: str
    username: str | None
    messages: list[str]
    identity: list[str]
    preferences: list[str]
    projects: list[str]
    boundaries: list[str]
    summary: str | None


class RoastService:
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
        self._chat_locks: dict[int, asyncio.Lock] = {}

    def _get_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    async def _remaining_cooldown(
        self, *, chat_id: int, now: datetime
    ) -> timedelta | None:
        """Return time left in the 24h cooldown, or None if a fresh roast is allowed."""
        cutoff = now - COOLDOWN
        async with self.sessionmaker() as session:
            stmt = (
                select(RoastRun.run_at)
                .where(RoastRun.chat_id == chat_id, RoastRun.run_at >= cutoff)
                .order_by(desc(RoastRun.run_at))
                .limit(1)
            )
            last = (await session.execute(stmt)).scalar_one_or_none()
        if last is None:
            return None
        elapsed = now - last
        remaining = COOLDOWN - elapsed
        if remaining <= timedelta(0):
            return None
        return remaining

    async def _active_user_ids(
        self,
        *,
        chat_id: int,
        now: datetime,
        exclude_user_id: int | None,
    ) -> list[int]:
        """User IDs with at least one non-empty text message in the last 7 days.

        Bots (`is_bot=True`) are excluded. `exclude_user_id` is removed from the result
        if provided (used to drop the initiator for random target selection).
        """
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

    async def _resolve_target(
        self,
        *,
        chat_id: int,
        initiator_id: int,
        target_arg: str | None,
        now: datetime,
    ) -> tuple[int | None, str | None]:
        """Resolve the roast target.

        Returns (user_id, refusal_text). If refusal_text is not None, abort with that text.
        """
        if target_arg is None:
            candidates = await self._active_user_ids(
                chat_id=chat_id, now=now, exclude_user_id=initiator_id,
            )
            if not candidates:
                return None, "Некого жарить, в чате тишина."
            chosen = random.choice(candidates)
            return chosen, None

        # Explicit @username — strip leading @ and case-fold for lookup
        raw = target_arg.strip()
        if raw.startswith("@"):
            raw = raw[1:]
        if not raw:
            return None, "Дай нормальный @username, не пустоту."

        # Lookup in users table
        async with self.sessionmaker() as session:
            stmt = select(User.tg_id).where(func.lower(User.username) == raw.lower())
            target_uid = (await session.execute(stmt)).scalar_one_or_none()

        if target_uid is None:
            return None, f"Не знаю, кто такой @{raw}, в моей базе его нет."

        if target_uid == initiator_id:
            return None, "Сам себя не жарят, попроси кого-нибудь другого."

        # Bot check via get_chat_member (best-effort; if API fails we still continue)
        try:
            member = await self.bot.get_chat_member(chat_id, target_uid)
            user = getattr(member, "user", None)
            if user is not None and getattr(user, "is_bot", False) is True:
                return None, "Ботов не жарим, они и так перегреты."
        except TelegramBadRequest:
            pass
        except Exception:
            logger.exception("roast: get_chat_member failed chat=%s user=%s", chat_id, target_uid)

        # Activity check
        active = await self._active_user_ids(
            chat_id=chat_id, now=now, exclude_user_id=None,
        )
        if target_uid not in active:
            return None, f"У @{raw} нет следов за неделю, нечего разбирать."

        return target_uid, None

    async def _resolve_display(
        self, *, chat_id: int, user_id: int
    ) -> tuple[str, str | None]:
        """Return (display_name, username). Falls back to ("id{user_id}", None) on failure."""
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
            logger.exception("roast: get_chat_member failed chat=%s user=%s", chat_id, user_id)
        return f"id{user_id}", None

    async def _collect_target_context(
        self, *, chat_id: int, user_id: int
    ) -> _TargetContext:
        async with self.sessionmaker() as session:
            stmt = (
                select(Message.text, Message.date)
                .where(
                    Message.chat_id == chat_id,
                    Message.user_id == user_id,
                    Message.is_bot.is_(False),
                    func.length(Message.text) > 0,
                )
                .order_by(desc(Message.date))
                .limit(MAX_MESSAGES)
            )
            rows = (await session.execute(stmt)).all()
            profile = await session.get(UserMemoryProfile, (chat_id, user_id))

        # Oldest-to-newest for the LLM
        messages = [str(row[0]) for row in reversed(rows)]

        display_name, username = await self._resolve_display(
            chat_id=chat_id, user_id=user_id
        )

        return _TargetContext(
            user_id=user_id,
            display_name=display_name,
            username=username,
            messages=messages,
            identity=list(profile.identity or []) if profile else [],
            preferences=list(profile.preferences or []) if profile else [],
            projects=list(profile.projects or []) if profile else [],
            boundaries=list(profile.boundaries or []) if profile else [],
            summary=(profile.summary if profile and profile.summary else None),
        )

    @staticmethod
    def _format_list(items: list[str]) -> str:
        clean = [str(i).strip() for i in items if str(i).strip()]
        return ", ".join(clean) if clean else "—"

    @staticmethod
    def _format_messages(items: list[str]) -> str:
        if not items:
            return "(нет текстовых сообщений)"
        return "\n".join(f"{i}. {text}" for i, text in enumerate(items, start=1))

    @staticmethod
    def _format_boundaries(items: list[str]) -> str:
        clean = [str(i).strip() for i in items if str(i).strip()]
        if not clean:
            return "—"
        return "\n".join(f"- {b}" for b in clean)

    async def _build_prompts(
        self, *, chat_id: int, ctx: _TargetContext
    ) -> tuple[str, str]:
        conf = await self.settings.get_all(chat_id)
        style = str(conf.get("style", DEFAULT_STYLE_KEY))
        persona_prompt = await self.personas.get(style)

        system = f"{persona_prompt}\n\n{ROAST_RULES}"

        username_label = f"@{ctx.username}" if ctx.username else "без юзернейма"

        user = (
            f"Цель: {ctx.display_name} ({username_label})\n"
            f"\n"
            f"Профиль:\n"
            f"- identity: {self._format_list(ctx.identity)}\n"
            f"- preferences: {self._format_list(ctx.preferences)}\n"
            f"- projects: {self._format_list(ctx.projects)}\n"
            f"- summary: {ctx.summary or '—'}\n"
            f"\n"
            f"Hidden topics (то, что цель явно прятала — НЕ упоминай в шутке):\n"
            f"{self._format_boundaries(ctx.boundaries)}\n"
            f"\n"
            f"Последние сообщения цели (от старых к новым):\n"
            f"{self._format_messages(ctx.messages)}\n"
            f"\n"
            f"Жарь."
        )
        return system, user
