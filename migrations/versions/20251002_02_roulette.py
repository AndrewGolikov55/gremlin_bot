"""Add roulette winners table

Revision ID: 20251002_02_roulette
Revises: 20251002_01_global_settings
Create Date: 2025-10-02 13:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20251002_02_roulette"
down_revision = "20251002_01_global_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roulette_winners",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("won_at", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    op.create_index("ix_roulette_winners_chat_date", "roulette_winners", ["chat_id", "won_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_roulette_winners_chat_date", table_name="roulette_winners")
    op.drop_table("roulette_winners")
