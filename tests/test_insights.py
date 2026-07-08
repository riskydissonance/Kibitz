"""Tests for the time-windowed insights aggregate (history.insights)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from server import config
from server.core import history


def _rec(game_id: str, date: str | None, *, accuracy: float = 80.0, result: str = "win",
         motifs: list[str] | None = None, analyzed_at: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "game_id": game_id,
        "reviewed_side": "white",
        "analyzed_at": analyzed_at or "2024-01-01T00:00:00Z",
        "player_id": "alice",
        "platform": "lichess",
        "player_name": "alice",
        "date": date,
        "white": "alice",
        "black": "bob",
        "result": "1-0",
        "player_result": result,
        "opening": "Petrov Defense",
        "speed": "blitz",
        "accuracy": accuracy,
        "counts": {"inaccuracy": 0, "mistake": 1, "blunder": 1},
        "phase_loss": {"opening": 0.0, "middlegame": 20.0, "endgame": 0.0},
        "mistakes": [{"motifs": motifs or ["hung_piece"]}],
    }


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USERNAME", "alice")
    monkeypatch.setattr(config, "USERNAME_ALIASES", [])
    path = tmp_path / "history"
    path.mkdir()
    return str(tmp_path)


def _write(records: list[dict], data_dir: str) -> None:
    p = os.path.join(data_dir, "history", "games.jsonl")
    with open(p, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _day(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def test_insights_all_time(data_dir):
    _write([_rec("g1", _day(1)), _rec("g2", _day(40), accuracy=60.0, result="loss")], data_dir)
    out = history.insights(None, data_dir)
    assert out["games"] == 2
    assert out["results"] == {"win": 1, "loss": 1, "draw": 0}
    assert out["avg_accuracy"] == 70.0
    # Motifs carry human labels for the frontend.
    assert out["top_motifs"][0]["motif"] == "hung_piece"
    assert "hanging pieces" in out["top_motifs"][0]["label"]
    assert out["weakest_phase"] == "middlegame"


def test_insights_window_filters_by_played_date(data_dir):
    _write([_rec("g1", _day(1)), _rec("g2", _day(40))], data_dir)
    out = history.insights(7, data_dir)
    assert out["games"] == 1
    assert out["days"] == 7


def test_insights_falls_back_to_analyzed_date(data_dir):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    _write([_rec("g1", None, analyzed_at=now)], data_dir)
    assert history.insights(7, data_dir)["games"] == 1


def test_insights_only_counts_my_games(data_dir):
    other = _rec("g9", _day(1))
    other["player_id"] = "someone_else"
    other["player_name"] = "someone_else"
    _write([_rec("g1", _day(1)), other], data_dir)
    assert history.insights(None, data_dir)["games"] == 1


def test_insights_without_identity_counts_everything(data_dir, monkeypatch):
    # No configured username (paste-only user) -> aggregate all analyzed games.
    monkeypatch.setattr(config, "USERNAME", "")
    other = _rec("g9", _day(1))
    other["player_id"] = "someone_else"
    other["player_name"] = "someone_else"
    _write([_rec("g1", _day(1)), other], data_dir)
    assert history.insights(None, data_dir)["games"] == 2


def test_insights_empty_history(data_dir):
    _write([], data_dir)
    out = history.insights(30, data_dir)
    assert out["games"] == 0


def test_history_rows_ordered_by_played_date_then_import_time(data_dir):
    # Played date wins over import (analyzed_at) order; no played date falls back to import day;
    # same-day games tiebreak on import time.
    recent_played = _rec("g-recent", _day(1), analyzed_at="2024-01-01T00:00:00Z")
    old_played_new_import = _rec("g-old", _day(40), analyzed_at="2099-01-01T00:00:00Z")
    undated = _rec("g-undated", None, analyzed_at=_day(2) + "T12:00:00Z")
    _write([old_played_new_import, undated, recent_played], data_dir)
    rows = history.history_rows(data_dir=data_dir)
    assert [r["game_id"] for r in rows] == ["g-recent", "g-undated", "g-old"]


def test_opening_accuracy_ignores_games_without_accuracy(data_dir):
    # Two Petrov games, only one with an accuracy: the average is that game's 80%, not 40%.
    no_acc = _rec("g2", _day(1))
    no_acc["accuracy"] = None
    _write([_rec("g1", _day(1)), no_acc], data_dir)
    (petrov,) = history.insights(None, data_dir)["openings"]
    assert petrov["games"] == 2
    assert petrov["avg_accuracy"] == 80.0


def test_insights_trend_is_chronological_and_capped(data_dir):
    # Oldest→newest by played day, one entry per game, with the fields the trend chart reads.
    _write(
        [
            _rec("g1", _day(3), accuracy=70.0),
            _rec("g2", _day(1), accuracy=90.0),
            _rec("g3", _day(2), accuracy=80.0, result="loss"),
        ],
        data_dir,
    )
    out = history.insights(None, data_dir)
    assert [t["accuracy"] for t in out["trend"]] == [70.0, 80.0, 90.0]
    assert out["trend"][1]["result"] == "loss"
    assert set(out["trend"][0]) == {"date", "accuracy", "result", "opening"}
