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


# ── get_sidecar_system_suffix ────────────────────────────────────────────────

def test_sidecar_suffix_includes_chat_memory_update_field() -> None:
    svc = _make_svc()
    suffix = svc.get_sidecar_system_suffix()
    assert "chat_memory_update" in suffix


def test_sidecar_suffix_includes_members_and_lore() -> None:
    svc = _make_svc()
    suffix = svc.get_sidecar_system_suffix()
    assert "members" in suffix
    assert "lore" in suffix


# ── _apply_chat_memory_update ────────────────────────────────────────────────

def _make_chat_mem(members: list[str] | None = None, lore: list[str] | None = None) -> MagicMock:
    cm = MagicMock(spec=[])
    cm.members = list(members or [])
    cm.lore = list(lore or [])
    return cm


def test_apply_chat_memory_update_adds_to_members() -> None:
    svc = _make_svc()
    cm = _make_chat_mem()
    svc._apply_chat_memory_update(cm, {"members": ["denzel любит CS"], "lore": None})
    assert "denzel любит CS" in cm.members


def test_apply_chat_memory_update_adds_to_lore() -> None:
    svc = _make_svc()
    cm = _make_chat_mem()
    svc._apply_chat_memory_update(cm, {"members": None, "lore": ["вечером играют в CS2"]})
    assert "вечером играют в CS2" in cm.lore


def test_apply_chat_memory_update_deduplicates() -> None:
    svc = _make_svc()
    cm = _make_chat_mem(members=["denzel любит CS"])
    svc._apply_chat_memory_update(cm, {"members": ["denzel любит CS"], "lore": None})
    assert cm.members.count("denzel любит CS") == 1


def test_apply_chat_memory_update_enforces_fifo_limit() -> None:
    svc = _make_svc()
    # 12 existing entries in newest-first order; "fact 0" is the oldest (index 11)
    existing = [f"fact {i}" for i in range(11, -1, -1)]  # ["fact 11", "fact 10", ..., "fact 0"]
    cm = _make_chat_mem(members=existing)
    svc._apply_chat_memory_update(cm, {"members": ["brand new fact"], "lore": None})
    assert len(cm.members) == 12
    assert "brand new fact" in cm.members
    assert "fact 0" not in cm.members  # oldest (tail) evicted


# ── build_chat_memory_block ──────────────────────────────────────────────────

def _make_session(chat_mem: MagicMock | None = None) -> AsyncMock:
    session = AsyncMock()
    session.get = AsyncMock(return_value=chat_mem)
    return session


@pytest.mark.asyncio
async def test_build_chat_memory_block_returns_none_when_no_row() -> None:
    svc = _make_svc()
    session = _make_session(chat_mem=None)
    result = await svc.build_chat_memory_block(session, chat_id=1, app_conf={"user_memory_enabled": True})
    assert result is None


@pytest.mark.asyncio
async def test_build_chat_memory_block_returns_none_when_empty() -> None:
    svc = _make_svc()
    cm = _make_chat_mem()
    session = _make_session(chat_mem=cm)
    result = await svc.build_chat_memory_block(session, chat_id=1, app_conf={"user_memory_enabled": True})
    assert result is None


@pytest.mark.asyncio
async def test_build_chat_memory_block_contains_member_fact() -> None:
    svc = _make_svc()
    cm = _make_chat_mem(members=["denzel любит CS"])
    session = _make_session(chat_mem=cm)
    result = await svc.build_chat_memory_block(session, chat_id=1, app_conf={"user_memory_enabled": True})
    assert result is not None
    assert "denzel любит CS" in result


@pytest.mark.asyncio
async def test_build_chat_memory_block_contains_background_instruction() -> None:
    svc = _make_svc()
    cm = _make_chat_mem(members=["fact"])
    session = _make_session(chat_mem=cm)
    result = await svc.build_chat_memory_block(session, chat_id=1, app_conf={"user_memory_enabled": True})
    assert result is not None
    assert "фоновые знания" in result.lower()


@pytest.mark.asyncio
async def test_build_chat_memory_block_respects_token_cap() -> None:
    svc = _make_svc()
    many = ["x" * 180 for _ in range(12)]
    cm = _make_chat_mem(members=many, lore=many)
    session = _make_session(chat_mem=cm)
    result = await svc.build_chat_memory_block(session, chat_id=1, app_conf={"user_memory_enabled": True})
    assert result is not None
    assert _estimate_tokens(result) <= 200


@pytest.mark.asyncio
async def test_build_chat_memory_block_returns_none_when_disabled() -> None:
    svc = _make_svc()
    cm = _make_chat_mem(members=["fact"])
    session = _make_session(chat_mem=cm)
    result = await svc.build_chat_memory_block(session, chat_id=1, app_conf={"user_memory_enabled": False})
    assert result is None
