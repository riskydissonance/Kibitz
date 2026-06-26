"""Apply a staged update for the `zip` channel — download the latest release's source tarball from
GitHub and extract it over the install folder, in place.

This is the self-update path for users who downloaded the source ZIP (no `.git`, and maybe no `git`
binary). It's invoked by the double-click launcher on the next start when an update sentinel exists
(written by the board's "Update now" button → POST /api/apply-update). The `git` channel uses
`git pull` instead; the read-only `.app` can't self-update at all.

Pure stdlib + httpx, cross-platform (no `git`, no `rsync`). Best-effort and ATOMIC-ish: it downloads
+ extracts to a temp dir first and only copies over the folder on full success, so a failed download
leaves the existing install untouched. Exits non-zero on any failure so the launcher falls back to
the existing code.

Runtime + user state is preserved (never overwritten): the venv, the data/cache dirs, the user's
`.mcp.json` (holds their username), `example_pgns/`, and — crucially — the launcher scripts
themselves, so the running launcher never rewrites itself mid-run.
"""
from __future__ import annotations

import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from server import config
from server.core import updates

# Paths/files that must survive an update (relative to the project root). Anything matching is
# skipped when copying the new tree over the old one.
_PRESERVE = {
    ".git",
    ".venv",
    ".chess-review",
    ".mcp.json",
    ".update-requested",
    "example_pgns",
    "settings.json",
    "Tintin's AI Chess Analysis.command",
    "Tintin's AI Chess Analysis.bat",
}
_PRESERVE_SUFFIXES = (".command", ".bat")  # never rewrite a launcher that may be executing us


def _log(msg: str) -> None:
    print(f"[apply_update] {msg}", flush=True)


def _resolve_tarball() -> tuple[str, str] | None:
    """(tag, tarball_url) for the latest release, or None if there's none / we're offline."""
    rel = updates._fetch_latest_release()
    if not rel or not rel.get("tag"):
        return None
    tag = rel["tag"]
    # Prefer the API-provided tarball; else the predictable archive URL for the tag.
    url = f"https://github.com/{config.UPDATE_REPO}/archive/refs/tags/{tag}.tar.gz"
    return tag, url


def _download(url: str, dest: Path) -> bool:
    try:
        with httpx.stream(
            "GET", url,
            headers={"User-Agent": "chess-analysis-mcp"},
            timeout=config.UPDATE_TIMEOUT,
            follow_redirects=True,
        ) as resp:
            if resp.status_code != 200:
                _log(f"download failed: HTTP {resp.status_code}")
                return False
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
        return dest.stat().st_size > 0
    except (httpx.HTTPError, OSError) as exc:
        _log(f"download error: {exc}")
        return False


def _extracted_root(extract_dir: Path) -> Path | None:
    """GitHub source tarballs nest everything under a single top folder (repo-tag-sha/). Return it."""
    entries = [p for p in extract_dir.iterdir() if p.is_dir()]
    return entries[0] if len(entries) == 1 else None


def _plan_files(src_root: Path):
    """Yield (src_file, rel) for every file to install, skipping preserved paths. Dirs are created
    implicitly from each file's parent, so we don't need to yield them."""
    for src in src_root.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(src_root)
        if rel.parts[0] in _PRESERVE or src.name in _PRESERVE or src.suffix in _PRESERVE_SUFFIXES:
            continue
        yield src, rel


def _rollback(dst_root: Path, backed_up: list, created: list) -> None:
    """Undo a partial install: delete the brand-new files we added, restore the originals we moved
    aside. Best-effort — logs anything it can't put back rather than raising."""
    for rel in created:
        try:
            (dst_root / rel).unlink()
        except OSError:
            pass
    for rel, backup in backed_up:
        dst = dst_root / rel
        try:
            if dst.exists():
                dst.unlink()
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup), str(dst))
        except OSError as exc:
            _log(f"rollback: could not restore {rel}: {exc}")


def _copy_over(src_root: Path, dst_root: Path) -> bool:
    """Install the new tree over dst_root *atomically-ish*, returning True on full success.

    Each file we'd overwrite is first moved into a backup dir (created INSIDE dst_root so the move is
    a same-filesystem rename — fast and reliable on Windows and macOS alike); then the new file is
    copied in. On ANY error (a locked/read-only file, antivirus, a full disk — all far more common on
    Windows) we roll back: delete the files added so far and restore every original, so a half-updated
    tree never persists. Returns False after a clean rollback. Files removed upstream are left in
    place (conservative, never deletes user/runtime state)."""
    files = list(_plan_files(src_root))
    backup_dir = Path(tempfile.mkdtemp(dir=dst_root, prefix=".update-backup-"))
    backed_up: list = []   # (rel, backup_path) for files that already existed (to restore on failure)
    created: list = []     # rel for brand-new files (to delete on failure)
    try:
        for src, rel in files:
            dst = dst_root / rel
            if dst.exists():
                backup = backup_dir / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(backup))   # same-FS rename: preserve the original
                backed_up.append((rel, backup))
            else:
                created.append(rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    except (OSError, shutil.Error) as exc:
        _log(f"copy error: {exc}; rolling back to the previous version")
        _rollback(dst_root, backed_up, created)
        shutil.rmtree(backup_dir, ignore_errors=True)
        return False
    shutil.rmtree(backup_dir, ignore_errors=True)
    return True


def main() -> int:
    if not config.UPDATE_CHECK_ENABLED:
        _log("update check disabled; nothing to do.")
        return 0
    resolved = _resolve_tarball()
    if resolved is None:
        _log("no newer release found (or offline); leaving install unchanged.")
        return 1
    tag, url = resolved
    _log(f"updating to {tag} from {url}")

    with tempfile.TemporaryDirectory(prefix="chess-update-") as tmp:
        tmp_path = Path(tmp)
        tarball = tmp_path / "src.tar.gz"
        if not _download(url, tarball):
            return 1
        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()
        try:
            with tarfile.open(tarball, "r:gz") as tf:
                tf.extractall(extract_dir, filter="data")  # filter="data" = path-traversal safe (3.12+/backport)
        except (tarfile.TarError, OSError, TypeError) as exc:
            # TypeError: older Python without the `filter` kwarg — retry without it.
            if isinstance(exc, TypeError):
                try:
                    with tarfile.open(tarball, "r:gz") as tf:
                        tf.extractall(extract_dir)
                except (tarfile.TarError, OSError) as exc2:
                    _log(f"extract error: {exc2}")
                    return 1
            else:
                _log(f"extract error: {exc}")
                return 1
        root = _extracted_root(extract_dir)
        if root is None:
            _log("unexpected tarball layout; aborting.")
            return 1
        if not _copy_over(root, Path(config.PROJECT_ROOT)):
            return 1  # _copy_over already rolled back to the previous version

    _log(f"updated to {tag}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
