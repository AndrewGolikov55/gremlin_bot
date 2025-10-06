"""Add roulette participants and title codes

Revision ID: 2025100601_roulette_participants
Revises: 20251002_02_roulette
Create Date: 2025-10-06 22:45:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "2025100601_roulette_participants"
down_revision = "20251002_02_roulette"
branch_labels = None
depends_on = None


TITLE_MAP = {
    "пидор": "pidor",
    "скуф": "skuf",
    "красавчик": "beauty",
    "клоун": "clown",
}


def upgrade() -> None:
    op.add_column("roulette_winners", sa.Column("title_code", sa.String(length=64), nullable=True))

    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, title FROM roulette_winners")).fetchall()
    for row in rows:
        title = (row.title or "").strip().lower()
        code = TITLE_MAP.get(title, "custom")
        connection.execute(
            sa.text("UPDATE roulette_winners SET title_code = :code WHERE id = :id"),
            {"code": code, "id": row.id},
        )

    op.alter_column("roulette_winners", "title_code", nullable=False)

    op.create_table(
        "roulette_participants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("registered_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("chat_id", "user_id", name="uq_roulette_participants_chat_user"),
    )
    op.create_index(
        "ix_roulette_participants_chat",
        "roulette_participants",
        ["chat_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_roulette_participants_chat", table_name="roulette_participants")
    op.drop_table("roulette_participants")
    op.drop_column("roulette_winners", "title_code")
