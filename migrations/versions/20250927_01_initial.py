"""Initial schema

Revision ID: 20250927_01_initial
Revises: 
Create Date: 2025-09-27 14:35:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "20250927_01_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chats",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tg_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("is_admin_cached", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "chat_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("chat_id", "key", name="uq_chat_settings_key"),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("reply_to_id", sa.BigInteger(), nullable=True),
        sa.Column("date", sa.DateTime(), nullable=False),
        sa.Column("is_bot", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("chat_id", "message_id", name="uq_messages_chat_message"),
    )

    op.create_index("ix_chat_settings_chat_id", "chat_settings", ["chat_id"])
    op.create_index("ix_messages_chat_id", "messages", ["chat_id"])
    op.create_index("ix_messages_date", "messages", ["date"])
    op.create_index("ix_messages_user_id", "messages", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_messages_user_id", table_name="messages")
    op.drop_index("ix_messages_date", table_name="messages")
    op.drop_index("ix_messages_chat_id", table_name="messages")
    op.drop_table("messages")

    op.drop_index("ix_chat_settings_chat_id", table_name="chat_settings")
    op.drop_table("chat_settings")

    op.drop_table("users")
    op.drop_table("chats")
