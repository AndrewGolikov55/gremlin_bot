from __future__ import annotations

import logging
import random
from typing import Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SetMessageReaction
from aiogram.types import Message as TgMessage, ReactionTypeEmoji
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..services.context import ChatTurn
from ..services.llm.client import (
    LLMError,
    LLMRateLimitError,
    generate as llm_generate,
    resolve_llm_options,
)
from ..services.usage_limits import UsageLimiter
from ..services.user_memory import UserMemoryService


logger = logging.getLogger("reactions")

# Official Bot API list from ReactionTypeEmoji:
# https://core.telegram.org/bots/api#reactiontypeemoji
REACTION_EMOJI_POOL: tuple[str, ...] = (
    "❤",
    "👍",
    "👎",
    "🔥",
    "🥰",
    "👏",
    "😁",
    "🤔",
    "🤯",
    "😱",
    "🤬",
    "😢",
    "🎉",
    "🤩",
    "🤮",
    "💩",
    "🙏",
    "👌",
    "🕊",
    "🤡",
    "🥱",
    "🥴",
    "😍",
    "🐳",
    "❤‍🔥",
    "🌚",
    "🌭",
    "💯",
    "🤣",
    "⚡",
    "🍌",
    "🏆",
    "💔",
    "🤨",
    "😐",
    "🍓",
    "🍾",
    "💋",
    "🖕",
    "😈",
    "😴",
    "😭",
    "🤓",
    "👻",
    "👨‍💻",
    "👀",
    "🎃",
    "🙈",
    "😇",
    "😨",
    "🤝",
    "✍",
    "🤗",
    "🫡",
    "🎅",
    "🎄",
    "☃",
    "💅",
    "🤪",
    "🗿",
    "🆒",
    "💘",
    "🙉",
    "🦄",
    "😘",
    "💊",
    "🙊",
    "😎",
    "👾",
    "🤷‍♂",
    "🤷",
    "🤷‍♀",
    "😡",
)

DEFAULT_REACTION_PROMPT = (
    "Выбери одну реакцию на сообщение с учётом краткого контекста. "
    "Ответь ровно одним эмодзи из списка: {pool}. "
    "Если уместной реакции нет, верни только '-'. Без слов."
)


