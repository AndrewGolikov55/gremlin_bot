#!/usr/bin/env python3
"""Migrate semver tags + CHANGELOG headings to CalVer (YYYY.MM.DD.N).

Run once:
    python scripts/migrate_to_calver.py --dry-run   # preview
    python scripts/migrate_to_calver.py             # apply

After apply, the script prints exact commands to push new tags and delete
old ones from origin. Copy those lines and run them.

Revert (rare):
    python scripts/migrate_to_calver.py --revert
"""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path
from typing import NoReturn

MAP: dict[str, str] = {
    "v0.1.0":  "2026.04.12.0",
    "v0.1.1":  "2026.04.13.0",
    "v0.1.2":  "2026.04.13.1",
    "v0.2.0":  "2026.04.14.0",
    "v0.2.1":  "2026.04.14.1",
    "v0.3.0":  "2026.04.14.2",
    "v0.3.1":  "2026.04.14.3",
    "v0.3.2":  "2026.04.14.4",
    "v0.4.0":  "2026.04.14.5",
    "v0.4.1":  "2026.04.14.6",
    "v0.5.0":  "2026.04.17.0",
    "v0.5.1":  "2026.04.17.1",
    "v0.6.0":  "2026.04.17.2",
    "v0.6.1":  "2026.04.18.0",
    "v0.6.2":  "2026.04.18.1",
    "v0.6.3":  "2026.04.21.0",
    "v0.6.4":  "2026.04.23.0",
    "v0.7.0":  "2026.05.09.0",
    "v0.7.1":  "2026.05.09.1",
    "v0.7.2":  "2026.05.09.2",
    "v0.8.0":  "2026.05.10.0",
    "v0.8.1":  "2026.05.16.0",
    "v0.9.0":  "2026.05.16.1",
    "v0.10.0": "2026.05.16.2",
    "v0.11.0": "2026.05.16.3",
    "v0.12.0": "2026.05.16.4",
    "v0.12.1": "2026.05.16.5",
    "v0.12.2": "2026.05.16.6",
    "v0.12.3": "2026.05.16.7",
    "v0.12.4": "2026.05.18.0",
    "v0.13.0": "2026.05.18.1",
    "v0.13.1": "2026.05.18.2",
    "v0.13.2": "2026.05.18.3",
}


def _die(msg: str) -> NoReturn:
    raise SystemExit(f"error: {msg}")


def run_git(*args: str) -> str:
    """Run a git command, return stdout as string. Die on non-zero exit."""
    proc = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        _die(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def tag_exists(name: str) -> bool:
    """Return True if a git tag with the given name exists locally."""
    return bool(run_git("tag", "-l", name).strip())


def ensure_clean_tree() -> None:
    """Refuse to proceed if working tree has uncommitted changes."""
    if run_git("status", "--porcelain").strip():
        _die(
            "working tree has uncommitted changes — "
            "commit or stash them first"
        )


def sed_inplace(
    path: Path,
    pattern: str,
    replacement: str,
    *,
    dry_run: bool = False,
) -> int:
    """In-place regex replace on a file. Returns number of substitutions made.

    Pattern is treated as MULTILINE so `^` matches line starts. Replacement
    is plain text (not a regex template).
    """
    text = path.read_text()
    new_text, n = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if n > 0 and not dry_run:
        path.write_text(new_text)
    return n


CHANGELOG_DEFAULT = Path("CHANGELOG.md")


def apply(
    *,
    dry_run: bool,
    changelog_path: Path = CHANGELOG_DEFAULT,
    mapping: dict[str, str] | None = None,
) -> None:
    """Apply CalVer migration: rewrite CHANGELOG headings + recreate tags.

    Idempotent: a second run after a successful first prints "skip" for
    each already-migrated tag and finds no headings to rewrite.
    """
    if mapping is None:
        mapping = MAP
    if not dry_run:
        ensure_clean_tree()

    # 1. CHANGELOG rewrite
    for old, new in mapping.items():
        old_v = old.removeprefix("v")
        n = sed_inplace(
            changelog_path,
            rf"^## \[{re.escape(old_v)}\] -",
            f"## [{new}] -",
            dry_run=dry_run,
        )
        if n == 0:
            print(f"  note: no CHANGELOG entry for {old} (tag-only migration)")

    # 2. Tag migration
    for old, new in mapping.items():
        if not tag_exists(old):
            if tag_exists(new):
                print(f"  skip: {old} already migrated to {new}")
            else:
                print(f"  warn: {old} missing and {new} not yet created")
            continue
        sha = run_git("rev-list", "-1", old).strip()
        if dry_run:
            print(f"DRY: would create {new} -> {sha} (was {old})")
            continue
        if not tag_exists(new):
            run_git("tag", "-a", new, sha, "-m", new)
        run_git("tag", "-d", old)

    if not dry_run:
        _print_operator_instructions(mapping)


def _print_operator_instructions(mapping: dict[str, str]) -> None:
    old_list = " ".join(mapping.keys())
    print("\nLocal migration done. Next steps for operator:\n")
    print("  # 1. Push the new release commit + all new tags")
    print("  git push origin main")
    print("  git push origin --tags\n")
    print("  # 2. Delete the old tags from origin")
    print(f"  git push origin --delete {old_list}\n")
    print("  # 3. (If GitHub Releases exist) re-point them via:")
    print("  #    gh release edit <old-tag> --tag <new-tag>")


def revert(
    *,
    changelog_path: Path = CHANGELOG_DEFAULT,
    mapping: dict[str, str] | None = None,
) -> None:
    """Inverse of apply()."""
    if mapping is None:
        mapping = MAP
    ensure_clean_tree()

    # 1. CHANGELOG: rewrite headings back
    for old, new in mapping.items():
        old_v = old.removeprefix("v")
        sed_inplace(
            changelog_path,
            rf"^## \[{re.escape(new)}\] -",
            f"## [{old_v}] -",
        )

    # 2. Tags: recreate old, delete new
    for old, new in mapping.items():
        if not tag_exists(new):
            continue
        sha = run_git("rev-list", "-1", new).strip()
        if not tag_exists(old):
            run_git("tag", "-a", old, sha, "-m", old)
        run_git("tag", "-d", new)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate semver tags + CHANGELOG to CalVer (YYYY.MM.DD.N)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true",
        help="show what would change without modifying anything",
    )
    mode.add_argument(
        "--revert", action="store_true",
        help="undo a previous migration (recreates old tags, restores CHANGELOG)",
    )
    args = parser.parse_args()

    if args.revert:
        revert()
    else:
        apply(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
