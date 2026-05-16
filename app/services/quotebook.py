"""Weekly «Афоризм недели» service.

Runs every Sunday 20:00 MSK (APScheduler cron). For each active chat:
  1. close previous open round (stop_poll, count votes, pick winner with
     optional drama runoff, +1 RouletteScoreAdjustment, announce)
  2. open new round (collect candidates, optional LLM selection, send_poll,
     persist QuoteWeekRound row)

Single-process deployment is assumed. Per-chat asyncio.Lock guards races
between cron tick, manual triggers, and startup catch-up inside one process.
The UNIQUE(chat_id, week_start) constraint is a last-line defence — if two
processes ever raced, only one row would persist, but both Telegram polls
would already have been sent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import Chat, Message, QuoteWeekRound, RouletteScoreAdjustment
from .app_config import AppConfigService
from .llm.client import LLMError, LLMRateLimitError, resolve_llm_options
from .llm.client import generate as llm_generate
from .settings import SettingsService

logger = logging.getLogger("bot.quotebook")

MoscowTZ = ZoneInfo("Europe/Moscow")
PER_CHAT_SLEEP_SEC = 0.5
DRAMA_PAUSE_SEC = 2
LLM_MAX_TOKENS = 100
LLM_TEMPERATURE = 0.6
TG_POLL_OPTION_LIMIT = 100
TG_POLL_QUESTION_LIMIT = 290
WINDOW_DAYS = 7
MIN_CANDIDATES = 3
MAX_POLL_OPTIONS = 6
LLM_INPUT_TOP_N = 50
CATCH_UP_STALE_HOURS = 24
MSG_MIN_LEN = 20
MSG_MAX_LEN = 300


def _week_start_for(now: datetime) -> date:
    """Return Monday of the most-recently-completed calendar week.

    `now` must be timezone-aware. Examples (Europe/Moscow):
        Sun 2026-05-17 20:00 → 2026-05-11 (week 11..17 just completed at cron-time)
        Mon 2026-05-18 09:00 → 2026-05-11 (last full week 11..17)
        Sun 2026-05-17 09:00 → 2026-05-04 (current week 11..17 not finished yet)

    Logic: a week is considered «completed» once Sunday 20:00 (cron-time) has
    passed. Before that boundary, the previous Monday-of-current-week minus 7
    days is the answer; after that, the current week's Monday.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(now.tzinfo)
    # Find the most recent Sunday 20:00 boundary that is <= now.
    # weekday(): Mon=0..Sun=6. Days since most recent Sunday (today if Sun else past).
    days_since_sun = (local.weekday() - 6) % 7
    last_sun_date = local.date() - timedelta(days=days_since_sun)
    # If today is Sunday but before 20:00, last boundary is the previous Sunday.
    if days_since_sun == 0 and local.hour < 20:
        last_sun_date = last_sun_date - timedelta(days=7)
    # week_start = Monday of the week that just ended on last_sun_date.
    return last_sun_date - timedelta(days=6)


@dataclass(frozen=True)
class Candidate:
    message_id: int
    user_id: int
    text: str
    reply_count: int
    date: datetime


@dataclass(frozen=True)
class PollOption:
    text: str
    author_user_id: int
    source_message_id: int


def score_candidate(c: Candidate, *, max_reply: int, now: datetime) -> float:
    """Heuristic score in [0.0, 1.0] used for top-50 cut and LLM-fallback ranking.

    Weights: 0.6 reply, 0.3 length (capped at 200), 0.1 recency (linear decay over 7d).
    """
    reply_norm = 0.0
    if max_reply > 0:
        reply_norm = min(c.reply_count, max_reply) / max_reply
    length_norm = min(len(c.text), 200) / 200.0
    age_seconds = (now - c.date).total_seconds()
    window_seconds = WINDOW_DAYS * 24 * 3600
    recency_norm = max(0.0, 1.0 - (age_seconds / window_seconds))
    return 0.6 * reply_norm + 0.3 * length_norm + 0.1 * recency_norm


