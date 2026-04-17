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
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "lore",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("chat_id"),
    )


def downgrade() -> None:
    op.drop_table("chat_memories")
