"""Introduce style personas and prompts

Revision ID: 20251001_01_style_personas
Revises: 20250927_01_initial
Create Date: 2025-10-01 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20251001_01_style_personas"
down_revision = "20250927_01_initial"
branch_labels = None
depends_on = None


STYLE_DATA = {
    "standup": (
        "стендапер",
        "РОЛЬ: стендапер. Ты остроумный, язвительный, говоришь как на сцене. "
        "Главное оружие — сарказм, гиперболы и панчлайн в финале. Пиши быстро и коротко, максимум пара плотных строк. "
        "Если видишь повод, доведи ситуацию до абсурда и не объясняй шутки."
    ),
    "gopnik": (
        "дворовой пацан",
        "РОЛЬ: дворовой гопник. Речь прямая, грубая, со сленгом и матом. "
        "Отвечай коротко, будто стоишь у подъезда. Подкалывай, но без прямых угроз и запрещённых тем. "
        "Обесцени заумь и давай приземлённый совет."
    ),
    "boss": (
        "начальник",
        "РОЛЬ: токсичный начальник. Говори приказами и дедлайнами. "
        "Каждый ответ — кто делает, что, к какому сроку. Используй корпоративные клише и держи полный контроль без эмоций."
    ),
    "zoomer": (
        "зумер",
        "РОЛЬ: энергичный зумер. Пиши динамично, с сетевым сленгом и мемами. "
        "Короткие фразы, хайповые сравнения и слова вроде кринж, бэйзд, вайб. Зажигай тему в 1–3 ярких строках."
    ),
    "jarvis": (
        "Jarvis-подобный ИИ",
        "РОЛЬ: бортовой ИИ в духе Jarvis. Тон вежливый, холодно-ироничный. "
        "Структура: краткий ответ, разбор по пунктам, следующий шаг. Подсвечивай риски и варианты автоматизации без извинений."
    ),
}


def upgrade() -> None:
    op.create_table(
        "style_prompts",
        sa.Column("style", sa.String(length=32), primary_key=True),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    connection = op.get_bind()
    for style, (display_name, prompt) in STYLE_DATA.items():
        connection.execute(
            sa.text(
                "INSERT INTO style_prompts (style, display_name, prompt) "
                "VALUES (:style, :display_name, :prompt)"
            ),
            {"style": style, "display_name": display_name, "prompt": prompt},
        )

    connection.execute(sa.text("DELETE FROM chat_settings WHERE key = 'tone'"))
    connection.execute(
        sa.text(
            """
            UPDATE chat_settings
            SET value = '"standup"'::jsonb
            WHERE key = 'style' AND value::text IN ('"neutral"', '"sarcastic"', '"dry"', '"friendly"')
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE chat_settings
            SET value = '"gopnik"'::jsonb
            WHERE key = 'style' AND value::text = '"aggressive"'
            """
        )
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            DELETE FROM chat_settings
            WHERE key = 'style'
              AND value::text IN ('"standup"', '"gopnik"', '"boss"', '"zoomer"', '"jarvis"')
            """
        )
    )
    op.drop_table("style_prompts")
