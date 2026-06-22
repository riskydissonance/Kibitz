"""FastAPI app factory: JSON board API + the static no-build frontend."""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from server.core import app_liveness
from server.core import lifecycle
from server.web.routes_board import router as board_router
from server.web.routes_chat import router as chat_router
from server.web.routes_history import router as history_router
from server.web.routes_settings import router as settings_router


def _resolve_frontend_dir() -> Path | None:
    """Locate the static frontend, working for BOTH a source checkout and an installed wheel.

    A plain wheel install (e.g. `uv run` for the MCP server) ships `frontend/` inside the package
    as `server/_frontend/` (see pyproject force-include); a source/editable run uses the repo-root
    `frontend/` sibling. Try the packaged copy first, then the source layout. Returning None means
    the UI genuinely wasn't shipped — `create_app` logs loudly rather than silently 404-ing at `/`.
    """
    here = Path(__file__).resolve()
    packaged = here.parent.parent / "_frontend"        # server/web/app.py -> server/_frontend
    source = here.parents[2] / "frontend"              # <repo>/frontend (source/editable checkout)
    for candidate in (packaged, source):
        if candidate.is_dir():
            return candidate
    return None


_FRONTEND_DIR = _resolve_frontend_dir()


def create_app() -> FastAPI:
    app = FastAPI(title="Chess Review board", docs_url="/api/docs")

    # In app mode (double-click launcher), self-exit shortly after the browser tab is closed.
    # No-op for the MCP-driven board and tests (config.APP_MODE is off there).
    app_liveness.start()

    @app.middleware("http")
    async def _mark_activity(request: Request, call_next):
        # Any board interaction keeps the session alive (resets the idle watchdog).
        lifecycle.touch()
        return await call_next(request)

    app.include_router(board_router, prefix="/api")
    app.include_router(chat_router, prefix="/api")
    app.include_router(history_router, prefix="/api")
    app.include_router(settings_router, prefix="/api")

    # Mount the raw frontend last so /api/* routes win. html=True serves index.html at /.
    if _FRONTEND_DIR is not None:
        app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")
    else:
        # The UI wasn't packaged with this install — the board would 404 at `/`. Don't fail silently:
        # this is a packaging bug (see _resolve_frontend_dir), and a bare 404 is impossible to debug.
        print(
            "[chess-web] WARNING: frontend assets not found; the board UI is unavailable and '/' "
            "will 404. This usually means the package was installed without 'server/_frontend'.",
            file=sys.stderr,
            flush=True,
        )

    return app
