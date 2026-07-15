"""Turn a player's OWN mistakes into puzzles — a "learn from your blunders" trainer.

Every analysed game already stores, per flagged mistake, the position it was played from
(`fen_before`), the move actually played (`uci`), the engine's best reply (`best_uci`/`best_san`),
the tactical motif tags (`motifs`), and how much win chance it cost (`win_drop`) — see
`history.build_game_record`. That's exactly a single-best-move puzzle: sit the solver in the
position they went wrong in and ask them to find the move they missed.

So puzzles are pure re-use of existing analysis — no engine calls — which keeps this off the
engine pool entirely. Two entry points:

  - build_puzzles(...)  -> list[Puzzle dicts], hardest-lesson-first, filterable by theme/kind
  - themes(...)         -> per-motif counts (labelled) for "train your weaknesses" chips

Both draw from the SAME set of games as the Insights panel (`history.my_records`), so a weakness
the Insights panel names always has puzzles behind it.
"""
from __future__ import annotations

from typing import Optional

import chess

from server.core import analysis_cache
from server.core import history

# Blunders are the most instructive (biggest swings), then mistakes, then inaccuracies. Used both
# to sort puzzles hardest-lesson-first and to let the UI offer a severity filter.
_KIND_ORDER = {"blunder": 0, "mistake": 1, "inaccuracy": 2}

# Motifs where the engine's follow-up IS the lesson (a concrete tactic), so the drill continues
# past the first move while the sequence stays forcing. Positional slips drill one move only —
# a long quiet PV there is engine noise, not a combination to find.
_TACTICAL_MOTIFS = {
    "missed_mate", "allowed_mate", "missed_capture", "missed_fork", "hung_piece", "back_rank",
}

_MAX_DRILL_PLIES = 9  # up to 5 solver moves; mate lines still end exactly on the mate

# Below this win% before the mistake, the player was already lost — drilling "the move you missed"
# in a hopeless position is a poor lesson (there usually wasn't a save anyway).
_MIN_WIN_BEFORE = 20.0

# Ply < 10 (first 5 full moves) is still book/opening theory — engine evals are noisy there and
# "mistakes" flagged that early are rarely a real lesson.
_MIN_PLY = 10


def _drill_line(fen: str, pv: list[str], motifs: list[str]) -> tuple[list[str], list[str], bool]:
    """The playable drill sequence from `pv`: (ucis, sans, ends_in_mate).

    Replays the PV validating legality, then decides how much of it the solver must find:
      - a line reaching CHECKMATE keeps everything up to (and including) the mating move;
      - a tactical mistake (see _TACTICAL_MOTIFS) keeps the FORCING prefix — each further solver
        move must be a capture, check or promotion, stopping before the first quiet move;
      - anything else is a one-move puzzle.
    Always ends on a solver move (odd length), never exceeds _MAX_DRILL_PLIES.
    """
    board = chess.Board(fen)
    moves: list[chess.Move] = []
    sans: list[str] = []
    forcing: list[bool] = []  # per ply: was the move a capture / check / promotion?
    mate_at: Optional[int] = None  # ply index whose move delivered mate
    for i, uci in enumerate(pv[:_MAX_DRILL_PLIES]):
        try:
            mv = chess.Move.from_uci(uci)
        except ValueError:
            break
        if not board.is_legal(mv):
            break
        forcing.append(bool(board.is_capture(mv) or mv.promotion or board.gives_check(mv)))
        sans.append(board.san(mv))
        board.push(mv)
        moves.append(mv)
        if board.is_checkmate():
            mate_at = i
            break

    if not moves:
        return [], [], False
    if mate_at is not None and mate_at % 2 == 0:  # the solver delivers the mate — play it all out
        keep = mate_at + 1
        return [m.uci() for m in moves[:keep]], sans[:keep], True

    keep = 1
    if _TACTICAL_MOTIFS & set(motifs or []):
        # Extend two plies at a time (opponent reply + our next move) while our move stays forcing.
        while keep + 2 <= len(moves) and forcing[keep + 1]:
            keep += 2
    return [m.uci() for m in moves[:keep]], sans[:keep], False


def _san(fen: str, uci: str) -> Optional[str]:
    """SAN for `uci` in `fen`, or None if the move is illegal there (defensive: stored data)."""
    try:
        board = chess.Board(fen)
        move = chess.Move.from_uci(uci)
        if not board.is_legal(move):  # san() alone doesn't reject illegal moves
            return None
        return board.san(move)
    except (ValueError, AssertionError, KeyError):
        return None


