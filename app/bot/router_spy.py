from __future__ import annotations

from typing import Protocol

from aiogram import Router, types
from aiogram.filters import Command, CommandObject

from app.services.spy.subscription_service import SpyAdminRequired, SpySubscriptionView

router = Router(name="spy")


class SpySubscriptionManager(Protocol):
    async def add_subscription(self, **kwargs: object) -> object: ...

    async def remove_subscription(self, **kwargs: object) -> bool: ...

    async def list_subscriptions(self, *, chat_id: int) -> list[SpySubscriptionView]: ...


@router.message(Command("spy_add"))
async def cmd_spy_add(
    message: types.Message,
    command: CommandObject,
    spy_subscriptions: SpySubscriptionManager,
) -> None:
    if not await _ensure_group_message(message):
        return
    source_ref = (command.args or "").strip()
    if not source_ref:
        await message.reply("Использование: /spy_add @channel или /spy_add https://t.me/channel")
        return
    user_id = _actor_user_id(message)
    if user_id is None:
        await message.reply("Не вижу пользователя, который вызвал команду.")
        return
    try:
        await spy_subscriptions.add_subscription(
            chat_id=message.chat.id,
            source_ref=source_ref,
            actor_user_id=user_id,
        )
    except SpyAdminRequired:
        await message.reply("Gremlin Spy могут настраивать только админы чата.")
        return
    except RuntimeError as exc:
        if "MTProto reader is not configured" in str(exc):
            await message.reply(
                "Gremlin Spy пока не настроен: нет Telegram API credentials для чтения каналов."
            )
            return
        raise
    except ValueError as exc:
        await message.reply(f"Не могу добавить источник: {exc}")
        return
    await message.reply("Источник добавлен в Gremlin Spy ✅")


@router.message(Command("spy_remove"))
async def cmd_spy_remove(
    message: types.Message,
    command: CommandObject,
    spy_subscriptions: SpySubscriptionManager,
) -> None:
    if not await _ensure_group_message(message):
        return
    source_ref = (command.args or "").strip()
    if not source_ref:
        await message.reply("Использование: /spy_remove @channel")
        return
    user_id = _actor_user_id(message)
    if user_id is None:
        await message.reply("Не вижу пользователя, который вызвал команду.")
        return
    try:
        removed = await spy_subscriptions.remove_subscription(
            chat_id=message.chat.id,
            source_ref=source_ref,
            actor_user_id=user_id,
        )
    except SpyAdminRequired:
        await message.reply("Gremlin Spy могут настраивать только админы чата.")
        return
    except ValueError as exc:
        await message.reply(f"Не могу отключить источник: {exc}")
        return
    await message.reply("Источник отключён от этого чата ✅" if removed else "Такого источника в этом чате нет.")


@router.message(Command("spy_list"))
async def cmd_spy_list(
    message: types.Message,
    spy_subscriptions: SpySubscriptionManager,
) -> None:
    if not await _ensure_group_message(message):
        return
    items = await spy_subscriptions.list_subscriptions(chat_id=message.chat.id)
    if not items:
        await message.reply("В этом чате нет Gremlin Spy источников.")
        return
    lines = ["Gremlin Spy источники:"]
    lines.extend(f"• {_format_subscription_item(item)}" for item in items)
    await message.reply("\n".join(lines))


async def _ensure_group_message(message: types.Message) -> bool:
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("Эта команда доступна только в групповых чатах.")
        return False
    return True


def _actor_user_id(message: types.Message) -> int | None:
    user = getattr(message, "from_user", None)
    user_id = getattr(user, "id", None)
    return user_id if isinstance(user_id, int) else None


def _format_subscription_item(item: SpySubscriptionView) -> str:
    title = item.title or item.username or f"source:{item.source_id}"
    username = f" (@{item.username})" if item.username else ""
    link = f" — {item.public_url}" if item.public_url else ""
    return f"{title}{username}{link}"
