from __future__ import annotations

from unittest.mock import MagicMock

from app.services.user_memory import _relationship_summary


def _make_relation(affinity: float = 0.0, tension: float = 0.0) -> MagicMock:
    rel = MagicMock()
    rel.affinity = affinity
    rel.tension = tension
    return rel


def test_relationship_summary_friendly():
    assert _relationship_summary(_make_relation(affinity=0.8)) == "отношения дружеские"


def test_relationship_summary_warm():
    assert _relationship_summary(_make_relation(affinity=0.3)) == "отношения тёплые"


def test_relationship_summary_neutral():
    assert _relationship_summary(_make_relation(affinity=0.0)) == "отношения нейтральные"


def test_relationship_summary_tense():
    assert _relationship_summary(_make_relation(affinity=-0.4)) == "отношения напряжённые"


def test_relationship_summary_hostile():
    assert _relationship_summary(_make_relation(affinity=-0.8)) == "отношения враждебные"


def test_relationship_summary_never_returns_none():
    for affinity in [-1.0, -0.5, -0.1, 0.0, 0.1, 0.5, 1.0]:
        result = _relationship_summary(_make_relation(affinity=affinity))
        assert result is not None, f"Got None for affinity={affinity}"
        assert isinstance(result, str)