def _puzzle_from_mistake(rec: dict, m: dict, pv: Optional[list[str]] = None) -> Optional[dict]:
    """One puzzle dict from a stored mistake, or None if it can't be a solvable puzzle.

    A mistake makes a puzzle only when we have the position AND a concrete better move to find,
    and that better move actually differs from what was played (else there's nothing to solve).
    `pv` is the engine's full best line when known (record field or analysis-cache); the drill
    then continues past move one for tactics and plays mates out to the end.
    """
    fen = (m.get("fen_before") or "").strip()
    solution = (m.get("best_uci") or "").strip()
    played = (m.get("uci") or "").strip()
    if not fen or not solution or solution == played:
        return None

    win_before = m.get("win_before")
    if win_before is not None and win_before < _MIN_WIN_BEFORE:
        return None
    ply = m.get("ply")
    if ply is not None and ply < _MIN_PLY:
        return None

    legal_san = _san(fen, solution)
    if not legal_san:  # unusable solution (illegal in the stored FEN) — skip rather than lie
        return None
    solution_san = m.get("best_san") or legal_san

    try:
        side_to_move = "white" if chess.Board(fen).turn == chess.WHITE else "black"
    except ValueError:
        return None

    motifs = list(m.get("motifs") or [])
    line = pv if pv and pv[0] == solution else [solution]
    line_uci, line_san, mate = _drill_line(fen, line, motifs)
    if not line_uci:  # can't happen once `solution` validated legal, but stay defensive
        line_uci, line_san, mate = [solution], [solution_san], False
    return {
        "id": f"{rec.get('game_id', '')}:{m.get('ply', 0)}",
        "game_id": rec.get("game_id"),
        "game_url": rec.get("game_url"),
        "date": rec.get("date"),
        "white": rec.get("white"),
        "black": rec.get("black"),
        "opening": rec.get("opening") or rec.get("eco"),
        "move_number": m.get("move_number"),
        "color": side_to_move,  # the side to move in the puzzle == the side that blundered == solver
        "fen": fen,
        "played_uci": played,
        "played_san": m.get("san") or _san(fen, played),
        "solution_uci": solution,
        "solution_san": solution_san,
        # The full drill sequence (solver moves at even indices, engine replies between); one
        # entry = a plain single-move puzzle. `mate` marks a line that ends in checkmate.
        "line_uci": line_uci,
        "line_san": line_san,
        "mate": mate,
        "classification": m.get("classification"),
        "phase": m.get("phase"),
        "win_drop": m.get("win_drop", 0.0),
        "motifs": motifs,
        "themes": [history._MOTIF_LABELS.get(x, x) for x in motifs],
    }


def build_puzzles(
    motif: Optional[str] = None,
    kinds: Optional[list[str]] = None,
    days: Optional[int] = None,
    limit: Optional[int] = None,
    data_dir: Optional[str] = None,
    eco: Optional[str] = None,
) -> list[dict]:
    """Puzzles built from the user's own mistakes, hardest lesson first.

    `motif` keeps only puzzles tagged with that motif (e.g. "hung_piece", "missed_fork") — the
    "train this weakness" filter. `kinds` keeps only those classifications (subset of
    inaccuracy/mistake/blunder; default all). `days` limits to recent games (0/None = all).
    `eco` keeps only puzzles whose source game matches that ECO code (case-insensitive) — the
    "drill this opening" filter from the Insights repertoire report.
    Sorted blunders-first then by win chance lost, so the biggest, clearest lessons come first.
    """
    kept = set(kinds) if kinds else None
    eco_filter = eco.upper() if eco else None
    puzzles: list[dict] = []
    for rec in history.my_records(days, data_dir):
        if eco_filter is not None and (rec.get("eco") or "").upper() != eco_filter:
            continue
        cached_pvs: Optional[dict[int, list[str]]] = None  # analysis-cache lines, fetched lazily
        for m in rec.get("mistakes", []):
            if kept is not None and m.get("classification") not in kept:
                continue
            if motif is not None and motif not in (m.get("motifs") or []):
                continue
            # Full engine PV for the sequence drill: on the record for new games; recovered from
            # the analysis cache for records written before best_line_uci was stored.
            pv = m.get("best_line_uci")
            if not pv:
                if cached_pvs is None:
                    cached_pvs = analysis_cache.mistake_lines(
                        rec.get("game_id") or "", rec.get("reviewed_side") or ""
                    )
                pv = cached_pvs.get(m.get("ply"))
            p = _puzzle_from_mistake(rec, m, pv)
            if p is not None:
                puzzles.append(p)

    puzzles.sort(
        key=lambda p: (_KIND_ORDER.get(p["classification"], 9), -float(p.get("win_drop") or 0.0))
    )
    if limit and limit > 0:
        puzzles = puzzles[:limit]
    return puzzles


def themes(
    days: Optional[int] = None,
    kinds: Optional[list[str]] = None,
    data_dir: Optional[str] = None,
) -> list[dict]:
    """Per-motif puzzle counts (labelled), most common first — the "your weaknesses" chips.

    Only counts motifs that actually yield a solvable puzzle, and honours the same `kinds`
    severity filter as `build_puzzles`, so a chip's count always matches how many puzzles
    `build_puzzles(motif=..., kinds=...)` will return.
    """
    kept = set(kinds) if kinds else None
    counts: dict[str, int] = {}
    for rec in history.my_records(days, data_dir):
        for m in rec.get("mistakes", []):
            if kept is not None and m.get("classification") not in kept:
                continue
            if _puzzle_from_mistake(rec, m) is None:
                continue
            for motif in m.get("motifs") or []:
                counts[motif] = counts.get(motif, 0) + 1
    out = [
        {"motif": k, "label": history._MOTIF_LABELS.get(k, k), "count": v}
        for k, v in counts.items()
    ]
    out.sort(key=lambda t: (-t["count"], t["motif"]))
    return out
