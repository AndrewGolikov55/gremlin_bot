from __future__ import annotations

import asyncio
import re
from html import escape
from typing import Dict, List

from aiogram import F, Router, types
from aiogram.filters import Command
from sqlalchemy.ext.asyncio import AsyncSession

from ..services.context import ContextService, build_messages
from ..services.llm.ollama import (
    OpenRouterError,
    OpenRouterRateLimitError,
    generate as llm_generate,
)
from ..services.moderation import apply_moderation
from ..services.persona import StylePromptService
from ..services.app_config import AppConfigService
from ..services.roulette import RouletteService
from ..services.settings import SettingsService
from ..services.usage_limits import UsageLimiter


router = Router(name="fun")

PROMPT_TEXT = "Введите новое прозвище для рулетки (или напишите 'reset' чтобы сбросить)."

DEFAULT_SUMMARY_PROMPT = (
    "Ты — {style_label}. Сделай краткую, но живую сводку последних сообщений в своей манере."
    " Включи атмосферу, ключевые участки разговора и финальный вывод."
    " Никаких выдуманных фактов и @-упоминаний."
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


def _strip_mentions(text: str) -> str:
    return re.sub(r"@([\w]{1,32})", r"\1", text)


@router.message(Command("roll"))
async def cmd_roll(
    message: types.Message,
    roulette: RouletteService,
):
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Команда доступна только в групповых чатах.")
        return

    result = await roulette.roll(message.chat.id, initiator=str(message.from_user.id))
    if not result.success:
        await message.reply(result.message)


@router.message(Command("rollstats_montly"))
async def cmd_rollstats_monthly(message: types.Message, roulette: RouletteService):
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Команда доступна только в групповых чатах.")
        return
    stats = await roulette.get_stats_monthly(message.chat.id)
    await message.reply(stats)


@router.message(Command("rollstats_total"))
async def cmd_rollstats_total(message: types.Message, roulette: RouletteService):
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Команда доступна только в групповых чатах.")
        return
    stats = await roulette.get_stats_total(message.chat.id)
    await message.reply(stats)


@router.message(Command("summary"))
async def cmd_summary(
    message: types.Message,
    session: AsyncSession,
    settings: SettingsService,
    context: ContextService,
    personas: StylePromptService,
    app_config: AppConfigService,
    usage_limits: UsageLimiter,
):
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

        if requests:
            allowed, counts, exceeded = await usage_limits.consume(message.chat.id, requests)
            if not allowed:
                if "summary" in exceeded:
                    used = counts.get("summary", summary_limit)
                    await message.reply(
                        f"🤖 Лимит сводок на сегодня исчерпан ({used}/{summary_limit})."
                    )
                else:
                    used = counts.get("llm", llm_limit)
                    await message.reply(
                        f"🤖 Лимит запросов к модели исчерпан ({used}/{llm_limit}). Попробуй завтра."
                    )
                return

        turns = await context.get_recent_turns(session, message.chat.id, max_turns)
        if not turns:
            await message.reply("Нечего пересказывать: история пуста.")
            return

        style = str(conf.get("style", "standup"))
        display_map = await personas.get_display_map()
        style_prompts = await personas.get_all()
        style_label = display_map.get(style, display_map.get("standup", style))
        style_prompt = style_prompts.get(style, style_prompts.get("standup", ""))

        system_prompt = _compose_summary_prompt(
            style_label,
            style_prompt,
            base_prompt=summary_prompt_template,
        )
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
        )

        max_answer_tokens = max(200, min(1024, prompt_token_limit // 2))
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
                temperature=float(conf.get("temperature", 0.8) or 0.8),
                top_p=float(conf.get("top_p", 0.9) or 0.9),
            )
        except OpenRouterRateLimitError as exc:
            wait_hint = ""
            if exc.retry_after and exc.retry_after > 0:
                wait_hint = f" Попробуй через ~{int(exc.retry_after)} с."
            await message.reply("🤖 Модель перегружена." + wait_hint)
            return
        except OpenRouterError:
            await message.reply("🤖 LLM вернула ошибку. Попробуй позже.")
            return
        except Exception:
            await message.reply("🤖 Не удалось подготовить сводку.")
            return

        cleaned = apply_moderation(summary_text).strip()
        if not cleaned:
            await message.reply("🤖 Не удалось подготовить сводку.")
            return

        heading = f"<b>Сводка по чату за последние {len(turns)} сообщений</b>"
        safe_body = escape(_strip_mentions(cleaned))
        full_text = heading + "\n\n" + safe_body
        for chunk in _split_message(full_text):
            await message.reply(chunk, allow_sending_without_reply=True)


@router.message(Command("reg"))
async def cmd_reg(message: types.Message, roulette: RouletteService):
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
async def cmd_unreg(message: types.Message, roulette: RouletteService):
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
        await message.reply("Прозвище сброшено.")
    else:
        await settings.set(chat_id, "roulette_custom_title", text)
        await message.reply(f"Новое прозвище установлено: {text}")


@router.message(Command("rolltitle"))
async def cmd_rolltitle(message: types.Message, settings: SettingsService):
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Команда доступна только в групповых чатах.")
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.reply("Использование: /rolltitle новое_прозвище")
        return
    title = args[1].strip()
    await settings.set(message.chat.id, "roulette_custom_title", title)
    await message.reply(f"Новое прозвище установлено: {title}")
