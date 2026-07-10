"""Per-game review status + notes for the "My games" panel.

Each game (keyed by `(game_id, reviewed_side)`) can be marked reviewed and/or annotated with a
free-text note. Every change is appended as one record to
`<DATA_DIR>/history/game_reviews.jsonl`, mirroring the append-only JSONL convention in `srs.py`:
nothing is ever rewritten in place, and folding the log (`review_states`) replays records in
order so the latest value wins per field. Engine-free, deterministic, best-effort: readers never
raise, a missing/garbled reviews file just means "nothing reviewed/annotated yet".
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

from server import config

# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------
def _data_dir(data_dir: Optional[str]) -> str:
    return data_dir if data_dir is not None else config.DATA_DIR


def _reviews_path(data_dir: Optional[str] = None) -> str:
    return os.path.join(_data_dir(data_dir), "history", "game_reviews.jsonl")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _key(game_id: str, reviewed_side: Optional[str]) -> str:
    return f"{game_id}:{reviewed_side or ''}"


# --------------------------------------------------------------------------------------
# Reviews (append-only log)
# --------------------------------------------------------------------------------------
def load_reviews(data_dir: Optional[str] = None) -> list[dict]:
    """All recorded review updates, chronological (file order). Never raises."""
    path = _reviews_path(data_dir)
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # skip a garbled line rather than fail the whole read
    except FileNotFoundError:
        return []
    return out


def review_states(data_dir: Optional[str] = None) -> dict[str, dict]:
    """Fold the review log into one state per `(game_id, reviewed_side)` key.

    Replays records in order: each record can update `reviewed` and/or `note` independently,
    so the latest value of each field wins (a note update doesn't clobber a prior reviewed flag).
    """
    states: dict[str, dict] = {}
    for r in load_reviews(data_dir):
        gid = r.get("game_id")
        if not gid:
            continue
        key = _key(gid, r.get("reviewed_side"))
        st = states.setdefault(
            key,
            {"game_id": gid, "reviewed_side": r.get("reviewed_side"), "reviewed": False, "note": "", "ts": None},
        )
        if "reviewed" in r and r["reviewed"] is not None:
            st["reviewed"] = bool(r["reviewed"])
        if "note" in r and r["note"] is not None:
            st["note"] = str(r["note"])
        st["ts"] = r.get("ts") or st["ts"]
    return states


def get_state(game_id: str, reviewed_side: Optional[str], data_dir: Optional[str] = None) -> Optional[dict]:
    return review_states(data_dir).get(_key(game_id, reviewed_side))


def set_review(
    game_id: str,
    reviewed_side: Optional[str],
    reviewed: Optional[bool] = None,
    note: Optional[str] = None,
    data_dir: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """Append one review update record; returns the folded state afterwards."""
    if not game_id:
        raise ValueError("game_id is required")
    path = _reviews_path(data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ts = now.isoformat(timespec="seconds").replace("+00:00", "Z") if now else _now_iso()
    rec = {"game_id": game_id, "reviewed_side": reviewed_side, "ts": ts}
    if reviewed is not None:
        rec["reviewed"] = bool(reviewed)
    if note is not None:
        rec["note"] = str(note)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return get_state(game_id, reviewed_side, data_dir)
