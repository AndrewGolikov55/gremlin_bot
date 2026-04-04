import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_readme_documents_dev_requirements_and_make_checks() -> None:
    readme = _read("README.md")

    assert "requirements-dev.txt" in readme
    assert "make lint` проверяет только `ruff check .`" in readme
    assert "make typecheck` запускает только `mypy app tests`" in readme
    assert "make test` запускает только `pytest`" in readme
    assert "make check` запускает `ruff check .`, `mypy app tests`, `pytest`" in readme


def test_quality_workflow_runs_validation_on_push_and_pr() -> None:
    workflow = _read(".github/workflows/quality.yml")
    expected_python = f"{sys.version_info.major}.{sys.version_info.minor}"

    assert "on:" in workflow
    assert "push:" in workflow
    assert "pull_request:" in workflow
    assert f"python-version: '{expected_python}'" in workflow
    assert "python -m pip install -r requirements-dev.txt" in workflow
    assert "ruff check ." in workflow
    assert "mypy app tests" in workflow
    assert "pytest" in workflow
