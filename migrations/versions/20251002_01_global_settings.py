"""Introduce global app settings table

Revision ID: 20251002_01_global_settings
Revises: 20251001_01_style_personas
Create Date: 2025-10-02 12:00:00.000000
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "20251002_01_global_settings"
down_revision = "20251001_01_style_personas"
branch_labels = None
depends_on = None


GLOBAL_DEFAULTS = {
    "context_max_turns": 100,
    "max_length": 0,
    "context_max_prompt_tokens": 32000,
    "interject_p": 5,
    "interject_cooldown": 60,
}


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    connection = op.get_bind()
    insert_stmt = sa.text(
        "INSERT INTO app_settings (key, value) VALUES (:key, CAST(:value AS jsonb))"
    )
    for key, value in GLOBAL_DEFAULTS.items():
        connection.execute(insert_stmt, {"key": key, "value": json.dumps(value)})

    delete_stmt = sa.text("DELETE FROM chat_settings WHERE key = :key")
    for key in GLOBAL_DEFAULTS.keys():
        connection.execute(delete_stmt, {"key": key})


def downgrade() -> None:
    op.drop_table("app_settings")
