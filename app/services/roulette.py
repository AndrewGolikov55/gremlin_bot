from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime, date
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import desc, func, select
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.chat import Chat
from ..models.roulette import RouletteWinner, RouletteParticipant
from ..models.chat import Chat
from ..services.context import (
    ContextService,
    build_messages,
    build_system_prompt,
    DEFAULT_CHAT_PROMPT,
    DEFAULT_FOCUS_SUFFIX,
)
from ..services.llm.ollama import (
    OpenRouterError,
    OpenRouterRateLimitError,
    generate as llm_generate,
    resolve_llm_options,
)
from ..services.moderation import apply_moderation
from ..services.persona import StylePromptService
from ..services.settings import SettingsService
from ..services.app_config import AppConfigService
from ..utils.llm import resolve_temperature


logger = logging.getLogger("roulette")
MoscowTZ = ZoneInfo("Europe/Moscow")

TITLE_CHOICES = [
    ("pidor", "Пидор"),
    ("skuf", "Скуф"),
    ("beauty", "Красавчик"),
    ("clown", "Клоун"),
]

DEFAULT_ROULETTE_PROMPT = (
    "Ты — ведущий шуточной рулетки. Говори от первого лица и не описывай себя со стороны."
    " Пиши 1–2 короткие фразы без Markdown, поддержи стиль выбранной персоны и не раскрывай победителя."
)


@dataclass
class RollResult:
    success: bool
    message: str


@dataclass
class StatsEntry:
    user_id: int
    username: str | None
    wins: int


