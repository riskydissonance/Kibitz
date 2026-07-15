"""Tests for the puzzle trainer (puzzles.py) and the Lichess study export PGN."""
from __future__ import annotations

import json
import os

import pytest

from server import config
from server.core import lichess_study
from server.core import puzzles

# A real middlegame position (white to move) so SAN conversion is exercised on legal moves.
_FEN_W = "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4"
# Black to move.
_FEN_B = "rnbqkb1r/pppp1ppp/5n2/4p3/4P3/5N2/PPPPBPPP/RNBQK2R b KQkq - 3 3"


def _mistake(
    fen: str = _FEN_W,
    played: str = "d2d3",
    best: str = "e1g1",
    best_san: str | None = "O-O",
    classification: str = "blunder",
    win_drop: float = 25.0,
    motifs: list[str] | None = None,
    ply: int = 21,
) -> dict:
    return {
        "ply": ply,
        "move_number": (ply + 1) // 2,
        "color": "white",
        "san": "d3",
        "uci": played,
        "best_san": best_san,
        "best_uci": best,
        "classification": classification,
        "win_before": 60.0,
        "win_after": 60.0 - win_drop,
        "win_drop": win_drop,
        "phase": "middlegame",
        "fen_before": fen,
        "motifs": motifs or [],
    }


def _rec(game_id: str, mistakes: list[dict], *, date: str = "2026-01-15") -> dict:
    return {
        "schema_version": 1,
        "game_id": game_id,
        "reviewed_side": "white",
        "analyzed_at": "2026-01-15T00:00:00Z",
        "player_id": "alice",
        "platform": "lichess",
        "player_name": "alice",
        "date": date,
        "white": "alice",
        "black": "bob",
        "result": "1-0",
        "player_result": "win",
        "opening": "Italian Game",
        "speed": "blitz",
        "accuracy": 80.0,
        "game_url": f"https://lichess.org/{game_id}",
        "counts": {"inaccuracy": 0, "mistake": 0, "blunder": len(mistakes)},
        "phase_loss": {"opening": 0.0, "middlegame": 20.0, "endgame": 0.0},
        "mistakes": mistakes,
    }


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USERNAME", "alice")
    monkeypatch.setattr(config, "USERNAME_ALIASES", [])
    (tmp_path / "history").mkdir()
    return str(tmp_path)


