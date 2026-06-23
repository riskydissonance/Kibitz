"""Regression tests for the static frontend mount.

A wheel install (`uv run` for the MCP server) installs only the `server` package; the frontend
used to live as a repo-root sibling of `server/`, so it was NOT shipped in the wheel and the board
served the API but 404'd at `/` ({"detail":"Not Found"}). pyproject force-includes it as
`server/_frontend/` and `app._resolve_frontend_dir` finds it there (or in the source tree). These
tests guard both halves: the resolver locates a real directory, and `/` actually serves the UI.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from server.web import app as app_module


def test_frontend_dir_resolves_to_a_real_directory():
    # Whatever the install layout, the resolver must find shipped assets — not None (which would
    # mean the board has no UI and `/` 404s).
    resolved = app_module._resolve_frontend_dir()
    assert resolved is not None, "frontend assets not found — the board would 404 at '/'"
    assert (resolved / "index.html").is_file()


def test_root_serves_the_board_not_a_404():
    client = TestClient(app_module.create_app())
    # The bug: API up, UI missing. Assert the UI is actually mounted.
    assert client.get("/api/app-config").status_code == 200
    root = client.get("/")
    assert root.status_code == 200, "GET / should serve index.html, not 404"
    assert "<!doctype html>" in root.text.lower()


def test_doctor_endpoint_reports_dependency_status():
    # The setup banner (checkSetup() in main.js) reads /api/doctor; it must always return the three
    # checks with a boolean `ok`, and flag `claude` as optional so a missing CLI never reads as a
    # blocker.
    client = TestClient(app_module.create_app())
    r = client.get("/api/doctor")
    assert r.status_code == 200
    checks = r.json()["checks"]
    for name in ("python", "stockfish", "claude"):
        assert isinstance(checks[name]["ok"], bool)
    assert checks["claude"].get("optional") is True


def test_packaged_frontend_location_matches_pyproject_force_include():
    # The wheel ships assets at server/_frontend/ (pyproject [tool.hatch...force-include]); the
    # resolver checks that path first. Keep the two in lockstep so a rename can't silently break
    # installed copies while source checkouts keep working.
    server_pkg = Path(app_module.__file__).resolve().parent.parent  # .../server
    assert server_pkg.name == "server"
    # The resolver's packaged candidate is server/_frontend (a sibling of server/web/).
    assert app_module._resolve_frontend_dir() in (
        server_pkg / "_frontend",
        server_pkg.parent / "frontend",
    )


# --- Local-only request guard (CSRF / DNS-rebinding defence) -------------------------------------


def test_same_origin_request_is_allowed():
    # The board's own frontend sends Origin: http://127.0.0.1:<port> — must pass.
    client = TestClient(app_module.create_app())
    r = client.get("/api/app-config", headers={"origin": "http://127.0.0.1:8765"})
    assert r.status_code == 200


def test_cross_origin_request_is_blocked():
    # A page on evil.com that makes the browser POST to the board must be rejected before any route
    # runs (so it can't spend Claude quota). The Host stays local; the Origin gives it away.
    client = TestClient(app_module.create_app())
    r = client.post("/api/chat", headers={"origin": "https://evil.com"}, json={"question": "hi"})
    assert r.status_code == 403


def test_non_local_host_is_blocked():
    # DNS rebinding: the IP resolves to 127.0.0.1 but the browser still sends the attacker's Host.
    client = TestClient(app_module.create_app())
    r = client.get("/api/app-config", headers={"host": "attacker.example"})
    assert r.status_code == 403


def test_opaque_origin_allowed_for_reads_blocked_for_writes():
    # The file:// loading splash (Origin: null) polls /api/app-config — allow that GET; but a
    # sandboxed iframe also gets Origin: null, so never let it reach a state-changing POST.
    client = TestClient(app_module.create_app())
    assert client.get("/api/app-config", headers={"origin": "null"}).status_code == 200
    assert client.post("/api/chat", headers={"origin": "null"}, json={"question": "hi"}).status_code == 403