class RouletteService:
    def __init__(
        self,
        *,
        bot: Bot,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: SettingsService,
        app_config: AppConfigService,
        context: ContextService,
        personas: StylePromptService,
    ) -> None:
        self.bot = bot
        self.sessionmaker = sessionmaker
        self.settings = settings
        self.app_config = app_config
        self.context = context
        self.personas = personas

    def _today(self) -> date:
        return datetime.now(MoscowTZ).date()

    async def _has_winner_today(self, session: AsyncSession, chat_id: int) -> bool:
        stmt = (
            select(func.count())
            .select_from(RouletteWinner)
            .where(RouletteWinner.chat_id == chat_id, RouletteWinner.won_at == self._today())
        )
        count = (await session.execute(stmt)).scalar() or 0
        return count > 0

    async def roll(self, chat_id: int, *, initiator: str | None = None, force: bool = False) -> RollResult:
        today = self._today()
        async with self.sessionmaker() as session:
            if not force and await self._has_winner_today(session, chat_id):
                return RollResult(False, "Рулетка уже запускалась сегодня. Возвращайся завтра!")

            participants = await self._fetch_participants(session, chat_id)
            if not participants:
                return RollResult(False, "Некого разыгрывать — зарегистрируйтесь командой /reg.")

            winner_user_id, winner_username = random.choice(participants)
            title_code, title_display = await self._pick_title(chat_id)

            try:
                await self._announce(chat_id, winner_user_id, winner_username, title_code, title_display)
            except OpenRouterRateLimitError as exc:
                logger.warning(
                    "Rate limit during roulette announcement chat=%s retry_after=%s",
                    chat_id,
                    exc.retry_after,
                )
                await self._announce_without_llm(chat_id, winner_user_id, winner_username, title_display)
            except OpenRouterError:
                logger.exception("LLM failed while preparing roulette announcement chat=%s", chat_id)
                await self._announce_without_llm(chat_id, winner_user_id, winner_username, title_display)
            except Exception:
                logger.exception("Unexpected error during roulette announcement chat=%s", chat_id)
                await self._announce_without_llm(chat_id, winner_user_id, winner_username, title_display)

            winner = RouletteWinner(
                chat_id=chat_id,
                user_id=winner_user_id,
                username=winner_username,
                title=title_display,
                title_code=title_code,
                won_at=today,
            )
            session.add(winner)
            await session.commit()

            return RollResult(True, "Розыгрыш завершён!")

    async def _fetch_participants(self, session: AsyncSession, chat_id: int) -> list[tuple[int, str | None]]:
        stmt = (
            select(RouletteParticipant.user_id, RouletteParticipant.username)
            .where(RouletteParticipant.chat_id == chat_id)
        )
        rows = await session.execute(stmt)
        return [(row[0], row[1]) for row in rows.fetchall()]

    async def register_participant(self, chat_id: int, user_id: int, username: str | None) -> tuple[bool, int]:
        # ignore obvious bot accounts by username suffix
        if username and username.lower().endswith("bot"):
            return False, await self.participant_count(chat_id)
        async with self.sessionmaker() as session:
            stmt = select(RouletteParticipant).where(
                RouletteParticipant.chat_id == chat_id,
                RouletteParticipant.user_id == user_id,
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing is None:
                participant = RouletteParticipant(
                    chat_id=chat_id,
                    user_id=user_id,
                    username=username,
                )
                session.add(participant)
                is_new = True
            else:
                if username and existing.username != username:
                    existing.username = username
                is_new = False
            await session.commit()

        count = await self.participant_count(chat_id)
        return is_new, count

    async def participant_count(self, chat_id: int) -> int:
        async with self.sessionmaker() as session:
            username_col = sa.func.coalesce(RouletteParticipant.username, "")
            stmt = select(func.count(RouletteParticipant.id)).where(
                RouletteParticipant.chat_id == chat_id,
                sa.not_(username_col.ilike("%bot")),
            )
            return (await session.execute(stmt)).scalar() or 0

    async def unregister_participant(self, chat_id: int, user_id: int) -> tuple[bool, int]:
        async with self.sessionmaker() as session:
            stmt = select(RouletteParticipant).where(
                RouletteParticipant.chat_id == chat_id,
                RouletteParticipant.user_id == user_id,
            )
            participant = (await session.execute(stmt)).scalar_one_or_none()
            if participant is None:
                return False, await self.participant_count(chat_id)
            await session.delete(participant)
            await session.commit()
        return True, await self.participant_count(chat_id)

    async def _pick_title(self, chat_id: int) -> tuple[str, str]:
        conf = await self.settings.get_all(chat_id)
        custom = conf.get("roulette_custom_title")
        if custom:
            return "custom", str(custom)
        return random.choice(TITLE_CHOICES)

    async def _announce(
        self,
        chat_id: int,
        user_id: int,
        username: str | None,
        title_code: str,
        title_display: str,
    ) -> None:
        conf = await self.settings.get_all(chat_id)
        app_conf = await self.app_config.get_all()
        style_prompts = await self.personas.get_all()
        provider, fallback_enabled = resolve_llm_options(app_conf)

        max_turns = int(app_conf.get("context_max_turns", 100) or 100)
        prompt_limit = self._prompt_token_limit(app_conf)
        async with self.sessionmaker() as session:
            turns = await self.context.get_recent_turns(session, chat_id, max_turns)
        focus_text = (
            "Скоро объявим обладателя звания '"
            + title_display
            + "'. Подогрей интригу, но не раскрывай имя."
        )
        base_prompt = str(app_conf.get("prompt_roulette_base") or DEFAULT_ROULETTE_PROMPT).strip()
        if not base_prompt:
            base_prompt = str(app_conf.get("prompt_chat_base") or DEFAULT_CHAT_PROMPT)
        focus_suffix = str(app_conf.get("prompt_focus_suffix") or DEFAULT_FOCUS_SUFFIX)
        system_prompt = build_system_prompt(
            conf,
            focus_text=focus_text,
            style_prompts=style_prompts,
            base_prompt=base_prompt,
            focus_suffix=focus_suffix,
        )
        messages = build_messages(
            system_prompt,
            turns,
            max_turns=max_turns,
            max_tokens=prompt_limit,
        )

        intrigue = await llm_generate(
            messages,
            temperature=resolve_temperature(conf),
            top_p=float(conf.get("top_p", 0.9) or 0.9),
            max_tokens=self._max_completion_tokens(app_conf, prompt_limit),
            provider=provider,
            fallback_enabled=fallback_enabled,
        )
        intrigue_clean = apply_moderation(intrigue).strip()
        final_intrigue = self._prepare_intrigue_text(intrigue_clean, title_display)
        await self.bot.send_message(chat_id, final_intrigue)

        await asyncio.sleep(3)

        final_message = self._format_final_message(title_display, user_id, username)
        await self.bot.send_message(chat_id, final_message, parse_mode="HTML")

    def _prepare_intrigue_text(self, text: str, title_display: str) -> str:
        cleaned = " ".join((text or "").split())
        if not cleaned:
            return self._default_intrigue(title_display)
        sentences = re.split(r"(?<=[.!?…])\s+", cleaned)
        trimmed = " ".join(sentences[:2]).strip()
        if not trimmed:
            trimmed = cleaned
        if len(trimmed) > 240:
            trimmed = trimmed[:240].rsplit(" ", 1)[0].rstrip(",;:.!? ") + "…"
        if not trimmed:
            return self._default_intrigue(title_display)
        return trimmed

    def _default_intrigue(self, title_display: str) -> str:
        return f"🎰 Я кручу барабан за «{title_display}». Скоро назову победителя."

    async def _announce_without_llm(
        self,
        chat_id: int,
        user_id: int,
        username: str | None,
        title_display: str,
    ) -> None:
        headline = f"🎰 Я запускаю рулетку за «{title_display}». Держи кулачки."
        try:
            await self.bot.send_message(chat_id, headline)
        except Exception:
            logger.exception("Failed to send fallback roulette headline chat=%s", chat_id)
        await asyncio.sleep(3)
        final_message = self._format_final_message(title_display, user_id, username)
        try:
            await self.bot.send_message(chat_id, final_message, parse_mode="HTML")
        except Exception:
            logger.exception("Failed to send fallback roulette result chat=%s", chat_id)

    def _format_final_message(self, title_display: str, user_id: int, username: str | None) -> str:
        mention = f"<a href='tg://user?id={user_id}'>{escape_html(username) if username else 'победитель'}</a>"
        return f"🏆 Звание «{title_display}» достаётся {mention}!"

    def _prompt_token_limit(self, app_conf: dict[str, object]) -> int:
        raw_limit = app_conf.get("context_max_prompt_tokens", 32000)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 32000
        if limit <= 0:
            limit = 32000
        return max(2000, min(60000, limit))

    def _max_completion_tokens(
        self,
        app_conf: dict[str, object],
        prompt_limit: int | None = None,
    ) -> int:
        limit = prompt_limit or self._prompt_token_limit(app_conf)
        default_cap = 2048
        base_cap = max(limit // 4, 200)
        allowed = max(200, min(default_cap, base_cap))
        override_raw = app_conf.get("max_length")
        try:
            override = int(override_raw)
        except (TypeError, ValueError):
            override = None
        if override and override > 0:
            allowed = min(allowed, override)
        return allowed


    async def get_stats(self, chat_id: int) -> str:
        heading_title, monthly, overall = await self._prepare_stats(
            chat_id,
            include_monthly=True,
            include_total=True,
        )
        lines = self._build_stats_header(heading_title)
        lines.extend(self._format_stats("За месяц:", monthly))
        lines.extend(self._format_stats("За всё время:", overall))
        return "\n".join(lines)

    async def get_stats_monthly(self, chat_id: int) -> str:
        heading_title, monthly, _ = await self._prepare_stats(
            chat_id,
            include_monthly=True,
            include_total=False,
        )
        lines = self._build_stats_header(heading_title)
        lines.extend(self._format_stats("За месяц:", monthly))
        return "\n".join(lines)

    async def get_stats_total(self, chat_id: int) -> str:
        heading_title, _, overall = await self._prepare_stats(
            chat_id,
            include_monthly=False,
            include_total=True,
        )
        lines = self._build_stats_header(heading_title)
        lines.extend(self._format_stats("За всё время:", overall))
        return "\n".join(lines)

    async def _prepare_stats(
        self,
        chat_id: int,
        *,
        include_monthly: bool,
        include_total: bool,
    ) -> tuple[str, list[StatsEntry], list[StatsEntry]]:
        conf = await self.settings.get_all(chat_id)
        today = datetime.now(MoscowTZ)
        month_start = today.replace(day=1).date()

        async with self.sessionmaker() as session:
            monthly: list[StatsEntry]
            overall: list[StatsEntry]
            if include_monthly:
                monthly = await self._aggregate(session, chat_id, start=month_start)
            else:
                monthly = []
            if include_total:
                overall = await self._aggregate(session, chat_id)
            else:
                overall = []
            last_winner = (
                await session.execute(
                    select(RouletteWinner.title)
                    .where(RouletteWinner.chat_id == chat_id)
                    .order_by(desc(RouletteWinner.created_at))
                    .limit(1)
                )
            ).scalar_one_or_none()

        display_map = {code: name for code, name in TITLE_CHOICES}
        custom_title = conf.get("roulette_custom_title")
        display_map["custom"] = custom_title or "Прозвище"

        heading_title = last_winner or display_map.get("custom") or "«Пидор/Скуф/…»"
        return heading_title, monthly, overall

    def _build_stats_header(self, heading_title: str) -> list[str]:
        return ["🏅 Результаты рулетки", f"Текущее звание: {heading_title}"]

    async def _aggregate(
        self,
        session: AsyncSession,
        chat_id: int,
        *,
        start: date | None = None,
    ) -> list[StatsEntry]:
        count_col = func.count().label("cnt")
        stmt = (
            select(
                RouletteWinner.user_id,
                func.max(RouletteWinner.username).label("username"),
                count_col,
            )
            .where(RouletteWinner.chat_id == chat_id)
        )
        if start:
            stmt = stmt.where(RouletteWinner.won_at >= start)
        stmt = (
            stmt.group_by(RouletteWinner.user_id)
            .order_by(count_col.desc(), RouletteWinner.user_id)
        )

        rows = await session.execute(stmt)
        return [StatsEntry(user_id=row.user_id, username=row.username, wins=row.cnt) for row in rows]

    def _format_stats(
        self,
        header: str,
        stats: list[StatsEntry],
    ) -> list[str]:
        lines = ["", header]
        if not stats:
            lines.append("— пока пусто")
            return lines
        for entry in stats:
            mention = entry.username if entry.username else f"ID {entry.user_id}"
            lines.append(f"• {mention} — {entry.wins}")
        return lines

    async def reset_daily_winner(self, chat_id: int) -> None:
        today = self._today()
        async with self.sessionmaker() as session:
            await session.execute(
                RouletteWinner.__table__.delete().where(
                    RouletteWinner.chat_id == chat_id,
                    RouletteWinner.won_at == today,
                )
            )
            await session.commit()

    async def run_auto_roll(self) -> None:
        today = self._today()
        async with self.sessionmaker() as session:
            stmt = select(Chat.id).where(Chat.is_active.is_(True))
            chats = [row[0] for row in (await session.execute(stmt)).fetchall()]
        for chat_id in chats:
            conf = await self.settings.get_all(chat_id)
            if not conf.get("roulette_auto_enabled"):
                continue
            async with self.sessionmaker() as session:
                if await self._has_winner_today(session, chat_id):
                    continue
                participants = await self._fetch_participants(session, chat_id)
                if not participants:
                    continue
            result = await self.roll(chat_id, initiator="auto", force=False)
            if not result.success:
                logger.info("Auto-roll skipped chat=%s reason=%s", chat_id, result.message)


def escape_html(text: str | None) -> str:
    if not text:
        return "Игрок"
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
