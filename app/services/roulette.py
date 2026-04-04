from __future__ import annotations

import asyncio
import logging
import random
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.chat import Chat
from ..models.roulette import RouletteParticipant, RouletteWinner
from ..services.app_config import AppConfigService
from ..services.context import (
    DEFAULT_CHAT_PROMPT,
    DEFAULT_FOCUS_SUFFIX,
    ChatTurn,
    ContextService,
    build_messages,
    build_system_prompt,
)
from ..services.llm.client import (
    LLMError,
    LLMRateLimitError,
    resolve_llm_options,
)
from ..services.llm.client import (
    generate as llm_generate,
)
from ..services.moderation import apply_moderation
from ..services.persona import StylePromptService
from ..services.settings import SettingsService
from ..services.user_memory import UserMemoryService
from ..utils.llm import resolve_temperature

logger = logging.getLogger("roulette")
MoscowTZ = ZoneInfo("Europe/Moscow")

LEGACY_TITLE_CHOICES = [
    ("pidor", "Пидор"),
    ("skuf", "Скуф"),
    ("beauty", "Красавчик"),
    ("clown", "Клоун"),
]
DEFAULT_GENERATED_TITLE = "Герой дня"

DEFAULT_ROULETTE_PROMPT = (
    "Ты — ведущий шуточной рулетки. Говори от первого лица и не описывай себя со стороны."
    " Пиши 1–2 короткие фразы без Markdown, поддержи стиль выбранной персоны и не раскрывай победителя."
)
DEFAULT_ROULETTE_TITLE_PROMPT = (
    "Ты придумываешь одно короткое шуточное звание для рулетки в чате. "
    "Опирайся только на последние сообщения этого чата и активную роль бота. "
    "Верни только само звание без кавычек, без пояснений, без эмодзи, максимум 4 слова. "
    "Не называй конкретного участника и не пиши слово 'звание'. "
    "Избегай слишком общих вариантов вроде 'герой дня', 'победитель дня' и 'лучший дня', "
    "если в истории есть более конкретные поводы для шутки."
)
WINNER_PLACEHOLDER = "[[winner]]"
GENERIC_GENERATED_TITLES = {
    "герой дня",
    "героя дня",
    "победитель дня",
    "лучший дня",
    "лучший чат",
    "звезда дня",
}


def _looks_like_bot_username(username: str | None) -> bool:
    return bool(username and username.strip().lower().endswith("bot"))


