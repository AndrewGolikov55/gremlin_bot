from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...models import SpyPlayer, SpyRound
from ...utils.locks import get_chat_lock
from .common import RoundStatus
from .spy_locations import SPY_LOCATIONS

logger = logging.getLogger(__name__)

DISCUSSION = timedelta(minutes=5)
VOTE_OPEN_PERIOD = 60  # seconds
MIN_PLAYERS = 3
MAX_PLAYERS = 9  # Telegram poll caps at 10 options; we reserve 1 for "никто"

# Reaper thresholds: anything in an open status older than these is abandoned.
LOBBY_MAX_AGE = timedelta(hours=2)
ACTIVE_MAX_AGE = timedelta(hours=1)


@dataclass
class _PlayerInfo:
    user_id: int
    display: str
    is_spy: bool


class SpyService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        bot: Bot,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, chat_id: int) -> asyncio.Lock:
        return get_chat_lock(chat_id, self._locks)

    async def _resolve_display(self, *, chat_id: int, user_id: int) -> str:
        try:
            member = await self.bot.get_chat_member(chat_id, user_id)
            user = getattr(member, "user", None)
            if user is not None:
                return str(user.first_name or user.username or f"id{user_id}")
        except TelegramBadRequest:
            pass
        except Exception:
            logger.exception("spy: get_chat_member failed chat=%s user=%s", chat_id, user_id)
        return f"id{user_id}"

    async def _get_open_round(self, *, chat_id: int) -> SpyRound | None:
        async with self.sessionmaker() as session:
            stmt = (
                select(SpyRound)
                .where(
                    SpyRound.chat_id == chat_id,
                    SpyRound.status.in_([
                        RoundStatus.LOBBY.value,
                        RoundStatus.ACTIVE.value,
                        RoundStatus.VOTING.value,
                    ]),
                )
                .limit(1)
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    async def start_lobby(self, *, chat_id: int, initiator_id: int) -> None:
        async with self._lock(chat_id):
            existing = await self._get_open_round(chat_id=chat_id)
            if existing is not None:
                await self.bot.send_message(
                    chat_id, "🎯 Игра в шпиона уже запущена. /spy_abort чтобы отменить."
                )
                return
            try:
                async with self.sessionmaker() as session:
                    async with session.begin():
                        round_ = SpyRound(
                            chat_id=chat_id,
                            initiator_user_id=initiator_id,
                            location=random.choice(SPY_LOCATIONS),
                            status=RoundStatus.LOBBY.value,
                        )
                        session.add(round_)
                        await session.flush()
                        session.add(SpyPlayer(round_id=round_.id, user_id=initiator_id, is_spy=False))
            except IntegrityError:
                await self.bot.send_message(
                    chat_id, "🎯 Игра в шпиона уже запущена. /spy_abort чтобы отменить."
                )
                return
        await self.bot.send_message(
            chat_id,
            "🎯 Игра «Шпион» — лобби открыто!\n"
            f"Чтобы присоединиться — /spy_join. Игроков нужно от {MIN_PLAYERS} до {MAX_PLAYERS}.\n"
            "Инициатор: /spy_start чтобы начать, /spy_abort чтобы отменить.",
        )

    async def join(self, *, chat_id: int, user_id: int) -> None:
        async with self._lock(chat_id):
            async with self.sessionmaker() as session:
                round_ = await self._fetch_open(session, chat_id)
                if round_ is None:
                    await self.bot.send_message(chat_id, "Лобби не открыто. /spy чтобы запустить.")
                    return
                if round_.status != RoundStatus.LOBBY.value:
                    await self.bot.send_message(chat_id, "Игра уже началась, ждите следующего раунда.")
                    return
                existing = await session.get(SpyPlayer, (round_.id, user_id))
                if existing is not None:
                    await self.bot.send_message(chat_id, "Ты уже в игре.")
                    return
                players_count = await self._count_players(session, round_.id)
                if players_count >= MAX_PLAYERS:
                    await self.bot.send_message(chat_id, f"Слотов нет, максимум {MAX_PLAYERS}.")
                    return
                session.add(SpyPlayer(round_id=round_.id, user_id=user_id, is_spy=False))
                await session.commit()
        display = await self._resolve_display(chat_id=chat_id, user_id=user_id)
        await self.bot.send_message(chat_id, f"➕ {display} в игре.")

    async def start_round(self, *, chat_id: int, initiator_id: int) -> None:
        async with self._lock(chat_id):
            async with self.sessionmaker() as session:
                round_ = await self._fetch_open(session, chat_id)
                if round_ is None or round_.status != RoundStatus.LOBBY.value:
                    await self.bot.send_message(chat_id, "Нечего стартовать — лобби не открыто.")
                    return
                if round_.initiator_user_id != initiator_id:
                    await self.bot.send_message(chat_id, "Запустить может только инициатор.")
                    return
                player_ids = await self._player_ids(session, round_.id)
                if len(player_ids) < MIN_PLAYERS:
                    await self.bot.send_message(
                        chat_id, f"Нужно минимум {MIN_PLAYERS} игроков, сейчас {len(player_ids)}.",
                    )
                    return
                spy_uid = random.choice(player_ids)
                now = datetime.utcnow()
                await session.execute(
                    update(SpyRound)
                    .where(SpyRound.id == round_.id)
                    .values(
                        status=RoundStatus.ACTIVE.value,
                        spy_user_id=spy_uid,
                        ends_at=now + DISCUSSION,
                    )
                )
                await session.execute(
                    update(SpyPlayer)
                    .where(SpyPlayer.round_id == round_.id, SpyPlayer.user_id == spy_uid)
                    .values(is_spy=True)
                )
                await session.commit()
        # Inline reveal button
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔎 Узнать роль", callback_data=f"spy:reveal:{round_.id}")],
        ])
        mins = int(DISCUSSION.total_seconds() // 60)
        await self.bot.send_message(
            chat_id,
            f"🎯 Игра началась! Локация загадана. Один из вас — шпион.\n"
            f"Каждый игрок жмёт «Узнать роль», обсуждение {mins} мин.\n"
            "Когда готовы — /spy_vote (или дождитесь автоматического голосования).",
            reply_markup=kb,
        )

    async def reveal_role(self, *, chat_id: int, user_id: int, round_id: int) -> tuple[str, bool]:
        """Return (alert_text, found). `found=False` if user is not in the round."""
        async with self.sessionmaker() as session:
            round_ = await session.get(SpyRound, round_id)
            if round_ is None or round_.chat_id != chat_id:
                return "Раунд не найден.", False
            if round_.status not in {RoundStatus.ACTIVE.value, RoundStatus.VOTING.value}:
                return "Игра уже закрыта.", False
            player = await session.get(SpyPlayer, (round_id, user_id))
            if player is None:
                return "Ты не в этой игре.", False
            if player.revealed_at is None:
                await session.execute(
                    update(SpyPlayer)
                    .where(SpyPlayer.round_id == round_id, SpyPlayer.user_id == user_id)
                    .values(revealed_at=datetime.utcnow())
                )
                await session.commit()
            if player.is_spy:
                return "🕵️ Ты ШПИОН. Угадай локацию.", True
            return f"📍 Локация: {round_.location}", True

    async def start_vote(self, *, chat_id: int, initiator_id: int) -> None:
        async with self._lock(chat_id):
            async with self.sessionmaker() as session:
                round_ = await self._fetch_open(session, chat_id)
                if round_ is None or round_.status != RoundStatus.ACTIVE.value:
                    await self.bot.send_message(chat_id, "Голосовать нечем — игра не идёт.")
                    return
                if round_.initiator_user_id != initiator_id:
                    await self.bot.send_message(chat_id, "Голосование запускает инициатор.")
                    return
                player_ids = await self._player_ids(session, round_.id)
                players: list[_PlayerInfo] = []
                for uid in player_ids:
                    display = await self._resolve_display(chat_id=chat_id, user_id=uid)
                    players.append(_PlayerInfo(uid, display, uid == round_.spy_user_id))
                options: list = [p.display for p in players] + ["Никто (ничья)"]
                poll_msg = await self.bot.send_poll(
                    chat_id=chat_id,
                    question="🎯 Кто шпион?",
                    options=options,
                    is_anonymous=True,
                    open_period=VOTE_OPEN_PERIOD,
                    allows_multiple_answers=False,
                )
                await session.execute(
                    update(SpyRound)
                    .where(SpyRound.id == round_.id)
                    .values(
                        status=RoundStatus.VOTING.value,
                        vote_poll_id=poll_msg.poll.id if poll_msg.poll else None,
                        vote_message_id=poll_msg.message_id,
                    )
                )
                await session.commit()
                round_id = round_.id
                spy_uid = round_.spy_user_id
                location = round_.location
        # Schedule auto-resolve
        asyncio.create_task(
            self._auto_resolve_after(
                chat_id=chat_id, round_id=round_id, players=players,
                spy_uid=spy_uid, location=location, poll_message_id=poll_msg.message_id,
            )
        )

    async def _auto_resolve_after(
        self,
        *,
        chat_id: int,
        round_id: int,
        players: list[_PlayerInfo],
        spy_uid: int | None,
        location: str,
        poll_message_id: int,
    ) -> None:
        await asyncio.sleep(VOTE_OPEN_PERIOD + 3)
        try:
            poll = await self.bot.stop_poll(chat_id=chat_id, message_id=poll_message_id)
        except Exception:
            logger.exception("spy: stop_poll failed chat=%s msg=%s", chat_id, poll_message_id)
            return

        # Resolve outcome
        spy_index = next((i for i, p in enumerate(players) if p.is_spy), None)
        counts = [opt.voter_count for opt in poll.options]
        top = max(counts) if counts else 0
        winners = [i for i, c in enumerate(counts) if c == top and top > 0]

        if len(winners) == 1 and spy_index is not None and winners[0] == spy_index:
            outcome_text = (
                f"🎯 Команда раскрыла шпиона! Локация: {location}.\n"
                f"Шпион: {players[spy_index].display}."
            )
            outcome = "team_win"
        else:
            outcome_text = (
                f"🕵️ Шпион ушёл! Локация была: {location}.\n"
                f"Шпион: {players[spy_index].display if spy_index is not None else '—'}."
            )
            outcome = "spy_win"

        async with self.sessionmaker() as session:
            await session.execute(
                update(SpyRound)
                .where(SpyRound.id == round_id)
                .values(
                    status=RoundStatus.FINISHED.value,
                    outcome=outcome,
                    finished_at=datetime.utcnow(),
                )
            )
            await session.commit()

        await self.bot.send_message(chat_id, outcome_text)

    async def abort(self, *, chat_id: int, initiator_id: int) -> None:
        async with self._lock(chat_id):
            async with self.sessionmaker() as session:
                round_ = await self._fetch_open(session, chat_id)
                if round_ is None:
                    await self.bot.send_message(chat_id, "Нечего отменять.")
                    return
                if round_.initiator_user_id != initiator_id:
                    await self.bot.send_message(chat_id, "Отменить может только инициатор.")
                    return
                await session.execute(
                    update(SpyRound)
                    .where(SpyRound.id == round_.id)
                    .values(
                        status=RoundStatus.ABORTED.value,
                        outcome="aborted",
                        finished_at=datetime.utcnow(),
                    )
                )
                await session.commit()
        await self.bot.send_message(chat_id, "🎯 Игра в шпиона отменена.")

    async def recover_stale(self, *, now: datetime | None = None) -> int:
        """Expire LOBBY/ACTIVE/VOTING rounds whose orchestration was lost on restart."""
        if now is None:
            now = datetime.utcnow()
        lobby_cutoff = now - LOBBY_MAX_AGE
        active_cutoff = now - ACTIVE_MAX_AGE
        async with self.sessionmaker() as session:
            r1 = await session.execute(
                update(SpyRound)
                .where(
                    SpyRound.status == RoundStatus.LOBBY.value,
                    SpyRound.started_at < lobby_cutoff,
                )
                .values(
                    status=RoundStatus.ABORTED.value,
                    outcome="recovered_stale",
                    finished_at=now,
                )
            )
            r2 = await session.execute(
                update(SpyRound)
                .where(
                    SpyRound.status.in_([RoundStatus.ACTIVE.value, RoundStatus.VOTING.value]),
                    SpyRound.started_at < active_cutoff,
                )
                .values(
                    status=RoundStatus.FINISHED.value,
                    outcome="recovered_stale",
                    finished_at=now,
                )
            )
            await session.commit()
            rc1 = int(r1.rowcount or 0)  # type: ignore[attr-defined]
            rc2 = int(r2.rowcount or 0)  # type: ignore[attr-defined]
            return rc1 + rc2

    # ---------- helpers ----------

    @staticmethod
    async def _fetch_open(session: AsyncSession, chat_id: int) -> SpyRound | None:
        stmt = (
            select(SpyRound)
            .where(
                SpyRound.chat_id == chat_id,
                SpyRound.status.in_([
                    RoundStatus.LOBBY.value,
                    RoundStatus.ACTIVE.value,
                    RoundStatus.VOTING.value,
                ]),
            )
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def _count_players(session: AsyncSession, round_id: int) -> int:
        from sqlalchemy import func as sa_func
        stmt = select(sa_func.count()).select_from(SpyPlayer).where(SpyPlayer.round_id == round_id)
        return int((await session.execute(stmt)).scalar_one())

    @staticmethod
    async def _player_ids(session: AsyncSession, round_id: int) -> list[int]:
        stmt = select(SpyPlayer.user_id).where(SpyPlayer.round_id == round_id)
        return [int(row[0]) for row in (await session.execute(stmt)).all()]
