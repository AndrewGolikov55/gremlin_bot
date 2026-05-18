from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...models import (
    Message,
    RapbattleRound,
    RouletteScoreAdjustment,
    User,
    UserMemoryProfile,
)
from ...utils.locks import get_chat_lock
from ...utils.text import strip_markdown
from ..app_config import AppConfigService
from ..llm.client import LLMError, LLMRateLimitError, generate as llm_generate, resolve_llm_options
from ..persona import DEFAULT_STYLE_KEY, StylePromptService
from ..settings import SettingsService
from .common import RoundStatus

logger = logging.getLogger(__name__)

VOTE_OPEN_PERIOD = 60  # seconds

RAP_RULES = (
    "Жанр: рэп-баттл двух соперников. Сгенерируй ЧЕТЫРЕ строки рифмованного"
    " текста от лица заданного бойца, обращённого к оппоненту.\n"
    "- Опирайся на личные крючки из профиля оппонента и его сообщений\n"
    "- Можно жёстко и язвительно, без призывов к насилию\n"
    "- Никакого markdown — plain text\n"
    "- Только 4 строки куплета, без вступлений и пояснений"
)


class RapbattleService:
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

    async def _resolve(self, *, chat_id: int, user_id: int) -> tuple[str, str | None]:
        try:
            member = await self.bot.get_chat_member(chat_id, user_id)
            if member.status in {
                ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED,
            }:
                u = getattr(member, "user", None)
                if u is not None:
                    return str(u.first_name or u.username or f"id{user_id}"), (u.username or None)
        except TelegramBadRequest:
            pass
        except Exception:
            logger.exception("rapbattle: get_chat_member failed")
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

    async def _fighter_context(
        self, *, chat_id: int, user_id: int,
    ) -> tuple[list[str], list[str], list[str], str]:
        async with self.sessionmaker() as session:
            profile = await session.get(UserMemoryProfile, (chat_id, user_id))
            stmt = (
                select(Message.text)
                .where(
                    Message.chat_id == chat_id,
                    Message.user_id == user_id,
                    Message.is_bot.is_(False),
                    func.length(Message.text) > 0,
                )
                .order_by(Message.date.desc())
                .limit(15)
            )
            rows = (await session.execute(stmt)).all()
        messages = [str(row[0]) for row in rows]
        identity = list(profile.identity or []) if profile else []
        preferences = list(profile.preferences or []) if profile else []
        summary = (profile.summary if profile else None) or "—"
        return identity, preferences, messages, summary

    async def _persona_prompt(self, chat_id: int) -> str:
        conf = await self.settings.get_all(chat_id)
        style = str(conf.get("style", DEFAULT_STYLE_KEY))
        return await self.personas.get(style)

    async def start(
        self, *, chat_id: int, initiator_id: int, opponent_arg: str | None,
        opponent_reply_id: int | None,
    ) -> None:
        opponent_uid: int | None = None
        if opponent_reply_id is not None:
            opponent_uid = opponent_reply_id
        elif opponent_arg:
            opponent_uid = await self._resolve_username(opponent_arg)
        if opponent_uid is None:
            await self.bot.send_message(
                chat_id, "Укажи оппонента: /rapbattle @username или в реплае.",
            )
            return
        if opponent_uid == initiator_id:
            await self.bot.send_message(chat_id, "Сам с собой не баттлят.")
            return

        async with self._lock(chat_id):
            try:
                async with self.sessionmaker() as session:
                    async with session.begin():
                        round_ = RapbattleRound(
                            chat_id=chat_id,
                            challenger_a_id=initiator_id,
                            challenger_b_id=opponent_uid,
                            verses=[],
                            status=RoundStatus.GENERATING.value,
                        )
                        session.add(round_)
                        await session.flush()
                        round_id = round_.id
            except IntegrityError:
                await self.bot.send_message(chat_id, "Уже идёт рэп-баттл.")
                return

        a_display, _ = await self._resolve(chat_id=chat_id, user_id=initiator_id)
        b_display, _ = await self._resolve(chat_id=chat_id, user_id=opponent_uid)
        await self.bot.send_message(
            chat_id, f"🎤 Рэп-баттл: <b>{a_display}</b> vs <b>{b_display}</b>. Поехали!",
        )

        persona = await self._persona_prompt(chat_id)
        a_ident, a_prefs, a_msgs, a_summary = await self._fighter_context(
            chat_id=chat_id, user_id=initiator_id,
        )
        b_ident, b_prefs, b_msgs, b_summary = await self._fighter_context(
            chat_id=chat_id, user_id=opponent_uid,
        )

        verses: list[dict] = []
        for round_no in (1, 2):
            for side in ("a", "b"):
                if side == "a":
                    fighter, opponent_block = (
                        f"Боец A: {a_display}\nIdentity: {', '.join(a_ident) or '—'}\n"
                        f"Preferences: {', '.join(a_prefs) or '—'}\nSummary: {a_summary}\n",
                        f"Оппонент B: {b_display}\nIdentity: {', '.join(b_ident) or '—'}\n"
                        f"Preferences: {', '.join(b_prefs) or '—'}\nSummary: {b_summary}\n"
                        f"Реплики оппонента:\n" + "\n".join(b_msgs[:10]),
                    )
                else:
                    fighter, opponent_block = (
                        f"Боец B: {b_display}\nIdentity: {', '.join(b_ident) or '—'}\n"
                        f"Preferences: {', '.join(b_prefs) or '—'}\nSummary: {b_summary}\n",
                        f"Оппонент A: {a_display}\nIdentity: {', '.join(a_ident) or '—'}\n"
                        f"Preferences: {', '.join(a_prefs) or '—'}\nSummary: {a_summary}\n"
                        f"Реплики оппонента:\n" + "\n".join(a_msgs[:10]),
                    )
                user_prompt = (
                    f"Раунд {round_no}.\n\n{fighter}\n{opponent_block}\n\n"
                    "Сгенерируй куплет из 4 строк."
                )
                text = await self._llm(system=f"{persona}\n\n{RAP_RULES}", user=user_prompt) or "(LLM в обмороке)"
                verses.append({"round": round_no, "by": side, "text": text})
                display = a_display if side == "a" else b_display
                await self.bot.send_message(
                    chat_id, f"🎤 <b>{display}</b> (раунд {round_no}):\n\n{text}",
                )
                await asyncio.sleep(1.0)

        # Open vote
        poll_msg = await self.bot.send_poll(
            chat_id=chat_id,
            question="🎤 Кто победил?",
            options=[a_display, b_display],
            is_anonymous=True,
            open_period=VOTE_OPEN_PERIOD,
            allows_multiple_answers=False,
        )
        async with self.sessionmaker() as session:
            await session.execute(
                update(RapbattleRound)
                .where(RapbattleRound.id == round_id)
                .values(
                    verses=verses,
                    status=RoundStatus.VOTING.value,
                    poll_id=poll_msg.poll.id if poll_msg.poll else None,
                    poll_message_id=poll_msg.message_id,
                )
            )
            await session.commit()

        asyncio.create_task(self._resolve_after(
            chat_id=chat_id, round_id=round_id,
            poll_message_id=poll_msg.message_id,
            a_id=initiator_id, b_id=opponent_uid,
            a_display=a_display, b_display=b_display,
        ))

    async def _llm(self, *, system: str, user: str) -> str | None:
        conf = await self.app_config.get_all()
        provider = resolve_llm_options(conf)
        try:
            text = await llm_generate(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.95,
                max_tokens=220,
                provider=provider,
            )
        except (LLMError, LLMRateLimitError):
            logger.exception("rapbattle: LLM failed")
            return None
        return strip_markdown(text).strip() if text else None

    async def _resolve_after(
        self, *, chat_id: int, round_id: int, poll_message_id: int,
        a_id: int, b_id: int, a_display: str, b_display: str,
    ) -> None:
        await asyncio.sleep(VOTE_OPEN_PERIOD + 3)
        try:
            poll = await self.bot.stop_poll(chat_id=chat_id, message_id=poll_message_id)
        except Exception:
            logger.exception("rapbattle: stop_poll failed")
            return

        counts = [opt.voter_count for opt in poll.options]
        if not counts or counts[0] == counts[1]:
            outcome_text = "🤝 Ничья!"
            winner_id: int | None = None
        elif counts[0] > counts[1]:
            outcome_text = f"🏆 Победил {a_display}! +1 к рулетке."
            winner_id = a_id
        else:
            outcome_text = f"🏆 Победил {b_display}! +1 к рулетке."
            winner_id = b_id

        async with self.sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    update(RapbattleRound)
                    .where(RapbattleRound.id == round_id)
                    .values(
                        status=RoundStatus.FINISHED.value,
                        winner_user_id=winner_id,
                        finished_at=datetime.utcnow(),
                    )
                )
                if winner_id is not None:
                    session.add(RouletteScoreAdjustment(
                        chat_id=chat_id,
                        user_id=winner_id,
                        delta=-1,  # negative = winning the roulette (score reduction)
                        reason="rapbattle_win",
                        source_id=round_id,
                    ))
        await self.bot.send_message(chat_id, outcome_text)
