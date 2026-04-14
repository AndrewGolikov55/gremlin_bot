from __future__ import annotations

import logging

import sqlalchemy as sa
from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.chat import Chat, ChatSetting
from ..utils.version import get_version, read_release_notes
from .app_config import AppConfigService

logger = logging.getLogger(__name__)

LAST_BROADCASTED_KEY = "last_broadcasted_version"


class ReleaseBroadcaster:
    def __init__(
        self,
        *,
        bot: Bot,
        sessionmaker: async_sessionmaker[AsyncSession],
        app_config: AppConfigService,
    ) -> None:
        self._bot = bot
        self._sessionmaker = sessionmaker
        self._app_config = app_config

    async def broadcast_if_new_version(self) -> None:
        current_version = get_version()
        last_version = await self._app_config.get(LAST_BROADCASTED_KEY)

        if last_version == current_version:
            return

        notes = read_release_notes()
        if not notes:
            logger.info(
                "Release %s has no user-facing notes; skipping broadcast",
                current_version,
            )
            await self._app_config.set(LAST_BROADCASTED_KEY, current_version)
            return

        if last_version is None:
            logger.info(
                "First release tracked (%s); marking as broadcasted without sending",
                current_version,
            )
            await self._app_config.set(LAST_BROADCASTED_KEY, current_version)
            return

        targets = await self._active_chat_ids()
        if not targets:
            logger.info("No active chats for release %s broadcast", current_version)
            await self._app_config.set(LAST_BROADCASTED_KEY, current_version)
            return

        delivered = 0
        for chat_id in targets:
            try:
                await self._bot.send_message(chat_id, notes, parse_mode=None)
                delivered += 1
            except Exception:
                logger.exception(
                    "Failed to broadcast release %s to chat %s",
                    current_version,
                    chat_id,
                )

        logger.info(
            "Release %s broadcasted to %s/%s chats",
            current_version,
            delivered,
            len(targets),
        )
        await self._app_config.set(LAST_BROADCASTED_KEY, current_version)

    async def _active_chat_ids(self) -> list[int]:
        """Return IDs of group/channel chats where the bot is active.

        Filters applied:
        - Chat.id < 0: skip private chats (Telegram private-chat IDs are positive)
        - Chat.is_active: skip chats the bot was kicked from / can no longer reach
        - ChatSetting["is_active"] != False: skip chats where the admin disabled the bot
        """
        async with self._sessionmaker() as session:
            disabled_subq = select(ChatSetting.chat_id).where(
                ChatSetting.key == "is_active",
                ChatSetting.value.cast(sa.String) == "false",
            )
            result = await session.execute(
                select(Chat.id).where(
                    Chat.is_active.is_(True),
                    Chat.id < 0,
                    Chat.id.not_in(disabled_subq),
                )
            )
            return [row[0] for row in result.all()]
