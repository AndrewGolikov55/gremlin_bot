# Chat Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-chat memory (facts about members + chat lore) that the bot stores via sidecar, injects as background context, and that admins can view/clear.

**Architecture:** New `ChatMemory` model (one row per chat), methods added to `UserMemoryService`. The sidecar JSON gets a `chat_memory_update` field. Context injection adds a compact block (≤150 tokens) with a "background knowledge, use sparingly" instruction. Admin panel gains a view+clear section on the existing memory page.

**Tech Stack:** SQLAlchemy async (JSONB), Alembic, FastAPI (admin), aiogram (bot), pytest + asyncio.

---

### Task 1: ChatMemory model + Alembic migration

**Files:**
- Modify: `app/models/memory.py`
- Modify: `app/models/__init__.py`
- Create: `migrations/versions/20260417_01_chat_memory.py`

- [ ] **Step 1: Add `ChatMemory` to `app/models/memory.py`**

Append after the `RelationshipState` class:

```python
class ChatMemory(Base):
    __tablename__ = "chat_memories"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    members: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), default=list
    )
    lore: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), default=list
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
```

- [ ] **Step 2: Export `ChatMemory` from `app/models/__init__.py`**

Change the import line:
```python
from .memory import RelationshipState, UserMemoryProfile
```
to:
```python
from .memory import ChatMemory, RelationshipState, UserMemoryProfile
```

Add `"ChatMemory"` to `__all__`.

- [ ] **Step 3: Create migration**

Create `migrations/versions/20260417_01_chat_memory.py`:

```python
"""Add chat_memories table

Revision ID: 20260417_01_chat_memory
Revises: 20260416_01_persona_ux
Create Date: 2026-04-17 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260417_01_chat_memory"
down_revision = "20260416_01_persona_ux"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_memories",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "members",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "lore",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("chat_id"),
    )


def downgrade() -> None:
    op.drop_table("chat_memories")
```

- [ ] **Step 4: Run mypy and ruff**

```bash
cd /home/agolikov/Work/home/gremlin_bot
python -m mypy app/models/memory.py app/models/__init__.py
python -m ruff check app/models/memory.py app/models/__init__.py
```
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add app/models/memory.py app/models/__init__.py migrations/versions/20260417_01_chat_memory.py
git commit -m "feat(chat-memory): add ChatMemory model and migration"
```

---

### Task 2: Extend `SidecarResult` + `parse_sidecar_response`

**Files:**
- Modify: `app/services/user_memory.py`
- Create: `tests/services/test_chat_memory.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_chat_memory.py`:

```python
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
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd /home/agolikov/Work/home/gremlin_bot
python -m pytest tests/services/test_chat_memory.py -v
```
Expected: `AttributeError: 'SidecarResult' has no attribute 'chat_memory'`

- [ ] **Step 3: Add `chat_memory` field to `SidecarResult`**

In `app/services/user_memory.py`, change the `SidecarResult` dataclass from:
```python
@dataclass(slots=True)
class SidecarResult:
    reply: str
    relation: dict[str, Any] | None
    memory: dict[str, Any] | None
    raw_json: dict[str, Any] | None
```
to:
```python
@dataclass(slots=True)
class SidecarResult:
    reply: str
    relation: dict[str, Any] | None
    memory: dict[str, Any] | None
    chat_memory: dict[str, Any] | None
    raw_json: dict[str, Any] | None
```

- [ ] **Step 4: Update `parse_sidecar_response` to extract `chat_memory_update`**

In `parse_sidecar_response`, find the block that extracts `relation` and `memory`:
```python
        reply = str(payload.get("reply") or "").strip()
        relation = payload.get("relationship_update")
        if not isinstance(relation, dict):
            relation = None
        memory = payload.get("memory_update")
        if not isinstance(memory, dict):
            memory = None
