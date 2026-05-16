"""Add ship_results table for /ship compatibility cache

Revision ID: 20260516_02_ship_results
Revises: 20260516_03_roast_runs
Create Date: 2026-05-16 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260516_02_ship_results"
down_revision = "20260516_03_roast_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ship_results",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id_a", sa.BigInteger(), nullable=False),
        sa.Column("user_id_b", sa.BigInteger(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("rendered_text", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "computed_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "chat_id", "user_id_a", "user_id_b", name="uq_ship_results_pair"
        ),
    )
    op.create_index("ix_ship_results_chat_id", "ship_results", ["chat_id"])
    op.create_index("ix_ship_results_computed_at", "ship_results", ["computed_at"])


def downgrade() -> None:
    op.drop_index("ix_ship_results_computed_at", table_name="ship_results")
    op.drop_index("ix_ship_results_chat_id", table_name="ship_results")
    op.drop_table("ship_results")
