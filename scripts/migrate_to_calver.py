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
