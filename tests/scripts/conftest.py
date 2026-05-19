from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch git repo for testing migration script."""
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "config", "user.name", "t"], check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], check=True)
    subprocess.run(["git", "config", "tag.gpgsign", "false"], check=True)
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], check=True)
    return tmp_path
