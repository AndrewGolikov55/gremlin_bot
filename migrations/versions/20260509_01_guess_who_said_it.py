"""Add guess_rounds and roulette_score_adjustments tables

Revision ID: 20260509_01_guess_who_said_it
Revises: 20260417_01_chat_memory
Create Date: 2026-05-09 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260509_01_guess_who_said_it"
down_revision = "20260417_01_chat_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guess_rounds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("poll_id", sa.String(length=64), nullable=False),
        sa.Column("chat_message_id", sa.BigInteger(), nullable=False),
        sa.Column("source_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("author_user_id", sa.BigInteger(), nullable=False),
        sa.Column("correct_option_id", sa.Integer(), nullable=False),
        sa.Column(
            "option_user_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("first_winner_user_id", sa.BigInteger(), nullable=True),
        sa.Column("first_winner_at", sa.DateTime(), nullable=True),
        sa.Column("selection_mode", sa.String(length=16), nullable=False, server_default="llm"),
    )
    op.create_index("ix_guess_rounds_chat_id", "guess_rounds", ["chat_id"])
    op.create_index("ix_guess_rounds_poll_id", "guess_rounds", ["poll_id"], unique=True)
    op.create_index("ix_guess_rounds_started_at", "guess_rounds", ["started_at"])

    op.create_table(
        "roulette_score_adjustments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_roulette_score_adj_chat_id", "roulette_score_adjustments", ["chat_id"])
    op.create_index("ix_roulette_score_adj_user_id", "roulette_score_adjustments", ["user_id"])
    op.create_index("ix_roulette_score_adj_created_at", "roulette_score_adjustments", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_roulette_score_adj_created_at", table_name="roulette_score_adjustments")
    op.drop_index("ix_roulette_score_adj_user_id", table_name="roulette_score_adjustments")
    op.drop_index("ix_roulette_score_adj_chat_id", table_name="roulette_score_adjustments")
    op.drop_table("roulette_score_adjustments")

    op.drop_index("ix_guess_rounds_started_at", table_name="guess_rounds")
    op.drop_index("ix_guess_rounds_poll_id", table_name="guess_rounds")
    op.drop_index("ix_guess_rounds_chat_id", table_name="guess_rounds")
    op.drop_table("guess_rounds")
