"""Tests for the zip-channel updater's atomic copy + rollback (scripts/apply_update.py)."""
from __future__ import annotations

import shutil

import pytest

import scripts.apply_update as au


def _make_install(tmp_path):
    """A fake existing install with user/runtime state that must survive an update."""
    root = tmp_path / "install"
    (root / "server").mkdir(parents=True)
    (root / "server" / "old.py").write_text("v1")
    (root / ".mcp.json").write_text("USER")
    (root / ".venv").mkdir()
    (root / ".venv" / "x").write_text("env")
    (root / "Kibitz.command").write_text("OLD LAUNCHER")
    return root


def _make_release(tmp_path):
    """A fake extracted release tree (what GitHub's tarball expands to, minus the top folder)."""
    src = tmp_path / "repo-v2"
    (src / "server").mkdir(parents=True)
    (src / "server" / "old.py").write_text("v2")        # updated
    (src / "server" / "new.py").write_text("NEW")        # added
    (src / ".mcp.json").write_text("SHOULD-NOT-WIN")     # must be skipped (preserved)
    (src / "Kibitz.command").write_text("NEW LAUNCHER")  # must be skipped
    return src


def test_copy_over_success_preserves_and_updates(tmp_path):
    install, src = _make_install(tmp_path), _make_release(tmp_path)
    assert au._copy_over(src, install) is True
    assert (install / "server" / "old.py").read_text() == "v2"      # updated
    assert (install / "server" / "new.py").read_text() == "NEW"     # added
    assert (install / ".mcp.json").read_text() == "USER"            # preserved
    assert (install / ".venv" / "x").read_text() == "env"           # preserved
    assert (install / "Kibitz.command").read_text() == "OLD LAUNCHER"  # launcher untouched
    assert not list(install.glob(".update-backup-*"))               # backup cleaned on success


def test_copy_over_rolls_back_on_failure(tmp_path, monkeypatch):
    install = tmp_path / "install"
    (install / "sub").mkdir(parents=True)
    (install / "a.py").write_text("orig-a")
    (install / "sub" / "b.py").write_text("orig-b")
    src = tmp_path / "repo"
    (src / "sub").mkdir(parents=True)
    (src / "a.py").write_text("new-a")
    (src / "sub" / "b.py").write_text("new-b")
    (src / "c.py").write_text("new-c")  # brand new — must be removed on rollback

    # Fail on the second file copied, simulating a locked/read-only file (common on Windows).
    real = shutil.copy2
    state = {"n": 0}

    def flaky(s, d, *a, **k):
        state["n"] += 1
        if state["n"] == 2:
            raise OSError("simulated locked file")
        return real(s, d, *a, **k)

    monkeypatch.setattr(shutil, "copy2", flaky)
    assert au._copy_over(src, install) is False
    # Everything restored to the original; the half-written new file is gone.
    assert (install / "a.py").read_text() == "orig-a"
    assert (install / "sub" / "b.py").read_text() == "orig-b"
    assert not (install / "c.py").exists()
    assert not list(install.glob(".update-backup-*"))


def test_plan_files_skips_preserved(tmp_path):
    src = tmp_path / "repo"
    (src / "server").mkdir(parents=True)
    (src / "server" / "keep.py").write_text("x")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref")
    (src / ".mcp.json").write_text("x")
    (src / "run.command").write_text("x")
    rels = {str(rel) for _, rel in au._plan_files(src)}
    assert "server/keep.py".replace("/", __import__("os").sep) in rels
    assert not any(".git" in r or ".mcp.json" in r or r.endswith(".command") for r in rels)
