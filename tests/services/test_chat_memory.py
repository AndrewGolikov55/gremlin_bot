from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.user_memory import UserMemoryService, _estimate_tokens


def _make_svc() -> UserMemoryService:
    return UserMemoryService.__new__(UserMemoryService)


# ── SidecarResult / parse_sidecar_response ──────────────────────────────────

def test_parse_sidecar_includes_chat_memory() -> None:
    svc = _make_svc()
    raw = (
        '{"reply":"ok","relationship_update":null,"memory_update":null,'
        '"chat_memory_update":{"members":["denzel любит CS"],"lore":["вечером играют"]}}'
    )
    result = svc.parse_sidecar_response(raw)
    assert result.reply == "ok"
    assert result.chat_memory == {"members": ["denzel любит CS"], "lore": ["вечером играют"]}


def test_parse_sidecar_chat_memory_none_when_missing() -> None:
    svc = _make_svc()
    raw = '{"reply":"ok","relationship_update":null,"memory_update":null}'
    result = svc.parse_sidecar_response(raw)
    assert result.chat_memory is None


def test_parse_sidecar_chat_memory_none_when_invalid_type() -> None:
    svc = _make_svc()
    raw = '{"reply":"ok","chat_memory_update":"invalid"}'
    result = svc.parse_sidecar_response(raw)
    assert result.chat_memory is None
