"""Add tables for new stateful games: spy, akinator, wordchain, rapbattle, storychain

Revision ID: 20260518_01_new_games
Revises: 20260516_05_ship_cache_clear
Create Date: 2026-05-18 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision = "20260518_01_new_games"
down_revision = "20260516_05_ship_cache_clear"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------- spy ----------
    op.create_table(
        "spy_rounds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("initiator_user_id", sa.BigInteger(), nullable=False),
        sa.Column("location", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("spy_user_id", sa.BigInteger(), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("ends_at", sa.DateTime(), nullable=True),
        sa.Column("vote_poll_id", sa.String(length=64), nullable=True),
        sa.Column("vote_message_id", sa.BigInteger(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_spy_rounds_chat_id", "spy_rounds", ["chat_id"])
    op.create_index("ix_spy_rounds_chat_status", "spy_rounds", ["chat_id", "status"])
    op.create_index(
        "ux_spy_rounds_chat_open",
        "spy_rounds",
        ["chat_id"],
        unique=True,
        postgresql_where=text("status IN ('lobby','active','voting')"),
    )
    op.create_table(
        "spy_players",
        sa.Column(
            "round_id", sa.Integer(),
            sa.ForeignKey("spy_rounds.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("is_spy", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("revealed_at", sa.DateTime(), nullable=True),
    )

    # ---------- akinator ----------
    op.create_table(
        "akinator_rounds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("initiator_user_id", sa.BigInteger(), nullable=False),
        sa.Column("target_user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("questions_asked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("winner_user_id", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_akinator_rounds_chat_id", "akinator_rounds", ["chat_id"])
    op.create_index("ix_akinator_rounds_chat_status", "akinator_rounds", ["chat_id", "status"])
    op.create_index(
        "ux_akinator_rounds_chat_active",
        "akinator_rounds",
        ["chat_id"],
        unique=True,
        postgresql_where=text("status = 'active'"),
    )
    op.create_table(
        "akinator_questions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "round_id", sa.Integer(),
            sa.ForeignKey("akinator_rounds.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("asker_user_id", sa.BigInteger(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.String(length=8), nullable=False),
        sa.Column("asked_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_akinator_questions_round_id", "akinator_questions", ["round_id"])

    # ---------- wordchain ----------
    op.create_table(
        "wordchain_rounds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("last_word", sa.String(length=64), nullable=True),
        sa.Column("last_user_id", sa.BigInteger(), nullable=True),
        sa.Column("last_word_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("loser_user_id", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_wordchain_rounds_chat_id", "wordchain_rounds", ["chat_id"])
    op.create_index("ix_wordchain_rounds_chat_status", "wordchain_rounds", ["chat_id", "status"])
    op.create_index(
        "ux_wordchain_rounds_chat_active",
        "wordchain_rounds",
        ["chat_id"],
        unique=True,
        postgresql_where=text("status = 'active'"),
    )
    op.create_table(
        "wordchain_words",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "round_id", sa.Integer(),
            sa.ForeignKey("wordchain_rounds.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("word", sa.String(length=64), nullable=False),
        sa.Column("played_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("round_id", "word", name="uq_wordchain_words_round_word"),
    )
    op.create_index("ix_wordchain_words_round_id", "wordchain_words", ["round_id"])

    # ---------- rapbattle ----------
    op.create_table(
        "rapbattle_rounds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("challenger_a_id", sa.BigInteger(), nullable=False),
        sa.Column("challenger_b_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "verses",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("poll_id", sa.String(length=64), nullable=True),
        sa.Column("poll_message_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("winner_user_id", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_rapbattle_rounds_chat_id", "rapbattle_rounds", ["chat_id"])
    op.create_index("ix_rapbattle_rounds_chat_status", "rapbattle_rounds", ["chat_id", "status"])
    op.create_index(
        "ux_rapbattle_rounds_chat_open",
        "rapbattle_rounds",
        ["chat_id"],
        unique=True,
        postgresql_where=text("status IN ('generating','voting')"),
    )

    # ---------- storychain ----------
    op.create_table(
        "storychain_rounds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("seed", sa.Text(), nullable=False),
        sa.Column("target_contributions", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("seed_message_id", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("finalised_at", sa.DateTime(), nullable=True),
        sa.Column("finale", sa.Text(), nullable=True),
    )
    op.create_index("ix_storychain_rounds_chat_id", "storychain_rounds", ["chat_id"])
    op.create_index("ix_storychain_rounds_chat_status", "storychain_rounds", ["chat_id", "status"])
    op.create_index(
        "ux_storychain_rounds_chat_active",
        "storychain_rounds",
        ["chat_id"],
        unique=True,
        postgresql_where=text("status IN ('active','finalising')"),
    )
    op.create_table(
        "storychain_contributions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "round_id", sa.Integer(),
            sa.ForeignKey("storychain_rounds.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_storychain_contributions_round_id", "storychain_contributions", ["round_id"])


def downgrade() -> None:
    op.drop_index("ix_storychain_contributions_round_id", table_name="storychain_contributions")
    op.drop_table("storychain_contributions")
    op.drop_index("ux_storychain_rounds_chat_active", table_name="storychain_rounds")
    op.drop_index("ix_storychain_rounds_chat_status", table_name="storychain_rounds")
    op.drop_index("ix_storychain_rounds_chat_id", table_name="storychain_rounds")
    op.drop_table("storychain_rounds")

    op.drop_index("ux_rapbattle_rounds_chat_open", table_name="rapbattle_rounds")
    op.drop_index("ix_rapbattle_rounds_chat_status", table_name="rapbattle_rounds")
    op.drop_index("ix_rapbattle_rounds_chat_id", table_name="rapbattle_rounds")
    op.drop_table("rapbattle_rounds")

    op.drop_index("ix_wordchain_words_round_id", table_name="wordchain_words")
    op.drop_table("wordchain_words")
    op.drop_index("ux_wordchain_rounds_chat_active", table_name="wordchain_rounds")
    op.drop_index("ix_wordchain_rounds_chat_status", table_name="wordchain_rounds")
    op.drop_index("ix_wordchain_rounds_chat_id", table_name="wordchain_rounds")
    op.drop_table("wordchain_rounds")

    op.drop_index("ix_akinator_questions_round_id", table_name="akinator_questions")
    op.drop_table("akinator_questions")
    op.drop_index("ux_akinator_rounds_chat_active", table_name="akinator_rounds")
    op.drop_index("ix_akinator_rounds_chat_status", table_name="akinator_rounds")
    op.drop_index("ix_akinator_rounds_chat_id", table_name="akinator_rounds")
    op.drop_table("akinator_rounds")

    op.drop_table("spy_players")
    op.drop_index("ux_spy_rounds_chat_open", table_name="spy_rounds")
    op.drop_index("ix_spy_rounds_chat_status", table_name="spy_rounds")
    op.drop_index("ix_spy_rounds_chat_id", table_name="spy_rounds")
    op.drop_table("spy_rounds")
