from __future__ import annotations

import textwrap
from pathlib import Path

from app.services.persona import load_persona_files, parse_persona_file


def test_parse_persona_file_extracts_display_name_and_prompt():
    content = textwrap.dedent("""\
        ---
        display_name: дворовой пацан
        ---

        Тебя зовут Гремлин. 27 лет.
        Речь живая, с матом для акцента.
    """)
    result = parse_persona_file(content)
    assert result["display_name"] == "дворовой пацан"
    assert "Тебя зовут Гремлин" in result["prompt"]
    assert "---" not in result["prompt"]


def test_parse_persona_file_without_frontmatter_uses_fallback():
    content = "Просто текст без frontmatter."
    result = parse_persona_file(content, fallback_display_name="тест")
    assert result["display_name"] == "тест"
    assert result["prompt"] == "Просто текст без frontmatter."


def test_load_persona_files_reads_all_md_files(tmp_path: Path) -> None:
    (tmp_path / "gopnik.md").write_text(
        "---\ndisplay_name: пацан\n---\n\nТы пацан.",
        encoding="utf-8",
    )
    (tmp_path / "boss.md").write_text(
        "---\ndisplay_name: босс\n---\n\nТы босс.",
        encoding="utf-8",
    )
    (tmp_path / "not_a_persona.txt").write_text("ignored", encoding="utf-8")

    result = load_persona_files(tmp_path)

    assert "gopnik" in result
    assert "boss" in result
    assert "not_a_persona" not in result
    assert result["gopnik"]["display_name"] == "пацан"
    assert "Ты пацан." in result["gopnik"]["prompt"]


def test_load_persona_files_returns_empty_for_missing_dir():
    result = load_persona_files(Path("/nonexistent/path"))
    assert result == {}