```
Change to:
```python
        reply = str(payload.get("reply") or "").strip()
        relation = payload.get("relationship_update")
        if not isinstance(relation, dict):
            relation = None
        memory = payload.get("memory_update")
        if not isinstance(memory, dict):
            memory = None
        chat_memory = payload.get("chat_memory_update")
        if not isinstance(chat_memory, dict):
            chat_memory = None
```

Then update the return at the bottom of `parse_sidecar_response`:
```python
        return SidecarResult(reply=reply, relation=relation, memory=memory, raw_json=payload)
```
to:
```python
        return SidecarResult(reply=reply, relation=relation, memory=memory, chat_memory=chat_memory, raw_json=payload)
```

Also update the two early-return `SidecarResult` calls (the empty-text and non-dict fallbacks) to include `chat_memory=None`:
```python
        return SidecarResult(reply="", relation=None, memory=None, chat_memory=None, raw_json=None)
```
and:
```python
        return SidecarResult(reply=text, relation=None, memory=None, chat_memory=None, raw_json=None)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/services/test_chat_memory.py::test_parse_sidecar_includes_chat_memory tests/services/test_chat_memory.py::test_parse_sidecar_chat_memory_none_when_missing tests/services/test_chat_memory.py::test_parse_sidecar_chat_memory_none_when_invalid_type -v
