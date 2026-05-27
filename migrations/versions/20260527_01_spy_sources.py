"""Add Gremlin Spy channel intelligence tables

Revision ID: 20260527_01_spy_sources
Revises: 20260518_01_new_games
Create Date: 2026-05-27 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260527_01_spy_sources"
down_revision = "20260518_01_new_games"
branch_labels = None
depends_on = None

json_object = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")
json_array = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "spy_sources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("public_url", sa.Text(), nullable=True),
        sa.Column("reader_mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_seen_external_id", sa.String(length=128), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("metadata_json", json_object, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("username", name="uq_spy_sources_username"),
    )
    op.create_index("ix_spy_sources_username", "spy_sources", ["username"])

    op.create_table(
        "spy_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("spy_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("chat_id", "source_id", name="uq_spy_subscriptions_chat_source"),
    )
    op.create_index("ix_spy_subscriptions_chat_id", "spy_subscriptions", ["chat_id"])
    op.create_index("ix_spy_subscriptions_source_id", "spy_subscriptions", ["source_id"])

    op.create_table(
        "spy_posts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("spy_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_post_id", sa.String(length=128), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("media", json_array, nullable=False, server_default="[]"),
        sa.Column("raw_payload", json_object, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source_id", "external_post_id", name="uq_spy_posts_source_external"),
    )
    op.create_index("ix_spy_posts_source_id", "spy_posts", ["source_id"])

    op.create_table(
        "spy_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "post_id",
            sa.Integer(),
            sa.ForeignKey("spy_posts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("comment_text", sa.Text(), nullable=True),
        sa.Column("delivered_message_id", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("post_id", "chat_id", name="uq_spy_deliveries_post_chat"),
    )
    op.create_index("ix_spy_deliveries_post_id", "spy_deliveries", ["post_id"])
    op.create_index("ix_spy_deliveries_chat_id", "spy_deliveries", ["chat_id"])


def downgrade() -> None:
    op.drop_index("ix_spy_deliveries_chat_id", table_name="spy_deliveries")
    op.drop_index("ix_spy_deliveries_post_id", table_name="spy_deliveries")
    op.drop_table("spy_deliveries")

    op.drop_index("ix_spy_posts_source_id", table_name="spy_posts")
    op.drop_table("spy_posts")

    op.drop_index("ix_spy_subscriptions_source_id", table_name="spy_subscriptions")
    op.drop_index("ix_spy_subscriptions_chat_id", table_name="spy_subscriptions")
    op.drop_table("spy_subscriptions")

    op.drop_index("ix_spy_sources_username", table_name="spy_sources")
    op.drop_table("spy_sources")
