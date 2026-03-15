"""Add user memory and relationship tables

Revision ID: 20260315_01_user_memory
Revises: 2025100601_roulette_participants
Create Date: 2026-03-15 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260315_01_user_memory"
down_revision = "2025100601_roulette_participants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_memory_profiles",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("identity", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("preferences", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("boundaries", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("projects", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("memory_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_message_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("chat_id", "user_id"),
    )

    op.create_table(
        "relationship_states",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("affinity", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("familiarity", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("tension", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("tone_hint", sa.String(length=32), nullable=True),
        sa.Column("last_interaction_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("chat_id", "user_id"),
    )
    op.create_index("ix_messages_chat_user_date", "messages", ["chat_id", "user_id", "date"])


def downgrade() -> None:
    op.drop_index("ix_messages_chat_user_date", table_name="messages")
    op.drop_table("relationship_states")
    op.drop_table("user_memory_profiles")