```
Expected: 3 PASSED.

- [ ] **Step 6: Run full test suite to check for regressions**

```bash
python -m pytest tests/ -v
```
Expected: all existing tests pass.

- [ ] **Step 7: Commit**

```bash
git add app/services/user_memory.py tests/services/test_chat_memory.py
git commit -m "feat(chat-memory): add chat_memory field to SidecarResult"
```

---

### Task 3: Update `get_sidecar_system_suffix`

**Files:**
- Modify: `app/services/user_memory.py`
- Modify: `tests/services/test_chat_memory.py`

- [ ] **Step 1: Add failing test**

Append to `tests/services/test_chat_memory.py`:

```python
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
```

- [ ] **Step 2: Run to verify tests fail**

```bash
python -m pytest tests/services/test_chat_memory.py::test_sidecar_suffix_includes_chat_memory_update_field tests/services/test_chat_memory.py::test_sidecar_suffix_includes_members_and_lore -v
```
Expected: FAIL (field not in suffix yet).

- [ ] **Step 3: Update `get_sidecar_system_suffix` in `app/services/user_memory.py`**

Replace the entire method body. Find:
```python
    def get_sidecar_system_suffix(self) -> str:
        return (
            "Верни только JSON без Markdown: "
            '{"reply":"...","relationship_update":{"rapport_delta":-1..1,'
            '"tone_hint":"neutral|warm|careful|null"},'
            '"memory_update":{"summary":null|"...",'
            '"identity":[],"preferences":[],"boundaries":[]}}. '
```
Change to:
```python
    def get_sidecar_system_suffix(self) -> str:
        return (
            "Верни только JSON без Markdown: "
            '{"reply":"...","relationship_update":{"rapport_delta":-1..1,'
            '"tone_hint":"neutral|warm|careful|null"},'
            '"memory_update":{"summary":null|"...",'
            '"identity":[],"preferences":[],"boundaries":[]},'
            '"chat_memory_update":{"members":[],"lore":[]}}. '
```

Then at the end of the method (after the existing instructions about identity/preferences/boundaries), add before the closing `"Не дублируй..."` line. Find:
```python
            "Не дублируй уже известные факты другими словами — если факт уже есть, не добавляй его снова."
        )
```
Change to:
```python
            "Не дублируй уже известные факты другими словами — если факт уже есть, не добавляй его снова. "
            "chat_memory_update.members — факты о конкретных участниках чата (не о тебе, не об отправителе — "
            "его факты идут в memory_update). "
            "chat_memory_update.lore — общий контекст чата: внутренние шутки, темы, события. "
            "Используй null или [] в chat_memory_update когда обновлять нечего."
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/services/test_chat_memory.py -v
```
Expected: all tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/services/user_memory.py tests/services/test_chat_memory.py
git commit -m "feat(chat-memory): add chat_memory_update to sidecar suffix"
```

---

### Task 4: `_apply_chat_memory_update` + extend `apply_sidecar_update`

**Files:**
- Modify: `app/services/user_memory.py`
- Modify: `tests/services/test_chat_memory.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/services/test_chat_memory.py`:

```python
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
    # 12 existing entries; "fact 0" is the oldest (index 0)
    existing = [f"fact {i}" for i in range(12)]
    cm = _make_chat_mem(members=existing)
    svc._apply_chat_memory_update(cm, {"members": ["brand new fact"], "lore": None})
    assert len(cm.members) == 12
    assert "brand new fact" in cm.members
    assert "fact 0" not in cm.members  # oldest evicted
```

- [ ] **Step 2: Run to verify tests fail**

```bash
python -m pytest tests/services/test_chat_memory.py::test_apply_chat_memory_update_adds_to_members tests/services/test_chat_memory.py::test_apply_chat_memory_update_enforces_fifo_limit -v
```
Expected: `AttributeError: '_apply_chat_memory_update' not defined`.

- [ ] **Step 3: Add `_apply_chat_memory_update` to `UserMemoryService`**

Add this method inside `UserMemoryService`, after `_apply_memory_update`:

```python
    def _apply_chat_memory_update(self, chat_mem: ChatMemory, payload: dict[str, Any]) -> None:
        for bucket in ("members", "lore"):
            raw = payload.get(bucket)
            if not isinstance(raw, list):
                continue
            fresh = [str(v).strip() for v in raw if isinstance(v, str) and str(v).strip()]
            if not fresh:
                continue
            existing = list(getattr(chat_mem, bucket) or [])
            # fresh first + reversed existing → FIFO: oldest entry (index 0) is evicted when at limit
            merged = _merge_unique_strings(fresh + list(reversed(existing)), [], limit=12)
            setattr(chat_mem, bucket, merged)
        chat_mem.updated_at = datetime.utcnow()
```

Also add the `ChatMemory` import at the top of the imports block in `user_memory.py`:
```python
from ..models.memory import ChatMemory, RelationshipState, UserMemoryProfile
```

- [ ] **Step 4: Extend `apply_sidecar_update` to handle `chat_memory`**

In `apply_sidecar_update`, change the early-return guard from:
```python
        if not result.relation and not result.memory:
            return
```
to:
```python
        if not result.relation and not result.memory and not result.chat_memory:
            return
```

Then inside the `async with self._sessionmaker() as session:` block, add handling for `chat_memory` after the existing `result.relation` block:
```python
            if result.chat_memory:
                chat_mem = await session.get(ChatMemory, chat_id)
                if chat_mem is None:
                    chat_mem = ChatMemory(chat_id=chat_id)
                    session.add(chat_mem)
                self._apply_chat_memory_update(chat_mem, result.chat_memory)

            await session.commit()
```

(Move the existing `await session.commit()` to be after the new `chat_memory` block.)

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/services/test_chat_memory.py -v
```
Expected: all tests PASSED.

- [ ] **Step 6: Run full test suite**

```bash
python -m pytest tests/ -v
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/services/user_memory.py tests/services/test_chat_memory.py
git commit -m "feat(chat-memory): add _apply_chat_memory_update and extend apply_sidecar_update"
```

---

### Task 5: `build_chat_memory_block`

**Files:**
- Modify: `app/services/user_memory.py`
- Modify: `tests/services/test_chat_memory.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/services/test_chat_memory.py`:

```python
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
```

- [ ] **Step 2: Run to verify tests fail**

```bash
python -m pytest tests/services/test_chat_memory.py::test_build_chat_memory_block_returns_none_when_no_row -v
```
Expected: `AttributeError: 'UserMemoryService' object has no attribute 'build_chat_memory_block'`.

- [ ] **Step 3: Implement `build_chat_memory_block` in `UserMemoryService`**

Add this method after `build_reaction_memory_block`:

```python
    async def build_chat_memory_block(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        app_conf: dict[str, object],
    ) -> str | None:
        if not self.is_enabled(app_conf):
            return None

        chat_mem = await session.get(ChatMemory, chat_id)
        if chat_mem is None:
            return None

        members = list(chat_mem.members or [])
        lore = list(chat_mem.lore or [])
        if not members and not lore:
            return None

        _FOOTER = (
            "Это фоновые знания — ты их просто знаешь как участник чата. "
            "Используй только когда органично вписывается в разговор. Не перечисляй без повода."
        )
        _PER_BUCKET_CAP = 55  # tokens per bucket

        def _pick(entries: list[str]) -> str:
            chosen: list[str] = []
            used = 0
            for entry in reversed(entries):  # most recent entry last → iterate newest-first
                t = _estimate_tokens(entry)
                if used + t > _PER_BUCKET_CAP:
                    break
                chosen.append(entry)
                used += t
            return "; ".join(reversed(chosen))

        lines: list[str] = []
        if members:
            packed = _pick(members)
            if packed:
                lines.append(f"members: {packed}")
        if lore:
            packed = _pick(lore)
            if packed:
                lines.append(f"lore: {packed}")

        if not lines:
            return None

        return "## Факты о чате (фоновые знания)\n" + "\n".join(lines) + "\n\n" + _FOOTER
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/services/test_chat_memory.py -v
```
Expected: all tests PASSED.

- [ ] **Step 5: Run full test suite**

```bash
python -m pytest tests/ -v
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/services/user_memory.py tests/services/test_chat_memory.py
git commit -m "feat(chat-memory): add build_chat_memory_block"
```

---

### Task 6: Wire chat memory into `router_triggers.py`

**Files:**
- Modify: `app/bot/router_triggers.py`

There are three handlers that build `context_blocks` and call `apply_sidecar_update`. Each needs the chat memory block added to `context_blocks`.

- [ ] **Step 1: Update `handle_focus_reply` — context_blocks**

Find (around line 196):
```python
        memory_block = None
        if personalization_enabled and message.from_user:
            speaker_name = message.from_user.username or message.from_user.full_name
            memory_block = await memory.build_user_memory_block(
                session,
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                query_text=focus_text or raw_focus,
                app_conf=app_conf,
                speaker_name=speaker_name,
                exclude_message_id=message.message_id,
            )
        if personalization_enabled and message.from_user and memory.sidecar_enabled(app_conf):
            system_prompt += "\n\n" + memory.get_sidecar_system_suffix()
        messages_for_llm = build_messages(
            system_prompt,
            turns,
            max_turns,
            prompt_token_limit,
            context_blocks=[memory_block] if memory_block else None,
        )
```

Change to:
```python
        memory_block = None
        if personalization_enabled and message.from_user:
            speaker_name = message.from_user.username or message.from_user.full_name
            memory_block = await memory.build_user_memory_block(
                session,
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                query_text=focus_text or raw_focus,
                app_conf=app_conf,
                speaker_name=speaker_name,
                exclude_message_id=message.message_id,
            )
        chat_memory_block = await memory.build_chat_memory_block(
            session,
            chat_id=message.chat.id,
            app_conf=app_conf,
        )
        if personalization_enabled and message.from_user and memory.sidecar_enabled(app_conf):
            system_prompt += "\n\n" + memory.get_sidecar_system_suffix()
        _ctx = [b for b in [memory_block, chat_memory_block] if b]
        messages_for_llm = build_messages(
            system_prompt,
            turns,
            max_turns,
            prompt_token_limit,
            context_blocks=_ctx or None,
        )
```

Note: `handle_focus_reply` has two calls to `build_messages` (one for vision, one for text). Find the second one near line 236:
```python
                context_blocks=[memory_block] if memory_block else None,
```
Change to:
```python
                context_blocks=_ctx or None,
```

- [ ] **Step 2: Update `handle_photo_reply` — context_blocks**

Find (around line 505):
```python
    memory_block = None
    if personalization_enabled and message.from_user:
        speaker_name = message.from_user.username or message.from_user.full_name
        memory_block = await memory.build_user_memory_block(
            session,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            query_text=focus_text or raw_focus or _message_storage_text(message),
            app_conf=app_conf,
            speaker_name=speaker_name,
            exclude_message_id=message.message_id,
        )
    if personalization_enabled and message.from_user and memory.sidecar_enabled(app_conf):
        system_prompt += "\n\n" + memory.get_sidecar_system_suffix()
    ...
    messages_for_llm = build_vision_messages(
        ...
        context_blocks=[memory_block] if memory_block else None,
    )
```

Change to:
```python
    memory_block = None
    if personalization_enabled and message.from_user:
        speaker_name = message.from_user.username or message.from_user.full_name
        memory_block = await memory.build_user_memory_block(
            session,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            query_text=focus_text or raw_focus or _message_storage_text(message),
            app_conf=app_conf,
            speaker_name=speaker_name,
            exclude_message_id=message.message_id,
        )
    chat_memory_block = await memory.build_chat_memory_block(
        session,
        chat_id=message.chat.id,
        app_conf=app_conf,
    )
    if personalization_enabled and message.from_user and memory.sidecar_enabled(app_conf):
        system_prompt += "\n\n" + memory.get_sidecar_system_suffix()
    _ctx = [b for b in [memory_block, chat_memory_block] if b]
    ...
    messages_for_llm = build_vision_messages(
        ...
        context_blocks=_ctx or None,
    )
```

- [ ] **Step 3: Update `_handle_voice_message` — context_blocks**

Find (around line 964):
```python
    context_blocks: list[str] = []
    if memory_block:
        context_blocks.append(memory_block)
```

Change to:
```python
    context_blocks: list[str] = []
    if memory_block:
        context_blocks.append(memory_block)
    chat_memory_block = await memory.build_chat_memory_block(
        session,
        chat_id=chat_id,
        app_conf=app_conf,
    )
    if chat_memory_block:
        context_blocks.append(chat_memory_block)
```

- [ ] **Step 4: Run full test suite**

```bash
python -m pytest tests/ -v
```
Expected: all pass.

- [ ] **Step 5: Run mypy on the modified file**

```bash
python -m mypy app/bot/router_triggers.py
```
Expected: no errors (this file is in the mypy exclude list, so it will return clean).

- [ ] **Step 6: Commit**

```bash
git add app/bot/router_triggers.py
git commit -m "feat(chat-memory): inject chat memory block in direct-reply context"
```

---

### Task 7: Admin panel — view and clear chat memory

**Files:**
- Modify: `app/admin/router.py`

- [ ] **Step 1: Add `ChatMemory` import to `app/admin/router.py`**

Find:
```python
from ..models.memory import RelationshipState, UserMemoryProfile
```
Change to:
```python
from ..models.memory import ChatMemory, RelationshipState, UserMemoryProfile
```

- [ ] **Step 2: Update `chat_memory_view` to fetch and render chat-level memory**

Find the `chat_memory_view` endpoint (around line 204). Change from:
```python
    @router.get("/chats/{chat_id}/memory", response_class=HTMLResponse)
    async def chat_memory_view(
        chat_id: int,
        token: str = Depends(require_token),
        session: AsyncSession = Depends(get_session),
    ) -> str:
        chat = await session.get(Chat, chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="Chat not found")

        rows = (
            await session.execute(...)
        ).all()
        body = _render_memory_users_body(chat, rows, token)
        return HTMLResponse(_render_page(f"Память чата {chat.id}", token, "chats", body))
```
to:
```python
    @router.get("/chats/{chat_id}/memory", response_class=HTMLResponse)
    async def chat_memory_view(
        chat_id: int,
        token: str = Depends(require_token),
        session: AsyncSession = Depends(get_session),
    ) -> str:
        chat = await session.get(Chat, chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="Chat not found")

        rows = (
            await session.execute(
                select(UserMemoryProfile, User, RelationshipState)
                .outerjoin(User, User.tg_id == UserMemoryProfile.user_id)
                .outerjoin(
                    RelationshipState,
                    and_(
                        RelationshipState.chat_id == UserMemoryProfile.chat_id,
                        RelationshipState.user_id == UserMemoryProfile.user_id,
                    ),
                )
                .where(UserMemoryProfile.chat_id == chat_id)
                .order_by(UserMemoryProfile.updated_at.desc())
            )
        ).all()
        chat_mem = await session.get(ChatMemory, chat_id)
        body = _render_memory_users_body(chat, rows, chat_mem, token)
        return HTMLResponse(_render_page(f"Память чата {chat.id}", token, "chats", body))
```

- [ ] **Step 3: Add three clear endpoints**

Add after the `chat_memory_user_reset` endpoint (around line 289):

```python
    @router.post("/chats/{chat_id}/chat-memory/clear-members", response_class=HTMLResponse)
    async def chat_memory_clear_members(
        chat_id: int,
        token: str = Depends(require_token),
        session: AsyncSession = Depends(get_session),
    ) -> str:
        chat = await session.get(Chat, chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        chat_mem = await session.get(ChatMemory, chat_id)
        if chat_mem is not None:
            chat_mem.members = []
            await session.commit()
        rows = (await session.execute(
            select(UserMemoryProfile, User, RelationshipState)
            .outerjoin(User, User.tg_id == UserMemoryProfile.user_id)
            .outerjoin(RelationshipState, and_(
                RelationshipState.chat_id == UserMemoryProfile.chat_id,
                RelationshipState.user_id == UserMemoryProfile.user_id,
            ))
            .where(UserMemoryProfile.chat_id == chat_id)
            .order_by(UserMemoryProfile.updated_at.desc())
        )).all()
        chat_mem_fresh = await session.get(ChatMemory, chat_id)
        body = _render_memory_users_body(chat, rows, chat_mem_fresh, token, note="Участники очищены.")
        return HTMLResponse(_render_page(f"Память чата {chat.id}", token, "chats", body))

    @router.post("/chats/{chat_id}/chat-memory/clear-lore", response_class=HTMLResponse)
    async def chat_memory_clear_lore(
        chat_id: int,
        token: str = Depends(require_token),
        session: AsyncSession = Depends(get_session),
    ) -> str:
        chat = await session.get(Chat, chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        chat_mem = await session.get(ChatMemory, chat_id)
        if chat_mem is not None:
            chat_mem.lore = []
            await session.commit()
        rows = (await session.execute(
            select(UserMemoryProfile, User, RelationshipState)
            .outerjoin(User, User.tg_id == UserMemoryProfile.user_id)
            .outerjoin(RelationshipState, and_(
                RelationshipState.chat_id == UserMemoryProfile.chat_id,
                RelationshipState.user_id == UserMemoryProfile.user_id,
            ))
            .where(UserMemoryProfile.chat_id == chat_id)
            .order_by(UserMemoryProfile.updated_at.desc())
        )).all()
        chat_mem_fresh = await session.get(ChatMemory, chat_id)
        body = _render_memory_users_body(chat, rows, chat_mem_fresh, token, note="Лор очищен.")
        return HTMLResponse(_render_page(f"Память чата {chat.id}", token, "chats", body))

    @router.post("/chats/{chat_id}/chat-memory/clear", response_class=HTMLResponse)
    async def chat_memory_clear_all(
        chat_id: int,
        token: str = Depends(require_token),
        session: AsyncSession = Depends(get_session),
    ) -> str:
        chat = await session.get(Chat, chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        chat_mem = await session.get(ChatMemory, chat_id)
        if chat_mem is not None:
            chat_mem.members = []
            chat_mem.lore = []
            await session.commit()
        rows = (await session.execute(
            select(UserMemoryProfile, User, RelationshipState)
            .outerjoin(User, User.tg_id == UserMemoryProfile.user_id)
            .outerjoin(RelationshipState, and_(
                RelationshipState.chat_id == UserMemoryProfile.chat_id,
                RelationshipState.user_id == UserMemoryProfile.user_id,
            ))
            .where(UserMemoryProfile.chat_id == chat_id)
            .order_by(UserMemoryProfile.updated_at.desc())
        )).all()
        body = _render_memory_users_body(chat, rows, None, token, note="Память чата очищена.")
        return HTMLResponse(_render_page(f"Память чата {chat.id}", token, "chats", body))
```

- [ ] **Step 4: Update `_render_memory_users_body` to accept and render chat memory**

The function signature currently is:
```python
def _render_memory_users_body(
    chat: Chat,
    rows: list[tuple[UserMemoryProfile, User | None, RelationshipState | None]],
    token: str | None,
) -> str:
```

Change to:
```python
def _render_memory_users_body(
    chat: Chat,
    rows: list[tuple[UserMemoryProfile, User | None, RelationshipState | None]],
    chat_mem: ChatMemory | None,
    token: str | None,
    *,
    note: str | None = None,
) -> str:
```

Add `_render_chat_memory_section()` at the top of the return, after the `settings_url` line. The full new `_render_memory_users_body`:

```python
def _render_memory_users_body(
    chat: Chat,
    rows: list[tuple[UserMemoryProfile, User | None, RelationshipState | None]],
    chat_mem: ChatMemory | None,
    token: str | None,
    *,
    note: str | None = None,
) -> str:
    settings_url = _build_url(f"/admin/chats/{chat.id}", token)
    items = []
    for profile, user, relation in rows:
        username = user.username if user and user.username else str(profile.user_id)
        detail_url = _build_url(f"/admin/chats/{chat.id}/memory/{profile.user_id}", token)
        rapport = f"{_relationship_rapport(relation):.2f}" if relation else "0.00"
        relation_kind = _relationship_kind_label(relation)
        visible_count = str(_visible_memory_count(profile))
        items.append(
            "<tr>"
            f"<td>{escape(username)}</td>"
            f"<td>{escape(visible_count)}</td>"
            f"<td>{escape(rapport)}</td>"
            f"<td>{escape(relation_kind)}</td>"
            f"<td>{escape(profile.updated_at.strftime('%Y-%m-%d %H:%M:%S') if profile.updated_at else '—')}</td>"
            f"<td class='text-end'><a class='btn btn-sm btn-outline-primary' href='{escape(detail_url)}'>Открыть</a></td>"
            "</tr>"
        )

    table = (
        "<div class='table-responsive'><table class='table table-hover align-middle'>"
        "<thead><tr><th>Пользователь</th><th>Записей</th><th>Отношение</th><th>Тип отношений</th><th>Обновлено</th><th></th></tr></thead>"
        f"<tbody>{''.join(items)}</tbody></table></div>"
        if items
        else "<div class='alert alert-info'>Память по участникам ещё не накопилась.</div>"
    )

    chat_mem_section = _render_chat_memory_section(chat, chat_mem, token)
    note_html = f"<div class='alert alert-success'>{escape(note)}</div>" if note else ""

    return (
        "<div class='container py-4'>"
        "<div class='d-flex justify-content-between align-items-center mb-3'>"
        f"<div><h1 class='h3 mb-0'>Память участников</h1><div class='text-muted'>{escape(chat.title)}</div></div>"
        f"<a class='btn btn-outline-secondary' href='{escape(settings_url)}'>← Настройки чата</a>"
        "</div>"
        f"{note_html}"
        f"{chat_mem_section}"
        f"{table}"
        "</div>"
    )
```

- [ ] **Step 5: Add `_render_chat_memory_section` helper**

Add this function before `_render_memory_users_body`:

```python
def _render_chat_memory_section(chat: Chat, chat_mem: ChatMemory | None, token: str | None) -> str:
    members = list(chat_mem.members or []) if chat_mem else []
    lore = list(chat_mem.lore or []) if chat_mem else []

    if not members and not lore:
        return ""

    clear_members_url = _build_url(f"/admin/chats/{chat.id}/chat-memory/clear-members", token)
    clear_lore_url = _build_url(f"/admin/chats/{chat.id}/chat-memory/clear-lore", token)
    clear_all_url = _build_url(f"/admin/chats/{chat.id}/chat-memory/clear", token)

    def _list_items(entries: list[str]) -> str:
        if not entries:
            return "<span class='text-muted'>(пусто)</span>"
        return "".join(f"<li class='mb-1'>{escape(e)}</li>" for e in entries)

    members_count = f"{len(members)}/12"
    lore_count = f"{len(lore)}/12"

    return (
        "<div class='card mb-4'>"
        "<div class='card-header fw-bold'>Память чата</div>"
        "<div class='card-body'>"
        "<div class='row'>"
        "<div class='col-md-6'>"
        f"<h6>Участники ({escape(members_count)})</h6>"
        f"<ul class='list-unstyled small'>{_list_items(members)}</ul>"
        f"<form method='post' action='{escape(clear_members_url)}'>"
        "<button class='btn btn-sm btn-outline-warning' type='submit'>Очистить участников</button>"
        "</form>"
        "</div>"
        "<div class='col-md-6'>"
        f"<h6>Лор чата ({escape(lore_count)})</h6>"
        f"<ul class='list-unstyled small'>{_list_items(lore)}</ul>"
        f"<form method='post' action='{escape(clear_lore_url)}'>"
        "<button class='btn btn-sm btn-outline-warning' type='submit'>Очистить лор</button>"
        "</form>"
        "</div>"
        "</div>"
        f"<form method='post' action='{escape(clear_all_url)}' class='mt-2'>"
        "<button class='btn btn-sm btn-outline-danger' type='submit'>Очистить всё</button>"
        "</form>"
        "</div>"
        "</div>"
    )
```

- [ ] **Step 6: Run full test suite**

```bash
python -m pytest tests/ -v
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/admin/router.py
git commit -m "feat(chat-memory): add admin panel section for viewing and clearing chat memory"
```

---

### Task 8: Final checks and release prep

- [ ] **Step 1: Run ruff on all changed files**

```bash
cd /home/agolikov/Work/home/gremlin_bot
python -m ruff check app/services/user_memory.py app/models/memory.py app/models/__init__.py
```
Expected: no errors (admin and bot files are in the ruff exclude list).

- [ ] **Step 2: Run mypy on checked files**

```bash
python -m mypy app/services/user_memory.py app/models/memory.py app/models/__init__.py
```
Expected: no errors.

- [ ] **Step 3: Run full test suite one final time**

```bash
python -m pytest tests/ -v
```
Expected: all pass.

- [ ] **Step 4: Update CHANGELOG.md**

Add a new `## [0.5.2]` section at the top of `CHANGELOG.md`:

```markdown
## [0.5.2] - 2026-04-17

### Added

- Память чата: бот теперь запоминает факты об участниках (`members`) и общий контекст чата (`lore`). Когда кто-то просит запомнить факт о другом участнике — он сохраняется в память чата, а не игнорируется. Факты используются как фоновые знания — без постоянного упоминания.
- Лимит: 12 записей на каждую категорию (FIFO при переполнении), каждый блок памяти занимает не более ~150 токенов.
- Админ-раздел «Память участников» теперь показывает раздел «Память чата» с кнопками очистки по категориям.
```

- [ ] **Step 5: Update version in `pyproject.toml`**

Change:
```toml
version = "0.5.1"
```
to:
```toml
version = "0.5.2"
```

- [ ] **Step 6: Final commit**

```bash
git add CHANGELOG.md pyproject.toml
git commit -m "chore(release): v0.5.2"
```
