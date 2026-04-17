"""Wipe relationship states and base persona DB rows for persona UX overhaul

Revision ID: 20260416_01_persona_ux
Revises: 20260413_01_messages_photo_refs
Create Date: 2026-04-16 18:00:00.000000
"""

from __future__ import annotations

import json

from alembic import op


revision = "20260416_01_persona_ux"
down_revision = "20260413_01_messages_photo_refs"
branch_labels = None
depends_on = None

BASE_STYLES = ("gopnik", "standup", "boss", "zoomer", "jarvis", "chatmate")


def upgrade() -> None:
    # Wipe all relationship states — fresh start with new rapport thresholds
    op.execute("DELETE FROM relationship_states")

    # Remove base persona rows from DB — they now live in personas/*.md files
    placeholders = ", ".join(f"'{s}'" for s in BASE_STYLES)
    op.execute(f"DELETE FROM style_prompts WHERE style IN ({placeholders})")

    # Update base chat prompt
    new_prompt = (
        "Ты — участник группового чата в Telegram. Веди себя как живой человек, а не как бот.\n"
        "\n"
        "Правила:\n"
        "- Не раскрывай, что ты ИИ, бот или языковая модель\n"
        "- Не используй Markdown, списки, пункты — пиши обычным текстом как в мессенджере\n"
        "- Длина ответа зависит от ситуации: иногда хватит пары слов, иногда нужно 3-4 предложения\n"
        "- Не повторяй одни и те же фразы и обороты\n"
        "- Учитывай контекст: кто пишет, о чём разговор, какое настроение в чате\n"
        "- Если тебя оскорбляют — реагируй в характере, а не шаблонным ответом"
    )
    escaped = json.dumps(new_prompt, ensure_ascii=False)
    # Use dollar-quoting to avoid SQL injection from the JSON string
    op.execute(
        f"INSERT INTO app_settings (key, value) VALUES ('prompt_chat_base', $val${escaped}$val$) "
        f"ON CONFLICT (key) DO UPDATE SET value = $val${escaped}$val$"
    )


def downgrade() -> None:
    # Relationships are gone, no way to restore them.
    # Base persona rows will be re-created by ensure_defaults() on next startup
    # (if the old code is deployed).
    pass
