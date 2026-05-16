"""Truncate ship_results cache after rendered_text format change in v0.12.1

Revision ID: 20260516_05_truncate_ship_results
Revises: 20260516_04_quote_week_rounds
Create Date: 2026-05-16 00:00:00.000000
"""
from __future__ import annotations

from alembic import op

revision = "20260516_05_truncate_ship_results"
down_revision = "20260516_04_quote_week_rounds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM ship_results")


def downgrade() -> None:
    # Pure cache invalidation — nothing to restore.
    pass
