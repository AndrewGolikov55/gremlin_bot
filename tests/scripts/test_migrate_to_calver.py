from __future__ import annotations

from scripts.migrate_to_calver import MAP


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
