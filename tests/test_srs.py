"""Tests for the puzzle spaced-repetition scheduler (srs.py)."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from server.core import srs


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "history").mkdir()
    return str(tmp_path)


def test_record_and_load_attempts_round_trip(data_dir):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    srs.record_attempt("g1:7", "pass", True, data_dir=data_dir, now=now)
    attempts = srs.load_attempts(data_dir=data_dir)
    assert len(attempts) == 1
    a = attempts[0]
    assert a["puzzle_id"] == "g1:7"
    assert a["result"] == "pass"
    assert a["first_try"] is True
    assert a["ts"] == "2026-01-01T00:00:00Z"


def test_load_attempts_missing_file_returns_empty(data_dir):
    assert srs.load_attempts(data_dir=data_dir) == []


def test_result_normalised_to_pass_or_fail(data_dir):
    srs.record_attempt("g1:1", "garbage", False, data_dir=data_dir)
    (a,) = srs.load_attempts(data_dir=data_dir)
    assert a["result"] == "fail"


def test_box_transitions_pass_raises_fail_resets(data_dir):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    srs.record_attempt("p1", "pass", True, data_dir=data_dir, now=now)
    srs.record_attempt("p1", "pass", False, data_dir=data_dir, now=now + timedelta(days=2))
    states = srs.puzzle_states(data_dir=data_dir)
    assert states["p1"]["box"] == 2
    assert states["p1"]["seen"] == 2

    srs.record_attempt("p1", "fail", False, data_dir=data_dir, now=now + timedelta(days=5))
    states = srs.puzzle_states(data_dir=data_dir)
    assert states["p1"]["box"] == 0
    assert states["p1"]["seen"] == 3


def test_box_caps_at_max_box(data_dir):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(10):
        srs.record_attempt("p1", "pass", False, data_dir=data_dir, now=now + timedelta(days=i))
    states = srs.puzzle_states(data_dir=data_dir)
    assert states["p1"]["box"] == srs.MAX_BOX


def test_is_due_never_seen():
    assert srs.is_due(None, datetime.now(timezone.utc)) is True


def test_is_due_respects_interval_then_becomes_due(data_dir):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    srs.record_attempt("p1", "pass", True, data_dir=data_dir, now=now)  # box 0 -> 1 (1 day)
    states = srs.puzzle_states(data_dir=data_dir)
    state = states["p1"]
    assert state["box"] == 1

    assert srs.is_due(state, now + timedelta(hours=1)) is False
    assert srs.is_due(state, now + timedelta(days=1)) is True


def test_order_puzzles_tiers_due_then_new_then_not_due(data_dir):
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    # p_due: seen and failed recently -> box 0, always due.
    srs.record_attempt("p_due", "fail", False, data_dir=data_dir, now=now - timedelta(days=1))
    # p_not_due: passed recently, box 1 (1-day interval), not yet elapsed.
    srs.record_attempt("p_not_due", "pass", True, data_dir=data_dir, now=now - timedelta(hours=1))

    puzzles = [
        {"id": "p_due"},
        {"id": "p_new"},
        {"id": "p_not_due"},
    ]
    ordered = srs.order_puzzles(puzzles, data_dir=data_dir, now=now, rng=random.Random(0))

    assert [p["id"] for p in ordered] == ["p_due", "p_new", "p_not_due"]
    assert all("srs" in p for p in ordered)
    assert ordered[0]["srs"]["due"] is True
    assert ordered[1]["srs"]["seen"] == 0
    assert ordered[2]["srs"]["due"] is False


def test_order_puzzles_shuffles_within_tier_deterministically_with_seed(data_dir):
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    puzzles = [{"id": f"new{i}"} for i in range(6)]  # all never-seen -> one tier
    out1 = srs.order_puzzles(list(puzzles), data_dir=data_dir, now=now, rng=random.Random(42))
    out2 = srs.order_puzzles(list(puzzles), data_dir=data_dir, now=now, rng=random.Random(42))
    assert [p["id"] for p in out1] == [p["id"] for p in out2]
    assert {p["id"] for p in out1} == {p["id"] for p in puzzles}


def test_order_puzzles_keeps_all_puzzles(data_dir):
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    puzzles = [{"id": f"x{i}"} for i in range(20)]
    ordered = srs.order_puzzles(puzzles, data_dir=data_dir, now=now)
    assert len(ordered) == 20
    assert {p["id"] for p in ordered} == {p["id"] for p in puzzles}


# --- daily_session ---------------------------------------------------------------------------


def test_daily_session_due_seen_before_never_seen():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    items = [{"id": "new1"}, {"id": "due1"}, {"id": "new2"}]
    states = {
        "due1": {"box": 0, "last_ts": "2026-01-08T00:00:00Z", "seen": 1},
    }
    out = srs.daily_session(items, states, now)
    assert [p["id"] for p in out] == ["due1", "new1", "new2"]


def test_daily_session_skips_not_due_seen_items():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    items = [{"id": "not_due"}, {"id": "new1"}]
    states = {
        # box 1 -> 1 day interval; last_ts is now, so not yet due.
        "not_due": {"box": 1, "last_ts": "2026-01-10T00:00:00Z", "seen": 1},
    }
    out = srs.daily_session(items, states, now)
    assert [p["id"] for p in out] == ["new1"]


def test_daily_session_dedups_by_id():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    items = [{"id": "a"}, {"id": "a"}, {"id": "b"}]
    out = srs.daily_session(items, {}, now)
    assert [p["id"] for p in out] == ["a", "b"]


def test_daily_session_caps_at_limit():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    items = [{"id": f"n{i}"} for i in range(5)]
    out = srs.daily_session(items, {}, now, limit=3)
    assert len(out) == 3
    assert [p["id"] for p in out] == ["n0", "n1", "n2"]
