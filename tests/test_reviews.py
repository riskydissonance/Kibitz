"""Tests for per-game review status + notes (reviews.py)."""
from __future__ import annotations

import pytest

from server.core import reviews


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "history").mkdir()
    return str(tmp_path)


def test_missing_file_empty(data_dir):
    assert reviews.review_states(data_dir=data_dir) == {}
    assert reviews.load_reviews(data_dir=data_dir) == []


def test_set_and_get_reviewed(data_dir):
    reviews.set_review("g1", "white", reviewed=True, data_dir=data_dir)
    st = reviews.get_state("g1", "white", data_dir=data_dir)
    assert st["reviewed"] is True
    assert st["note"] == ""


def test_note_and_reviewed_merge_independently(data_dir):
    reviews.set_review("g1", "white", reviewed=True, data_dir=data_dir)
    reviews.set_review("g1", "white", note="watch the pin", data_dir=data_dir)
    st = reviews.get_state("g1", "white", data_dir=data_dir)
    assert st["reviewed"] is True
    assert st["note"] == "watch the pin"


def test_latest_reviewed_wins(data_dir):
    reviews.set_review("g1", "white", reviewed=True, data_dir=data_dir)
    reviews.set_review("g1", "white", reviewed=False, data_dir=data_dir)
    assert reviews.get_state("g1", "white", data_dir=data_dir)["reviewed"] is False


def test_distinct_sides_independent(data_dir):
    reviews.set_review("g1", "white", note="w", data_dir=data_dir)
    reviews.set_review("g1", "black", note="b", data_dir=data_dir)
    assert reviews.get_state("g1", "white", data_dir=data_dir)["note"] == "w"
    assert reviews.get_state("g1", "black", data_dir=data_dir)["note"] == "b"


def test_set_review_requires_game_id(data_dir):
    with pytest.raises(ValueError):
        reviews.set_review("", "white", reviewed=True, data_dir=data_dir)
