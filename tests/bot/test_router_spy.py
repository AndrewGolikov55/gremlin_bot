from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.router_spy import cmd_spy_add, cmd_spy_list, cmd_spy_remove
from app.services.spy.subscription_service import SpyAdminRequired, SpySubscriptionView


class FakeSpySubscriptions:
    def __init__(self) -> None:
        self.add_calls: list[dict[str, object]] = []
        self.remove_calls: list[dict[str, object]] = []
        self.items: list[SpySubscriptionView] = []
        self.admin_error = False
        self.unavailable_error = False

    async def add_subscription(self, **kwargs: object) -> object:
        if self.admin_error:
            raise SpyAdminRequired("blocked")
        if self.unavailable_error:
            raise RuntimeError("Gremlin Spy MTProto reader is not configured")
        self.add_calls.append(kwargs)
        return SimpleNamespace(id=1)

    async def remove_subscription(self, **kwargs: object) -> bool:
        if self.admin_error:
            raise SpyAdminRequired("blocked")
        self.remove_calls.append(kwargs)
        return True

    async def list_subscriptions(self, *, chat_id: int) -> list[SpySubscriptionView]:
        return self.items


def _message(chat_type: str = "supergroup", user_id: int | None = 42) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=-1001, type=chat_type),
        from_user=SimpleNamespace(id=user_id) if user_id is not None else None,
        reply=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_spy_add_requires_group_chat() -> None:
    service = FakeSpySubscriptions()
    message = _message(chat_type="private")

    await cmd_spy_add(message, SimpleNamespace(args="@channel"), service)

    message.reply.assert_awaited_once_with("Эта команда доступна только в групповых чатах.")
    assert service.add_calls == []


@pytest.mark.asyncio
async def test_spy_add_passes_channel_to_subscription_service() -> None:
    service = FakeSpySubscriptions()
    message = _message()

    await cmd_spy_add(message, SimpleNamespace(args="https://t.me/gospodindirectorpivs"), service)

    assert service.add_calls == [
        {
            "chat_id": -1001,
            "source_ref": "https://t.me/gospodindirectorpivs",
            "actor_user_id": 42,
        }
    ]
    message.reply.assert_awaited_once_with("Источник добавлен в Gremlin Spy ✅")


@pytest.mark.asyncio
async def test_spy_add_reports_admin_boundary() -> None:
    service = FakeSpySubscriptions()
    service.admin_error = True
    message = _message()

    await cmd_spy_add(message, SimpleNamespace(args="@channel"), service)

    message.reply.assert_awaited_once_with("Gremlin Spy могут настраивать только админы чата.")


@pytest.mark.asyncio
async def test_spy_add_reports_runtime_unavailable() -> None:
    service = FakeSpySubscriptions()
    service.unavailable_error = True
    message = _message()

    await cmd_spy_add(message, SimpleNamespace(args="https://t.me/gospodindirectorpivs"), service)

    message.reply.assert_awaited_once_with(
        "Gremlin Spy пока не настроен: нет Telegram API credentials для чтения каналов."
    )
    assert service.add_calls == []


@pytest.mark.asyncio
async def test_spy_remove_disables_subscription() -> None:
    service = FakeSpySubscriptions()
    message = _message()

    await cmd_spy_remove(message, SimpleNamespace(args="@gospodindirectorpivs"), service)

    assert service.remove_calls == [
        {"chat_id": -1001, "source_ref": "@gospodindirectorpivs", "actor_user_id": 42}
    ]
    message.reply.assert_awaited_once_with("Источник отключён от этого чата ✅")


@pytest.mark.asyncio
async def test_spy_list_renders_enabled_sources() -> None:
    service = FakeSpySubscriptions()
    service.items = [
        SpySubscriptionView(
            subscription_id=1,
            source_id=2,
            username="gospodindirectorpivs",
            title="Господин директор Пивс",
            public_url="https://t.me/gospodindirectorpivs",
            reader_mode="mtproto",
            last_seen_external_id="123",
        )
    ]
    message = _message()

    await cmd_spy_list(message, service)

    message.reply.assert_awaited_once_with(
        "Gremlin Spy источники:\n"
        "• Господин директор Пивс (@gospodindirectorpivs) — https://t.me/gospodindirectorpivs"
    )
