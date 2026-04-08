from __future__ import annotations

import asyncio
import logging
import re
from html import escape
from typing import Dict, List

from aiogram import F, Router, types
from aiogram.filters import Command
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.memory import RelationshipState, UserMemoryProfile
from ..models.user import User
from ..services.context import ChatTurn, ContextService, build_messages
from ..services.llm.client import (
    LLMError,
    LLMRateLimitError,
    generate as llm_generate,
    resolve_llm_options,
)
from ..services.moderation import apply_moderation
from ..services.persona import StylePromptService, DEFAULT_STYLE_KEY
from ..services.app_config import AppConfigService
from ..services.message_history import store_telegram_message
from ..services.roulette import RouletteService
from ..services.settings import SettingsService
from ..services.usage_limits import UsageLimiter
from ..services.user_memory import UserMemoryService
from ..utils.llm import resolve_temperature
from .router_admin import PROMPT_TEXT
from .constants import START_PRIVATE_RESPONSE


router = Router(name="fun")
logger = logging.getLogger("bot.summary")

DEFAULT_SUMMARY_PROMPT = (
    "Ты — {style_label}. Сделай краткую, но живую сводку последних сообщений в своей манере."
    " Включи атмосферу, ключевые участки разговора и финальный вывод."
    " Никаких выдуманных фактов и Markdown-форматирования."
)

DEFAULT_SUMMARY_CLOSING = "Собери одно сообщение по последним {count} сообщениям чата."

SUMMARY_LOCKS: Dict[int, asyncio.Lock] = {}


def _get_summary_lock(chat_id: int) -> asyncio.Lock:
    lock = SUMMARY_LOCKS.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        SUMMARY_LOCKS[chat_id] = lock
    return lock


def _compose_summary_prompt(style_label: str, style_prompt: str, *, base_prompt: str | None = None) -> str:
    template = base_prompt or DEFAULT_SUMMARY_PROMPT
    try:
        base = template.format(style_label=style_label)
    except KeyError:
        base = template
    if style_prompt:
        return base + "\n\n" + style_prompt
    return base


def _resolve_prompt_token_limit(conf: dict[str, object]) -> int:
    raw = conf.get("context_max_prompt_tokens", 32000)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 32000
    return max(2000, min(60000, value))


def _split_message(text: str, limit: int = 4096) -> List[str]:
    chunks: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1 or split_at < limit * 0.5:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at == -1 or split_at < limit * 0.5:
            split_at = limit
        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:limit]
            split_at = len(chunk)
        else:
            entity_start = chunk.rfind("&")
            entity_end = chunk.rfind(";")
            if entity_start != -1 and (entity_end == -1 or entity_start > entity_end):
                chunk = chunk[:entity_start].rstrip()
                split_at = len(chunk)
        if not chunk:
            chunk = remaining[:limit]
            split_at = len(chunk)
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip("\n ")
    return chunks


def _sanitize_summary_body(text: str) -> str:
    text = re.sub(r"@([\w]{1,32})", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.S)
    return text


def _summary_participants(turns: list[ChatTurn]) -> list[tuple[int, str | None]]:
    participants: list[tuple[int, str | None]] = []
    seen: set[int] = set()
    for turn in reversed(turns):
        user_id = getattr(turn, "user_id", None)
        if getattr(turn, "is_bot", False) or not user_id or user_id in seen:
            continue
        seen.add(int(user_id))
        participants.append((int(user_id), getattr(turn, "speaker", None)))
        if len(participants) >= 4:
            break
    return participants


async def _store_command_once(session: AsyncSession, message: types.Message) -> bool:
    try:
        created = await store_telegram_message(session, message)
        await session.commit()
        return created
    except IntegrityError:
        await session.rollback()
        logger.debug(
            "Duplicate command ignored chat=%s message_id=%s text=%r",
            message.chat.id,
            message.message_id,
            message.text,
        )
        return False
    except Exception:
        await session.rollback()
        raise


