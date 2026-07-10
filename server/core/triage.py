"""Persistent crash/exit triage log — one place that always records WHY the server stopped.

The board has several ways to vanish and most leave nothing behind: the app-liveness and idle
watchdogs call os._exit (skipping atexit), an unhandled exception in an engine/job thread only
prints to a stderr the GUI launcher discards, and a native crash in the Stockfish-bound engine
(SIGSEGV/SIGABRT) makes the whole process disappear with no Python traceback at all. When a user
reports "it just stopped again" there was no durable record to triage from.

This writes an append-only event log to <DATA_DIR>/logs/triage.log (independent of the .app
launcher's launch.log, so it works in dev, MCP, and app mode alike) and installs process-wide
crash handlers that turn every silent death into a logged line:

  - faulthandler          -> C-level, all-thread traceback on a fatal signal (SIGSEGV/SIGABRT/...)
  - sys.excepthook        -> uncaught exception on the main thread (+ traceback)
  - threading.excepthook  -> uncaught exception in ANY thread (engine pool, jobs, watchdogs)
  - SIGTERM/SIGHUP        -> log the terminating signal before the default action runs
  - atexit                -> a normal interpreter shutdown (tells it apart from os._exit / kill)

Every explicit exit path (run_web crash/clean/ctrl-c, both watchdogs, the Quit button) also calls
`event(...)`, so the log reads as one timeline. Best-effort throughout: logging must never be the
thing that breaks the server, so every write is wrapped and failures are swallowed.
"""
from __future__ import annotations

import atexit
import faulthandler
import json
import os
import signal
import sys
import threading
import traceback as _tb
from datetime import datetime, timezone
from typing import Optional

from server import config

_lock = threading.Lock()
_installed = False
_fault_fh = None  # kept open for faulthandler's lifetime (it writes to a raw fd)


def _data_dir(data_dir: Optional[str]) -> str:
    return data_dir if data_dir is not None else config.DATA_DIR


def log_path(data_dir: Optional[str] = None) -> str:
    return os.path.join(_data_dir(data_dir), "logs", "triage.log")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fmt(v) -> str:
    s = str(v)
    return s if (" " not in s and "\n" not in s) else json.dumps(s)


def event(kind: str, message: str = "", data_dir: Optional[str] = None, **fields) -> None:
    """Append one triage event line. Never raises."""
    try:
        path = log_path(data_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        parts = [_now(), f"pid={os.getpid()}", f"kind={kind}"]
        if message:
            parts.append(message)
        for k, v in fields.items():
            if v is None:
                continue
            parts.append(f"{k}={_fmt(v)}")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(" ".join(parts) + "\n")
    except Exception:
        pass  # triage logging must never break the caller


def exception_event(kind: str, exc: BaseException, data_dir: Optional[str] = None, **fields) -> None:
    """Log an exception event followed by its indented traceback block. Never raises."""
    try:
        event(kind, f"error={_fmt(repr(exc))}", data_dir=data_dir, **fields)
        tb = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
        with open(log_path(data_dir), "a", encoding="utf-8") as fh:
            for ln in tb.rstrip().splitlines():
                fh.write("    " + ln + "\n")
    except Exception:
        pass


def recent(lines: int = 200, data_dir: Optional[str] = None) -> str:
    """Tail of the triage log (best-effort). Empty string if none yet / unreadable."""
    try:
        with open(log_path(data_dir), "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-max(1, lines):])
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


def install(context: str = "") -> None:
    """Install crash handlers + record a startup event. Idempotent; safe in any mode/thread."""
    global _installed, _fault_fh
    with _lock:
        if _installed:
            return
        _installed = True

    event(
        "startup",
        context=context,
        version=getattr(config, "APP_VERSION", "?"),
        app_mode=config.APP_MODE,
        python=sys.version.split()[0],
        argv=" ".join(sys.argv),
    )

    # Native crashes (engine/interpreter): dump every thread's stack to the log on a fatal signal.
    # faulthandler.enable already covers SIGSEGV/SIGFPE/SIGABRT/SIGBUS/SIGILL.
    try:
        path = log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _fault_fh = open(path, "a", encoding="utf-8")
        faulthandler.enable(file=_fault_fh, all_threads=True)
    except Exception:
        pass

    # Uncaught exception on the main thread.
    _prev_hook = sys.excepthook

    def _excepthook(exc_type, exc, tb):
        if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            exception_event("uncaught-exception", exc)
        _prev_hook(exc_type, exc, tb)

    sys.excepthook = _excepthook

    # Uncaught exception in ANY thread (engine pool, jobs, watchdogs).
    _prev_threadhook = getattr(threading, "excepthook", None)

    def _threadhook(args):
        if args.exc_type is not None and not issubclass(args.exc_type, SystemExit):
            exc = args.exc_value if args.exc_value is not None else args.exc_type()
            exception_event("thread-exception", exc, thread=getattr(args.thread, "name", "?"))
        if _prev_threadhook is not None:
            _prev_threadhook(args)

    if _prev_threadhook is not None:
        threading.excepthook = _threadhook

    # Terminating signals: log before the default action (main thread only; ignore otherwise).
    def _sig_handler(signum, frame):
        try:
            name = signal.Signals(signum).name
        except Exception:
            name = str(signum)
        event("signal", signal=name)
        try:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        except Exception:
            os._exit(1)

    for _name in ("SIGTERM", "SIGHUP"):
        _s = getattr(signal, _name, None)
        if _s is not None:
            try:
                signal.signal(_s, _sig_handler)
            except (ValueError, OSError):
                pass  # not the main thread / unsupported platform

    atexit.register(lambda: event("process-exit", note="atexit"))
