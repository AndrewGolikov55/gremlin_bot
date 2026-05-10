"""Add monthly_champions table and chat_memories.monthly_champion column

Revision ID: 20260510_01_monthly_champion
Revises: 20260509_01_guess_who_said_it
Create Date: 2026-05-10 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260510_01_monthly_champion"
down_revision = "20260509_01_guess_who_said_it"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "monthly_champions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "tied_with",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "daily_title_snapshot",
            sa.String(length=255),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "announced_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("chat_id", "period_start", name="uq_monthly_champions_chat_period"),
    )
    op.create_index("ix_monthly_champions_chat_id", "monthly_champions", ["chat_id"])
    op.create_index(
        "ix_monthly_champions_chat_period",
        "monthly_champions",
        ["chat_id", "period_start"],
    )

    op.add_column(
        "chat_memories",
        sa.Column(
            "monthly_champion",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("chat_memories", "monthly_champion")
    op.drop_index("ix_monthly_champions_chat_period", table_name="monthly_champions")
    op.drop_index("ix_monthly_champions_chat_id", table_name="monthly_champions")
    op.drop_table("monthly_champions")
