"""Harden roulette uniqueness

Revision ID: 20260322_01_roulette_idempotency
Revises: 20260315_01_user_memory
Create Date: 2026-03-22 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260322_01_roulette_idempotency"
down_revision = "20260315_01_user_memory"
branch_labels = None
depends_on = None


def _has_unique_constraint(table_name: str, constraint_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    constraints = inspector.get_unique_constraints(table_name)
    return any(item.get("name") == constraint_name for item in constraints)


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM roulette_winners
        WHERE id IN (
            SELECT id
            FROM (
                SELECT
                    id,
                    row_number() OVER (
                        PARTITION BY chat_id, won_at
                        ORDER BY created_at ASC, id ASC
                    ) AS rn
                FROM roulette_winners
            ) ranked
            WHERE ranked.rn > 1
        )
        """
    )
    op.execute(
        """
        DELETE FROM roulette_participants
        WHERE id IN (
            SELECT id
            FROM (
                SELECT
                    id,
                    row_number() OVER (
                        PARTITION BY chat_id, user_id
                        ORDER BY registered_at ASC, id ASC
                    ) AS rn
                FROM roulette_participants
            ) ranked
            WHERE ranked.rn > 1
        )
        """
    )
    if not _has_unique_constraint("roulette_winners", "uq_roulette_winners_chat_day"):
        op.create_unique_constraint(
            "uq_roulette_winners_chat_day",
            "roulette_winners",
            ["chat_id", "won_at"],
        )
    if not _has_unique_constraint("roulette_participants", "uq_roulette_participants_chat_user"):
        op.create_unique_constraint(
            "uq_roulette_participants_chat_user",
            "roulette_participants",
            ["chat_id", "user_id"],
        )


def downgrade() -> None:
    if _has_unique_constraint("roulette_participants", "uq_roulette_participants_chat_user"):
        op.drop_constraint("uq_roulette_participants_chat_user", "roulette_participants", type_="unique")
    if _has_unique_constraint("roulette_winners", "uq_roulette_winners_chat_day"):
        op.drop_constraint("uq_roulette_winners_chat_day", "roulette_winners", type_="unique")