class QuotebookService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        bot: Bot,
        settings: SettingsService,
        app_config: AppConfigService,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.bot = bot
        self.settings = settings
        self.app_config = app_config
        self._chat_locks: dict[int, asyncio.Lock] = {}

    def _get_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    async def _get_bot_username(self) -> str | None:
        try:
            me = await self.bot.get_me()
        except Exception:  # noqa: BLE001 — display-side fallback; never raises out
            logger.exception("quotebook: bot.get_me failed")
            return None
        return getattr(me, "username", None)

    async def collect_candidates(
        self,
        *,
        chat_id: int,
        now: datetime,
    ) -> list[Candidate]:
        """Return Message-derived candidates from the last 7 days.

        Filters: is_bot=False, len(text) in [20, 300], text does not start with
        '/' nor with '@<bot_username>'. `now` is naive (treated as UTC) and the
        7-day window is `[now - 7d, now)` to match how Message.date is stored.
        """
        window_start = now - timedelta(days=WINDOW_DAYS)
        bot_username = await self._get_bot_username()
        bot_mention_prefix = f"@{bot_username.lower()}" if bot_username else None

        async with self.sessionmaker() as session:
            stmt = (
                select(Message)
                .where(
                    Message.chat_id == chat_id,
                    Message.is_bot.is_(False),
                    Message.date >= window_start,
                    Message.date < now,
                )
                .order_by(Message.date.asc())
            )
            rows = (await session.execute(stmt)).scalars().all()

            # reply_count: count other in-window messages whose reply_to_id matches
            # each candidate's message_id. Done in-memory over the same window
            # to keep the implementation simple and dialect-agnostic.
            reply_counts: dict[int, int] = {}
            for m in rows:
                if m.reply_to_id is not None:
                    reply_counts[m.reply_to_id] = reply_counts.get(m.reply_to_id, 0) + 1

            candidates: list[Candidate] = []
            for m in rows:
                text = (m.text or "").strip()
                if len(text) < MSG_MIN_LEN or len(text) > MSG_MAX_LEN:
                    continue
                if text.startswith("/"):
                    continue
                if bot_mention_prefix and text.lower().startswith(bot_mention_prefix):
                    continue
                candidates.append(Candidate(
                    message_id=m.message_id,
                    user_id=m.user_id,
                    text=text,
                    reply_count=reply_counts.get(m.message_id, 0),
                    date=m.date,
                ))
            return candidates

    @staticmethod
    def _heuristic_top(
        candidates: list[Candidate], *, now: datetime, limit: int
    ) -> list[Candidate]:
        if not candidates:
            return []
        max_reply = max((c.reply_count for c in candidates), default=0)
        ranked = sorted(
            candidates,
            key=lambda c: score_candidate(c, max_reply=max_reply, now=now),
            reverse=True,
        )
        return ranked[:limit]

    @staticmethod
    def _to_poll_option(c: Candidate) -> PollOption:
        return PollOption(
            text=c.text,
            author_user_id=c.user_id,
            source_message_id=c.message_id,
        )

    async def _llm_select_indices(
        self,
        ranked_for_llm: list[Candidate],
    ) -> list[int] | None:
        """Ask LLM for up to 6 «memey» 1-based indices into `ranked_for_llm`.

        Returns parsed list of indices (in range [1, len(ranked_for_llm)],
        deduplicated, preserving LLM order, truncated to MAX_POLL_OPTIONS) or
        None on any failure (LLM error, unparseable JSON, empty result).
        """
        lines: list[str] = []
        for idx, c in enumerate(ranked_for_llm, start=1):
            lines.append(f"[{idx}] uid{c.user_id}: {c.text}")
        listing = "\n".join(lines)
        user_prompt = (
            f"Выбери из списка ниже до {MAX_POLL_OPTIONS} цитат, которые звучат "
            f"как мем чата: острые, абсурдные, неожиданные, цепляющие. "
            f"Скучных «топ-цитат» не бывает — лучше меньше, но в точку.\n\n"
            f"Кандидаты (формат: [N] uid<author>: текст):\n{listing}\n\n"
            f"Верни JSON-массив индексов выбранных цитат, отсортированный по "
            f"убыванию «мемности». Например: [4, 1, 7, 2]. Без пояснений, без markdown."
        )

        conf = await self.app_config.get_all()
        provider = resolve_llm_options(conf)

        try:
            raw = await llm_generate(
                [{"role": "user", "content": user_prompt}],
                temperature=LLM_TEMPERATURE,
                max_tokens=LLM_MAX_TOKENS,
                provider=provider,
            )
        except (LLMError, LLMRateLimitError):
            logger.exception("quotebook: LLM provider failed")
            return None
        except Exception:
            logger.exception("quotebook: unexpected LLM error")
            return None

        if not raw:
            return None
        match = re.search(r"\[[^\[\]]*\]", raw)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except (ValueError, TypeError):
            return None
        if not isinstance(parsed, list):
            return None

        max_idx = len(ranked_for_llm)
        result: list[int] = []
        seen: set[int] = set()
        for x in parsed:
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if 1 <= i <= max_idx and i not in seen:
                seen.add(i)
                result.append(i)
            if len(result) >= MAX_POLL_OPTIONS:
                break
        return result or None

    async def select_options(
        self,
        candidates: list[Candidate],
        *,
        now: datetime,
        chat_id: int | None = None,
    ) -> list[PollOption]:
        """Threshold ladder.

        - len < 3 → []  (caller skips the week)
        - 3 <= len <= 6 → all candidates, ranked by heuristic score (no LLM)
        - len > 6 → top-50 by score → LLM selects ≤6, fallback to heuristic top-6
        """
        if len(candidates) < MIN_CANDIDATES:
            return []
        if len(candidates) <= MAX_POLL_OPTIONS:
            ranked = self._heuristic_top(candidates, now=now, limit=MAX_POLL_OPTIONS)
            return [self._to_poll_option(c) for c in ranked]

        # > 6 candidates: top-50 by heuristic → LLM → ≤6, fallback to heuristic top-6
        ranked_for_llm = self._heuristic_top(candidates, now=now, limit=LLM_INPUT_TOP_N)
        indices = await self._llm_select_indices(ranked_for_llm)
        if indices:
            picked = [ranked_for_llm[i - 1] for i in indices]
            return [self._to_poll_option(c) for c in picked]
        # Fallback: heuristic top-6
        fallback = self._heuristic_top(candidates, now=now, limit=MAX_POLL_OPTIONS)
        return [self._to_poll_option(c) for c in fallback]

    @staticmethod
    def _truncate_option(text: str) -> str:
        if len(text) <= TG_POLL_OPTION_LIMIT:
            return text
        return text[: TG_POLL_OPTION_LIMIT - 1].rstrip() + "…"

    @staticmethod
    def _week_start_from_naive(now: datetime) -> date:
        """Treat naive `now` as Moscow-local and compute the closed week's Monday."""
        if now.tzinfo is None:
            aware = now.replace(tzinfo=MoscowTZ)
        else:
            aware = now
        return _week_start_for(aware)

    async def open_new_round(self, *, chat_id: int, now: datetime) -> bool:
        """Open a fresh poll for `chat_id` covering the last 7 days.

        Returns True iff a poll was sent and persisted. False on skip
        (insufficient candidates, TG forbidden, UNIQUE race).
        """
        candidates = await self.collect_candidates(chat_id=chat_id, now=now)
        if len(candidates) < MIN_CANDIDATES:
            logger.info(
                "quotebook.skip chat=%s reason=insufficient_candidates count=%d",
                chat_id, len(candidates),
            )
            return False

        options = await self.select_options(candidates, now=now, chat_id=chat_id)
        if not options:
            logger.info(
                "quotebook.skip chat=%s reason=no_options_after_selection", chat_id
            )
            return False

        question = "Афоризм недели?"
        poll_option_texts = [self._truncate_option(opt.text) for opt in options]

        try:
            poll_msg = await self.bot.send_poll(
                chat_id=chat_id,
                question=question,
                options=poll_option_texts,
                type="regular",
                is_anonymous=False,
                allows_multiple_answers=False,
            )
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            logger.warning(
                "quotebook.send_poll permission failed chat=%s: %s", chat_id, exc
            )
            return False
        except TelegramAPIError:
            logger.exception("quotebook.send_poll TG API error chat=%s", chat_id)
            return False

        if poll_msg.poll is None:
            logger.warning("quotebook.send_poll returned no poll for chat=%s", chat_id)
            return False

        week_start = self._week_start_from_naive(now)
        async with self.sessionmaker() as session:
            row = QuoteWeekRound(
                chat_id=chat_id,
                week_start=week_start,
                poll_id=poll_msg.poll.id,
                poll_message_id=poll_msg.message_id,
                options=[
                    {
                        "text": opt.text,
                        "author_user_id": opt.author_user_id,
                        "source_message_id": opt.source_message_id,
                    }
                    for opt in options
                ],
                opened_at=datetime.utcnow(),
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                logger.info(
                    "quotebook: integrity race for chat=%s week=%s; rolling back poll",
                    chat_id, week_start,
                )
                # Try to remove the duplicate Telegram poll; ignore failures.
                try:
                    await self.bot.stop_poll(
                        chat_id=chat_id, message_id=poll_msg.message_id
                    )
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    logger.exception(
                        "quotebook: failed to stop_poll after race chat=%s", chat_id
                    )
                return False

        logger.info(
            "quotebook.round.opened chat=%s poll=%s week_start=%s n_options=%d",
            chat_id, poll_msg.poll.id, week_start, len(options),
        )
        return True

    async def _resolve_author_name(self, *, chat_id: int, user_id: int) -> str:
        """Best-effort display name via bot.get_chat_member; falls back to f'id{user_id}'."""
        try:
            member = await self.bot.get_chat_member(chat_id, user_id)
        except TelegramBadRequest:
            return f"id{user_id}"
        except Exception:  # noqa: BLE001 — never bubble out of cosmetics
            logger.exception(
                "quotebook.get_chat_member failed chat=%s user=%s", chat_id, user_id
            )
            return f"id{user_id}"
        user = getattr(member, "user", None)
        if user is None:
            return f"id{user_id}"
        return str(user.first_name or user.username or f"id{user_id}")

    async def _llm_render_winner(
        self, *, full_text: str, author_name: str
    ) -> str:
        prompt = (
            f"Афоризм недели в нашем чате выбран голосованием.\n"
            f"Цитата: «{full_text}»\n"
            f"Автор: {author_name}\n"
            f"Объяви результат 3-4 строки в своей персоне.\n"
            f"Plain text, без HTML и Markdown."
        )
        conf = await self.app_config.get_all()
        provider = resolve_llm_options(conf)
        try:
            raw = await llm_generate(
                [{"role": "user", "content": prompt}],
                temperature=0.8,
                max_tokens=250,
                provider=provider,
            )
        except (LLMError, LLMRateLimitError):
            logger.exception("quotebook: LLM render-winner failed")
            return f"📜 Афоризм недели: «{full_text}» — {author_name}."
        except Exception:
            logger.exception("quotebook: unexpected LLM error in render-winner")
            return f"📜 Афоризм недели: «{full_text}» — {author_name}."
        if not raw or not raw.strip():
            return f"📜 Афоризм недели: «{full_text}» — {author_name}."
        return raw.strip()

    async def _find_open_round(
        self, *, chat_id: int, now: datetime
    ) -> QuoteWeekRound | None:
        cutoff = now - timedelta(days=8)
        async with self.sessionmaker() as session:
            stmt = (
                select(QuoteWeekRound)
                .where(
                    QuoteWeekRound.chat_id == chat_id,
                    QuoteWeekRound.closed_at.is_(None),
                    QuoteWeekRound.opened_at >= cutoff,
                )
                .order_by(QuoteWeekRound.opened_at.desc())
                .limit(1)
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    async def close_previous_round_if_any(
        self, *, chat_id: int, now: datetime
    ) -> None:
        round_ = await self._find_open_round(chat_id=chat_id, now=now)
        if round_ is None:
            return

        try:
            poll = await self.bot.stop_poll(
                chat_id=chat_id, message_id=round_.poll_message_id
            )
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            logger.warning(
                "quotebook.stop_poll failed chat=%s poll=%s: %s",
                chat_id, round_.poll_id, exc,
            )
            # Mark the round as closed with NULL winner so we don't retry forever.
            async with self.sessionmaker() as session:
                db_row = await session.get(QuoteWeekRound, round_.id)
                if db_row is not None:
                    db_row.closed_at = datetime.utcnow()
                    await session.commit()
            return
        except TelegramAPIError:
            logger.exception("quotebook.stop_poll TG API error chat=%s", chat_id)
            return

        voter_counts = [int(o.voter_count) for o in poll.options]
        total = int(getattr(poll, "total_voter_count", sum(voter_counts)))

        if total == 0:
            await self._finalize_no_winner(
                round_=round_, voter_counts=voter_counts, chat_id=chat_id
            )
            return

        max_count = max(voter_counts)
        winners_idx = [i for i, c in enumerate(voter_counts) if c == max_count]
        if len(winners_idx) == 1:
            winner_idx = winners_idx[0]
            await self._finalize_with_winner(
                round_=round_,
                voter_counts=voter_counts,
                winner_idx=winner_idx,
                chat_id=chat_id,
            )
            return

        # Tie — drama runoff: 3 drama messages + final announcement via finalize.
        tied_names: list[str] = []
        for i in winners_idx:
            uid = int(round_.options[i]["author_user_id"])
            tied_names.append(
                await self._resolve_author_name(chat_id=chat_id, user_id=uid)
            )
        await self._safe_send(
            chat_id,
            f"Ничья на вершине: {', '.join(tied_names)}. Бросаю кости...",
        )
        await asyncio.sleep(DRAMA_PAUSE_SEC)
        await self._safe_send(chat_id, "🎲🎲🎲")
        await asyncio.sleep(DRAMA_PAUSE_SEC)
        winner_idx = random.choice(winners_idx)
        await self._safe_send(chat_id, "Барабанная дробь... и победитель —")
        await asyncio.sleep(DRAMA_PAUSE_SEC)
        await self._finalize_with_winner(
            round_=round_,
            voter_counts=voter_counts,
            winner_idx=winner_idx,
            chat_id=chat_id,
        )

    async def _safe_send(self, chat_id: int, text: str) -> None:
        try:
            await self.bot.send_message(chat_id=chat_id, text=text)
        except TelegramAPIError:
            logger.exception("quotebook._safe_send failed chat=%s", chat_id)

    async def _finalize_no_winner(
        self,
        *,
        round_: QuoteWeekRound,
        voter_counts: list[int],
        chat_id: int,
    ) -> None:
        msg = "📜 Афоризм недели не выбран — никто не пришёл голосовать."
        try:
            await self.bot.send_message(chat_id=chat_id, text=msg)
        except TelegramAPIError:
            logger.exception("quotebook.no_winner send_message failed chat=%s", chat_id)
        async with self.sessionmaker() as session:
            db_row = await session.get(QuoteWeekRound, round_.id)
            if db_row is None:
                return
            db_row.closed_at = datetime.utcnow()
            db_row.final_counts = list(voter_counts)
            await session.commit()

    async def _finalize_with_winner(
        self,
        *,
        round_: QuoteWeekRound,
        voter_counts: list[int],
        winner_idx: int,
        chat_id: int,
    ) -> None:
        option = round_.options[winner_idx]
        winner_user_id = int(option["author_user_id"])
        full_text = str(option["text"])

        # Persist winner + RouletteScoreAdjustment in the same transaction.
        async with self.sessionmaker() as session:
            db_row = await session.get(QuoteWeekRound, round_.id)
            if db_row is None:
                logger.warning(
                    "quotebook: round %s disappeared before finalize", round_.id
                )
                return
            db_row.closed_at = datetime.utcnow()
            db_row.winner_user_id = winner_user_id
            db_row.winner_option_idx = winner_idx
            db_row.final_counts = list(voter_counts)
            session.add(RouletteScoreAdjustment(
                chat_id=chat_id,
                user_id=winner_user_id,
                delta=+1,
                reason="quote_week_winner",
                source_id=db_row.id,
            ))
            await session.commit()

        author_name = await self._resolve_author_name(
            chat_id=chat_id, user_id=winner_user_id
        )
        text = await self._llm_render_winner(
            full_text=full_text, author_name=author_name
        )
        try:
            await self.bot.send_message(chat_id=chat_id, text=text)
        except TelegramAPIError:
            logger.exception("quotebook.winner send_message failed chat=%s", chat_id)