def _coerce_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if not isinstance(value, (float, str, bytes, bytearray)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: object, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, (str, bytes, bytearray)):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
        memory: UserMemoryService,
    ) -> None:
        self.bot = bot
        self.sessionmaker = sessionmaker
        self.settings = settings
        self.app_config = app_config
        self.context = context
        self.personas = personas
        self.memory = memory
        self._roll_locks: dict[int, asyncio.Lock] = {}

    def _get_roll_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._roll_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._roll_locks[chat_id] = lock
        return lock

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
        async with self._get_roll_lock(chat_id):
            async with self.sessionmaker() as session:
                if not force and await self._has_winner_today(session, chat_id):
                    return RollResult(False, "Рулетка уже запускалась сегодня. Возвращайся завтра!")

                participants = await self._fetch_participants(session, chat_id)
                if not participants:
                    return RollResult(False, "Некого разыгрывать — зарегистрируйтесь командой /reg.")

                winner_user_id, winner_username = random.choice(participants)
                conf = await self.settings.get_all(chat_id)
                app_conf = await self.app_config.get_all()
                title_code, title_display = await self._pick_title(
                    session,
                    chat_id=chat_id,
                    conf=conf,
                    app_conf=app_conf,
                )

                delivered = False
                try:
                    delivered = await self._announce(chat_id, winner_user_id, winner_username, title_display)
                except LLMRateLimitError as exc:
                    logger.warning(
                        "Rate limit during roulette announcement chat=%s retry_after=%s",
                        chat_id,
                        exc.retry_after,
                    )
                    delivered = await self._announce_without_llm(chat_id, winner_user_id, winner_username, title_display)
                except LLMError:
                    logger.exception("LLM failed while preparing roulette announcement chat=%s", chat_id)
                    delivered = await self._announce_without_llm(chat_id, winner_user_id, winner_username, title_display)
                except Exception:
                    logger.exception("Unexpected error during roulette announcement chat=%s", chat_id)
                    delivered = await self._announce_without_llm(chat_id, winner_user_id, winner_username, title_display)

                if not delivered:
                    return RollResult(False, "Не удалось отправить сообщение в чат.")

                winner = RouletteWinner(
                    chat_id=chat_id,
                    user_id=winner_user_id,
                    username=winner_username,
                    title=title_display,
                    title_code=title_code,
                    won_at=today,
                )
                session.add(winner)
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    logger.info("Roulette winner already exists chat=%s date=%s", chat_id, today)
                    return RollResult(False, "Рулетка уже запускалась сегодня. Возвращайся завтра!")

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
        if _looks_like_bot_username(username):
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
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                is_new = False

        count = await self.participant_count(chat_id)
        return is_new, count

    async def participant_count(self, chat_id: int) -> int:
        async with self.sessionmaker() as session:
            username_col = sa.func.trim(sa.func.coalesce(RouletteParticipant.username, ""))
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

    async def _pick_title(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        conf: dict[str, object] | None = None,
        app_conf: dict[str, object] | None = None,
    ) -> tuple[str, str]:
        conf = conf or await self.settings.get_all(chat_id)
        custom = str(conf.get("roulette_custom_title") or "").strip()
        if custom:
            return "custom", self._sanitize_generated_title(custom, fallback=custom)

        app_conf = app_conf or await self.app_config.get_all()
        generated = await self._generate_title(session, chat_id=chat_id, conf=conf, app_conf=app_conf)
        if generated:
            return "generated", generated
        return "generated", DEFAULT_GENERATED_TITLE

    async def _generate_title(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        conf: dict[str, object],
        app_conf: dict[str, object],
    ) -> str | None:
        title_turns_raw = app_conf.get("roulette_title_context_messages", 200) or 200
        title_turns = _coerce_int(title_turns_raw, 200)
        title_turns = max(20, min(500, title_turns))

        fetch_limit = max(100, min(1200, title_turns * 3))
        turns = await self.context.get_recent_turns(session, chat_id, fetch_limit)
        history_block = self._build_title_history(turns, title_turns)
        if not history_block:
            return DEFAULT_GENERATED_TITLE

        provider, fallback_enabled = resolve_llm_options(app_conf)
        style = str(conf.get("style", ""))
        style_prompts = await self.personas.get_all()
        style_prompt = style_prompts.get(style) or style_prompts.get("gopnik", "")
        system_prompt = DEFAULT_ROULETTE_TITLE_PROMPT
        if style_prompt:
            system_prompt += "\n\n" + style_prompt.strip()

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": history_block},
        ]

        try:
            raw_title = await llm_generate(
                messages,
                temperature=resolve_temperature(conf),
                top_p=_coerce_float(conf.get("top_p", 0.9) or 0.9, 0.9),
                max_tokens=self._title_completion_tokens(app_conf),
                provider=provider,
                fallback_enabled=fallback_enabled,
            )
        except (LLMRateLimitError, LLMError):
            logger.exception("Failed to generate roulette title chat=%s", chat_id)
            return self._heuristic_title(turns) or DEFAULT_GENERATED_TITLE

        title = self._sanitize_generated_title(raw_title, fallback=DEFAULT_GENERATED_TITLE)
        logger.debug("Roulette title candidate chat=%s raw=%r sanitized=%r", chat_id, raw_title, title)
        if self._is_generic_generated_title(title):
            retry_messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": history_block
                    + "\n\n"
                    + "Это слишком общее. Придумай более конкретное, смешное и привязанное к теме чата звание. "
                    + "Верни только новое короткое звание.",
                },
            ]
            try:
                retry_raw_title = await llm_generate(
                    retry_messages,
                    temperature=resolve_temperature(conf),
                    top_p=_coerce_float(conf.get("top_p", 0.9) or 0.9, 0.9),
                    max_tokens=self._title_completion_tokens(app_conf),
                    provider=provider,
                    fallback_enabled=fallback_enabled,
                )
                retry_title = self._sanitize_generated_title(
                    retry_raw_title,
                    fallback=DEFAULT_GENERATED_TITLE,
                )
                logger.debug(
                    "Roulette title retry chat=%s raw=%r sanitized=%r",
                    chat_id,
                    retry_raw_title,
                    retry_title,
                )
                if retry_title and not self._is_generic_generated_title(retry_title):
                    return retry_title
            except (LLMRateLimitError, LLMError):
                logger.exception("Failed to retry roulette title generation chat=%s", chat_id)
        return self._heuristic_title(turns) or title

    async def _announce(
        self,
        chat_id: int,
        user_id: int,
        username: str | None,
        title_display: str,
    ) -> bool:
        conf = await self.settings.get_all(chat_id)
        app_conf = await self.app_config.get_all()
        style_prompts = await self.personas.get_all()
        provider, fallback_enabled = resolve_llm_options(app_conf)

        max_turns = int(app_conf.get("context_max_turns", 100) or 100)
        prompt_limit = self._prompt_token_limit(app_conf)
        async with self.sessionmaker() as session:
            turns = await self.context.get_recent_turns(session, chat_id, max_turns)
            winner_memory_block = await self._build_winner_memory_block(
                session=session,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                conf=conf,
                app_conf=app_conf,
            )

        focus_text = f"Скоро объявим обладателя звания '{title_display}'. Подогрей интригу, но не раскрывай имя."
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

        try:
            intrigue = await llm_generate(
                messages,
                temperature=resolve_temperature(conf),
                top_p=_coerce_float(conf.get("top_p", 0.9) or 0.9, 0.9),
                max_tokens=self._max_completion_tokens(app_conf, prompt_limit),
                provider=provider,
                fallback_enabled=fallback_enabled,
            )
        except (LLMRateLimitError, LLMError):
            logger.exception("Failed to generate roulette intrigue chat=%s", chat_id)
            return await self._announce_without_llm(chat_id, user_id, username, title_display)

        intrigue_clean = apply_moderation(intrigue).strip()
        final_intrigue = self._prepare_intrigue_text(intrigue_clean, title_display)
        try:
            await self.bot.send_message(chat_id, final_intrigue)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            if self._is_missing_chat_error(exc):
                await self._deactivate_chat(chat_id)
                logger.warning("Disabling chat %s for roulette headline: %s", chat_id, exc)
                return False
            logger.exception("Failed to send roulette headline chat=%s", chat_id)
            return False
        except Exception:
            logger.exception("Failed to send roulette headline chat=%s", chat_id)
            return False

        await asyncio.sleep(3)

        final_message = await self._generate_winner_result_message(
            chat_id=chat_id,
            turns=turns,
            conf=conf,
            app_conf=app_conf,
            style_prompts=style_prompts,
            provider=provider,
            fallback_enabled=fallback_enabled,
            title_display=title_display,
            user_id=user_id,
            username=username,
            winner_memory_block=winner_memory_block,
            prompt_limit=prompt_limit,
        )
        try:
            await self.bot.send_message(chat_id, final_message, parse_mode="HTML")
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            if self._is_missing_chat_error(exc):
                await self._deactivate_chat(chat_id)
                logger.warning("Disabling chat %s for roulette result: %s", chat_id, exc)
                return False
            logger.exception("Failed to send roulette result chat=%s", chat_id)
            return False
        except Exception:
            logger.exception("Failed to send roulette result chat=%s", chat_id)
            return False

        return True

    async def _build_winner_memory_block(
        self,
        *,
        session: AsyncSession,
        chat_id: int,
        user_id: int,
        username: str | None,
        conf: dict[str, object],
        app_conf: dict[str, object],
    ) -> str | None:
        if not bool(conf.get("personalization_enabled", True)):
            return None
        if not self.memory.is_enabled(app_conf):
            return None
        speaker_name = username or f"ID {user_id}"
        return await self.memory.build_user_memory_block(
            session,
            chat_id=chat_id,
            user_id=user_id,
            query_text=None,
            app_conf=app_conf,
            speaker_name=speaker_name,
            include_relation=False,
        )

    async def _generate_winner_result_message(
        self,
        *,
        chat_id: int,
        turns: list,
        conf: dict[str, object],
        app_conf: dict[str, object],
        style_prompts: dict[str, str],
        provider: str,
        fallback_enabled: bool,
        title_display: str,
        user_id: int,
        username: str | None,
        winner_memory_block: str | None,
        prompt_limit: int,
    ) -> str:
        focus_text = (
            f"Объяви, что звание «{title_display}» получает {WINNER_PLACEHOLDER}. "
            "Сохрани манеру активной роли и стиль шуточной рулетки. "
            "Обыграй звание через известные факты о победителе и его недавние сообщения. "
            "Если данных мало, опирайся только на его последние сообщения и не выдумывай. "
            "Не пересказывай внутреннюю справку напрямую. Не добавляй в объявление оценку ваших "
            "отношений, недоверие, слежку, контроль, настороженность или скрытую угрозу: "
            "победное объявление должно звучать уместно, живо и чуть празднично. "
            f"Обязательно используй маркер {WINNER_PLACEHOLDER} вместо имени победителя."
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
            max_turns=len(turns),
            max_tokens=prompt_limit,
            context_blocks=[winner_memory_block] if winner_memory_block else None,
        )

        try:
            raw_result = await llm_generate(
                messages,
                temperature=resolve_temperature(conf),
                top_p=_coerce_float(conf.get("top_p", 0.9) or 0.9, 0.9),
                max_tokens=self._max_completion_tokens(app_conf, prompt_limit),
                provider=provider,
                fallback_enabled=fallback_enabled,
            )
        except (LLMRateLimitError, LLMError):
            logger.exception("Failed to generate personalized roulette winner message chat=%s", chat_id)
            return self._format_final_message(title_display, user_id, username)

        return self._prepare_winner_message(raw_result, title_display, user_id, username)

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
        return self._ensure_quoted_title(trimmed, title_display)

    def _default_intrigue(self, title_display: str) -> str:
        return f"🎰 Я кручу барабан за «{title_display}». Скоро назову победителя."

    def _ensure_quoted_title(self, text: str, title_display: str) -> str:
        if not text or not title_display:
            return text
        if f"«{title_display}»" in text or f"\"{title_display}\"" in text:
            return text

        escaped_title = re.escape(title_display)
        pattern = re.compile(
            rf"(?P<prefix>\b(?:звание|титул)\s+)(?P<title>{escaped_title})(?P<suffix>\b)",
            flags=re.IGNORECASE,
        )
        replaced = pattern.sub(
            lambda match: f"{match.group('prefix')}«{match.group('title')}»{match.group('suffix')}",
            text,
            count=1,
        )
        if replaced != text:
            return replaced

        bare_pattern = re.compile(rf"(?<![«\"']){escaped_title}(?![»\"'])")
        return bare_pattern.sub(f"«{title_display}»", text, count=1)

    def _sanitize_generated_title(self, raw_title: str, *, fallback: str) -> str:
        cleaned = " ".join((raw_title or "").replace("\n", " ").split())
        quoted_match = re.search(r"[«\"]([^»\"]{2,80})[»\"]", cleaned)
        if quoted_match:
            cleaned = quoted_match.group(1)
        cleaned = re.sub(r"^```(?:\w+)?", "", cleaned).strip("` ")
        cleaned = re.sub(r"^звание[:\s-]+", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"^(скоро\s+объявлю|объявляю|кручу\s+рулетку|рулетка)\s+", "", cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.strip(" \"'«».,:;!?-")
        cleaned = re.sub(r"[@#][\w_]+", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            return fallback
        words = cleaned.split()
        if len(words) > 4:
            cleaned = " ".join(words[:4]).strip()
        if len(cleaned) > 80:
            cleaned = cleaned[:80].rsplit(" ", 1)[0].strip() or cleaned[:80].strip()
        return cleaned or fallback

    def _build_title_history(self, turns: list[ChatTurn], max_messages: int) -> str | None:
        lines: list[str] = []
        for turn in reversed(turns):
            if turn.is_bot:
                continue
            text = " ".join((turn.text or "").replace("\n", " ").split()).strip()
            if not text or text.startswith("/"):
                continue
            speaker = (turn.speaker or "Участник").strip()
            line = f"{speaker}: {text[:180]}"
            lines.append(line)
            if len(lines) >= max_messages:
                break
        if not lines:
            return None
        lines.reverse()
        return (
            "Последние человеческие реплики в чате. Опирайся только на них, не на прошлые объявления рулетки.\n"
            + "\n".join(lines)
            + "\n\nПридумай одно новое короткое шуточное звание по мотивам этих реплик. Верни только само звание."
        )

    def _is_generic_generated_title(self, title: str) -> bool:
        normalized = " ".join((title or "").strip().lower().split())
        return normalized in GENERIC_GENERATED_TITLES

    def _heuristic_title(self, turns: list[ChatTurn]) -> str | None:
        recent_texts: list[str] = []
        for turn in reversed(turns):
            if turn.is_bot:
                continue
            text = " ".join((turn.text or "").replace("\n", " ").split()).strip().lower()
            if not text or text.startswith("/"):
                continue
            recent_texts.append(text)
            if len(recent_texts) >= 80:
                break
        if not recent_texts:
            return None

        combined = " ".join(reversed(recent_texts))
        if "мыльные пузыри" in combined or (
            "мыльн" in combined and "пузыр" in combined
        ):
            return "Повелитель пузырей"
        if "аниме" in combined or "боруто" in combined or "каваки" in combined:
            return "Аниме-эксперт"
        if "пиво" in combined or "пив" in combined:
            return "Пивной эстет"

        profanity_hits = sum(
            combined.count(word)
            for word in ("долбо", "хуй", "лох", "пидор", "блять")
        )
        apology_hits = sum(
            combined.count(word)
            for word in ("прости", "извин", "ладно", "дружить")
        )
        if profanity_hits >= 3 and apology_hits >= 1:
            return "Мастер примирений"
        if profanity_hits >= 3:
            return "Король подколов"

        token_pattern = re.compile(r"[a-zA-Zа-яА-ЯёЁ]{4,}")
        stopwords = {
            "привет",
            "работаешь",
            "почему",
            "прямо",
            "ответь",
            "вопрос",
            "давно",
            "недавно",
            "можешь",
            "рассказать",
            "участников",
            "участнике",
            "ответ",
            "делаешь",
            "дело",
            "ладно",
            "люблю",
            "прости",
            "извини",
            "давай",
            "говорить",
            "можно",
            "круче",
            "дела",
            "мне",
            "тебе",
            "кого",
            "только",
            "этом",
            "чате",
        }
        rude_roots = ("долбо", "хуй", "пидор", "блять", "лох")
        counts: Counter[str] = Counter()
        for text in recent_texts:
            for token in token_pattern.findall(text):
                normalized = token.lower()
                if normalized in stopwords:
                    continue
                if any(root in normalized for root in rude_roots):
                    continue
                counts[normalized] += 1
        if not counts:
            return None

        keyword, repeats = counts.most_common(1)[0]
        if repeats < 2:
            return None
        if keyword.startswith("пузыр"):
            return "Главный по пузырям"
        if keyword.startswith("пив"):
            return "Главный по пиву"
        if keyword.startswith("аниме"):
            return "Главный по аниме"
        if keyword.startswith("борут"):
            return "Эксперт по Боруто"
        if keyword.startswith("кавак"):
            return "Эксперт по Каваки"
        return f"Главный по {keyword}"

    def _prepare_winner_message(
        self,
        text: str,
        title_display: str,
        user_id: int,
        username: str | None,
    ) -> str:
        cleaned = " ".join(apply_moderation(text).split())
        if not cleaned:
            return self._format_final_message(title_display, user_id, username)
        if WINNER_PLACEHOLDER not in cleaned:
            suffix = f" Победитель — {WINNER_PLACEHOLDER}."
            cleaned = cleaned.rstrip() + suffix
        if len(cleaned) > 320:
            cleaned = cleaned[:320].rsplit(" ", 1)[0].rstrip(",;:.!? ") + "…"

        mention = f"<a href='tg://user?id={user_id}'>{escape_html(username) if username else 'победитель'}</a>"
        return escape_html(cleaned).replace(WINNER_PLACEHOLDER, mention)

    async def _announce_without_llm(
        self,
        chat_id: int,
        user_id: int,
        username: str | None,
        title_display: str,
    ) -> bool:
        headline = f"🎰 Я запускаю рулетку за «{title_display}». Держи кулачки."
        try:
            await self.bot.send_message(chat_id, headline)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            if self._is_missing_chat_error(exc):
                await self._deactivate_chat(chat_id)
                logger.warning("Disabling chat %s for roulette fallback headline: %s", chat_id, exc)
                return False
            logger.exception("Failed to send fallback roulette headline chat=%s", chat_id)
            return False
        except Exception:
            logger.exception("Failed to send fallback roulette headline chat=%s", chat_id)
            return False
        await asyncio.sleep(3)
        final_message = self._format_final_message(title_display, user_id, username)
        try:
            await self.bot.send_message(chat_id, final_message, parse_mode="HTML")
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            if self._is_missing_chat_error(exc):
                await self._deactivate_chat(chat_id)
                logger.warning("Disabling chat %s for roulette fallback result: %s", chat_id, exc)
                return False
            logger.exception("Failed to send fallback roulette result chat=%s", chat_id)
            return False
        except Exception:
            logger.exception("Failed to send fallback roulette result chat=%s", chat_id)
            return False

        return True

    def _format_final_message(self, title_display: str, user_id: int, username: str | None) -> str:
        mention = f"<a href='tg://user?id={user_id}'>{escape_html(username) if username else 'победитель'}</a>"
        return f"🏆 Звание «{title_display}» достаётся {mention}!"

    def _prompt_token_limit(self, app_conf: dict[str, object]) -> int:
        raw_limit = app_conf.get("context_max_prompt_tokens", 32000)
        limit = _coerce_int(raw_limit, 32000)
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
        override = _coerce_int(override_raw, 0)
        if override and override > 0:
            allowed = min(allowed, override)
        return allowed

    def _title_completion_tokens(self, app_conf: dict[str, object]) -> int | None:
        override_raw = app_conf.get("max_length")
        override = _coerce_int(override_raw, 0)
        if override <= 0:
            return None
        if override and override > 0:
            return max(32, min(override, 256))
        return None

    async def _deactivate_chat(self, chat_id: int) -> None:
        async with self.sessionmaker() as session:
            chat = await session.get(Chat, chat_id)
            if chat is None or not chat.is_active:
                return
            chat.is_active = False
            await session.commit()
            logger.info("Marked chat %s as inactive after Telegram error", chat_id)

    @staticmethod
    def _is_missing_chat_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            hint in text
            for hint in (
                "chat not found",
                "bot was blocked by the user",
                "bot was kicked",
                "user is deactivated",
            )
        )

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

        display_map = {code: name for code, name in LEGACY_TITLE_CHOICES}
        custom_title = str(conf.get("roulette_custom_title") or "").strip()
        display_map["custom"] = custom_title or "Своё звание"

        if custom_title:
            heading_title = custom_title
        elif last_winner:
            heading_title = last_winner
        else:
            heading_title = "авто по истории"
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
                sa.delete(RouletteWinner).where(
                    RouletteWinner.chat_id == chat_id,
                    RouletteWinner.won_at == today,
                )
            )
            await session.commit()

    async def run_auto_roll(self) -> None:
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
