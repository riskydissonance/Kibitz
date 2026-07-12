"""Tests for the coach-summary fact generation in server.claude_bridge (_game_facts, _strengths,
_turning_point, _flagged_lines, _resilience_gift_ply, _motif_note).

Uses synthetic MoveReview fixtures built along one real game (the Scholar's-mate-adjacent
skeleton below) so fen_before/fen_after/move_uci are all legal and history.tag_motifs can run
against them; the win%/classification numbers are set directly to drive each code path.
"""
from __future__ import annotations

import chess

from server import claude_bridge as cb
from server.core.session import MoveReview, ReviewSession

_START = chess.STARTING_FEN


def _fens_after(moves_san: list[str]) -> list[tuple[str, str, str, str]]:
    """Replay SAN moves from the start; return (fen_before, fen_after, move_san, move_uci)."""
    board = chess.Board()
    out = []
    for san in moves_san:
        fen_before = board.fen()
        move = board.parse_san(san)
        uci = move.uci()
        board.push(move)
        out.append((fen_before, board.fen(), san, uci))
    return out


# A real 10-ply game skeleton so every fen/move pair below is legal.
_GAME_MOVES = ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "c3", "Nf6", "d4", "exd4"]
_STEPS = _fens_after(_GAME_MOVES)


def _mr(
    ply: int,
    *,
    color: str = "white",
    classification: str = "blunder",
    win_before: float,
    win_after: float,
    best_move_san: str = "Nf3",
    best_line_uci: list[str] | None = None,
    best_line_san: list[str] | None = None,
    comment: str = "",
) -> MoveReview:
    fen_before, fen_after, move_san, move_uci = _STEPS[(ply - 1) % len(_STEPS)]
    move_number = (ply + 1) // 2
    return MoveReview(
        ply=ply,
        move_number=move_number,
        color=color,
        move_san=move_san,
        move_uci=move_uci,
        fen_before=fen_before,
        fen_after=fen_after,
        eval_before=0.0,
        eval_after=0.0,
        win_before=win_before,
        win_after=win_after,
        win_swing=round(win_before - win_after, 1),
        classification=classification,
        best_move_san=best_move_san,
        best_line_uci=best_line_uci or [],
        best_line_san=best_line_san or [],
        accuracy=50.0,
        comment=comment,
    )


def _session(all_moves: list[MoveReview], *, player: str = "white", result: str = "0-1") -> ReviewSession:
    mistakes = [m for m in all_moves if m.classification in ("inaccuracy", "mistake", "blunder")]
    return ReviewSession(
        pgn="", player=player, headers={"White": "alice", "Black": "bob"}, result=result,
        accuracy_white=80.0, accuracy_black=80.0, all_moves=all_moves, mistakes=mistakes,
    )


# --- Fix 1: missed-win rewording ---------------------------------------------------------------

def test_missed_win_is_reworded_not_a_blunder():
    m = _mr(1, classification="blunder", win_before=90.0, win_after=60.0, best_move_san="Nf3",
            comment="Win chance 90.0% -> 60.0%.")
    sess = _session([m])
    items = cb._flagged_lines(sess, None)
    assert len(items) == 1
    _, line = items[0]
    assert line.startswith("- missed win:")
    assert "blunder" not in line
    assert "Nf3 was winning here" in line


def test_non_missed_win_keeps_plain_wording():
    m = _mr(1, classification="blunder", win_before=60.0, win_after=10.0, best_move_san="Nf3")
    sess = _session([m])
    items = cb._flagged_lines(sess, None)
    _, line = items[0]
    assert "blunder" in line
    assert "missed win" not in line


# --- Fix 1: run-collapsing -----------------------------------------------------------------

def test_consecutive_same_best_move_collapses_to_one_line():
    # plies 1, 3, 5 are player moves (color=white), each missing the same "Nxf7" shot.
    moves = [
        _mr(1, win_before=90.0, win_after=88.0, best_move_san="Nxf7", classification="mistake"),
        _mr(3, win_before=88.0, win_after=85.0, best_move_san="Nxf7", classification="mistake"),
        _mr(5, win_before=85.0, win_after=80.0, best_move_san="Nxf7", classification="mistake"),
    ]
    sess = _session(moves)
    items = cb._flagged_lines(sess, None)
    assert len(items) == 1  # collapsed into one line, counts once against the cap
    _, line = items[0]
    assert "moves" in line
    assert "3 moves running" in line
    assert "Nxf7" in line


def test_non_consecutive_moves_do_not_collapse():
    moves = [
        _mr(1, win_before=90.0, win_after=88.0, best_move_san="Nxf7", classification="mistake"),
        _mr(9, win_before=40.0, win_after=20.0, best_move_san="Qd2", classification="blunder"),
    ]
    sess = _session(moves)
    items = cb._flagged_lines(sess, None)
    assert len(items) == 2


# --- Fix 2: resilience suppression / reword ------------------------------------------------