class ReactionService:
    def __init__(
        self,
        *,
        bot: Bot,
        sessionmaker: async_sessionmaker[AsyncSession],
        usage_limits: UsageLimiter,
        memory: UserMemoryService,
    ) -> None:
        self.bot = bot
        self.sessionmaker = sessionmaker
        self.usage_limits = usage_limits
        self.memory = memory

    async def maybe_react_to_message(
        self,
        message: TgMessage,
        conf: dict[str, object],
        app_conf: dict[str, object],
        turns: Sequence[ChatTurn],
    ) -> None:
        probability = int(app_conf.get("reaction_p", 0) or 0)
        if probability <= 0:
            return

        text = (message.text or message.caption or "").strip()
        if not text or not message.from_user or message.from_user.is_bot:
            return

        roll = random.uniform(0, 100)
        if roll > probability:
            logger.debug(
                "Skip reaction chat=%s message=%s roll=%.2f p=%s",
                message.chat.id,
                message.message_id,
                roll,
                probability,
            )
            return

        if not await self._consume_llm_budget(message.chat.id, app_conf):
            logger.debug(
                "LLM limit reached for reaction chat=%s message=%s",
                message.chat.id,
                message.message_id,
            )
            return

        memory_block = None
        if bool(conf.get("personalization_enabled", True)):
            async with self.sessionmaker() as session:
                memory_block = await self.memory.build_reaction_memory_block(
                    session,
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    query_text=text,
                    app_conf=app_conf,
                    speaker_name=message.from_user.username or message.from_user.full_name,
                    exclude_message_id=message.message_id,
                )

        emoji = await self._generate_reaction_emoji(
            text=text,
            memory_block=memory_block,
            chat_block=_build_chat_context_block(
                turns,
                current_user_id=message.from_user.id,
                current_text=text,
            ),
            app_conf=app_conf,
        )
        if not emoji:
            return

        try:
            await self.bot(
                SetMessageReaction(
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    reaction=[ReactionTypeEmoji(emoji=emoji)],
                    is_big=False,
                )
            )
            logger.info(
                "Reaction set chat=%s message=%s emoji=%s",
                message.chat.id,
                message.message_id,
                emoji,
            )
        except TelegramForbiddenError:
            logger.debug("No permission to set reaction chat=%s", message.chat.id)
        except TelegramBadRequest as exc:
            logger.debug(
                "Failed to set reaction chat=%s message=%s emoji=%s: %s",
                message.chat.id,
                message.message_id,
                emoji,
                exc,
            )
        except Exception:
            logger.exception(
                "Unexpected error while setting reaction chat=%s message=%s",
                message.chat.id,
                message.message_id,
            )

    async def _generate_reaction_emoji(
        self,
        *,
        text: str,
        memory_block: str | None,
        chat_block: str | None,
        app_conf: dict[str, object],
    ) -> str | None:
        pool_text = " ".join(REACTION_EMOJI_POOL)
        messages = [
            {
                "role": "system",
                "content": DEFAULT_REACTION_PROMPT.format(pool=pool_text),
            }
        ]
        if memory_block:
            messages.append({"role": "user", "content": memory_block})
        if chat_block:
            messages.append({"role": "user", "content": chat_block})
        messages.append({"role": "user", "content": f"Сообщение: {text}"})

        provider = resolve_llm_options(app_conf)

        try:
            raw = await llm_generate(
                messages,
                temperature=1.0,
                top_p=0.9,
                max_tokens=None,
                provider=provider,
            )
        except LLMRateLimitError as exc:
            logger.debug("Rate limit during reaction selection retry_after=%s", exc.retry_after)
            return _fallback_reaction_emoji(text, memory_block, chat_block)
        except LLMError:
            logger.exception("LLM request failed during reaction selection")
            return _fallback_reaction_emoji(text, memory_block, chat_block)

        emoji = _extract_reaction_emoji(raw, REACTION_EMOJI_POOL)
        if emoji:
            logger.debug("Reaction selected by model emoji=%s", emoji)
            return emoji
        fallback = _fallback_reaction_emoji(text, memory_block, chat_block)
        if fallback:
            logger.debug("Reaction selected by heuristic fallback emoji=%s", fallback)
        return fallback

    async def _consume_llm_budget(self, chat_id: int | None, app_conf: dict[str, object]) -> bool:
        if chat_id is None:
            return True
        limit_raw = app_conf.get("llm_daily_limit", 0) or 0
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return True
        allowed, _, _ = await self.usage_limits.consume(chat_id, [("llm", limit)])
        return allowed


def _extract_reaction_emoji(raw: str, pool: Sequence[str]) -> str | None:
    text = (raw or "").strip()
    if not text or text == "-":
        return None
    for emoji in pool:
        if emoji in text:
            return emoji
    return None


def _fallback_reaction_emoji(
    text: str,
    memory_block: str | None,
    chat_block: str | None,
) -> str | None:
    normalized = f"{memory_block or ''}\n{chat_block or ''}\n{text}".lower()
    if any(token in normalized for token in ("люблю", "love", "любов", "обожаю")):
        return "❤️"
    if any(token in normalized for token in ("аниме", "наруто", "каваки", "боруто")):
        return "🤓"
    if any(token in normalized for token in ("пиво", "мёд", "сосиск", "еда", "мыльные пузыри", "шарики")):
        return "🤣"
    if any(token in normalized for token in ("лох", "долбо", "хуйл", "пидор", "блять", "нахуй")):
        return "🤡"
    if "?" in text:
        return "🤔"
    if any(token in normalized for token in ("прости", "извини", "сорри")):
        return "🤝"
    if any(token in normalized for token in ("поздрав", "ура", "вау", "круто")):
        return "🎉"
    if any(token in normalized for token in ("плохо", "груст", "печаль", "жаль")):
        return "😢"
    if any(token in normalized for token in ("йоу", "хаха", "ахах", "лол")):
        return "😁"
    return "👍"


def _build_chat_context_block(
    turns: Sequence[ChatTurn],
    *,
    current_user_id: int | None,
    current_text: str,
) -> str | None:
    lines: list[str] = []
    normalized_current = " ".join(current_text.split()).strip().lower()

    for turn in reversed(list(turns)):
        text = " ".join((turn.text or "").replace("\n", " ").split()).strip()
        if not text or text.startswith("/"):
            continue
        if (
            normalized_current
            and turn.user_id == current_user_id
            and text.lower() == normalized_current
        ):
            continue
        speaker = (turn.speaker or "unknown").strip()
        lines.append(f"{speaker}: {text[:100]}")
        if len(lines) >= 5:
            break

    if not lines:
        return None
    lines.reverse()
    return "Последние реплики в чате:\n" + "\n".join(lines)
