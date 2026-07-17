"""Shared pytest fixtures.

The Stockfish engine pool (server/core/engine.py) keeps its SimpleEngine subprocess handles
alive for the life of the process (reused, never spawned per-call) via non-daemon
threads/pipes under the hood. That's the right call for the running app (same reason the
standalone app exits via os._exit rather than a clean shutdown), but it means a pytest process
that has exercised the real engine can hang at interpreter exit waiting on those threads instead
of ever printing its final summary line. This autouse, session-scoped fixture shuts the pool
down after the whole test session so `pytest` always exits on its own.
"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="session", autouse=True)
def _shutdown_engine_pool_after_session():
    yield
    from server.core import engine

    try:
        engine.shutdown()
    except Exception:  # pragma: no cover - best-effort cleanup, never fail the suite
        pass
