"""Tests for pre-computed best_moves/threats on the analysis timeline.

Covers: analyze_game() populates best_moves/threats on timeline nodes, threats are skipped
when the side to move is in check, an old-shape cache entry (no new fields) still loads and
the /timeline route passes the new fields through unchanged. Uses the real Stockfish binary
(same as other engine-backed tests in this repo) at server.config.STOCKFISH_PATH, with
CHESS_DATA_DIR pointed at a pytest tmp_path so nothing touches the production data dir.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import config
from server.core import analysis_cache
from server.core.game_analysis import analyze_game
from server.core.session import ReviewSession

# Short real game so the sweep is fast; includes a check (3. Bb5+ style motif not needed —
# we use a simple line that puts black in check via a real tactical shot).
PGN_SIMPLE = """
[White "A"]
[Black "B"]
[Result "1-0"]

1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 1-0
"""

# A short forced-mate line so the position right before mate has the mated side in check on the
# FINAL node, and a mid-game check occurs one move earlier (Qh5+-style scholar's-mate skeleton).
PGN_WITH_CHECK = """
[White "A"]
[Black "B"]
[Result "1-0"]

1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0
"""


@pytest.fixture(autouse=True)
def _fast_depth(monkeypatch):
    # Keep the sweep depth low so these tests run quickly.
    monkeypatch.setattr(config, "SWEEP_DEPTH", 8)


def test_analysis_produces_best_moves_and_threats_in_timeline():
    sess = analyze_game(PGN_SIMPLE, player="white")
    assert sess.timeline, "expected a non-empty timeline"

    non_final_nodes = [n for n in sess.timeline if n.get("move_uci")]
    assert non_final_nodes, "expected at least one non-final node"

    # precomputed_depth is stamped on every node.
    assert all(n.get("precomputed_depth") == config.SWEEP_DEPTH for n in sess.timeline)

    # Every non-final, non-mate node should have at least one precomputed best move, shaped like
    # the live /best-moves and /threats payloads: {uci, san, win_percent, eval_cp[, mate]}.
    for n in non_final_nodes:
        assert "best_moves" in n
        assert n["best_moves"], f"node {n['node']} missing best_moves"
        bm = n["best_moves"][0]
        assert set(["uci", "san", "win_percent", "eval_cp"]).issubset(bm.keys())
        assert "threats" in n  # present even when empty


def test_threats_skipped_when_in_check():
    sess = analyze_game(PGN_WITH_CHECK, player="white")
    # Find the node whose position has the side to move in check (e.g. right after 2. Qh5, black
    # is not in check but after 4. Qxf7# black IS in check/mate). We check every node's own FEN.
    import chess

    checked_nodes = [n for n in sess.timeline if chess.Board(n["fen"]).is_check()]
    assert checked_nodes, "expected at least one in-check node in this line"
    for n in checked_nodes:
        assert n.get("threats") == []


def test_old_cache_without_new_fields_still_loads_and_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ANALYSIS_CACHE_ENABLED", True)
    monkeypatch.setattr(config, "ANALYSIS_CACHE_MAX", 1000)

    pgn = "1. e4 e5 2. Nf3 *"
    ucis = ["e2e4", "e7e5", "g1f3"]
    old_sess = ReviewSession(
        pgn=pgn,
        player="white",
        headers={"White": "me", "Black": "opp"},
        sweep_depth=16,
        # Old-shape timeline: no best_moves/threats/precomputed_depth fields at all.
        timeline=[{"move_uci": u, "fen": "x"} for u in ucis] + [{"fen": "y"}],
    )
    analysis_cache.store(old_sess)

    loaded = analysis_cache.load(pgn, "white")
    assert loaded is not None
    assert loaded.pgn == pgn
    for node in loaded.timeline:
        assert node.get("best_moves") is None
        assert node.get("threats") is None
        assert node.get("precomputed_depth") is None


def test_timeline_route_includes_new_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    from server.core import session as session_mod
    from server.web import app as app_module

    sess = analyze_game(PGN_SIMPLE, player="white")
    session_mod.set_session(sess)
    try:
        client = TestClient(app_module.create_app())
        r = client.get("/api/timeline")
        assert r.status_code == 200
        body = r.json()
        assert body["nodes"], "expected timeline nodes"
        non_final = [n for n in body["nodes"] if n.get("move_uci")]
        assert non_final
        assert all("best_moves" in n and "threats" in n for n in non_final)
        assert all(n.get("precomputed_depth") == config.SWEEP_DEPTH for n in body["nodes"])
    finally:
        session_mod.clear_session()