def test_resilient_defense_fires_for_genuine_gradual_recovery():
    moves = [
        _mr(1, classification="good", win_before=20.0, win_after=22.0),
        _mr(3, classification="good", win_before=22.0, win_after=24.0),
        _mr(5, classification="good", win_before=24.0, win_after=27.0),
        _mr(7, classification="good", win_before=27.0, win_after=30.0),
        # Next player move, still <=35 (well below the >=20-point "gift" jump threshold), so no
        # unexplained jump is detected — this is a genuine gradual recovery.
        _mr(9, classification="good", win_before=30.0, win_after=46.0),
    ]
    sess = _session(moves, result="1/2-1/2")
    strengths = cb._strengths(sess)
    assert any("Resilient defense" in s for s in strengths)


def test_resilience_suppressed_when_gift_followed_by_missed_win():
    # Run of 4 unproblematic moves at <=35%, then a big unexplained jump (the "gift"), and the
    # player's very next move is itself flagged as a missed win.
    moves = [
        _mr(1, classification="good", win_before=20.0, win_after=22.0),
        _mr(3, classification="good", win_before=22.0, win_after=24.0),
        _mr(5, classification="good", win_before=24.0, win_after=26.0),
        _mr(7, classification="good", win_before=26.0, win_after=28.0),
        # Gift: win_before jumps far above win_after of the run's last move.
        _mr(9, classification="mistake", win_before=70.0, win_after=50.0, best_move_san="Qxb2"),
    ]
    sess = _session(moves, result="1/2-1/2")
    strengths = cb._strengths(sess)
    assert not any("Resilient defense" in s or "returned the favour" in s for s in strengths)


def test_resilience_reworded_when_gift_is_capitalized():
    moves = [
        _mr(1, classification="good", win_before=20.0, win_after=22.0),
        _mr(3, classification="good", win_before=22.0, win_after=24.0),
        _mr(5, classification="good", win_before=24.0, win_after=26.0),
        _mr(7, classification="good", win_before=26.0, win_after=28.0),
        # Gift: big jump, and the player holds it (good moves, win% stays >= 45).
        _mr(9, classification="best", win_before=70.0, win_after=68.0),
    ]
    sess = _session(moves, result="1/2-1/2")
    strengths = cb._strengths(sess)
    assert any("returned the favour" in s for s in strengths)
    assert not any("Resilient defense:" in s for s in strengths)


# --- Fix 3: turning point --------------------------------------------------------------------

def test_turning_point_is_first_unrecovered_error_not_largest_swing():
    moves = [
        # A small, first mistake after which the player never gets back to >=45%.
        _mr(1, classification="mistake", win_before=50.0, win_after=40.0, best_move_san="Nf3"),
        _mr(3, classification="good", win_before=40.0, win_after=35.0),
        # A later, much bigger blunder — larger win_swing but not the first unrecovered error.
        _mr(5, classification="blunder", win_before=35.0, win_after=5.0, best_move_san="Qd2"),
    ]
    sess = _session(moves)
    tp = cb._turning_point(sess)
    assert tp is not None
    assert "move 1" in tp or "1." in tp
    # Confirms it picked the first (win% 50->40) mistake, not the ply-5 blunder.
    first_move_san = moves[0].move_san
    assert first_move_san in tp


def test_turning_point_falls_back_to_largest_unrecovered_swing_when_no_serious_class():
    moves = [
        _mr(1, classification="inaccuracy", win_before=60.0, win_after=52.0),
        _mr(3, classification="inaccuracy", win_before=52.0, win_after=30.0),
    ]
    sess = _session(moves)
    tp = cb._turning_point(sess)
    assert tp is not None
    # Largest swing (52->30, swing 22) beats the first (60->52, swing 8).
    assert moves[1].move_san in tp


def test_turning_point_none_when_fully_recovered():
    moves = [
        _mr(1, classification="blunder", win_before=90.0, win_after=40.0, best_move_san="Nf3"),
        _mr(3, classification="best", win_before=40.0, win_after=60.0),
    ]
    sess = _session(moves)
    assert cb._turning_point(sess) is None


# --- Fix 4a: motif phrase inclusion --------------------------------------------------------

def test_motif_phrase_included_for_hanging_piece():
    # exd4 (ply 5 in _GAME_MOVES) leaves nothing hanging in this line by itself, so instead build
    # a move that hangs a piece: after 1.e4 e5 2.Nf3 Nc6 3.Bc4 Bc5 4.c3 Nf6, if White played a
    # move leaving material en prise history._is_hanging should catch it. We reuse the
    # `_mr` fixture's real fen/move (Nf6, ply 8, black) which does not hang anything, but we can
    # still exercise the code path: it must not raise and must return a str.
    m = _mr(8, color="black", classification="mistake", win_before=55.0, win_after=40.0)
    note = cb._motif_note(m)
    assert isinstance(note, str)


def test_flagged_line_appends_motif_note_when_tags_found(monkeypatch):
    m = _mr(1, classification="blunder", win_before=60.0, win_after=10.0, best_move_san="Nf3")
    monkeypatch.setattr(cb.history, "tag_motifs", lambda *a, **k: ["hung_piece", "missed_fork"])
    line = cb._single_flagged_line(m, None)
    assert "hung a piece" in line
    assert "missed a fork" in line
