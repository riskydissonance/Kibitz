"""Backup/restore for the JSON/JSONL data directory (``config.DATA_DIR``).

Archives live at ``<DATA_DIR>/backups/<kind>-YYYY-MM-DD_HHMMSS.tar.gz`` (local time). Five kinds:
``daily``/``weekly``/``monthly`` (scheduler-created), ``manual`` (user-triggered "back up now"),
and ``pre-restore`` (taken automatically right before every restore, as a safety net).

Everything that mutates the data dir — creating a backup, restoring one — goes through a single
process-wide lock (``_LOCK``) so a backup and a restore can never run concurrently, and a restore
additionally refuses to start while a background analysis job (see ``server.web.jobs``) is running,
since the game-analysis writer and a restore both touch the same files.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tarfile
import threading
import time
from datetime import datetime
from typing import Optional

from server import config

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

KINDS = ("daily", "weekly", "monthly", "manual", "pre-restore")
SCHEDULED_KINDS = ("daily", "weekly", "monthly")

RETENTION = {
    "daily": 7,
    "weekly": 4,
    "monthly": 12,
    "manual": 5,
    "pre-restore": 3,
}

# Directories/patterns never included in an archive. "backups" excludes the backup store itself
# (self-inclusion guard); "analysis-cache" is fully regenerable so skipping it keeps archives small.
_EXCLUDED_TOP_DIRS = {"backups", "analysis-cache"}
_EXCLUDED_SUFFIXES = (".tmp", ".lock")

_NAME_RE = re.compile(
    r"^(daily|weekly|monthly|manual|pre-restore)-(\d{4}-\d{2}-\d{2})_(\d{6})\.tar\.gz$"
)

_STALE_LOCK_SECONDS = 3600  # ignore a per-period scheduler lockfile older than this

# 3.12+ ships tarfile's PEP 706 extraction filters; below that we validate members by hand.
_HAS_TAR_FILTERS = sys.version_info >= (3, 12)


class BackupError(Exception):
    """Base class for all backup/restore failures."""


class BackupBusyError(BackupError):
    """Another backup or restore is already in progress, or an analysis job is running."""


class BackupNotFoundError(BackupError):
    """The named backup archive doesn't exist."""


class InvalidBackupNameError(BackupError):
    """The supplied name isn't a valid, contained backup filename."""


# Mutual exclusion between backup creation and restore. A plain (non-reentrant) Lock so that
# a same-thread re-acquire attempt (e.g. a nested create_backup call not using `_locked=True`)
# correctly reports busy rather than silently succeeding via RLock reentrance.
_LOCK = threading.Lock()

# Simple module-level scheduler status, polled by list_backups().
_scheduler_state: dict = {"last_run": None, "last_error": None, "last_created": []}


# --------------------------------------------------------------------------------------
# Paths / naming
# --------------------------------------------------------------------------------------


def _backups_dir(data_dir: Optional[str] = None) -> str:
    return os.path.join(data_dir or config.DATA_DIR, "backups")


def _make_name(kind: str, when: Optional[datetime] = None) -> str:
    when = when or datetime.now()
    return f"{kind}-{when.strftime('%Y-%m-%d_%H%M%S')}.tar.gz"


def _parse_name(name: str) -> Optional[tuple]:
    """(kind, datetime) for a strictly-formatted backup filename, else None."""
    m = _NAME_RE.match(name)
    if not m:
        return None
    kind, date_part, time_part = m.groups()
    try:
        dt = datetime.strptime(f"{date_part}_{time_part}", "%Y-%m-%d_%H%M%S")
    except ValueError:
        return None
    return kind, dt


# --------------------------------------------------------------------------------------
# Archive creation
# --------------------------------------------------------------------------------------


