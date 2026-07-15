"""Spaced repetition (Leitner) scheduling for the puzzle trainer.

Every puzzle has a STABLE id (`"{game_id}:{ply}"`, see `puzzles._puzzle_from_mistake`). Each time
a solver finishes a puzzle we record one attempt (`record_attempt`) — pass or fail — appended to
`<DATA_DIR>/history/puzzle_attempts.jsonl`, mirroring the append-only JSONL convention in
`history.py`. Folding those attempts per puzzle_id gives a Leitner "box" (0..MAX_BOX): a pass
promotes a puzzle to a longer review interval, a fail drops it straight back to box 0.

Engine-free, deterministic, best-effort: readers never raise, a missing/garbled attempts file
just means "nothing scheduled yet" so the trainer keeps working with plain build order.
"""
from __future__ import annotations

import json
import os
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from server import config

# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------
def _data_dir(data_dir: Optional[str]) -> str:
    return data_dir if data_dir is not None else config.DATA_DIR


def _attempts_path(data_dir: Optional[str] = None) -> str:
    return os.path.join(_data_dir(data_dir), "history", "puzzle_attempts.jsonl")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------------------
# Attempts (append-only log)
# --------------------------------------------------------------------------------------
def record_attempt(
    puzzle_id: str,
    result: str,
    first_try: bool,
    data_dir: Optional[str] = None,
    now: Optional[datetime] = None,
) -> None:
    """Append one attempt record. `result` is normalised to "pass"/"fail"."""
    path = _attempts_path(data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ts = now.isoformat(timespec="seconds").replace("+00:00", "Z") if now else _now_iso()
    rec = {
        "puzzle_id": puzzle_id,
        "ts": ts,
        "result": "pass" if result == "pass" else "fail",
        "first_try": bool(first_try),
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def load_attempts(data_dir: Optional[str] = None) -> list[dict]:
    """All recorded attempts, chronological (file order). Never raises."""
    path = _attempts_path(data_dir)
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


# --------------------------------------------------------------------------------------
# Leitner boxes
# --------------------------------------------------------------------------------------
# Box -> days until due again. Box 0 (never passed / just failed) is due immediately.
BOX_INTERVALS_DAYS = [0, 1, 3, 7, 21]
MAX_BOX = 4


def puzzle_states(data_dir: Optional[str] = None) -> dict[str, dict]:
    """Fold the attempt log into one Leitner state per puzzle_id.

    Replays attempts in order: a pass promotes the box (capped at MAX_BOX), a fail resets it to 0.
    """
    states: dict[str, dict] = {}
    for a in load_attempts(data_dir):
        pid = a.get("puzzle_id")
        if not pid:
            continue
        st = states.setdefault(pid, {"box": 0, "last_ts": None, "seen": 0})
        if a.get("result") == "pass":
            st["box"] = min(st["box"] + 1, MAX_BOX)
        else:
            st["box"] = 0
        st["last_ts"] = a.get("ts")
        st["seen"] += 1
    return states


def _parse_ts(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def is_due(state: Optional[dict], now: datetime) -> bool:
    """A never-seen puzzle is always due; else due once its box interval has elapsed."""
    if not state:
        return True
    last = _parse_ts(state.get("last_ts") or "")
    if last is None:
        return True
    box = state.get("box", 0)
    interval = BOX_INTERVALS_DAYS[min(max(box, 0), MAX_BOX)]
    return now >= last + timedelta(days=interval)


def order_puzzles(
    puzzles: list[dict],
    data_dir: Optional[str] = None,
    now: Optional[datetime] = None,
    rng: Optional[random.Random] = None,
) -> list[dict]:
    """Annotate each puzzle with its `srs` state and order due/failed puzzles first.

    Three tiers, preserving anti-memorisation (shuffled within each tier):
      1. seen and due (previously failed, or a pass whose interval has elapsed)
      2. never seen
      3. seen but not yet due
    No puzzle is dropped; the input list's puzzles are kept intact (just reordered).
    """
    now = now or datetime.now(timezone.utc)
    rng = rng if rng is not None else random.Random()
    states = puzzle_states(data_dir)

    due_seen: list[dict] = []
    never_seen: list[dict] = []
    not_due: list[dict] = []

    for p in puzzles:
        st = states.get(p.get("id"))
        seen = st.get("seen", 0) if st else 0
        box = st.get("box", 0) if st else 0
        due = is_due(st, now)
        p["srs"] = {"box": box, "due": due, "seen": seen}
        if seen > 0 and due:
            due_seen.append(p)
        elif seen == 0:
            never_seen.append(p)
        else:
            not_due.append(p)

    rng.shuffle(due_seen)
    rng.shuffle(never_seen)
    rng.shuffle(not_due)
    return due_seen + never_seen + not_due


def daily_session(
    items: list[dict],
    states: dict[str, dict],
    now: datetime,
    limit: int = 10,
) -> list[dict]:
    """Today's bundle: due-and-seen puzzles first, then never-seen puzzles (already sorted
    hardest-lesson-first by the caller, i.e. by motif frequency / build_puzzles order), deduped by
    id, capped at `limit`. Pure function of its inputs so it's cheap to unit test."""
    due_seen: list[dict] = []
    never_seen: list[dict] = []
    seen_ids: set[str] = set()
    for p in items:
        pid = p.get("id")
        if not pid or pid in seen_ids:
            continue
        st = states.get(pid)
        seen = st.get("seen", 0) if st else 0
        if seen > 0:
            if is_due(st, now):
                due_seen.append(p)
                seen_ids.add(pid)
        else:
            never_seen.append(p)
            seen_ids.add(pid)
    out = due_seen + never_seen
    return out[:limit]
