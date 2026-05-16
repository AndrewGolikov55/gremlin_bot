"""Add quote_week_rounds table

Revision ID: 20260516_04_quote_week_rounds
Revises: 20260516_01_dice_game
Create Date: 2026-05-16 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260516_04_quote_week_rounds"
down_revision = "20260516_01_dice_game"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quote_week_rounds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("poll_id", sa.String(length=64), nullable=False),
        sa.Column("poll_message_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "options",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("winner_user_id", sa.BigInteger(), nullable=True),
        sa.Column("winner_option_idx", sa.Integer(), nullable=True),
        sa.Column(
            "final_counts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "chat_id", "week_start", name="uq_quote_week_rounds_chat_week"
        ),
        sa.UniqueConstraint("poll_id", name="uq_quote_week_rounds_poll_id"),
    )
    op.create_index(
        "ix_quote_week_rounds_chat_id", "quote_week_rounds", ["chat_id"]
    )
    op.create_index(
        "ix_quote_week_rounds_poll_id", "quote_week_rounds", ["poll_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_quote_week_rounds_poll_id", table_name="quote_week_rounds")
    op.drop_index("ix_quote_week_rounds_chat_id", table_name="quote_week_rounds")
    op.drop_table("quote_week_rounds")
