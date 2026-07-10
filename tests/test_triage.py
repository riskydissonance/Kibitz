"""Tests for the crash/exit triage log (triage.py)."""
from __future__ import annotations

import pytest

from server.core import triage


@pytest.fixture
def data_dir(tmp_path):
    return str(tmp_path)


def test_recent_missing_file_empty(data_dir):
    assert triage.recent(data_dir=data_dir) == ""


def test_event_writes_line(data_dir):
    triage.event("startup", context="test", data_dir=data_dir, pid_note="x")
    out = triage.recent(data_dir=data_dir)
    assert "kind=startup" in out
    assert "context=test" in out
    assert out.endswith("\n")


def test_event_creates_logs_dir(data_dir):
    import os
    triage.event("hello", data_dir=data_dir)
    assert os.path.isfile(triage.log_path(data_dir))


def test_fields_with_spaces_are_quoted(data_dir):
    triage.event("k", data_dir=data_dir, msg="two words")
    out = triage.recent(data_dir=data_dir)
    assert 'msg="two words"' in out


def test_none_fields_skipped(data_dir):
    triage.event("k", data_dir=data_dir, present=1, missing=None)
    out = triage.recent(data_dir=data_dir)
    assert "present=1" in out
    assert "missing" not in out


def test_exception_event_logs_traceback(data_dir):
    try:
        raise ValueError("boom")
    except ValueError as exc:
        triage.exception_event("exit-crash", exc, data_dir=data_dir)
    out = triage.recent(data_dir=data_dir)
    assert "kind=exit-crash" in out
    assert "ValueError" in out
    assert "boom" in out


def test_recent_tail_limit(data_dir):
    for i in range(50):
        triage.event("n", data_dir=data_dir, i=i)
    out = triage.recent(lines=10, data_dir=data_dir)
    assert len(out.strip().splitlines()) == 10


def test_event_never_raises_on_bad_dir(tmp_path):
    # A path whose parent is a FILE can't be mkdir'd — event must swallow it, not raise.
    bad = tmp_path / "afile"
    bad.write_text("x")
    triage.event("k", data_dir=str(bad / "sub"))  # must not raise
