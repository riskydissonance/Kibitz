"""Tests for the on-disk analysed-game cache (engine-free, fast).

The cache lets the board reopen a previously-analysed game instantly instead of re-running the
~20-45s Stockfish sweep. We verify the key is derived from the move list (so a PGN can be matched
before analysis), that it round-trips a session, and that disabling/capping behave."""
from __future__ import annotations

import pytest

from server import config
from server.core import analysis_cache
from server.core.session import ReviewSession

# "1. e4 e5 2. Nf3" — mainline UCIs the game_id keys on.
PGN = "1. e4 e5 2. Nf3 *"
UCIS = ["e2e4", "e7e5", "g1f3"]


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Point the cache at a temp DATA_DIR and force it enabled."""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ANALYSIS_CACHE_ENABLED", True)
    monkeypatch.setattr(config, "ANALYSIS_CACHE_MAX", 1000)
    return tmp_path


def _session(side: str = "white") -> ReviewSession:
    return ReviewSession(
        pgn=PGN,
        player=side,
        headers={"White": "me", "Black": "opp"},
        sweep_depth=16,
        timeline=[{"move_uci": u} for u in UCIS] + [{}],  # trailing node has no outgoing move
    )


def test_store_then_load_round_trip(cache_dir):
    analysis_cache.store(_session("white"))

    loaded = analysis_cache.load(PGN, "white")
    assert loaded is not None
    assert loaded.player == "white"
    assert loaded.pgn == PGN
    assert loaded.sweep_depth == 16
    # Navigation state is reset on a fresh open.
    assert loaded.current_index == 0
    assert loaded.explore_fen is None


def test_load_is_side_specific(cache_dir):
    analysis_cache.store(_session("white"))
    assert analysis_cache.load(PGN, "white") is not None
    assert analysis_cache.load(PGN, "black") is None  # different key, no entry


def test_load_auto_resolves_side(cache_dir, monkeypatch):
    # "me" plays White here; player="auto" should find the white entry via resolve_player.
    monkeypatch.setattr(config, "USERNAME", "me")
    monkeypatch.setattr(config, "USERNAME_ALIASES", [])
    analysis_cache.store(_session("white"))
    assert analysis_cache.load(PGN, "auto") is not None


def test_miss_for_unknown_game(cache_dir):
    assert analysis_cache.load("1. d4 d5 *", "white") is None


def test_disabled_stores_and_loads_nothing(cache_dir, monkeypatch):
    monkeypatch.setattr(config, "ANALYSIS_CACHE_ENABLED", False)
    analysis_cache.store(_session("white"))
    monkeypatch.setattr(config, "ANALYSIS_CACHE_ENABLED", True)
    assert analysis_cache.load(PGN, "white") is None  # nothing was written


def test_version_mismatch_is_a_miss(cache_dir, monkeypatch):
    analysis_cache.store(_session("white"))
    monkeypatch.setattr(analysis_cache, "CACHE_VERSION", analysis_cache.CACHE_VERSION + 1)
    assert analysis_cache.load(PGN, "white") is None


def test_prune_keeps_cap(cache_dir, monkeypatch):
    monkeypatch.setattr(config, "ANALYSIS_CACHE_MAX", 2)
    for i in range(5):
        sess = _session("white")
        # Distinct move lists -> distinct game_ids -> distinct files.
        sess.timeline = [{"move_uci": f"e2e{(i % 7) + 1}"}, {"move_uci": f"a7a{(i % 6) + 1}"}, {}]
        analysis_cache.store(sess)
    import os

    files = [n for n in os.listdir(analysis_cache._cache_dir()) if n.endswith(".json")]
    assert len(files) <= 2
