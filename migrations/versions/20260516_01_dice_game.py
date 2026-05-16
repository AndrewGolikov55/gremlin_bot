"""Add dice_rounds table

Revision ID: 20260516_01_dice_game
Revises: 20260510_01_monthly_champion
Create Date: 2026-05-16 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260516_01_dice_game"
down_revision = "20260510_01_monthly_champion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dice_rounds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "picks",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("dice_value", sa.Integer(), nullable=False),
        sa.Column("won", sa.Boolean(), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("rolled_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("dice_message_id", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_dice_rounds_chat_id", "dice_rounds", ["chat_id"])
    op.create_index("ix_dice_rounds_user_id", "dice_rounds", ["user_id"])
    op.create_index("ix_dice_rounds_rolled_at", "dice_rounds", ["rolled_at"])


def downgrade() -> None:
    op.drop_index("ix_dice_rounds_rolled_at", table_name="dice_rounds")
    op.drop_index("ix_dice_rounds_user_id", table_name="dice_rounds")
    op.drop_index("ix_dice_rounds_chat_id", table_name="dice_rounds")
    op.drop_table("dice_rounds")
