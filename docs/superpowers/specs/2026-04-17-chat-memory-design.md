# Chat Memory Design

**Date:** 2026-04-17  
**Status:** Approved

## Problem

Users in group chats often ask the bot to remember facts about other chat members ("запомни, что denzel_cw любит срать стоя"). The existing `user_memory_profiles` table stores facts only about the message sender — so the bot correctly ignores third-party facts. There is no place to store chat-level knowledge shared among all participants.

## Goal

Add a per-chat memory layer that stores:
- Facts about specific chat members (third parties)
- General chat lore: running jokes, shared context, recurring topics

The bot should use this knowledge as passive background, not reference it in every message.

## Approach

Embed new functionality in the existing `UserMemoryService`. New table, new sidecar field, new context block, new admin section.

---

## Data Model

New model in `app/models/memory.py`:

```python
class ChatMemory(Base):
    __tablename__ = "chat_memories"

    chat_id:    Mapped[int]        = mapped_column(BigInteger, primary_key=True)
    members:    Mapped[list[str]]  = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=list)
    lore:       Mapped[list[str]]  = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=list)
    updated_at: Mapped[datetime]   = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

**Limits per bucket:**
- Max 12 entries each
- Max 180 characters per entry
- New entries appended with dedup (`_merge_unique_strings`); when at capacity, oldest entry is dropped (FIFO)

**`members`** — facts about named chat participants: `"denzel_cw любит срать стоя"`, `"Kolya — модератор"`  
**`lore`** — general chat context: `"по вечерам обсуждают CS2"`, `"внутренняя шутка про унитаз"`

---

## Sidecar

`SidecarResult` gains a new field:

```python
@dataclass(slots=True)
class SidecarResult:
    reply: str
    relation: dict[str, Any] | None
    memory: dict[str, Any] | None
    chat_memory: dict[str, Any] | None   # NEW
    raw_json: dict[str, Any] | None
```

Expected JSON from LLM:

```json
{
  "reply": "...",
  "relationship_update": {...},
  "memory_update": {...},
  "chat_memory_update": {
    "members": ["denzel_cw любит срать стоя"],
    "lore": null
  }
}
```

### Sidecar system suffix additions

Added to `get_sidecar_system_suffix()`:
- `chat_memory_update` field description in the JSON schema hint
- Rules:
  - `members` — only facts about **named chat participants** (not the bot, not the sender themselves — those go to `memory_update`)
  - `lore` — general chat context, inside jokes, recurring themes
  - Use `null` or `[]` when nothing to update
  - Do not duplicate already-known facts

### apply_sidecar_update

`apply_sidecar_update` extended to accept `chat_id` and apply `chat_memory_update` to `ChatMemory` row (upsert).

---

## Context Injection

New method `build_chat_memory_block(session, *, chat_id, app_conf) -> str | None`.

Returns `None` if `ChatMemory` row doesn't exist or both buckets are empty.

**Hard token cap: 150 tokens (~600 chars).** Most-recent entries shown first. Format:

```
## Факты о чате (фоновые знания)
members: denzel_cw любит срать стоя; Kolya — модератор
lore: по вечерам тут обсуждают CS2

Это фоновые знания — ты их просто знаешь как участник чата. Используй только когда органично вписывается в разговор. Не перечисляй без повода.
```

Injected as a `context_block` (before history) alongside the existing user/group memory blocks.

**Token budget comparison:**
- User memory block: up to 500 tokens
- Group/social memory block: up to 260 tokens  
- Chat memory block: up to 150 tokens ← new, most compact

---

## Admin Panel

New section "Память чата" in the chat card, below existing memory sections.

**Layout:**
```
Память чата
───────────────────────────────────────
Участники (N/12)      Лор чата (N/12)
• denzel_cw любит…    • по вечерам CS2
• Kolya — модератор   (пусто)

[Очистить участников] [Очистить лор] [Очистить всё]
```

- Entries are **read-only** (viewing and reset only; no manual editing)
- Three clear buttons: per-bucket and combined
- Sections hidden if both buckets are empty (no clutter for new chats)

---

## Migration

New migration `migrations/versions/20260417_01_chat_memory.py`:

```sql
CREATE TABLE chat_memories (
    chat_id    BIGINT PRIMARY KEY,
    members    JSONB NOT NULL DEFAULT '[]',
    lore       JSONB NOT NULL DEFAULT '[]',
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);
```

No data wipes. No changes to existing tables.

---

## Files Changed

| File | Change |
|------|--------|
| `app/models/memory.py` | Add `ChatMemory` model |
| `app/services/user_memory.py` | `SidecarResult.chat_memory`, `build_chat_memory_block()`, extend `apply_sidecar_update()`, extend `get_sidecar_system_suffix()` |
| `app/bot/router_triggers.py` | Pass `chat_id` to `apply_sidecar_update`, include chat memory block in `context_blocks` |
| `app/admin/router.py` | New "Память чата" section with clear endpoints |
| `migrations/versions/20260417_01_chat_memory.py` | New table |
| `tests/services/test_chat_memory.py` | Tests for build/apply/merge logic |

---

## Tests

- `test_build_chat_memory_block_returns_none_when_empty`
- `test_build_chat_memory_block_respects_token_cap`
- `test_apply_chat_memory_update_merges_and_dedupes`
- `test_apply_chat_memory_update_enforces_fifo_limit`
- `test_parse_sidecar_response_includes_chat_memory`