def _tar_filter(root: str):
    def _filt(tarinfo: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
        name = tarinfo.name.lstrip("./")
        if not name or name == ".":
            return tarinfo
        top = name.split("/", 1)[0]
        if top in _EXCLUDED_TOP_DIRS:
            return None
        if name.endswith(_EXCLUDED_SUFFIXES):
            return None
        return tarinfo

    return _filt


def _write_archive(data_dir: str, tmp_path: str) -> None:
    with open(tmp_path, "wb") as raw:
        with tarfile.open(fileobj=raw, mode="w:gz") as tar:
            tar.add(data_dir, arcname=".", recursive=True, filter=_tar_filter(data_dir))
        raw.flush()
        os.fsync(raw.fileno())


def create_backup(kind: str, data_dir: Optional[str] = None, _locked: bool = False) -> dict:
    """Create a `<kind>` archive of the data dir (excluding backups/ and analysis-cache/).

    Written to a temp file under backups/, fsync'd, then atomically renamed into place — a reader
    of the backups dir never observes a partial archive under its final name.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown backup kind: {kind!r}")
    dd = data_dir or config.DATA_DIR
    backups_dir = _backups_dir(dd)
    os.makedirs(backups_dir, exist_ok=True)

    acquired = _locked
    if not _locked:
        acquired = _LOCK.acquire(blocking=False)
        if not acquired:
            raise BackupBusyError("A backup or restore is already in progress.")
    try:
        name = _make_name(kind)
        tmp_path = os.path.join(backups_dir, f".tmp-{name}")
        final_path = os.path.join(backups_dir, name)
        try:
            _write_archive(dd, tmp_path)
            os.rename(tmp_path, final_path)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
        _prune_kind(backups_dir, kind, RETENTION[kind])
        st = os.stat(final_path)
        return {"name": name, "kind": kind, "timestamp": st.st_mtime, "size": st.st_size}
    finally:
        if not _locked:
            _LOCK.release()


def _copy_backup_as(data_dir: str, src_name: str, kind: str) -> dict:
    """Duplicate an already-written archive under a new kind/name (for "multiple kinds due at
    once" — avoids re-taring the whole data dir per kind)."""
    backups_dir = _backups_dir(data_dir)
    name = _make_name(kind)
    tmp_path = os.path.join(backups_dir, f".tmp-{name}")
    final_path = os.path.join(backups_dir, name)
    src_path = os.path.join(backups_dir, src_name)
    with open(src_path, "rb") as fsrc, open(tmp_path, "wb") as fdst:
        shutil.copyfileobj(fsrc, fdst)
        fdst.flush()
        os.fsync(fdst.fileno())
    os.rename(tmp_path, final_path)
    _prune_kind(backups_dir, kind, RETENTION[kind])
    st = os.stat(final_path)
    return {"name": name, "kind": kind, "timestamp": st.st_mtime, "size": st.st_size}


# --------------------------------------------------------------------------------------
# Retention / listing
# --------------------------------------------------------------------------------------


def _prune_kind(backups_dir: str, kind: str, keep: int) -> None:
    entries = []
    try:
        names = os.listdir(backups_dir)
    except OSError:
        return
    for fn in names:
        parsed = _parse_name(fn)
        if parsed and parsed[0] == kind:
            entries.append(fn)
    entries.sort(reverse=True)  # filename format sorts lexically == chronologically
    for fn in entries[keep:]:
        try:
            os.remove(os.path.join(backups_dir, fn))
        except OSError:
            pass


def list_backups(data_dir: Optional[str] = None) -> dict:
    """Valid archives (name, kind, timestamp, size), newest first, plus scheduler status."""
    backups_dir = _backups_dir(data_dir)
    items = []
    try:
        names = os.listdir(backups_dir)
    except OSError:
        names = []
    for fn in names:
        parsed = _parse_name(fn)
        if not parsed:
            continue
        path = os.path.join(backups_dir, fn)
        try:
            st = os.stat(path)
        except OSError:
            continue
        kind, dt = parsed
        items.append({
            "name": fn,
            "kind": kind,
            "timestamp": dt.isoformat(),
            "size": st.st_size,
        })
    items.sort(key=lambda x: x["name"], reverse=True)
    return {"backups": items, "scheduler": dict(_scheduler_state)}


# --------------------------------------------------------------------------------------
# Safe extraction
# --------------------------------------------------------------------------------------


def _validate_member(member: tarfile.TarInfo, dest: str) -> None:
    if member.issym() or member.islnk() or member.ischr() or member.isblk() or member.isdev():
        raise BackupError(f"refusing unsafe tar member (link/device): {member.name}")
    name = member.name
    if not name or name in (".", "./"):
        return
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):  # absolute / Windows drive
        raise BackupError(f"refusing absolute tar member path: {member.name}")
    norm = os.path.normpath(name)
    if norm.startswith("..") or os.path.isabs(norm):
        raise BackupError(f"refusing tar member outside destination: {member.name}")
    target = os.path.realpath(os.path.join(dest, name))
    dest_real = os.path.realpath(dest)
    if target != dest_real and not target.startswith(dest_real + os.sep):
        raise BackupError(f"refusing tar member outside destination: {member.name}")


def _safe_extract(archive_path: str, dest: str) -> None:
    with tarfile.open(archive_path, "r:gz") as tar:
        members = tar.getmembers()
        for m in members:
            _validate_member(m, dest)
        if _HAS_TAR_FILTERS:
            tar.extractall(dest, members=members, filter="data")
        else:
            tar.extractall(dest, members=members)


# --------------------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------------------


def _resolve_backup_path(name: str, data_dir: Optional[str] = None) -> str:
    if not _NAME_RE.match(name):
        raise InvalidBackupNameError(f"invalid backup name: {name!r}")
    backups_dir = _backups_dir(data_dir)
    candidate = os.path.realpath(os.path.join(backups_dir, name))
    backups_real = os.path.realpath(backups_dir)
    if os.path.dirname(candidate) != backups_real:
        raise InvalidBackupNameError(f"invalid backup name: {name!r}")
    return candidate


def _reload_in_memory_state() -> bool:
    """Reload/clear module-level caches after replacing the data dir's contents on disk.

    Audited server/core/*.py for state that outlives a single request:
      - settings.py:  config.* is populated from settings.json at process start; re-apply it.
      - session.py:   `_SESSION` is a process-wide singleton describing the currently-open game;
                       it now refers to a game whose analysis-cache entry may be gone -> clear it.
      - chat_store.py: `_STORE` caches "why?" chat turns keyed by (game_id, side) in memory only;
                       drop it so stale chat doesn't pair with different restored history.
      - analysis_cache.py, history.py, srs.py, puzzles.py, chesscom.py, lichess*.py, tablebase.py,
        openings.py, evaluation.py, lines.py, multipgn.py, reviews.py, engine.py, local_llm.py,
        triage.py, updates.py, app_liveness.py, lifecycle.py: all read straight from disk (or hold
        no state derived from DATA_DIR) on every call — nothing to reload.
    Everything above can be reloaded/cleared in-process, so no restart is required.
    """
    from server.core import settings as settings_mod
    from server.core import session as session_mod
    from server.core import chat_store

    settings_mod.apply(settings_mod.load())
    session_mod.clear_session()
    chat_store.clear()
    return False  # restart_required


def restore_backup(name: str, data_dir: Optional[str] = None) -> dict:
    """Replace the data dir's contents with the named archive.

    Order: validate name -> refuse if busy/job-running -> take a pre-restore safety backup ->
    extract to a scratch dir -> swap each top-level entry into place (old copy kept aside until
    the swap succeeds) -> reload in-memory state. The backups/ directory itself is never touched.
    """
    dd = data_dir or config.DATA_DIR
    archive_path = _resolve_backup_path(name, dd)
    if not os.path.isfile(archive_path):
        raise BackupNotFoundError(f"no such backup: {name!r}")

    from server.web import jobs

    if jobs.status().get("status") == "pending":
        raise BackupBusyError("An analysis job is running; try again once it finishes.")

    if not _LOCK.acquire(blocking=False):
        raise BackupBusyError("A backup or restore is already in progress.")
    try:
        pre = create_backup("pre-restore", data_dir=dd, _locked=True)

        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        tmp_extract = os.path.join(dd, f".restore-tmp-{ts}")
        old_dir = os.path.join(dd, f".restore-old-{ts}")
        os.makedirs(tmp_extract, exist_ok=True)
        try:
            _safe_extract(archive_path, tmp_extract)
            os.makedirs(old_dir, exist_ok=True)
            top_entries = [e for e in os.listdir(tmp_extract)]
            moved: list[str] = []
            try:
                for entry in top_entries:
                    src_new = os.path.join(tmp_extract, entry)
                    dst = os.path.join(dd, entry)
                    if os.path.exists(dst) or os.path.islink(dst):
                        shutil.move(dst, os.path.join(old_dir, entry))
                    shutil.move(src_new, dst)
                    moved.append(entry)
            except Exception:
                # Roll back: remove any partially-placed new entries, restore the old copies.
                for entry in moved:
                    dst = os.path.join(dd, entry)
                    if os.path.isdir(dst) and not os.path.islink(dst):
                        shutil.rmtree(dst, ignore_errors=True)
                    elif os.path.exists(dst) or os.path.islink(dst):
                        os.remove(dst)
                    old_src = os.path.join(old_dir, entry)
                    if os.path.exists(old_src):
                        shutil.move(old_src, dst)
                raise
        finally:
            shutil.rmtree(tmp_extract, ignore_errors=True)
        shutil.rmtree(old_dir, ignore_errors=True)

        restart_required = _reload_in_memory_state()
        return {
            "restored": name,
            "pre_restore_backup": pre["name"],
            "restart_required": restart_required,
        }
    finally:
        _LOCK.release()


# --------------------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------------------


def _period_key(kind: str, when: datetime) -> str:
    if kind == "daily":
        return when.strftime("%Y-%m-%d")
    if kind == "weekly":
        y, w, _ = when.isocalendar()
        return f"{y}-W{w:02d}"
    if kind == "monthly":
        return when.strftime("%Y-%m")
    raise ValueError(kind)


def _is_due(backups_dir: str, kind: str, when: datetime) -> bool:
    key = _period_key(kind, when)
    try:
        names = os.listdir(backups_dir)
    except OSError:
        return True
    for fn in names:
        parsed = _parse_name(fn)
        if parsed and parsed[0] == kind and _period_key(kind, parsed[1]) == key:
            return False
    return True


def _lockfile_path(backups_dir: str, kind: str, period_key: str) -> str:
    return os.path.join(backups_dir, f".lock-{kind}-{period_key}")


def _acquire_period_lock(backups_dir: str, kind: str, period_key: str) -> bool:
    path = _lockfile_path(backups_dir, kind, period_key)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(path) > _STALE_LOCK_SECONDS:
                os.remove(path)
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return True
        except OSError:
            pass
        return False


def _release_period_lock(backups_dir: str, kind: str, period_key: str) -> None:
    try:
        os.remove(_lockfile_path(backups_dir, kind, period_key))
    except OSError:
        pass


def scheduler_check(data_dir: Optional[str] = None) -> dict:
    """Run one due-check: create daily/weekly/monthly backups whose period has none yet.

    When several kinds are due at once, tar the data dir once and copy the result for the other
    kinds (three periods due simultaneously still only costs one full archive pass). Never raises
    — failures are recorded in the module-level scheduler status instead, so a daemon-thread caller
    can loop forever without dying.
    """
    dd = data_dir or config.DATA_DIR
    now = datetime.now()
    created: list[str] = []
    try:
        backups_dir = _backups_dir(dd)
        os.makedirs(backups_dir, exist_ok=True)
        due = [k for k in SCHEDULED_KINDS if _is_due(backups_dir, k, now)]
        if due:
            acquired_periods = []
            for kind in due:
                pk = _period_key(kind, now)
                if _acquire_period_lock(backups_dir, kind, pk):
                    acquired_periods.append((kind, pk))
            try:
                if acquired_periods and _LOCK.acquire(blocking=False):
                    try:
                        first_kind = acquired_periods[0][0]
                        rec = create_backup(first_kind, data_dir=dd, _locked=True)
                        created.append(rec["name"])
                        for kind, _pk in acquired_periods[1:]:
                            rec2 = _copy_backup_as(dd, rec["name"], kind)
                            created.append(rec2["name"])
                    finally:
                        _LOCK.release()
                # else: lock busy (a manual backup/restore is running) — try again next tick.
            finally:
                for kind, pk in acquired_periods:
                    _release_period_lock(backups_dir, kind, pk)
        _scheduler_state["last_run"] = now.isoformat()
        _scheduler_state["last_error"] = None
        _scheduler_state["last_created"] = created
        return {"created": created}
    except Exception as exc:  # never let a scheduler tick raise
        _scheduler_state["last_run"] = now.isoformat()
        _scheduler_state["last_error"] = str(exc)
        return {"created": created, "error": str(exc)}


def start_scheduler(data_dir: Optional[str] = None, interval_seconds: int = 1800) -> threading.Thread:
    """Start the daemon thread: runs the check immediately, then every `interval_seconds`."""

    def _loop() -> None:
        while True:
            try:
                scheduler_check(data_dir=data_dir)
            except Exception as exc:  # pragma: no cover - scheduler_check already catches
                _scheduler_state["last_error"] = str(exc)
            time.sleep(interval_seconds)

    thread = threading.Thread(target=_loop, daemon=True, name="backup-scheduler")
    thread.start()
    return thread
