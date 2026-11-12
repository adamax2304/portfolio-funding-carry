"""
funding_carry/external/low_volume_audit_scheduler.py

Sprint 32 (2026-06-02) — Recurring low-volume audit scheduler.

PURE DIAGNOSTIC.  Wraps the existing `low_volume_audit.audit_once()` in a
recurring async loop intended to be wired into the main funding_carry bot
as one more side-channel diagnostic task (same pattern as `_heartbeat_loop`,
`_hourly_summary_loop`, `_daily_summary_loop` in main.py).

Hard constraints (re-asserted in every public path):
  * Never opens a trade.
  * Never calls executor / order placement / open_position.
  * Never mutates main bot state (book / cooldown / universe filter / strategy).
  * Failure (network, parse error, anything) MUST NOT crash main bot.
  * Default interval 30 minutes, env-configurable.

Outputs:
  * `funding_carry/state/low_volume_funding_shadow.jsonl`
       — handled by audit_once itself (one record per scanned candidate)
  * `funding_carry/state/low_volume_audit_summary.jsonl`
       — appended by this scheduler (one record per run)

The summary record contains aggregate counts + symbol-level "watch list"
status for the persistent depth_ok / unstable / transient set established
in Sprint 21.4.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────
_HERE      = Path(__file__).resolve().parent
_ROOT      = _HERE.parent
STATE_DIR  = _ROOT / "state"
SUMMARY_FILE = STATE_DIR / "low_volume_audit_summary.jsonl"

# ── Tracked symbols (Sprint 21.4 watch-list + new from spec) ───────────────
TRACKED_SYMBOLS: tuple[str, ...] = (
    "ARM", "CRWV", "PUNDIX", "SIGN", "AIGENSYN",
    "STEEM", "HOME", "MEME", "GENIUS", "CBRS", "POWR", "M",
)

# ── Interval ───────────────────────────────────────────────────────────────
DEFAULT_INTERVAL_S = 30 * 60  # 30 min per spec §3
MIN_INTERVAL_S     = 5  * 60  # safety floor — never poll faster than 5 min


def get_interval_s() -> float:
    """Resolve interval from env override at call time (testable)."""
    raw = os.environ.get("CARRY_LV_AUDIT_INTERVAL_S")
    if raw:
        try:
            v = float(raw)
            if v < MIN_INTERVAL_S:
                log.warning(
                    "[lv_audit_sched] env interval %.0fs < min %.0fs; "
                    "clamping to min", v, MIN_INTERVAL_S)
                return float(MIN_INTERVAL_S)
            return v
        except ValueError:
            log.warning("[lv_audit_sched] bad CARRY_LV_AUDIT_INTERVAL_S=%r,"
                        " falling back to default", raw)
    return float(DEFAULT_INTERVAL_S)


# ── Single-instance guard ──────────────────────────────────────────────────
#
# A boolean lock guards against duplicate scheduling (spec §7 test 8).
# This is intentionally process-local; with the watchdog model, a crashed
# bot reset clears the flag automatically on restart.
class _SingletonGuard:
    def __init__(self):
        self._active = False

    def acquire(self) -> bool:
        if self._active:
            return False
        self._active = True
        return True

    def release(self) -> None:
        self._active = False

    @property
    def active(self) -> bool:
        return self._active


_GUARD = _SingletonGuard()


def is_loop_active() -> bool:
    return _GUARD.active


def reset_guard_for_tests() -> None:
    _GUARD.release()


def _utc_iso(ts: Optional[float] = None) -> str:
    try:
        t = ts if ts is not None else time.time()
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
    except Exception:
        return str(ts or time.time())


# ── Summary builder ────────────────────────────────────────────────────────

def _read_recent_shadow_records(window_records: int = 200) -> list[dict]:
    """Read tail of low_volume_funding_shadow.jsonl, return last N records.

    Used to attribute the records just appended by the current audit_once()
    call.  Reading tail rather than tracking byte-offset keeps the scheduler
    independent of audit_once internals.
    """
    from .low_volume_audit import LOW_VOL_FILE
    if not LOW_VOL_FILE.exists():
        return []
    try:
        lines = LOW_VOL_FILE.read_text(encoding="utf-8").splitlines()[-window_records:]
        out = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return out
    except Exception as exc:
        log.debug("[lv_audit_sched] read tail failed: %s", exc)
        return []


def build_summary(audit_result: dict | None,
                  recent_records: list[dict]) -> dict:
    """Build the per-run summary record. Pure — no I/O."""
    audit_result = audit_result or {}

    by_verdict_input = audit_result.get("by_verdict") or {}
    candidates_total = audit_result.get("total_low_volume_candidates")

    # Re-derive verdict counts from the recent tail (covers cases where
    # audit_once raised mid-run and didn't return its own summary).
    by_verdict = Counter(by_verdict_input)
    if not by_verdict and recent_records:
        by_verdict = Counter(
            r.get("validation_verdict") or "unknown"
            for r in recent_records
        )

    depth_ok_records = [r for r in recent_records
                        if r.get("validation_verdict") == "depth_ok"]

    # Top depth_ok symbols by APR
    depth_ok_records_sorted = sorted(
        depth_ok_records,
        key=lambda r: -(r.get("gross_apr") or 0))
    top_symbols = [
        {
            "base": r.get("base"),
            "long_exchange": r.get("long_exchange"),
            "short_exchange": r.get("short_exchange"),
            "gross_apr": r.get("gross_apr"),
            "fee_coverage_ratio": r.get("fee_coverage_ratio"),
            "orderbook_spread_bps": r.get("orderbook_spread_bps"),
            "min_leg_volume_24h_usd": r.get("min_leg_volume_24h_usd"),
        }
        for r in depth_ok_records_sorted[:8]
    ]

    # Watch-list snapshot (per-tracked-symbol latest status from this run).
    watch_list: dict[str, dict] = {}
    for sym in TRACKED_SYMBOLS:
        matches = [r for r in recent_records if (r.get("base") or "") == sym]
        if not matches:
            watch_list[sym] = {"present": False}
            continue
        # Take the latest match
        m = max(matches, key=lambda r: r.get("ts") or 0)
        watch_list[sym] = {
            "present": True,
            "verdict": m.get("validation_verdict"),
            "gross_apr": m.get("gross_apr"),
            "fee_coverage_ratio": m.get("fee_coverage_ratio"),
            "orderbook_spread_bps": m.get("orderbook_spread_bps"),
            "depth_slip_long_bps": m.get("depth_slip_long_bps"),
            "depth_slip_short_bps": m.get("depth_slip_short_bps"),
            "min_leg_volume_24h_usd": m.get("min_leg_volume_24h_usd"),
        }

    now = time.time()
    return {
        "ts":     round(now, 3),
        "ts_iso": _utc_iso(now),
        "source": "low_volume_audit_summary",
        "candidates":            int(candidates_total
                                     if candidates_total is not None
                                     else len(recent_records)),
        "depth_ok":              int(by_verdict.get("depth_ok", 0)),
        "spread_too_wide":       int(by_verdict.get("spread_too_wide", 0)),
        "depth_too_thin":        int(by_verdict.get("depth_too_thin", 0)),
        "price_mismatch":        int(by_verdict.get("price_mismatch", 0)),
        "fee_coverage_too_low":  int(by_verdict.get("fee_coverage_too_low", 0)),
        "unknown":               int(by_verdict.get("unknown", 0)),
        "top_symbols":           top_symbols,
        "watch_list":            watch_list,
        "errors":                int(audit_result.get("errors", 0)),
        # Safety re-assertions — visible on every line.
        "mode":                  "shadow",
        "active_trading":        False,
        "paper_trade":           False,
        "executor_emit":         False,
    }


def append_summary(rec: dict, path: Optional[Path] = None) -> bool:
    # NB: resolve SUMMARY_FILE at call time so tests that monkey-patch the
    # module-level constant work; default-arg-at-import-time would freeze
    # the production path.
    p = path if path is not None else SUMMARY_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:
        log.debug("[lv_audit_sched] summary write failed: %s (path=%s)",
                  exc, p)
        return False


# ── One audit step (callable for tests + first-run kick) ───────────────────

#: The "audit callable" signature this scheduler expects.  Tests pass an
#: in-memory stub; production wires this to low_volume_audit.audit_once.
AuditFn = Callable[[], dict]


def _default_audit_fn() -> dict:
    """Synchronous wrapper around audit_once.  Imported lazily so test
    stubs can replace it without dragging in network deps at import."""
    from .low_volume_audit import audit_once
    return audit_once(write=True, refresh_free_scan=True)


def run_one_audit_step(audit_fn: AuditFn | None = None) -> dict:
    """Synchronous: run one audit cycle + append a summary.

    Returns the summary dict (also persisted to SUMMARY_FILE).
    Never raises — on failure produces a summary record with ``errors=1``
    so the time series remains continuous.
    """
    fn = audit_fn or _default_audit_fn
    audit_result: dict | None = None
    errors = 0
    try:
        audit_result = fn() or {}
    except Exception as exc:
        errors += 1
        log.warning("[lv_audit_sched] audit raised: %s", exc)
        audit_result = {"errors": 1}

    try:
        recent = _read_recent_shadow_records()
    except Exception as exc:
        log.debug("[lv_audit_sched] tail read raised: %s", exc)
        recent = []

    summary = build_summary(audit_result, recent)
    if errors and not summary.get("errors"):
        summary["errors"] = errors

    # Hard safety re-assertion — must NEVER be True even if downstream tries.
    summary["active_trading"] = False
    summary["paper_trade"]    = False
    summary["executor_emit"]  = False
    summary["mode"]           = "shadow"

    append_summary(summary)
    log.info(
        "[lv_audit_sched] run done: candidates=%d depth_ok=%d "
        "spread_wide=%d depth_thin=%d fee_low=%d price_mis=%d errors=%d",
        summary["candidates"], summary["depth_ok"],
        summary["spread_too_wide"], summary["depth_too_thin"],
        summary["fee_coverage_too_low"], summary["price_mismatch"],
        summary["errors"],
    )
    return summary


# ── Async recurring loop (the public scheduler entry point) ────────────────

async def low_volume_audit_loop(stop_event: asyncio.Event,
                                interval_s: Optional[float] = None,
                                audit_fn: AuditFn | None = None,
                                run_immediately: bool = False) -> None:
    """Recurring scheduler loop — drop into main bot's asyncio.create_task().

    Single-instance guarded: a second call while one is running is a no-op
    and returns immediately.  Stops cleanly when stop_event is set.

    On every tick, run_one_audit_step is offloaded to a worker thread
    (audit_once is blocking HTTP) so we never starve the bot's event loop.
    """
    if not _GUARD.acquire():
        log.info("[lv_audit_sched] loop already active — refusing duplicate")
        return
    iv = float(interval_s) if interval_s is not None else get_interval_s()
    if iv < MIN_INTERVAL_S:
        iv = float(MIN_INTERVAL_S)
    log.info("[lv_audit_sched] starting — interval=%.0fs (run_imm=%s)",
             iv, run_immediately)
    try:
        if run_immediately:
            try:
                await asyncio.to_thread(run_one_audit_step, audit_fn)
            except Exception as exc:
                log.warning("[lv_audit_sched] immediate run failed: %s", exc)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=iv)
                # If wait_for returned without timeout → stop requested.
                return
            except asyncio.TimeoutError:
                pass
            try:
                await asyncio.to_thread(run_one_audit_step, audit_fn)
            except Exception as exc:
                # Last-resort guard. run_one_audit_step already swallows,
                # but to_thread itself might fail (e.g. interpreter shutdown).
                log.warning("[lv_audit_sched] step failed: %s", exc)
    finally:
        _GUARD.release()
        log.info("[lv_audit_sched] stopped")