def _write(records: list[dict], data_dir: str) -> None:
    with open(os.path.join(data_dir, "history", "games.jsonl"), "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def test_build_puzzles_basic(data_dir):
    _write([_rec("g1", [_mistake(motifs=["hung_piece"])])], data_dir)
    out = puzzles.build_puzzles(data_dir=data_dir)
    assert len(out) == 1
    p = out[0]
    assert p["fen"] == _FEN_W
    assert p["solution_uci"] == "e1g1"
    assert p["solution_san"] == "O-O"
    assert p["color"] == "white"  # solver = side to move = the side that blundered
    assert p["themes"] == ["hanging pieces (leaving a piece en prise)"]
    assert p["game_url"] == "https://lichess.org/g1"


def test_puzzles_sorted_blunders_first_then_win_drop(data_dir):
    _write(
        [
            _rec(
                "g1",
                [
                    _mistake(classification="inaccuracy", win_drop=6.0, ply=21),
                    _mistake(classification="blunder", win_drop=20.0, ply=23),
                    _mistake(classification="blunder", win_drop=45.0, ply=25),
                    _mistake(classification="mistake", win_drop=12.0, ply=27),
                ],
            )
        ],
        data_dir,
    )
    out = puzzles.build_puzzles(data_dir=data_dir)
    assert [(p["classification"], p["win_drop"]) for p in out] == [
        ("blunder", 45.0),
        ("blunder", 20.0),
        ("mistake", 12.0),
        ("inaccuracy", 6.0),
    ]


def test_filters_by_eco(data_dir):
    najdorf = _rec("g1", [_mistake(motifs=["hung_piece"], ply=21)]) | {"eco": "B90"}
    italian = _rec("g2", [_mistake(motifs=["hung_piece"], ply=21)]) | {"eco": "C50"}
    _write([najdorf, italian], data_dir)
    out = puzzles.build_puzzles(eco="B90", data_dir=data_dir)
    assert len(out) == 1
    assert out[0]["game_id"] == "g1"
    # Case-insensitive match.
    assert len(puzzles.build_puzzles(eco="b90", data_dir=data_dir)) == 1
    assert puzzles.build_puzzles(eco="A00", data_dir=data_dir) == []


def test_filters_motif_kinds_and_limit(data_dir):
    _write(
        [
            _rec(
                "g1",
                [
                    _mistake(motifs=["hung_piece"], classification="blunder", ply=21),
                    _mistake(motifs=["missed_fork"], classification="mistake", ply=23),
                    _mistake(motifs=["hung_piece"], classification="inaccuracy", ply=25),
                ],
            )
        ],
        data_dir,
    )
    assert len(puzzles.build_puzzles(motif="hung_piece", data_dir=data_dir)) == 2
    assert len(puzzles.build_puzzles(kinds=["blunder"], data_dir=data_dir)) == 1
    assert len(puzzles.build_puzzles(limit=1, data_dir=data_dir)) == 1
    assert puzzles.build_puzzles(motif="back_rank", data_dir=data_dir) == []


def test_unsolvable_mistakes_are_skipped(data_dir):
    _write(
        [
            _rec(
                "g1",
                [
                    _mistake(best="", ply=21),  # no better move recorded
                    _mistake(best="d2d3", played="d2d3", ply=23),  # solution == played
                    _mistake(fen="", ply=25),  # no position
                    # illegal stored solution AND no stored SAN -> skipped, not served broken
                    _mistake(best="a1a8", best_san=None, ply=27),
                ],
            )
        ],
        data_dir,
    )
    assert puzzles.build_puzzles(data_dir=data_dir) == []


def test_themes_counts_match_solvable_puzzles(data_dir):
    _write(
        [
            _rec(
                "g1",
                [
                    _mistake(motifs=["hung_piece", "missed_fork"], ply=21),
                    _mistake(motifs=["hung_piece"], ply=23),
                    _mistake(motifs=["hung_piece"], best="", ply=25),  # unsolvable: not counted
                ],
            )
        ],
        data_dir,
    )
    out = puzzles.themes(data_dir=data_dir)
    assert out[0] == {
        "motif": "hung_piece",
        "label": "hanging pieces (leaving a piece en prise)",
        "count": 2,
    }
    assert {t["motif"]: t["count"] for t in out} == {"hung_piece": 2, "missed_fork": 1}


def test_lost_position_mistakes_are_excluded(data_dir):
    # win_before below _MIN_WIN_BEFORE: the player was already lost, no lesson to drill.
    _write(
        [_rec("g1", [_mistake(motifs=["hung_piece"]) | {"win_before": 15.0}])],
        data_dir,
    )
    assert puzzles.build_puzzles(data_dir=data_dir) == []


def test_opening_ply_mistakes_are_excluded(data_dir):
    # ply below _MIN_PLY: still book theory, not a real lesson.
    _write([_rec("g1", [_mistake(motifs=["hung_piece"], ply=6)])], data_dir)
    assert puzzles.build_puzzles(data_dir=data_dir) == []


def test_missing_win_before_or_ply_still_included(data_dir):
    # Old records without these fields shouldn't be filtered out.
    m1 = _mistake(motifs=["hung_piece"], ply=21)
    m1.pop("win_before")
    m2 = _mistake(motifs=["hung_piece"], ply=23)
    m2.pop("ply")
    m3 = _mistake(motifs=["hung_piece"], ply=25)
    m3.pop("win_before")
    m3.pop("ply")
    _write([_rec("g1", [m1, m2, m3])], data_dir)
    assert len(puzzles.build_puzzles(data_dir=data_dir)) == 3


def test_themes_counts_reflect_lost_position_and_ply_filters(data_dir):
    _write(
        [
            _rec(
                "g1",
                [
                    _mistake(motifs=["hung_piece"], ply=21),  # counted
                    _mistake(motifs=["hung_piece"], ply=23) | {"win_before": 10.0},  # excluded
                    _mistake(motifs=["hung_piece"], ply=6),  # excluded (opening)
                ],
            )
        ],
        data_dir,
    )
    out = puzzles.themes(data_dir=data_dir)
    assert {t["motif"]: t["count"] for t in out} == {"hung_piece": 1}


def test_black_to_move_puzzle_color(data_dir):
    _write([_rec("g1", [_mistake(fen=_FEN_B, played="d7d6", best="f6e4", best_san="Nxe4")])], data_dir)
    (p,) = puzzles.build_puzzles(data_dir=data_dir)
    assert p["color"] == "black"


# --- Lichess study chapter PGN -----------------------------------------------------------------
def test_puzzle_pgn_solution_mainline_played_variation(data_dir):
    _write([_rec("g1", [_mistake(motifs=["hung_piece"])])], data_dir)
    (p,) = puzzles.build_puzzles(data_dir=data_dir)
    pgn = lichess_study.puzzle_pgn(p)
    assert '[FEN "%s"]' % _FEN_W in pgn
    assert '[SetUp "1"]' in pgn
    assert "O-O $3" in pgn  # solution is the mainline, tagged "!!"
    assert "( 4. d3 $4" in pgn  # the played move is a "??" sideline
    assert "Find the move you missed" in pgn
    assert '[White "alice"]' in pgn and '[Black "bob"]' in pgn


def test_create_study_requires_token(monkeypatch):
    monkeypatch.setattr(config, "LICHESS_TOKEN", "")
    with pytest.raises(lichess_study.StudyError, match="study:write"):
        lichess_study.create_study("My study", [{"fen": _FEN_W, "solution_uci": "e1g1"}])


def test_create_study_posts_and_groups_by_orientation(monkeypatch, data_dir):
    _write(
        [
            _rec(
                "g1",
                [
                    _mistake(ply=21),  # white to move
                    _mistake(fen=_FEN_B, played="d7d6", best="f6e4", best_san="Nxe4", ply=22),
                ],
            )
        ],
        data_dir,
    )
    items = puzzles.build_puzzles(data_dir=data_dir)
    monkeypatch.setattr(config, "LICHESS_TOKEN", "tok")

    calls = []

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"id": "abc123XY"}

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append((url, data))
        return _Resp()

    monkeypatch.setattr(lichess_study.httpx, "post", fake_post)
    out = lichess_study.create_study("My blunders", items)

    assert out == {
        "study_id": "abc123XY",
        "study_url": f"{config.LICHESS_API_BASE}/study/abc123XY",
        "chapters": 2,
    }
    assert calls[0][0].endswith("/api/study")
    assert calls[0][1]["name"] == "My blunders"
    assert calls[0][1]["visibility"] == "private"
    # One import per orientation present, in practice mode.
    imports = [c for c in calls[1:]]
    assert all(u.endswith("/api/study/abc123XY/import-pgn") for u, _ in imports)
    assert sorted(d["orientation"] for _, d in imports) == ["black", "white"]
    assert all(d["mode"] == "practice" for _, d in imports)


