"""Sprint 58 — state file rotation for funding_carry.

Rotates large JSONL/JSON state files into gz-compressed dated archives,
truncating the active file so the bot keeps appending without disk
exhaustion.  Sprint 57 had a `sqlite3.OperationalError: database or
disk is full` incident at 01:31:51 on 2026-06-14.

Safety contract:
  * never deletes raw records — appends gzipped archive instead
  * uses copy-then-truncate so the bot's open writer handle does not
    break on Windows
  * keeps the last RETAIN rotations per file; older archives are
    pruned (only the .gz archives, never the active file)
  * skips files smaller than ROTATE_THRESHOLD_BYTES
  * silent when nothing to do
  * dry-run mode via --dry-run

Usage (Task Scheduler ZephStateFileRotate, daily 02:00):
    py -3 scripts/rotate_state.py
    py -3 scripts/rotate_state.py --dry-run
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
STATE_DIR = _ROOT / "state"

ROTATE_THRESHOLD_BYTES = 200 * 1024 * 1024  # 200 MB
RETAIN = 7

TARGETS = [
    "universe_filtered.jsonl",
    "free_funding_opportunities_shadow.jsonl",
    "spread_builder_diagnostic.jsonl",
    "aggressive_paper_events.jsonl",
    "opportunities.jsonl",
    "low_volume_funding_shadow.jsonl",
]


def _utc_stamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _disk_free_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except Exception:
        return 0


def _prune_old(file_path: Path, keep: int) -> list[str]:
    """Delete older .gz archives beyond `keep` rotations for one file."""
    pat = f"{file_path.stem}.*.{file_path.suffix.lstrip('.')}.gz"
    matches = sorted(file_path.parent.glob(pat),
                      key=lambda p: p.stat().st_mtime,
                      reverse=True)
    pruned: list[str] = []
    for old in matches[keep:]:
        try:
            old.unlink()
            pruned.append(old.name)
        except OSError:
            pass
    return pruned


def _rotate_one(file_path: Path, dry_run: bool) -> dict:
    """Rotate one file if larger than threshold.

    Returns:
        {"file": str, "action": str, "before_bytes": int,
         "after_bytes": int, "archive": str, "pruned": [...]}
    """
    res = {
        "file":        str(file_path.name),
        "action":      "skipped",
        "before_bytes": 0,
        "after_bytes":  0,
        "archive":     "",
        "pruned":      [],
        "error":       "",
    }
    if not file_path.exists():
        res["action"] = "missing"
        return res
    try:
        sz = file_path.stat().st_size
    except OSError as exc:
        res["error"] = f"stat: {exc}"
        return res
    res["before_bytes"] = sz
    if sz < ROTATE_THRESHOLD_BYTES:
        res["after_bytes"] = sz
        return res

    stamp = _utc_stamp()
    archive = file_path.parent / (
        f"{file_path.stem}.{stamp}{file_path.suffix}.gz")

    if dry_run:
        res["action"]  = "would_rotate"
        res["archive"] = str(archive.name)
        return res

    try:
        # Copy-then-truncate keeps any open writer handle on the original
        # file intact (Windows file-locking-friendly).
        with file_path.open("rb") as src, gzip.open(
                str(archive), "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, length=4 * 1024 * 1024)
        # Truncate the active file to zero length without breaking
        # the writer's file descriptor.
        with file_path.open("r+b") as fp:
            fp.truncate(0)
        res["action"]    = "rotated"
        res["archive"]   = str(archive.name)
        res["after_bytes"] = file_path.stat().st_size
        res["pruned"]    = _prune_old(file_path, RETAIN)
    except Exception as exc:
        res["error"] = f"{type(exc).__name__}: {exc}"
        if archive.exists():
            try:
                archive.unlink()
            except OSError:
                pass
    return res


def rotate_all(dry_run: bool = False) -> dict:
    free_before = _disk_free_bytes(STATE_DIR)
    results = []
    for fname in TARGETS:
        results.append(_rotate_one(STATE_DIR / fname, dry_run))
    free_after = _disk_free_bytes(STATE_DIR)
    return {
        "project":         "funding_carry",
        "ts_iso":          datetime.now(
            tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dry_run":         dry_run,
        "free_bytes_before": free_before,
        "free_bytes_after":  free_after,
        "free_gb_before":  round(free_before / 1e9, 2),
        "free_gb_after":   round(free_after / 1e9, 2),
        "rotated":         sum(1 for r in results if r["action"] == "rotated"),
        "skipped":         sum(1 for r in results if r["action"] == "skipped"),
        "missing":         sum(1 for r in results if r["action"] == "missing"),
        "errors":          sum(1 for r in results if r["error"]),
        "results":         results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Sprint 58 — state rotation")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out = rotate_all(dry_run=args.dry_run)
    # Append summary to a log file (cheap, ~250 bytes per run)
    log_path = STATE_DIR / "state_rotate_summary.jsonl"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
    except OSError:
        pass
    print(json.dumps(out, indent=2))
    return 0 if out["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
