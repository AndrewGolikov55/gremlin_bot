from __future__ import annotations

import asyncio
import logging
import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import Chat, ChatMemory, MonthlyChampion, RouletteWinner
from .app_config import AppConfigService
from .llm.client import LLMError, LLMRateLimitError, resolve_llm_options
from .llm.client import generate as llm_generate
from .roulette import RouletteService, StatsEntry
from .settings import SettingsService

logger = logging.getLogger(__name__)

MoscowTZ = ZoneInfo("Europe/Moscow")
CATCH_UP_DAY_LIMIT = 7
PER_CHAT_SLEEP_SEC = 0.5
DRAMA_PAUSE_SEC = 2
LLM_MAX_TOKENS = 250


def _previous_period(now: datetime) -> tuple[date, date]:
    """Returns (period_start, period_end_excl) for the calendar month BEFORE `now`'s month.

    `now` must be timezone-aware. The period is computed in `now.tzinfo`'s calendar.
    Example: now=2026-05-01 → (2026-04-01, 2026-05-01).
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current_month_first = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).date()
    last_day_of_prev = current_month_first - timedelta(days=1)
    period_start = last_day_of_prev.replace(day=1)
    return period_start, current_month_first


class MonthlyChampionService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        bot: Bot,
        roulette: RouletteService,
        settings: SettingsService,
        app_config: AppConfigService,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.bot = bot
        self.roulette = roulette
        self.settings = settings
        self.app_config = app_config
        self._chat_locks: dict[int, asyncio.Lock] = {}

    def _get_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    async def _resolve_display_name(self, *, chat_id: int, user_id: int) -> str:
        """Resolve user's display name with fallback chain:
        1. bot.get_chat_member → first_name or username (if active member)
        2. last RouletteWinner.username for this user_id in this chat
        3. f"id{user_id}"
        """
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
                    name = user.first_name or user.username
                    if name:
                        return str(name)
        except TelegramBadRequest:
            pass
        except Exception:
            logger.exception("get_chat_member failed for chat=%s user=%s", chat_id, user_id)

        async with self.sessionmaker() as session:
            stmt = (
                select(RouletteWinner.username)
                .where(RouletteWinner.chat_id == chat_id, RouletteWinner.user_id == user_id)
                .order_by(RouletteWinner.created_at.desc())
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row:
                return str(row)

        return f"id{user_id}"

    @staticmethod
    def _format_top_lines(top: list[StatsEntry]) -> str:
        lines: list[str] = []
        for i, entry in enumerate(top[:5], start=1):
            name = entry.username or f"id{entry.user_id}"
            lines.append(f"{i}. {name} — {entry.wins} побед")
        return "\n".join(lines)

    @staticmethod
    def _fallback_winner_text(daily_title: str, champion_name: str) -> str:
        return f"🏆 Король «{daily_title}» месяца — {champion_name}."

    async def _llm_call(self, user_prompt: str, *, system: str = "") -> str | None:
        conf = await self.app_config.get_all()
        provider = resolve_llm_options(conf)
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_prompt})
        try:
            return await llm_generate(
                messages,
                temperature=0.8,
                max_tokens=LLM_MAX_TOKENS,
                provider=provider,
            )
        except (LLMError, LLMRateLimitError):
            logger.exception("monthly_champion: LLM provider failed")
            return None
        except Exception:
            logger.exception("monthly_champion: unexpected LLM error")
            return None

    async def _render_winner(
        self,
        *,
        top: list[StatsEntry],
        champion_name: str,
        daily_title: str,
        month_label: str,
    ) -> str:
        top_block = self._format_top_lines(top)
        prompt = (
            f"Подведи итог месяца по рулетке. Звание дня в чате: «{daily_title}».\n"
            f"\n"
            f"Топ за {month_label}:\n"
            f"{top_block}\n"
            f"\n"
            f"Победитель месяца: {champion_name}.\n"
            f"\n"
            f"Сформулируй короткое (3-5 строк) поздравительное оглашение.\n"
            f"Обязательно используй титул в форме «Король X месяца», "
            f"где X — слово из «{daily_title}» во множественном родительном падеже "
            f"(пример: «Мудак дня» → «Король Мудаков месяца»).\n"
            f"Не сухой список — это шоу. В стиле своей персоны.\n"
            f"Не упоминай других участников по именам, только победителя.\n"
            f"Plain text, без HTML и Markdown."
        )
        text = await self._llm_call(prompt)
        if not text:
            return self._fallback_winner_text(daily_title, champion_name)
        return text.strip()

    async def _render_runoff_winner(
        self,
        *,
        tied_names: list[str],
        winner_name: str,
        daily_title: str,
    ) -> str:
        tied_str = ", ".join(tied_names)
        prompt = (
            f"Только что был runoff между {tied_str}, выпал {winner_name}.\n"
            f"Объяви его «Королём X месяца» (X из «{daily_title}», "
            f"во мн. родительном падеже).\n"
            f"3-5 строк, в стиле своей персоны. Plain text."
        )
        text = await self._llm_call(prompt)
        if not text:
            return self._fallback_winner_text(daily_title, winner_name)
        return text.strip()

    async def _render_empty(self, *, daily_title: str, month_label: str) -> str:
        prompt = (
            f"В этом месяце ({month_label}) в чате не было ни одного розыгрыша рулетки "
            f"на звание «{daily_title}».\n"
            f"Прокомментируй иронично, 2-3 строки.\n"
            f"Не объявляй никого королём — короля нет. Plain text."
        )
        text = await self._llm_call(prompt)
        if not text:
            return f"🏅 В этом месяце короля «{daily_title}» не нашлось — никто не рискнул."
        return text.strip()

    @staticmethod
    def _month_label_ru(period_start: date) -> str:
        months = [
            "январь", "февраль", "март", "апрель", "май", "июнь",
            "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
        ]
        return f"{months[period_start.month - 1]} {period_start.year}"

    async def _is_chat_targetable(self, chat_id: int) -> bool:
        async with self.sessionmaker() as session:
            chat = await session.get(Chat, chat_id)
            if chat is None or not chat.is_active:
                return False
        conf = await self.settings.get_all(chat_id)
        return bool(conf.get("is_active", True))

    async def _already_announced(self, *, chat_id: int, period_start: date) -> bool:
        async with self.sessionmaker() as session:
            stmt = select(MonthlyChampion.id).where(
                MonthlyChampion.chat_id == chat_id,
                MonthlyChampion.period_start == period_start,
            )
            return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def _resolve_daily_title(self, chat_id: int) -> str:
        conf = await self.settings.get_all(chat_id)
        custom = str(conf.get("roulette_custom_title") or "").strip()
        return custom or "Мудак дня"

    async def _persist_announcement(
        self,
        *,
        chat_id: int,
        period_start: date,
        champion: StatsEntry | None,
        display_name: str | None,
        tied_with: list[int],
        daily_title: str,
    ) -> None:
        async with self.sessionmaker() as session:
            row = MonthlyChampion(
                chat_id=chat_id,
                period_start=period_start,
                user_id=champion.user_id if champion else None,
                display_name=display_name,
                score=champion.wins if champion else 0,
                tied_with=tied_with,
                daily_title_snapshot=daily_title,
                announced_at=datetime.utcnow(),
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                logger.info(
                    "monthly_champion: integrity race for chat=%s period=%s",
                    chat_id, period_start,
                )
                return

            if champion is not None:
                mem = await session.get(ChatMemory, chat_id)
                payload = {
                    "user_id": champion.user_id,
                    "display_name": display_name,
                    "title": daily_title,
                    "period_start": period_start.isoformat(),
                }
                if mem is None:
                    mem = ChatMemory(
                        chat_id=chat_id,
                        members=[],
                        lore=[],
                        monthly_champion=payload,
                    )
                    session.add(mem)
                else:
                    mem.monthly_champion = payload
                await session.commit()

    async def _send_text(self, chat_id: int, text: str) -> None:
        await self.bot.send_message(chat_id=chat_id, text=text)

    async def process_chat(
        self,
        *,
        chat_id: int,
        period_start: date,
        period_end_excl: date,
    ) -> None:
        async with self._get_lock(chat_id):
            if not await self._is_chat_targetable(chat_id):
                return
            if await self._already_announced(chat_id=chat_id, period_start=period_start):
                return

            daily_title = await self._resolve_daily_title(chat_id)
            month_label = self._month_label_ru(period_start)

            async with self.sessionmaker() as session:
                top = await self.roulette._aggregate(
                    session,
                    chat_id,
                    start=period_start,
                    end=period_end_excl,
                )

            try:
                if not top:
                    text = await self._render_empty(
                        daily_title=daily_title, month_label=month_label
                    )
                    await self._send_text(chat_id, text)
                    await self._persist_announcement(
                        chat_id=chat_id,
                        period_start=period_start,
                        champion=None,
                        display_name=None,
                        tied_with=[],
                        daily_title=daily_title,
                    )
                    return

                tied = [e for e in top if e.wins == top[0].wins]
                tied_with: list[int] = []
                if len(tied) == 1:
                    champion = tied[0]
                else:
                    tied_names: list[str] = []
                    for e in tied:
                        tied_names.append(
                            await self._resolve_display_name(chat_id=chat_id, user_id=e.user_id)
                        )
                    await self._send_text(
                        chat_id,
                        "🏆 Итоги месяца. Ничья на вершине: "
                        f"{', '.join(tied_names)} — по {tied[0].wins} очков.\n"
                        "Решает рандом.",
                    )
                    await asyncio.sleep(DRAMA_PAUSE_SEC)
                    await self._send_text(chat_id, "🎲 Бросаем кости...")
                    await asyncio.sleep(DRAMA_PAUSE_SEC)
                    champion = random.choice(tied)
                    tied_with = [e.user_id for e in tied]

                champion_name = await self._resolve_display_name(
                    chat_id=chat_id, user_id=champion.user_id
                )

                if tied_with:
                    tied_names_for_prompt: list[str] = []
                    for e in tied:
                        tied_names_for_prompt.append(
                            await self._resolve_display_name(chat_id=chat_id, user_id=e.user_id)
                        )
                    text = await self._render_runoff_winner(
                        tied_names=tied_names_for_prompt,
                        winner_name=champion_name,
                        daily_title=daily_title,
                    )
                else:
                    text = await self._render_winner(
                        top=top,
                        champion_name=champion_name,
                        daily_title=daily_title,
                        month_label=month_label,
                    )

                await self._send_text(chat_id, text)
                await self._persist_announcement(
                    chat_id=chat_id,
                    period_start=period_start,
                    champion=champion,
                    display_name=champion_name,
                    tied_with=tied_with,
                    daily_title=daily_title,
                )
            except (TelegramForbiddenError, TelegramBadRequest) as exc:
                logger.warning(
                    "monthly_champion: telegram error for chat=%s: %s", chat_id, exc
                )
            except TelegramAPIError:
                logger.exception(
                    "monthly_champion: TelegramAPIError for chat=%s", chat_id
                )