def test_create_study_bad_token_maps_to_friendly_error(monkeypatch):
    monkeypatch.setattr(config, "LICHESS_TOKEN", "tok")

    class _Resp:
        status_code = 401
        text = "unauthorized"

        def json(self):
            return {}

    monkeypatch.setattr(lichess_study.httpx, "post", lambda *a, **k: _Resp())
    with pytest.raises(lichess_study.StudyError, match="study:write"):
        lichess_study.create_study("x", [{"fen": _FEN_W, "solution_uci": "e1g1", "color": "white"}])


# --- multi-move drill lines ----------------------------------------------------------------
# Kb5+Qd1 vs Ka8: 1.Kb6 Kb8 (forced) 2.Qd8# — a real mate-in-2 to exercise the sequence drill.
_FEN_M2 = "k7/8/8/1K6/8/8/8/3Q4 w - - 0 1"
_PV_M2 = ["b5b6", "a8b8", "d1d8"]


def test_mate_line_played_to_the_end(data_dir):
    _write(
        [_rec("g1", [_mistake(fen=_FEN_M2, played="d1d2", best="b5b6", best_san="Kb6",
                              motifs=["missed_mate"]) | {"best_line_uci": _PV_M2}])],
        data_dir,
    )
    (p,) = puzzles.build_puzzles(data_dir=data_dir)
    assert p["line_uci"] == _PV_M2
    assert p["line_san"] == ["Kb6", "Kb8", "Qd8#"]
    assert p["mate"] is True


