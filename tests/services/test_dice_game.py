from __future__ import annotations

import pytest

from app.services.dice_game import compute_delta


class TestComputeDelta:
    @pytest.mark.parametrize("picks,dice_value,expected", [
        # 1 number — win: -2, lose: 0
        ([3], 3, -2),
        ([3], 4, 0),
        ([6], 6, -2),
        ([1], 2, 0),
        # 2 numbers — win: -1, lose: 0
        ([1, 4], 1, -1),
        ([1, 4], 4, -1),
        ([1, 4], 5, 0),
        ([2, 5], 3, 0),
    ])
    def test_table(self, picks: list[int], dice_value: int, expected: int) -> None:
        assert compute_delta(picks, dice_value) == expected
