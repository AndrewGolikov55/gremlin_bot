from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
_RELEASE_NOTES = Path(__file__).resolve().parents[2] / "RELEASE_NOTES.md"


@lru_cache(maxsize=1)
def get_version() -> str:
    with _PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    project = data.get("project") or {}
    return str(project.get("version") or "0.0.0")


def read_release_notes() -> str:
    try:
        return _RELEASE_NOTES.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
