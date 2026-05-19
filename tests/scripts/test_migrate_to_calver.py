from __future__ import annotations

from pathlib import Path

import pytest

from scripts.migrate_to_calver import (
    MAP,
    apply,
    ensure_clean_tree,
    run_git,
    sed_inplace,
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


class TestSedInplace:
    def test_replaces_single_match_and_returns_count(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "x.md"
        f.write_text("## [0.1.0] - 2026-04-12\n\nbody\n")
        n = sed_inplace(f, r"^## \[0\.1\.0\] -", "## [2026.04.12.0] -")
        assert n == 1
        assert f.read_text().startswith("## [2026.04.12.0] - 2026-04-12")

    def test_returns_zero_when_no_match(self, tmp_path: Path) -> None:
        f = tmp_path / "x.md"
        f.write_text("nothing to see\n")
        n = sed_inplace(f, r"^## \[0\.1\.0\] -", "## [whatever] -")
        assert n == 0
        assert f.read_text() == "nothing to see\n"

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        f = tmp_path / "x.md"
        original = "## [0.1.0] - 2026-04-12\nbody\n"
        f.write_text(original)
        n = sed_inplace(
            f, r"^## \[0\.1\.0\] -", "## [2026.04.12.0] -", dry_run=True,
        )
        assert n == 1  # счёт всё равно возвращаем
        assert f.read_text() == original  # но не пишем

    def test_only_matches_at_line_start(self, tmp_path: Path) -> None:
        f = tmp_path / "x.md"
        f.write_text("  ## [0.1.0] - leading space, должен пропустить\n")
        n = sed_inplace(f, r"^## \[0\.1\.0\] -", "## [X] -")
        assert n == 0


class TestApplyDryRun:
    def test_dry_run_does_not_create_tags(
        self, fake_repo: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Set up: tag v0.1.0 exists, others don't
        run_git("tag", "-a", "v0.1.0", "-m", "v0.1.0")
        (fake_repo / "CHANGELOG.md").write_text(
            "## [0.1.0] - 2026-04-12\n\nbody\n"
        )

        # Limit MAP to one entry to keep the test fast and isolated
        apply(
            dry_run=True,
            changelog_path=fake_repo / "CHANGELOG.md",
            mapping={"v0.1.0": "2026.04.12.0"},
        )

        # New tag NOT created
        assert tag_exists("2026.04.12.0") is False
        # Old tag still here
        assert tag_exists("v0.1.0") is True
        # CHANGELOG not modified
        assert "0.1.0" in (fake_repo / "CHANGELOG.md").read_text()
        assert "2026.04.12.0" not in (fake_repo / "CHANGELOG.md").read_text()

        # Output mentions what WOULD happen
        out = capsys.readouterr().out
        assert "DRY" in out
        assert "v0.1.0" in out
        assert "2026.04.12.0" in out


class TestApplyWrite:
    def test_creates_new_tag_at_same_sha_and_deletes_old(
        self, fake_repo: Path,
    ) -> None:
        run_git("tag", "-a", "v0.1.0", "-m", "v0.1.0")
        old_sha = run_git("rev-list", "-1", "v0.1.0").strip()
        (fake_repo / "CHANGELOG.md").write_text(
            "## [0.1.0] - 2026-04-12\n\nbody\n"
        )
        run_git("add", "CHANGELOG.md")
        run_git("commit", "-q", "-m", "add changelog")

        apply(
            dry_run=False,
            changelog_path=fake_repo / "CHANGELOG.md",
            mapping={"v0.1.0": "2026.04.12.0"},
        )

        assert tag_exists("v0.1.0") is False
        assert tag_exists("2026.04.12.0") is True
        new_sha = run_git("rev-list", "-1", "2026.04.12.0").strip()
        assert new_sha == old_sha
        assert "[2026.04.12.0]" in (fake_repo / "CHANGELOG.md").read_text()
        assert "[0.1.0]" not in (fake_repo / "CHANGELOG.md").read_text()

    def test_idempotent_second_run_no_errors(
        self, fake_repo: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        run_git("tag", "-a", "v0.1.0", "-m", "v0.1.0")
        (fake_repo / "CHANGELOG.md").write_text(
            "## [0.1.0] - 2026-04-12\n\nbody\n"
        )
        run_git("add", "CHANGELOG.md")
        run_git("commit", "-q", "-m", "add changelog")

        # First run: migrates
        apply(
            dry_run=False,
            changelog_path=fake_repo / "CHANGELOG.md",
            mapping={"v0.1.0": "2026.04.12.0"},
        )
        # Commit the rewritten CHANGELOG so the tree is clean for run 2
        run_git("add", "CHANGELOG.md")
        run_git("commit", "-q", "-m", "calver rewrite")
        capsys.readouterr()  # clear

        # Second run: should be no-op, no exceptions
        apply(
            dry_run=False,
            changelog_path=fake_repo / "CHANGELOG.md",
            mapping={"v0.1.0": "2026.04.12.0"},
        )
        out = capsys.readouterr().out
        assert "skip: v0.1.0 already migrated to 2026.04.12.0" in out
        assert tag_exists("2026.04.12.0") is True
        assert tag_exists("v0.1.0") is False

    def test_dry_run_after_apply_finds_no_work(
        self, fake_repo: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Acceptance criterion: idempotency check via second --dry-run."""
        run_git("tag", "-a", "v0.1.0", "-m", "v0.1.0")
        (fake_repo / "CHANGELOG.md").write_text(
            "## [0.1.0] - 2026-04-12\nbody\n"
        )
        run_git("add", "CHANGELOG.md")
        run_git("commit", "-q", "-m", "add changelog")
        apply(
            dry_run=False,
            changelog_path=fake_repo / "CHANGELOG.md",
            mapping={"v0.1.0": "2026.04.12.0"},
        )
        capsys.readouterr()  # clear

        apply(
            dry_run=True,
            changelog_path=fake_repo / "CHANGELOG.md",
            mapping={"v0.1.0": "2026.04.12.0"},
        )
        out = capsys.readouterr().out
        assert "DRY: would create" not in out  # nothing left to do
        assert "skip" in out

    def test_refuses_dirty_tree(self, fake_repo: Path) -> None:
        (fake_repo / "dirty.txt").write_text("uncommitted\n")
        with pytest.raises(SystemExit, match="uncommitted"):
            apply(
                dry_run=False,
                changelog_path=fake_repo / "CHANGELOG.md",
                mapping={"v0.1.0": "2026.04.12.0"},
            )

    def test_prints_operator_instructions_on_real_run(
        self, fake_repo: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        run_git("tag", "-a", "v0.1.0", "-m", "v0.1.0")
        (fake_repo / "CHANGELOG.md").write_text(
            "## [0.1.0] - 2026-04-12\nbody\n"
        )
        run_git("add", "CHANGELOG.md")
        run_git("commit", "-q", "-m", "add changelog")
        apply(
            dry_run=False,
            changelog_path=fake_repo / "CHANGELOG.md",
            mapping={"v0.1.0": "2026.04.12.0"},
        )
        out = capsys.readouterr().out
        assert "git push origin --tags" in out
        assert "git push origin --delete v0.1.0" in out
        assert "gh release edit" in out
