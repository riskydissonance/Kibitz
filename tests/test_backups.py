"""Tests for backup/restore of the data dir (server/core/backups.py).

All tests point DATA_DIR at a pytest tmp_path — never the real ~/Library data dir, never port 8765.
"""
from __future__ import annotations

import os
import tarfile
import time
from datetime import datetime, timedelta

import pytest

from server import config
from server.core import backups
from server.web import jobs


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    # backups.py takes an explicit data_dir everywhere it matters, but reset the module-level
    # scheduler/lock state between tests so one test's leftovers can't wedge another.
    backups._scheduler_state.update(last_run=None, last_error=None, last_created=[])
    if backups._LOCK.acquire(blocking=False):
        backups._LOCK.release()
    yield tmp_path


def _seed_data(dd):
    os.makedirs(os.path.join(dd, "sub"), exist_ok=True)
    with open(os.path.join(dd, "history.jsonl"), "w") as f:
        f.write('{"a": 1}\n')
    with open(os.path.join(dd, "sub", "file.json"), "w") as f:
        f.write("{}")
    os.makedirs(os.path.join(dd, "analysis-cache"), exist_ok=True)
    with open(os.path.join(dd, "analysis-cache", "x.json"), "w") as f:
        f.write("{}")


def test_create_backup_excludes_backups_and_cache(data_dir):
    _seed_data(data_dir)
    rec = backups.create_backup("manual", data_dir=str(data_dir))
    archive_path = os.path.join(data_dir, "backups", rec["name"])
    assert os.path.isfile(archive_path)
    with tarfile.open(archive_path, "r:gz") as tar:
        names = tar.getnames()
    assert any(n.endswith("history.jsonl") for n in names)
    assert any(n.endswith("sub/file.json") or n.endswith("sub") for n in names)
    assert not any("analysis-cache" in n for n in names)
    assert not any("backups" in n for n in names)


def test_create_backup_self_inclusion_guard_with_existing_backups(data_dir):
    """A second backup, taken after a first backup already exists, must not include backups/ —
    proves the exclusion isn't just "empty dir happened to be skipped"."""
    _seed_data(data_dir)
    backups.create_backup("manual", data_dir=str(data_dir))
    rec2 = backups.create_backup("manual", data_dir=str(data_dir))
    archive_path = os.path.join(data_dir, "backups", rec2["name"])
    with tarfile.open(archive_path, "r:gz") as tar:
        names = tar.getnames()
    assert not any("backups" in n for n in names)


def test_retention_prunes_oldest_keeps_foreign_files(data_dir):
    backups_dir = os.path.join(data_dir, "backups")
    os.makedirs(backups_dir, exist_ok=True)
    base = datetime(2026, 1, 1)
    # 10 manual backups (retention keeps 5) with distinct, increasing timestamps.
    names = []
    for i in range(10):
        when = base + timedelta(minutes=i)
        name = f"manual-{when.strftime('%Y-%m-%d_%H%M%S')}.tar.gz"
        with tarfile.open(os.path.join(backups_dir, name), "w:gz"):
            pass
        names.append(name)
    foreign = os.path.join(backups_dir, "not-a-backup.txt")
    with open(foreign, "w") as f:
        f.write("keep me")
    weird = os.path.join(backups_dir, "manual-notadate.tar.gz")
    with open(weird, "w") as f:
        f.write("keep me too")

    backups._prune_kind(backups_dir, "manual", backups.RETENTION["manual"])

    remaining = set(os.listdir(backups_dir))
    kept_manual = sorted(n for n in remaining if n.startswith("manual-") and n in names)
    assert kept_manual == sorted(names)[-5:]
    assert os.path.basename(foreign) in remaining
    assert os.path.basename(weird) in remaining


def test_list_backups_reports_newest_first(data_dir):
    backups.create_backup("manual", data_dir=str(data_dir))
    time.sleep(1.1)  # filenames only have 1s resolution; ensure distinct names
    backups.create_backup("manual", data_dir=str(data_dir))
    result = backups.list_backups(data_dir=str(data_dir))
    assert len(result["backups"]) == 2
    assert result["backups"][0]["name"] > result["backups"][1]["name"]
    assert "scheduler" in result