@router.message(Command("roll"))
async def cmd_roll(
    message: types.Message,
    session: AsyncSession,
    roulette: RouletteService,
):
    if not await _store_command_once(session, message):
        return
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Команда доступна только в групповых чатах.")
        return

    result = await roulette.roll(message.chat.id, initiator=str(message.from_user.id))
    if not result.success:
        await message.reply(result.message)


@router.message(Command("rollstats_montly"))
async def cmd_rollstats_monthly(message: types.Message, session: AsyncSession, roulette: RouletteService):
    if not await _store_command_once(session, message):
        return
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Команда доступна только в групповых чатах.")
        return
    stats = await roulette.get_stats_monthly(message.chat.id)
    await message.reply(stats)


@router.message(Command("rollstats_total"))
async def cmd_rollstats_total(message: types.Message, session: AsyncSession, roulette: RouletteService):
    if not await _store_command_once(session, message):
        return
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Команда доступна только в групповых чатах.")
        return
    stats = await roulette.get_stats_total(message.chat.id)
    await message.reply(stats)


@router.message(Command("start"))
async def cmd_start(message: types.Message, session: AsyncSession):
    if not await _store_command_once(session, message):
        return
    if message.chat.type != "private":
        return
    await message.reply(START_PRIVATE_RESPONSE)


