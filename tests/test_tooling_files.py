from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_requirements_dev_lists_tooling_dependencies() -> None:
    requirements_dev = _read("requirements-dev.txt")

    assert "-r requirements.txt" in requirements_dev
    assert "pytest" in requirements_dev
    assert "ruff" in requirements_dev
    assert "mypy" in requirements_dev


def test_pyproject_contains_tooling_sections() -> None:
    pyproject = _read("pyproject.toml")

    assert "[tool.pytest.ini_options]" in pyproject
    assert "[tool.ruff]" in pyproject
    assert "[tool.mypy]" in pyproject
    assert 'testpaths = ["tests"]' in pyproject
    assert 'asyncio_mode = "auto"' in pyproject
    assert "line-length = 100" in pyproject
    assert 'target-version = "py311"' in pyproject
    assert 'python_version = "3.11"' in pyproject
    assert 'files = ["app", "tests"]' in pyproject


def test_makefile_and_gitignore_include_tooling_targets_and_caches() -> None:
    makefile = _read("Makefile")
    gitignore = _read(".gitignore")
    gitignore_tail = [line for line in gitignore.splitlines() if line.strip()][-4:]

    for target in ("lint:", "lint-fix:", "typecheck:", "test:", "check:"):
        assert target in makefile

    for target in (
        "dev-build:",
        "dev-up:",
        "dev-restart:",
        "dev-down:",
        "dev-logs:",
        "dev-ps:",
        "dev-shell:",
        "dev-migrate:",
    ):
        assert target in makefile

    assert "lint:\n\truff check ." in makefile
    assert "lint-fix:\n\truff check . --fix" in makefile
    assert "typecheck:\n\tmypy app tests" in makefile
    assert "test:\n\tpytest" in makefile
    assert "check:\n\truff check .\n\tmypy app tests\n\tpytest" in makefile

    assert ".mypy_cache/" in gitignore_tail
    assert ".ruff_cache/" in gitignore_tail
