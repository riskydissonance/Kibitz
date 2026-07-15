"""History + game-import + background-analysis routes for the web board.

Powers the board's third column: list previously-analysed local games (`/api/history`), browse a
Lichess user's recent games (`/api/lichess/games`), and reopen any of them by kicking off a
background analysis (`/api/analyze` + `/api/analysis-status`). Sync `def` handlers like
routes_board — they're light (history is a file read; lichess is a short HTTP call; analyze just
spawns a thread).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server import config
from server.core import chesscom
from server.core import game_analysis
from server.core import history
from server.core import lichess
from server.core import lichess_study
from server.core import multipgn
from server.core import puzzles
from server.core import reviews
from server.core import srs
from server.web import jobs

router = APIRouter()


class AnalyzeBody(BaseModel):
    pgn: str
    player: str = "auto"


class SyncBody(BaseModel):
    username: str = ""  # blank -> the configured chess.com handle
    max: int = 0  # how many recent games to check; 0 -> config.CHESSCOM_SYNC_MAX
    auto: bool = False  # launch-time sync (honours CHESS_CHESSCOM_SYNC=0) vs an explicit user click


class AnalyzeBatchBody(BaseModel):
    pgn: str
    player: str = "auto"
    username: str = ""  # the uploader's handle; blank -> auto-detect (handle common to all games)


def _side_for(headers: dict, self_handle: str | None, player: str) -> str:
    """Which side to review for one game: the uploader's handle if present, else the chosen
    player, else auto-detect from configured handles."""
    if self_handle:
        sh = self_handle.strip().lower()
        if (headers.get("White") or "").strip().lower() == sh:
            return "white"
        if (headers.get("Black") or "").strip().lower() == sh:
            return "black"
    if (player or "").lower() in ("white", "black"):
        return player.lower()
    return game_analysis.resolve_player(headers, "auto")


@router.get("/history")
def get_history() -> dict:
    """Newest-first list of EVERY previously-analysed game (for the "My games" panel).

    Intentionally unfiltered: we show all analysed games regardless of which account they were
    recorded under, so games analysed for a handle that isn't the configured user (e.g. a pasted
    Chess.com game, or a game reviewed from the opponent's side) are still reachable here.
    """
    try:
        rows = history.history_rows()
    except Exception as exc:  # pragma: no cover - history must never break the board
        return {"games": [], "error": str(exc)}
    return {"player_id": history.my_player_id(), "games": rows}


class ReviewBody(BaseModel):
    game_id: str
    reviewed_side: str = ""
    reviewed: bool | None = None
    note: str | None = None


@router.get("/reviews")
def get_reviews() -> dict:
    try:
        return {"reviews": reviews.review_states()}
    except Exception as exc:  # pragma: no cover - reviews must never break the board
        return {"reviews": {}, "error": str(exc)}


@router.post("/reviews")
def post_review(body: ReviewBody) -> JSONResponse:
    if not (body.game_id or "").strip():
        return JSONResponse({"error": "game_id required"}, status_code=400)
    if body.reviewed is None and body.note is None:
        return JSONResponse({"error": "nothing to update"}, status_code=400)
    try:
        st = reviews.set_review(
            body.game_id, body.reviewed_side or "", reviewed=body.reviewed, note=body.note
        )
        return JSONResponse({"ok": True, "state": st})
    except Exception as exc:  # pragma: no cover - defensive
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.get("/insights")
def get_insights(days: int = 0) -> dict:
    """Cross-game insights for the configured user within a time window (days=0 -> all time)."""
    try:
        return history.insights(days if days > 0 else None)
    except Exception as exc:  # pragma: no cover - insights must never break the board
        return {"games": 0, "error": str(exc)}


@router.get("/lichess/games")
def get_lichess_games(username: str = "", max: int = config.LICHESS_DEFAULT_MAX, perf: str = "") -> JSONResponse:
    """Recent Lichess games (newest first) for `username` (blank -> configured CHESS_USERNAME)."""
    try:
        games = lichess.fetch_user_games(username, max=max, perf=perf or None)
    except lichess.LichessError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"count": len(games), "games": [g.to_dict() for g in games]})


@router.get("/chesscom/games")
def get_chesscom_games(username: str = "", max: int = config.LICHESS_DEFAULT_MAX) -> JSONResponse:
    """Recent Chess.com games (newest first) for `username` (blank -> configured handle)."""
    try:
        games = chesscom.fetch_user_games(username, max=max)
    except chesscom.ChesscomError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"count": len(games), "games": [g.to_dict() for g in games]})


def _known_game_urls() -> set[str]:
    """Normalised URLs of every game already in history, for sync dedup."""
    urls = set()
    for r in history.load_records():
        url = (r.get("game_url") or "").strip().rstrip("/")
        if url:
            urls.add(url)
    return urls


@router.post("/sync/chesscom")
def post_sync_chesscom(body: SyncBody | None = None) -> JSONResponse:
    """Auto-sync: fetch the configured user's recent Chess.com games and analyse the new ones.

    Checks the newest `max` games against history (by game URL) and kicks off a background batch
    analysis of any not seen before — so they land in "My games" with no paste/upload. Returns the
    batch bootstrap (first game's PGN + side) when something new was found, else {"new_games": 0}.
    """
    if body and body.auto and not config.CHESSCOM_SYNC_ENABLED:
        return JSONResponse({"new_games": 0, "disabled": True})
    username = ((body.username if body else "") or config.CHESSCOM_USERNAME or "").strip()
    if not username:
        return JSONResponse({"error": "No chess.com username configured."}, status_code=400)
    n = (body.max if body and body.max > 0 else 0) or config.CHESSCOM_SYNC_MAX
    try:
        games = chesscom.fetch_user_games(username, max=n)
    except chesscom.ChesscomError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)

    known = _known_game_urls()
    new = [g for g in games if g.pgn and (g.url or "").strip().rstrip("/") not in known]
    if not new:
        return JSONResponse({"new_games": 0, "total_checked": len(games)})

    pgns = [g.pgn for g in new]
    sides = [_side_for(multipgn.headers_of(p), username, "auto") for p in pgns]
    jobs.start_batch(pgns, sides, self_handle=username, platform="chesscom")
    return JSONResponse(
        {
            "status": "pending",
            "new_games": len(new),
            "total_checked": len(games),
            "first_pgn": pgns[0],
            "first_side": sides[0],
            "self_handle": username,
        }
    )


@router.post("/analyze")
def post_analyze(body: AnalyzeBody) -> JSONResponse:
    """Start a background analysis of `pgn` (reviewing `player`); returns immediately as pending."""
    if not (body.pgn or "").strip():
        return JSONResponse({"error": "No PGN provided."}, status_code=400)
    return JSONResponse(jobs.start(body.pgn, player=body.player or "auto"))


@router.post("/analyze-batch")
def post_analyze_batch(body: AnalyzeBatchBody) -> JSONResponse:
    """Analyse a multi-game PGN (e.g. a Chess.com export) in the background, recording each game so
    the whole upload appears in "My games". Returns immediately with the game count + the first
    game (so the board can show it while the rest run)."""
    games = multipgn.split_pgn(body.pgn or "")
    if not games:
        return JSONResponse({"error": "No valid games found in that PGN."}, status_code=400)

    prefer = [config.USERNAME] + [a for _, a in config.USERNAME_ALIASES]
    self_handle = (body.username or "").strip() or multipgn.detect_self_handle(games, prefer=prefer)
    first_headers = multipgn.headers_of(games[0])
    platform = history._platform_from_headers(first_headers)
    sides = [_side_for(multipgn.headers_of(g), self_handle, body.player) for g in games]

    jobs.start_batch(games, sides, self_handle=self_handle, platform=platform)
    return JSONResponse(
        {
            "status": "pending",
            "total_games": len(games),
            "first_pgn": games[0],
            "first_side": sides[0],
            "self_handle": self_handle,
        }
    )


@router.get("/analysis-status")
def get_analysis_status() -> dict:
    """Poll target while a background analysis runs: idle | pending | ready | error."""
    return jobs.status()


# --- Puzzle trainer: solve your own mistakes, and export them to a Lichess study ----------------
_KINDS = ("inaccuracy", "mistake", "blunder")


def _parse_kinds(kinds: str) -> list[str] | None:
    """Comma-separated classification filter -> validated list (None = all)."""
    picked = [k.strip() for k in (kinds or "").split(",") if k.strip() in _KINDS]
    return picked or None


class StudyBody(BaseModel):
    name: str = ""  # study title; blank -> a sensible default
    motif: str = ""  # blank -> all motifs
    kinds: str = ""  # comma-separated subset of inaccuracy,mistake,blunder; blank -> all
    days: int = 0  # 0 -> all history
    limit: int = 60  # cap chapters (Lichess allows 64 per study)


@router.get("/puzzles")
def get_puzzles(
    motif: str = "", kinds: str = "", days: int = 0, limit: int = 0, order: str = "srs", eco: str = ""
) -> dict:
    """Puzzles built from the configured user's own mistakes (hardest lesson first).

    `motif` trains one weakness (e.g. "hung_piece"); `kinds` filters severity; `days` limits to
    recent games; `eco` drills one opening (from the Insights repertoire report). No engine work
    — pure re-use of stored analysis. `order="srs"` (default) puts due/previously-failed puzzles
    first per the Leitner scheduler (`srs.order_puzzles`); any other value keeps `build_puzzles`'s
    own order (still annotated with each puzzle's `srs` state)."""
    try:
        items = puzzles.build_puzzles(
            motif=motif or None,
            kinds=_parse_kinds(kinds),
            days=days or None,
            limit=limit or None,
            eco=eco or None,
        )
        if order == "srs":
            items = srs.order_puzzles(items)
        else:
            states = srs.puzzle_states()
            now = datetime.now(timezone.utc)
            for p in items:
                st = states.get(p.get("id"))
                p["srs"] = {
                    "box": st.get("box", 0) if st else 0,
                    "due": srs.is_due(st, now),
                    "seen": st.get("seen", 0) if st else 0,
                }
    except Exception as exc:  # pragma: no cover - a trainer must never break the board
        return {"puzzles": [], "error": str(exc)}
    return {"count": len(items), "puzzles": items}


@router.get("/puzzles/themes")
def get_puzzle_themes(days: int = 0, kinds: str = "") -> dict:
    """Per-motif puzzle counts (labelled) for the "train your weaknesses" chips, plus how many of
    each motif are currently due (Leitner scheduler)."""
    try:
        motif_themes = puzzles.themes(days=days or None, kinds=_parse_kinds(kinds))
        states = srs.puzzle_states()
        now = datetime.now(timezone.utc)
        due_by_motif: dict[str, int] = {}
        if states:
            for p in puzzles.build_puzzles(kinds=_parse_kinds(kinds), days=days or None):
                if not srs.is_due(states.get(p.get("id")), now):
                    continue
                for motif in p.get("motifs") or []:
                    due_by_motif[motif] = due_by_motif.get(motif, 0) + 1
        for t in motif_themes:
            t["due"] = due_by_motif.get(t["motif"], 0)
        return {"themes": motif_themes}
    except Exception as exc:  # pragma: no cover - defensive
        return {"themes": [], "error": str(exc)}


@router.get("/puzzles/daily")
def get_puzzles_daily() -> dict:
    """Today's session: a bundle of up to 10 puzzles — due-and-seen first, then never-seen
    puzzles prioritized by your most common weaknesses (same order `themes()` ranks motifs), so
    each day's drill front-loads whatever you're worst at. `done_today` counts attempts already
    recorded today (UTC), for a simple progress readout."""
    try:
        # Weakness-ranked: motifs most-common first (themes() order), puzzles within each motif
        # in build_puzzles' own hardest-first order; a puzzle can only match its first-listed motif
        # here since daily_session dedupes by id anyway.
        motif_order = [t["motif"] for t in puzzles.themes()]
        all_items = puzzles.build_puzzles()
        by_motif: dict[str, list[dict]] = {}
        unranked: list[dict] = []
        for p in all_items:
            motifs = p.get("motifs") or []
            m0 = next((m for m in motifs if m in motif_order), None)
            (by_motif.setdefault(m0, []) if m0 else unranked).append(p)
        ranked = [p for m in motif_order for p in by_motif.get(m, [])] + unranked
        states = srs.puzzle_states()
        now = datetime.now(timezone.utc)
        items = srs.daily_session(ranked, states, now, limit=10)
        today = now.date().isoformat()
        done_today = sum(
            1 for a in srs.load_attempts()
            if (a.get("ts") or "")[:10] == today
        )
        return {"count": len(items), "puzzles": items, "done_today": done_today}
    except Exception as exc:  # pragma: no cover - a trainer must never break the board
        return {"puzzles": [], "count": 0, "done_today": 0, "error": str(exc)}


class AttemptBody(BaseModel):
    puzzle_id: str
    result: str = "fail"
    first_try: bool = False


@router.post("/puzzles/attempt")
def post_puzzle_attempt(body: AttemptBody) -> dict:
    """Record one puzzle attempt for the Leitner scheduler."""
    try:
        srs.record_attempt(body.puzzle_id, body.result, body.first_try)
        return {"ok": True}
    except Exception as exc:  # pragma: no cover - a trainer must never break the board
        return {"ok": False, "error": str(exc)}


@router.post("/puzzles/lichess-study")
def post_lichess_study(body: StudyBody) -> JSONResponse:
    """Export the current puzzle selection to a new (private) Lichess study, one practice chapter
    per mistake. Needs a Lichess token with the study:write scope (⚙ Settings → Lichess token)."""
    items = puzzles.build_puzzles(
        motif=body.motif or None,
        kinds=_parse_kinds(body.kinds),
        days=body.days or None,
        limit=body.limit or lichess_study.MAX_CHAPTERS,
    )
    if not items:
        return JSONResponse({"error": "No puzzles match — analyze some games first."}, status_code=400)
    name = (body.name or "").strip() or _default_study_name(body, len(items))
    try:
        result = lichess_study.create_study(name, items)
    except lichess_study.StudyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse(result)


def _default_study_name(body: StudyBody, n: int) -> str:
    label = ""
    if body.motif:
        label = " · " + history._MOTIF_LABELS.get(body.motif, body.motif)
    return f"Kibitz — my {n} puzzles to review{label}"
