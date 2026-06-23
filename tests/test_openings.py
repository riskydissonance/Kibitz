"""Local ECO/opening lookup (no engine — pure python-chess over the vendored Lichess dataset)."""
from __future__ import annotations

from server.core import openings


def test_najdorf_deepest_match():
    eco, name = openings.classify_from_pgn("1. e4 c5 2. Nf3 d6 3. d4 cxd4 4. Nxd4 Nf6 5. Nc3 a6")
    assert eco == "B90"
    assert name == "Sicilian Defense: Najdorf Variation"


def test_transposition_matches_via_epd():
    # Italian reached by a different move order (2...Bc4 before ...Nc6) lands on the same position.
    _, direct = openings.classify_from_pgn("1. e4 e5 2. Nf3 Nc6 3. Bc4")
    _, transposed = openings.classify_from_pgn("1. e4 e5 2. Bc4 Nc6 3. Nf3")
    assert direct == transposed == "Italian Game"


def test_classify_from_fens_keeps_deepest():
    import chess

    board = chess.Board()
    fens = []
    for mv in "e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6".split():
        board.push_san(mv)
        fens.append(board.fen())
    eco, name = openings.classify_from_fens(fens)
    assert (eco, name) == ("B90", "Sicilian Defense: Najdorf Variation")


def test_no_match_returns_none():
    # A position off-book by move 1 (and an empty input) yields no name rather than raising.
    assert openings.classify_from_fens([]) == (None, None)


def test_book_loaded():
    assert len(openings._book()) > 3000
