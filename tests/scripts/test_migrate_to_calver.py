from __future__ import annotations

from pathlib import Path

import pytest

from scripts.migrate_to_calver import (
    MAP,
    ensure_clean_tree,
    run_git,
    tag_exists,
)


class TestMap:
    def test_has_all_33_entries(self) -> None:
        assert len(MAP) == 33

    def test_no_duplicate_targets(self) -> None:
        assert len(set(MAP.values())) == 33

    def test_keys_are_semver_with_v_prefix(self) -> None:
        import re
        for old in MAP:
            assert re.fullmatch(r"v\d+\.\d+\.\d+", old), old

    def test_values_are_calver(self) -> None:
        import re
        for new in MAP.values():
            assert re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\.\d+", new), new

    def test_v0_13_2_maps_to_2026_05_18_3(self) -> None:
        assert MAP["v0.13.2"] == "2026.05.18.3"

    def test_v0_1_0_maps_to_2026_04_12_0(self) -> None:
        assert MAP["v0.1.0"] == "2026.04.12.0"


class TestRunGit:
    def test_returns_stdout_string(self, fake_repo: Path) -> None:
        result = run_git("rev-parse", "--abbrev-ref", "HEAD")
        assert result.strip() == "main"

    def test_raises_on_nonzero_exit(self, fake_repo: Path) -> None:
        with pytest.raises(SystemExit):
            run_git("rev-parse", "nonexistent-ref")


class TestTagExists:
    def test_false_for_missing_tag(self, fake_repo: Path) -> None:
        assert tag_exists("v0.1.0") is False

    def test_true_for_existing_tag(self, fake_repo: Path) -> None:
        run_git("tag", "-a", "v0.1.0", "-m", "v0.1.0")
        assert tag_exists("v0.1.0") is True


class TestEnsureCleanTree:
    def test_passes_on_clean_tree(self, fake_repo: Path) -> None:
        ensure_clean_tree()  # no exception

    def test_raises_on_dirty_tree(self, fake_repo: Path) -> None:
        (fake_repo / "dirty.txt").write_text("uncommitted\n")
        with pytest.raises(SystemExit, match="uncommitted"):
            ensure_clean_tree()
