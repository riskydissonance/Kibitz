"""App-mode liveness: quit the server shortly after the app's browser tab is *really* closed.

Only active in **app mode** (CHESS_APP_MODE=1 — the double-click launcher), so the MCP-driven board
never self-exits when you close a tab. It is also gated behind `config.APP_AUTOQUIT` (default off),
so by default the app never self-quits; the rest of this module only matters when a user opts back
in with CHESS_APP_AUTOQUIT=1.

The hard part is telling "tab closed" apart from "tab lost focus / went to the background", because
browsers heavily throttle background-tab timers (down to ~once a minute). So we use two signals:

  - An explicit **close beacon**: the page fires `navigator.sendBeacon('/api/closing')` on `pagehide`
    (tab close / navigation / refresh). After `CLOSE_GRACE` seconds with no new heartbeat we exit —
    fast. A *refresh* also fires `pagehide`, but the reloaded page sends a heartbeat within ~1s,
    which cancels the pending close, so a refresh doesn't kill the server.
  - A slow **heartbeat backstop**: the page POSTs `/api/ping` periodically; if none arrives for
    `BEAT_TIMEOUT` (generous — minutes — so background throttling never trips it) we exit anyway,
    covering the rare case where `pagehide` never fires (e.g. the browser is killed).

Exit mirrors lifecycle.py: `os._exit` after `engine.shutdown()`.
"""
from __future__ import annotations

import os
import sys
import threading
import time

from server import config
from server.core import engine
from server.core import triage

# Generous heartbeat backstop (seconds). This is ONLY a safety net for a browser that was killed
# without firing the pagehide close-beacon; a real tab-close quits promptly via that beacon
# (CLOSE_GRACE), not this. It must be long enough that an ordinary backgrounded tab never trips it:
# browsers don't just throttle background-tab timers (~1/min), they FREEZE the tab after a few
# minutes, halting the heartbeat entirely — a 2-minute backstop then false-quit the app while it
# sat in the background. 30 minutes reclaims a genuinely-orphaned process without killing a tab the
# user merely switched away from. Overridable via CHESS_APP_BEAT_TIMEOUT.
BEAT_TIMEOUT: float = float(os.environ.get("CHESS_APP_BEAT_TIMEOUT", "1800"))
# After an explicit close beacon, wait this long for a heartbeat to resume (a refresh) before
# exiting. Short, so a real close quits promptly.
CLOSE_GRACE: float = float(os.environ.get("CHESS_APP_CLOSE_GRACE", "3"))

_lock = threading.Lock()
_armed = False  # becomes True after the first heartbeat (browser actually connected)
_last_beat = 0.0
_closing_at: float | None = None  # monotonic time the tab signalled it's unloading
_started = False
_stop = threading.Event()


def beat() -> None:
    """Heartbeat from the open tab. Arms the watchdog and cancels any pending close (e.g. refresh)."""
    global _armed, _last_beat, _closing_at
    with _lock:
        _armed = True
        _last_beat = time.monotonic()
        _closing_at = None


def closing() -> None:
    """The tab signalled it's unloading (pagehide). Starts the short close countdown."""
    global _closing_at
    with _lock:
        if _armed:
            _closing_at = time.monotonic()


def _expired() -> bool:
    with _lock:
        if not _armed:
            return False  # browser never connected yet — never exit
        now = time.monotonic()
        if _closing_at is not None and (now - _closing_at) >= CLOSE_GRACE:
            return True  # explicit close, and no heartbeat came back (not a refresh)
        return (now - _last_beat) >= BEAT_TIMEOUT  # backstop


def _analysis_running() -> bool:
    """Is a background game analysis (single or sync batch) in flight?

    While one is, we never self-exit: browsers freeze/discard background tabs (Chrome Memory
    Saver, Safari tab suspension), which silences the heartbeat exactly during the long syncs the
    user walked away from — exiting then throws away minutes of engine work and looks like a
    crash. The exit resumes (and the normal timeouts apply) once the job lands."""
    try:
        from server.web import jobs  # inline: core->web is a layering exception, kept call-local

        return jobs.status().get("status") == "pending"
    except Exception:  # pragma: no cover - liveness must never die on a status probe
        return False


def _exit_reason() -> dict:
    """A snapshot of why the watchdog is exiting, for the triage log."""
    with _lock:
        now = time.monotonic()
        closing = _closing_at is not None
        return {
            "trigger": "close-beacon" if closing else "heartbeat-backstop",
            "armed": _armed,
            "since_last_beat_s": round(now - _last_beat, 1) if _armed else None,
            "since_closing_s": round(now - _closing_at, 1) if closing else None,
        }


def _run() -> None:
    while not _stop.wait(1):
        if _expired() and not _analysis_running():
            triage.event("exit-app-liveness", **_exit_reason())
            print(
                "[chess-app] browser closed — shutting the app down.",
                file=sys.stderr,
                flush=True,
            )
            try:
                engine.shutdown()
            finally:
                os._exit(0)


def start() -> None:
    """Start the liveness watchdog once. No-op unless in app mode AND auto-quit is enabled.

    Default: the standalone app never self-quits (config.APP_AUTOQUIT is off), so closing the tab
    or a frozen background tab leaves the server running; the user quits via Settings → Quit, and
    reopening Kibitz.app just reconnects the browser to the still-running server. Set
    CHESS_APP_AUTOQUIT=1 to restore tab-close-quits-the-app behaviour."""
    global _started
    if not config.APP_MODE or not config.APP_AUTOQUIT:
        return
    with _lock:
        if _started:
            return
        _started = True
    _stop.clear()
    threading.Thread(target=_run, name="chess-app-liveness", daemon=True).start()


def stop() -> None:
    """Stop the watchdog without exiting (clean shutdown / tests)."""
    global _started
    _stop.set()
    with _lock:
        _started = False
