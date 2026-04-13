"""Add photo references to messages

Revision ID: 20260413_01_messages_photo_refs
Revises: 20260322_01_roulette_idempotency
Create Date: 2026-04-13 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260413_01_messages_photo_refs"
down_revision = "20260322_01_roulette_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("tg_file_id", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column("media_group_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_messages_chat_media_group",
        "messages",
        ["chat_id", "media_group_id"],
        unique=False,
        postgresql_where=sa.text("media_group_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_messages_chat_media_group", table_name="messages")
    op.drop_column("messages", "media_group_id")
    op.drop_column("messages", "tg_file_id")
