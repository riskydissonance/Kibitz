"""Export a batch of puzzles (from `puzzles.build_puzzles`) into a Lichess study.

Lichess has no API to create user PUZZLES (those are generated centrally from master games), but
it does let you create a STUDY and import chapters via the API. So the trainer's positions become
study chapters instead — and with `mode=practice` each chapter is an interactive "find the moves"
drill on Lichess itself, which is as close to a personal puzzle set as the platform allows.

Two write calls (both need a token with the `study:write` scope):
  - POST /api/study                     -> create the study, returns {"id": ...}
  - POST /api/study/{id}/import-pgn     -> add up to 64 chapters from a multi-game PGN

Each puzzle becomes one chapter whose mainline IS the solution move (so practice mode asks the
solver to find it), with the move actually played attached as an annotated `??` sideline. Chapters
are imported grouped by side-to-move, because import-pgn sets one board orientation for the whole
batch and we want the solver oriented as the side that has to move.
"""
from __future__ import annotations

import io
from typing import Optional

import chess
import chess.pgn
import httpx

from server import config

# Lichess study limits (see the API spec): a study holds at most 64 chapters, and one import-pgn
# call takes a PGN of up to that many games. We also cap the whole export so one click can't try
# to push hundreds of positions.
MAX_CHAPTERS = 64

# NAG 4 = "??" (blunder), NAG 3 = "!!" (brilliant) — annotate the played move vs. the one to find.
_NAG_BLUNDER = 4
_NAG_BEST = 3


class StudyError(RuntimeError):
    """A user-facing problem creating the study (missing token, wrong scope, network, rate limit)."""


def _headers() -> dict[str, str]:
    return {
        "User-Agent": "kibitz-chess-tutor",
        "Authorization": f"Bearer {config.LICHESS_TOKEN}",
    }


def puzzle_pgn(puzzle: dict) -> str:
    """One study chapter, as PGN, for a single puzzle.

    Mainline = the solution move (what practice mode asks the solver to find). The move actually
    played in the game hangs off as a variation flagged `??`, with a comment naming the lesson, so
    after solving (or peeking) the user sees exactly what they did and why it was worse.
    """
    board = chess.Board(puzzle["fen"])
    game = chess.pgn.Game()
    # SetUp/FEN make the chapter start from the puzzle position; the rest is context for the header.
    game.headers["Event"] = _chapter_title(puzzle)
    game.headers["Site"] = puzzle.get("game_url") or "https://lichess.org"
    game.headers["White"] = puzzle.get("white") or "?"
    game.headers["Black"] = puzzle.get("black") or "?"
    game.headers["Result"] = "*"
    if puzzle.get("date"):
        game.headers["Date"] = puzzle["date"]
    game.headers["FEN"] = puzzle["fen"]
    game.headers["SetUp"] = "1"
    game.headers["Orientation"] = puzzle.get("color") or "white"

    drop = puzzle.get("win_drop")
    lesson = f"You played {puzzle.get('played_san') or '??'}"
    if drop:
        lesson += f", losing {drop}% win chance"
    lesson += ". Find the move you missed."
    game.comment = lesson

    # Mainline = the whole drill sequence (solution + forced follow-up / mate), so practice mode
    # asks for every move of the tactic. Single-move puzzles just get their one move.
    line = puzzle.get("line_uci") or [puzzle["solution_uci"]]
    node = game
    for i, uci in enumerate(line):
        mv = chess.Move.from_uci(uci)
        if not board.is_legal(mv):
            break
        board.push(mv)
        node = node.add_variation(mv, nags={_NAG_BEST} if i == 0 else set())
        if i == 0:
            node.comment = "The move to find." if len(line) == 1 else "The move to find — play out the whole sequence."
    if puzzle.get("mate") and board.is_checkmate():
        node.comment = ((node.comment or "") + " Checkmate.").strip()

    played_uci = (puzzle.get("played_uci") or "").strip()
    if played_uci and played_uci != puzzle["solution_uci"]:
        try:
            played = chess.Move.from_uci(played_uci)
            start = chess.Board(puzzle["fen"])
            if start.is_legal(played):
                var = game.add_variation(played, nags={_NAG_BLUNDER})
                var.comment = "The move you played."
        except ValueError:
            pass

    exporter = chess.pgn.StringExporter(headers=True, variations=True, comments=True)
    return game.accept(exporter)