def test_restore_round_trip(data_dir, monkeypatch):
    monkeypatch.setattr(jobs, "status", lambda: {"status": "idle"})
    dd = str(data_dir)
    _seed_data(dd)
    with open(os.path.join(dd, "history.jsonl"), "w") as f:
        f.write("original\n")
    rec = backups.create_backup("manual", data_dir=dd)

    # Modify data after the backup.
    with open(os.path.join(dd, "history.jsonl"), "w") as f:
        f.write("modified\n")

    result = backups.restore_backup(rec["name"], data_dir=dd)
    assert result["restored"] == rec["name"]
    assert result["pre_restore_backup"].startswith("pre-restore-")

    with open(os.path.join(dd, "history.jsonl")) as f:
        assert f.read() == "original\n"
    # The pre-restore safety backup exists and backups/ itself was untouched by the restore.
    pre_path = os.path.join(dd, "backups", result["pre_restore_backup"])
    assert os.path.isfile(pre_path)


def test_restore_rejects_path_traversal(data_dir, monkeypatch):
    monkeypatch.setattr(jobs, "status", lambda: {"status": "idle"})
    with pytest.raises(backups.InvalidBackupNameError):
        backups.restore_backup("../../etc/passwd", data_dir=str(data_dir))
    with pytest.raises(backups.InvalidBackupNameError):
        backups.restore_backup("manual-2026-01-01_000000.tar.gz/../../x", data_dir=str(data_dir))
    with pytest.raises(backups.InvalidBackupNameError):
        backups.restore_backup("not-a-valid-name.tar.gz", data_dir=str(data_dir))


def test_restore_missing_backup_raises_not_found(data_dir, monkeypatch):
    monkeypatch.setattr(jobs, "status", lambda: {"status": "idle"})
    with pytest.raises(backups.BackupNotFoundError):
        backups.restore_backup("manual-2026-01-01_000000.tar.gz", data_dir=str(data_dir))


def test_restore_rejects_malicious_tar_members(data_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "status", lambda: {"status": "idle"})
    dd = str(data_dir)
    os.makedirs(os.path.join(dd, "backups"), exist_ok=True)
    bad_name = "manual-2026-01-01_000000.tar.gz"
    bad_path = os.path.join(dd, "backups", bad_name)
    with tarfile.open(bad_path, "w:gz") as tar:
        info = tarfile.TarInfo(name="../../evil.txt")
        data = b"pwned"
        info.size = len(data)
        import io
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(backups.BackupError):
        backups.restore_backup(bad_name, data_dir=dd)
    # The malicious path must never land outside the data dir.
    assert not os.path.exists(os.path.join(os.path.dirname(dd), "evil.txt"))


def test_restore_refused_when_job_running(data_dir, monkeypatch):
    dd = str(data_dir)
    os.makedirs(os.path.join(dd, "backups"), exist_ok=True)
    with tarfile.open(os.path.join(dd, "backups", "manual-2026-01-01_000000.tar.gz"), "w:gz"):
        pass
    monkeypatch.setattr(jobs, "status", lambda: {"status": "pending"})
    with pytest.raises(backups.BackupBusyError):
        backups.restore_backup("manual-2026-01-01_000000.tar.gz", data_dir=dd)


def test_create_backup_busy_when_lock_held(data_dir):
    dd = str(data_dir)
    assert backups._LOCK.acquire(blocking=False)
    try:
        with pytest.raises(backups.BackupBusyError):
            backups.create_backup("manual", data_dir=dd)
    finally:
        backups._LOCK.release()


def test_scheduler_check_creates_due_kinds_once_and_copies(data_dir, monkeypatch):
    dd = str(data_dir)
    _seed_data(dd)
    result = backups.scheduler_check(data_dir=dd)
    assert set(k for k in ("daily", "weekly", "monthly")) <= set(
        backups._parse_name(n)[0] for n in result["created"]
    ) or len(result["created"]) == 3
    # All three should exist on disk now (none due-before, all created this tick).
    listed = backups.list_backups(data_dir=dd)["backups"]
    kinds_present = {b["kind"] for b in listed}
    assert {"daily", "weekly", "monthly"} <= kinds_present
    assert backups._scheduler_state["last_error"] is None

    # Running again immediately: nothing is due anymore.
    result2 = backups.scheduler_check(data_dir=dd)
    assert result2["created"] == []
