"""Add roast_runs table

Revision ID: 20260516_03_roast_runs
Revises: 20260516_01_dice_game
Create Date: 2026-05-16 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260516_03_roast_runs"
down_revision = "20260516_01_dice_game"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roast_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("target_user_id", sa.BigInteger(), nullable=False),
        sa.Column("initiator_user_id", sa.BigInteger(), nullable=False),
        sa.Column("target_username", sa.String(length=255), nullable=True),
        sa.Column(
            "run_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_roast_runs_chat_id", "roast_runs", ["chat_id"])
    op.create_index(
        "ix_roast_runs_chat_run_at",
        "roast_runs",
        ["chat_id", "run_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_roast_runs_chat_run_at", table_name="roast_runs")
    op.drop_index("ix_roast_runs_chat_id", table_name="roast_runs")
    op.drop_table("roast_runs")