def test_tactical_line_keeps_forcing_prefix_only(data_dir):
    # Qd2 vs qd5: 1.Qxd5 (capture) Ke7 2.Qb7+ (check) — kept; a quiet 3rd move would be dropped.
    fen = "4k3/8/8/3q4/8/8/3Q4/4K3 w - - 0 1"
    pv = ["d2d5", "e8e7", "d5b7"]
    _write(
        [_rec("g1", [_mistake(fen=fen, played="d2a5", best="d2d5", best_san="Qxd5",
                              motifs=["missed_capture"]) | {"best_line_uci": pv}])],
        data_dir,
    )
    (p,) = puzzles.build_puzzles(data_dir=data_dir)
    assert p["line_uci"] == pv  # capture, forced reply, check — all forcing
    assert p["mate"] is False


def test_quiet_mistake_stays_single_move(data_dir):
    # Same line, but a positional motif: the drill must not demand a long quiet engine PV.
    fen = "4k3/8/8/3q4/8/8/3Q4/4K3 w - - 0 1"
    _write(
        [_rec("g1", [_mistake(fen=fen, played="d2a5", best="d2d5", best_san="Qxd5",
                              motifs=["pawn_grab"]) | {"best_line_uci": ["d2d5", "e8e7", "d5b7"]}])],
        data_dir,
    )
    (p,) = puzzles.build_puzzles(data_dir=data_dir)
    assert p["line_uci"] == ["d2d5"]


def test_line_recovered_from_analysis_cache(data_dir, monkeypatch):
    # A record written before best_line_uci existed: the trainer pulls the PV from the cached
    # ReviewSession keyed by the same (game_id, side).
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    _write(
        [_rec("g1", [_mistake(fen=_FEN_M2, played="d1d2", best="b5b6", best_san="Kb6",
                              motifs=["missed_mate"], ply=27)])],
        data_dir,
    )
    cache_dir = os.path.join(data_dir, "analysis-cache")
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, "g1_white.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "session": {"mistakes": [{"ply": 27, "best_line_uci": _PV_M2}]}}, fh)
    (p,) = puzzles.build_puzzles(data_dir=data_dir)
    assert p["line_uci"] == _PV_M2
    assert p["mate"] is True


def test_puzzle_pgn_multi_move_mainline(data_dir):
    _write(
        [_rec("g1", [_mistake(fen=_FEN_M2, played="d1d2", best="b5b6", best_san="Kb6",
                              motifs=["missed_mate"]) | {"best_line_uci": _PV_M2}])],
        data_dir,
    )
    (p,) = puzzles.build_puzzles(data_dir=data_dir)
    pgn = lichess_study.puzzle_pgn(p)
    assert "Kb6 $3" in pgn and "Kb8" in pgn and "Qd8#" in pgn  # whole sequence is the mainline
    assert "play out the whole sequence" in pgn
    assert "( 1. Qd2 $4" in pgn  # the played move still hangs off the start as the ?? sideline