@router.message(Command("summary"))
async def cmd_summary(
    message: types.Message,
    session: AsyncSession,
    settings: SettingsService,
    context: ContextService,
    personas: StylePromptService,
    app_config: AppConfigService,
    usage_limits: UsageLimiter,
    memory: UserMemoryService,
):
    if not await _store_command_once(session, message):
        return
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Команда доступна только в групповых чатах.")
        return

    conf = await settings.get_all(message.chat.id)
    if not conf.get("is_active", True):
        await message.reply("Бот выключен в этом чате.")
        return

    lock = _get_summary_lock(message.chat.id)
    if lock.locked():
        await message.reply("Сводка уже готовится, подожди немного.")
        return

    async with lock:
        app_conf = await app_config.get_all()
        provider = resolve_llm_options(app_conf)
        max_turns_raw = app_conf.get("context_max_turns", 100) or 100
        try:
            max_turns = int(max_turns_raw)
        except (TypeError, ValueError):
            max_turns = 100
        max_turns = max(10, min(500, max_turns))
        prompt_token_limit = _resolve_prompt_token_limit(app_conf)

        summary_limit_raw = app_conf.get("summary_daily_limit", 2) or 0
        llm_limit_raw = app_conf.get("llm_daily_limit", 0) or 0
        summary_prompt_template = str(app_conf.get("prompt_summary_base") or DEFAULT_SUMMARY_PROMPT)
        summary_closing_template = str(app_conf.get("prompt_summary_closing") or DEFAULT_SUMMARY_CLOSING)
        try:
            summary_limit = int(summary_limit_raw)
        except (TypeError, ValueError):
            summary_limit = 0
        try:
            llm_limit = int(llm_limit_raw)
        except (TypeError, ValueError):
            llm_limit = 0

        requests: list[tuple[str, int]] = []
        if summary_limit > 0:
            requests.append(("summary", summary_limit))
        if llm_limit > 0:
            requests.append(("llm", llm_limit))

        turns = await context.get_recent_turns(session, message.chat.id, max_turns)
        if not turns:
            await message.reply("Нечего пересказывать: история пуста.", allow_sending_without_reply=True)
            return

        consumed_prefixes: list[str] = []
        if requests:
            allowed, counts, exceeded = await usage_limits.consume(message.chat.id, requests)
            if not allowed:
                if "summary" in exceeded:
                    used = counts.get("summary", summary_limit)
                    await message.reply(
                        f"🤖 Лимит сводок на сегодня исчерпан ({used}/{summary_limit}).",
                        allow_sending_without_reply=True,
                    )
                else:
                    used = counts.get("llm", llm_limit)
                    await message.reply(
                        f"🤖 Лимит запросов к модели исчерпан ({used}/{llm_limit}). Попробуй завтра.",
                        allow_sending_without_reply=True,
                    )
                return
            consumed_prefixes = [prefix for prefix, _ in requests]

        style = str(conf.get("style", DEFAULT_STYLE_KEY))
        display_map = await personas.get_display_map()
        style_prompts = await personas.get_all()
        fallback_label = display_map.get(DEFAULT_STYLE_KEY, DEFAULT_STYLE_KEY)
        style_label = display_map.get(style, fallback_label)
        fallback_prompt = style_prompts.get(DEFAULT_STYLE_KEY, "")
        style_prompt = style_prompts.get(style, fallback_prompt)

        system_prompt = _compose_summary_prompt(
            style_label,
            style_prompt,
            base_prompt=summary_prompt_template,
        )
        context_blocks = None
        if bool(conf.get("personalization_enabled", True)) and memory.is_enabled(app_conf):
            participant_entries = _summary_participants(turns)
            social_block = await memory.build_summary_social_block(
                session,
                chat_id=message.chat.id,
                participants=participant_entries,
                app_conf=app_conf,
            )
            if social_block:
                context_blocks = [social_block]
        try:
            closing_text = summary_closing_template.format(count=len(turns))
        except KeyError:
            closing_text = summary_closing_template
        closing_text = closing_text.strip()
        messages_for_llm = build_messages(
            system_prompt,
            turns,
            max_turns=max_turns,
            max_tokens=prompt_token_limit,
            closing_text=closing_text,
            context_blocks=context_blocks,
        )

        default_cap = 4096
        base_cap = max(prompt_token_limit // 2, 200)
        max_answer_tokens = max(200, min(default_cap, base_cap))
        max_length_conf = app_conf.get("max_length")
        if isinstance(max_length_conf, (int, float, str)):
            try:
                max_len_value = int(float(max_length_conf))
            except (TypeError, ValueError):
                max_len_value = None
            if max_len_value and max_len_value > 0:
                max_answer_tokens = min(max_answer_tokens, max_len_value)

        try:
            summary_text = await llm_generate(
                messages_for_llm,
                max_tokens=max_answer_tokens,
                temperature=resolve_temperature(conf),
                top_p=float(conf.get("top_p", 0.9) or 0.9),
                provider=provider,
            )
        except LLMRateLimitError as exc:
            if consumed_prefixes:
                await usage_limits.refund(message.chat.id, consumed_prefixes)
            wait_hint = ""
            if exc.retry_after and exc.retry_after > 0:
                wait_hint = f" Попробуй через ~{int(exc.retry_after)} с."
            await message.reply("🤖 Модель перегружена." + wait_hint, allow_sending_without_reply=True)
            return
        except LLMError:
            if consumed_prefixes:
                await usage_limits.refund(message.chat.id, consumed_prefixes)
            await message.reply("🤖 LLM вернула ошибку. Попробуй позже.", allow_sending_without_reply=True)
            return
        except Exception:
            if consumed_prefixes:
                await usage_limits.refund(message.chat.id, consumed_prefixes)
            logger.exception(
                "Unexpected error while generating summary (provider=%s fallback=%s)",
                provider,
            )
            await message.reply("🤖 Не удалось подготовить сводку.", allow_sending_without_reply=True)
            return

        cleaned = apply_moderation(summary_text).strip()
        if not cleaned:
            logger.warning(
                "Summary model returned empty text; retrying chat=%s provider=%s fallback=%s",
                message.chat.id,
                provider,
            )
            retry_turns = turns[-max(10, min(len(turns), 20)) :]
            retry_limit = min(default_cap, max(prompt_token_limit // 2, 200))
            retry_messages = build_messages(
                system_prompt,
                retry_turns,
                max_turns=len(retry_turns),
                max_tokens=retry_limit,
                closing_text=closing_text,
                context_blocks=context_blocks,
            )
            try:
                retry_tokens = min(max_answer_tokens, max(512, max_answer_tokens // 2))
                summary_text = await llm_generate(
                    retry_messages,
                    max_tokens=retry_tokens,
                    temperature=resolve_temperature(conf),
                    top_p=float(conf.get("top_p", 0.9) or 0.9),
                    provider=provider,
                )
            except LLMRateLimitError as exc:
                if consumed_prefixes:
                    await usage_limits.refund(message.chat.id, consumed_prefixes)
                wait_hint = ""
                if exc.retry_after and exc.retry_after > 0:
                    wait_hint = f" Попробуй через ~{int(exc.retry_after)} с."
                await message.reply("🤖 Модель перегружена." + wait_hint, allow_sending_without_reply=True)
                return
            except LLMError:
                if consumed_prefixes:
                    await usage_limits.refund(message.chat.id, consumed_prefixes)
                await message.reply("🤖 LLM вернула ошибку. Попробуй позже.", allow_sending_without_reply=True)
                return
            except Exception:
                if consumed_prefixes:
                    await usage_limits.refund(message.chat.id, consumed_prefixes)
                logger.exception(
                    "Unexpected error during summary retry (provider=%s fallback=%s)",
                    provider,
                )
                await message.reply("🤖 Не удалось подготовить сводку.", allow_sending_without_reply=True)
                return

            cleaned = apply_moderation(summary_text).strip()
            if not cleaned:
                if consumed_prefixes:
                    await usage_limits.refund(message.chat.id, consumed_prefixes)
                logger.warning(
                    "Summary retry returned empty text chat=%s provider=%s",
                    message.chat.id,
                    provider,
                )
                await message.reply("🤖 Не удалось подготовить сводку.", allow_sending_without_reply=True)
                return

        heading = f"<b>Сводка по чату за последние {len(turns)} сообщений</b>"
        safe_body = escape(_sanitize_summary_body(cleaned))
        full_text = heading + "\n\n" + safe_body
        for chunk in _split_message(full_text):
            await message.reply(chunk, allow_sending_without_reply=True)


@router.message(Command("relationships"))
async def cmd_relationships(
    message: types.Message,
    session: AsyncSession,
):
    if not await _store_command_once(session, message):
        return
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Команда доступна только в групповых чатах.")
        return

    stmt = (
        select(RelationshipState, UserMemoryProfile, User)
        .outerjoin(
            UserMemoryProfile,
            (UserMemoryProfile.chat_id == RelationshipState.chat_id)
            & (UserMemoryProfile.user_id == RelationshipState.user_id),
        )
        .outerjoin(User, User.tg_id == RelationshipState.user_id)
        .where(RelationshipState.chat_id == message.chat.id)
    )
    rows = list((await session.execute(stmt)).all())
    if not rows:
        await message.reply("Пока не накопилось данных по взаимоотношениям в этом чате.", parse_mode=None)
        return

    rows.sort(
        key=lambda row: (
            -_relationship_rapport(row[0]),
            str(row[2].username if row[2] and row[2].username else row[0].user_id).lower(),
        )
    )

    lines = ["🤝 Взаимоотношения по чату:"]
    for index, (relation, profile, user) in enumerate(rows, start=1):
        username = user.username if user and user.username else str(relation.user_id)
        relation_label = _relationship_kind_label(relation)
        line = f"{index}. {username} - {relation_label}"
        facts = _collect_relationship_facts(profile)
        if facts:
            line += f"; факты: {', '.join(facts)}"
        lines.append(line)

    text = "\n".join(lines)
    for chunk in _split_message(text):
        await message.reply(chunk, allow_sending_without_reply=True, parse_mode=None)


@router.message(Command("reg"))
async def cmd_reg(message: types.Message, session: AsyncSession, roulette: RouletteService):
    if not await _store_command_once(session, message):
        return
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Команда доступна только в групповых чатах.")
        return
    if not message.from_user or message.from_user.is_bot:
        await message.reply("Ботам регистрироваться не нужно 😉")
        return
    user = message.from_user
    is_new, registered = await roulette.register_participant(
        message.chat.id,
        user.id,
        user.username,
    )
    try:
        total = await message.bot.get_chat_member_count(message.chat.id)
    except Exception:
        total = None
    suffix = f" (зарегистрировано: {registered})"
    if is_new:
        await message.reply(f"Вы зарегистрированы для рулетки{suffix}.")
    else:
        await message.reply(f"Вы уже в списке участников{suffix}.")


@router.message(Command("unreg"))
async def cmd_unreg(message: types.Message, session: AsyncSession, roulette: RouletteService):
    if not await _store_command_once(session, message):
        return
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Команда доступна только в групповых чатах.")
        return
    if not message.from_user or message.from_user.is_bot:
        await message.reply("Боты и так не участвуют.")
        return
    removed, registered = await roulette.unregister_participant(message.chat.id, message.from_user.id)
    suffix = f" (зарегистрировано: {registered})"
    if removed:
        await message.reply(f"Вы исключены из рулетки{suffix}.")
    else:
        await message.reply(f"Вас не было в списке участников{suffix}.")


@router.message(
    F.reply_to_message,
    F.reply_to_message.from_user.id == F.bot.id,
    F.reply_to_message.text == PROMPT_TEXT,
)
async def handle_custom_title_reply(
    message: types.Message,
    settings: SettingsService,
):
    chat_id = message.chat.id
    text = (message.text or "").strip()
    if not text or text.lower() in {"reset", "сброс", "отмена"}:
        await settings.set(chat_id, "roulette_custom_title", None)
        await message.reply("Фиксированное звание сброшено. Теперь бот снова будет придумывать его сам по истории чата.")
    else:
        await settings.set(chat_id, "roulette_custom_title", text)
        await message.reply(f"Новое фиксированное звание установлено: {text}")


@router.message(Command("rolltitle"))
async def cmd_rolltitle(message: types.Message, session: AsyncSession, settings: SettingsService):
    if not await _store_command_once(session, message):
        return
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Команда доступна только в групповых чатах.")
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.reply("Использование: /rolltitle новое_звание")
        return
    title = args[1].strip()
    await settings.set(message.chat.id, "roulette_custom_title", title)
    await message.reply(f"Новое фиксированное звание установлено: {title}")


def _collect_relationship_facts(profile: UserMemoryProfile | None) -> list[str]:
    if profile is None:
        return []

    facts: list[str] = []
    for value in profile.identity or []:
        cleaned = _clean_relationship_fact(str(value))
        if cleaned:
            facts.append(cleaned)
        if len(facts) >= 2:
            return facts

    for value in _visible_preferences(profile.preferences):
        cleaned = _clean_relationship_fact(str(value))
        if cleaned and cleaned not in facts:
            facts.append(cleaned)
        if len(facts) >= 2:
            return facts

    for value in profile.boundaries or []:
        cleaned = _clean_relationship_fact(str(value))
        if cleaned and cleaned not in facts:
            facts.append(cleaned)
        if len(facts) >= 2:
            return facts

    summary = _clean_relationship_fact(profile.summary or "")
    if summary and summary not in facts:
        facts.append(summary)
    return facts[:2]


def _clean_relationship_fact(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    text = re.sub(
        r"(?:,?\s*)предпочита(?:е|ё)мый\s+тон:\s*(?:neutral|warm|careful|нейтральный|т[её]плый|осторожный)\.?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?:,?\s*)предпочитает\s+(?:нейтральный|т[её]плый|осторожный)\s+тон\.?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return " ".join(text.split()).strip(" ,.;")


def _visible_preferences(values: list[str] | None) -> list[str]:
    if not values:
        return []
    return [
        item
        for item in values
        if not re.fullmatch(
            r"предпочита(?:е|ё)мый тон:\s*(neutral|warm|careful|нейтральный|т[её]плый|осторожный)",
            item.strip().lower(),
        )
    ]


def _relationship_rapport(relation: RelationshipState | None) -> float:
    if relation is None:
        return 0.0
    affinity = float(relation.affinity or 0)
    tension = float(relation.tension or 0)
    return max(-1.0, min(1.0, affinity - tension))


def _relationship_kind_label(relation: RelationshipState | None) -> str:
    rapport = _relationship_rapport(relation)
    if rapport >= 0.85:
        return "отношения почти дружеские"
    if rapport >= 0.35:
        return "отношения тёплые"
    if rapport <= -0.85:
        return "отношения близки к ненависти"
    if rapport <= -0.35:
        return "отношения напряжённые"
    return "без выраженного отношения"