def _chapter_title(puzzle: dict) -> str:
    """A short, distinct chapter name: opponent + theme, so the study's chapter list reads as a
    table of contents of what to work on."""
    themes = puzzle.get("themes") or []
    head = (themes[0] if themes else (puzzle.get("classification") or "mistake")).capitalize()
    opp = puzzle.get("black") if puzzle.get("color") == "white" else puzzle.get("white")
    move_no = puzzle.get("move_number")
    where = f" (move {move_no})" if move_no else ""
    return f"{head} vs {opp or '?'}{where}"


def _post(url: str, data: dict) -> httpx.Response:
    try:
        return httpx.post(url, data=data, headers=_headers(), timeout=config.LICHESS_TIMEOUT)
    except httpx.HTTPError as exc:
        raise StudyError(f"Could not reach Lichess: {exc}") from exc


def _raise_for_status(resp: httpx.Response, *, what: str) -> None:
    if resp.status_code in (401, 403):
        raise StudyError(
            "Lichess rejected the token for studies. Create a Personal Access Token WITH the "
            '"Create, update, delete studies" (study:write) permission at '
            "https://lichess.org/account/oauth/token and paste it in ⚙ Settings → Lichess token."
        )
    if resp.status_code == 429:
        raise StudyError("Lichess rate limit hit (max 30 new studies/day). Try again later.")
    if resp.status_code >= 400:
        raise StudyError(f"Lichess error {what} (HTTP {resp.status_code}): {resp.text[:200]}")


def create_study(name: str, puzzles: list[dict]) -> dict:
    """Create a Lichess study and import the puzzles as practice chapters.

    Returns {"study_url", "study_id", "chapters"} on success. Raises StudyError (user-facing) on
    any problem — a missing/under-scoped token being the common one.
    """
    if not config.LICHESS_TOKEN:
        raise StudyError(
            "Set a Lichess token to export to a study. Create one with the study:write permission "
            "at https://lichess.org/account/oauth/token, then paste it in ⚙ Settings → Lichess token."
        )
    if not puzzles:
        raise StudyError("No puzzles to export.")
    puzzles = puzzles[:MAX_CHAPTERS]

    create = _post(
        f"{config.LICHESS_API_BASE}/api/study",
        {
            "name": name[:100],
            "visibility": "private",
            # Study-wide feature toggles are required by the API; sensible permissive defaults.
            "computer": "everyone",
            "explorer": "everyone",
            "cloneable": "everyone",
            "shareable": "everyone",
            "chat": "everyone",
        },
    )
    _raise_for_status(create, what="creating the study")
    study_id = (create.json() or {}).get("id")
    if not study_id:
        raise StudyError("Lichess created the study but returned no id.")

    # import-pgn sets one orientation for the whole call, so group by side-to-move and import each
    # group separately — the solver is then always oriented as the side that must move.
    for color in ("white", "black"):
        group = [p for p in puzzles if (p.get("color") or "white") == color]
        if not group:
            continue
        # A PGN with multiple games (blank-line separated) becomes multiple chapters in one call.
        pgn = "\n\n\n".join(puzzle_pgn(p) for p in group)
        imp = _post(
            f"{config.LICHESS_API_BASE}/api/study/{study_id}/import-pgn",
            {"pgn": pgn, "orientation": color, "mode": "practice"},
        )
        _raise_for_status(imp, what="adding chapters")

    return {
        "study_id": study_id,
        "study_url": f"{config.LICHESS_API_BASE}/study/{study_id}",
        "chapters": len(puzzles),
    }
