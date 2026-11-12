"""
Sprint 32 (2026-06-02) — Low-volume audit scheduler tests.

  1.  Scheduler calls low_volume_audit without trading.
  2.  Summary record writes active_trading=false / paper_trade=false
      / executor_emit=false / mode=shadow.
  3.  Audit failure does not crash the loop (errors=1 in summary).
  4.  No executor/order/open_position imports or call sites in module.
  5.  Main funding_carry strategy code unchanged (no edits to universe.py,
      strategy/*, safety.py, execution/*).
  6.  Live vol_floor unchanged (VOLUME_FLOOR_USD_24H still 10_000_000).
  7.  Summary metrics count verdicts correctly.
  8.  Duplicate scheduling does not start a second audit loop.
  9.  Scheduler interval is configurable (env CARRY_LV_AUDIT_INTERVAL_S).
  10. Missing files / empty shadow tail handled safely.

Run:
    cd "C:\\Users\\adama\\Documents\\zeph ai projects\\funding_carry"
    py -3 tests/test_sprint32_low_volume_audit_scheduler.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent          # .../funding_carry
_PROJ = _ROOT.parent           # .../zeph ai projects
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from funding_carry.external import low_volume_audit_scheduler as S  # noqa: E402
from funding_carry.external import low_volume_audit as A  # noqa: E402
from funding_carry import config as CFG  # noqa: E402

_passed = 0
_failed = 0


def _check(c, m):
    global _passed, _failed
    if c:
        _passed += 1
        print(f"  PASS  {m}")
    else:
        _failed += 1
        print(f"  FAIL  {m}")


def _section(t):
    print("\n" + "=" * 70 + f"\n{t}\n" + "=" * 70)


def _redir(tmp: Path):
    """Redirect module-level file paths into a temp dir, reset guard."""
    S.SUMMARY_FILE = tmp / "summary.jsonl"
    A.LOW_VOL_FILE = tmp / "shadow.jsonl"
    S.reset_guard_for_tests()


# ──────────────────────────────────────────────────────────────────────────
def test_1_scheduler_calls_audit_no_trading():
    _section("1. scheduler calls audit_fn and produces summary "
             "(no trading side-effects)")
    tmp = Path(tempfile.mkdtemp())
    _redir(tmp)
    # Stub audit_fn — pretend audit_once returned 2 candidates with mixed
    # verdicts.  Also seed the shadow file (since scheduler reads its tail).
    A.LOW_VOL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with A.LOW_VOL_FILE.open("w", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": 1.0, "source": "funding_carry_low_volume_shadow",
            "base": "ARM", "long_exchange": "binance", "short_exchange": "bitget",
            "validation_verdict": "depth_ok", "gross_apr": 220.0,
            "fee_coverage_ratio": 3.1, "orderbook_spread_bps": 4.2,
            "depth_slip_long_bps": 3.0, "depth_slip_short_bps": 5.0,
            "min_leg_volume_24h_usd": 6_000_000, "mode": "shadow",
            "active_trading": False,
        }) + "\n")
        f.write(json.dumps({
            "ts": 2.0, "source": "funding_carry_low_volume_shadow",
            "base": "CRWV", "long_exchange": "bybit", "short_exchange": "mexc",
            "validation_verdict": "depth_too_thin", "gross_apr": 380.0,
            "fee_coverage_ratio": 1.2, "orderbook_spread_bps": 30.0,
            "min_leg_volume_24h_usd": 1_500_000, "mode": "shadow",
            "active_trading": False,
        }) + "\n")
    called = {"n": 0}

    def stub_audit_fn():
        called["n"] += 1
        return {"total_low_volume_candidates": 2,
                "by_verdict": {"depth_ok": 1, "depth_too_thin": 1}}

    summary = S.run_one_audit_step(stub_audit_fn)
    _check(called["n"] == 1, "audit_fn called exactly once")
    _check(S.SUMMARY_FILE.exists(), "summary file written")
    _check(summary["candidates"] == 2, f"candidates={summary['candidates']}")
    _check(summary["depth_ok"] == 1, f"depth_ok={summary['depth_ok']}")
    _check(summary["depth_too_thin"] == 1, f"depth_too_thin={summary['depth_too_thin']}")


def test_2_summary_flags():
    _section("2. summary record has active_trading/paper_trade/"
             "executor_emit=False, mode=shadow")
    tmp = Path(tempfile.mkdtemp())
    _redir(tmp)
    A.LOW_VOL_FILE.parent.mkdir(parents=True, exist_ok=True)
    A.LOW_VOL_FILE.write_text("", encoding="utf-8")
    summary = S.run_one_audit_step(lambda: {"total_low_volume_candidates": 0,
                                            "by_verdict": {}})
    _check(summary["active_trading"] is False, "active_trading=False")
    _check(summary["paper_trade"]    is False, "paper_trade=False")
    _check(summary["executor_emit"]  is False, "executor_emit=False")
    _check(summary["mode"] == "shadow", "mode=shadow")
    # And the on-disk row must agree (no race / late-mutation).
    rec = json.loads(S.SUMMARY_FILE.read_text(encoding="utf-8").strip())
    _check(rec["active_trading"] is False, "disk row active_trading=False")
    _check(rec["mode"] == "shadow", "disk row mode=shadow")


def test_3_audit_failure_doesnt_crash():
    _section("3. audit_fn raising does NOT crash; summary records errors=1")
    tmp = Path(tempfile.mkdtemp())
    _redir(tmp)
    A.LOW_VOL_FILE.parent.mkdir(parents=True, exist_ok=True)
    A.LOW_VOL_FILE.write_text("", encoding="utf-8")

    def crashing_audit():
        raise RuntimeError("simulated network blowup")
    summary = S.run_one_audit_step(crashing_audit)
    _check(summary["errors"] >= 1, f"errors={summary['errors']}")
    _check(summary["candidates"] == 0, f"candidates=0 ({summary['candidates']})")
    _check(S.SUMMARY_FILE.exists(), "summary still written on failure")


def test_4_no_executor_imports():
    _section("4. static scan: scheduler has no executor/order/PaperBook "
             "imports or call sites")
    src = Path(S.__file__).read_text(encoding="utf-8")
    # Strip docstrings + comments before pattern matching.
    code = re.sub(r'""".*?"""', "", src, flags=re.DOTALL)
    code = re.sub(r"#.*", "", code)
    banned_callsites = [
        "open_position(", ".open_position(", "place_order(",
        "create_order(", "submit_order(", "cancel_order(",
        "PaperBook(", "self.book.", "execute_trade(",
    ]
    hits = [s for s in banned_callsites if s in code]
    _check(not hits, f"no trading call sites ({hits})")
    banned_imports = [
        "from funding_carry.execution",
        "from funding_carry.strategy.executor",
        "import PaperBook",
    ]
    iimport_hits = [s for s in banned_imports if s in src]
    _check(not iimport_hits, f"no trading imports ({iimport_hits})")


def test_5_strategy_files_unchanged():
    _section("5. main bot strategy/safety/execution files unchanged "
             "(no Sprint 32 edits)")
    sprint32_marker = "Sprint 32"
    for sub in ("safety.py",):
        p = _ROOT / sub
        if p.exists():
            content = p.read_text(encoding="utf-8")
            _check(sprint32_marker not in content,
                   f"{sub} not edited by Sprint 32")
    for d in ("strategy", "execution"):
        dp = _ROOT / d
        if dp.exists():
            edited = []
            for f in dp.rglob("*.py"):
                try:
                    if sprint32_marker in f.read_text(encoding="utf-8"):
                        edited.append(f.name)
                except Exception:
                    pass
            _check(not edited, f"{d}/ untouched ({edited})")
    # main.py IS touched — confirm only the diagnostic-loop hook, not _cycle.
    main_src = (_ROOT / "main.py").read_text(encoding="utf-8")
    # The marker should appear in scheduler hook context only.
    sprint32_locs = [i for i, l in enumerate(main_src.splitlines(), 1)
                     if "Sprint 32" in l]
    # All Sprint 32 references must be inside the diagnostic-loop section
    # (lines BEFORE _cycle definition).
    cycle_line = next(
        (i for i, l in enumerate(main_src.splitlines(), 1)
         if "async def _cycle" in l), None)
    _check(cycle_line is not None, "found _cycle marker")
    if cycle_line is not None:
        _check(all(loc < cycle_line for loc in sprint32_locs),
               f"Sprint 32 edits only in diagnostic block "
               f"(locs={sprint32_locs}, _cycle@{cycle_line})")


def test_6_live_vol_floor_unchanged():
    _section("6. live vol_floor unchanged (VOLUME_FLOOR_USD_24H == 10_000_000)")
    _check(CFG.VOLUME_FLOOR_USD_24H == 10_000_000,
           f"config VOLUME_FLOOR_USD_24H={CFG.VOLUME_FLOOR_USD_24H}")
    _check(A.VOL_FLOOR_LIVE == 10_000_000.0,
           f"audit VOL_FLOOR_LIVE={A.VOL_FLOOR_LIVE}")


def test_7_verdict_counts_correct():
    _section("7. summary counts verdicts correctly across multiple records")
    tmp = Path(tempfile.mkdtemp())
    _redir(tmp)
    A.LOW_VOL_FILE.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    # 3 depth_ok, 2 spread_too_wide, 1 depth_too_thin, 1 fee_coverage_too_low,
    # 1 price_mismatch
    layout = [("depth_ok", 3), ("spread_too_wide", 2),
              ("depth_too_thin", 1), ("fee_coverage_too_low", 1),
              ("price_mismatch", 1)]
    ts = 100.0
    for v, n in layout:
        for i in range(n):
            ts += 1
            rows.append({
                "ts": ts, "base": f"SYM{int(ts)}", "validation_verdict": v,
                "long_exchange": "binance", "short_exchange": "bitget",
                "gross_apr": 50.0, "fee_coverage_ratio": 2.0,
                "orderbook_spread_bps": 5.0,
            })
    with A.LOW_VOL_FILE.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    audit_result = {"total_low_volume_candidates": len(rows),
                    "by_verdict": {v: n for v, n in layout}}
    summary = S.run_one_audit_step(lambda: audit_result)
    _check(summary["candidates"] == 8, f"candidates={summary['candidates']}")
    _check(summary["depth_ok"] == 3, f"depth_ok={summary['depth_ok']}")
    _check(summary["spread_too_wide"] == 2,
           f"spread_too_wide={summary['spread_too_wide']}")
    _check(summary["depth_too_thin"] == 1,
           f"depth_too_thin={summary['depth_too_thin']}")
    _check(summary["fee_coverage_too_low"] == 1,
           f"fee_low={summary['fee_coverage_too_low']}")
    _check(summary["price_mismatch"] == 1,
           f"price_mismatch={summary['price_mismatch']}")
    # Top symbols length capped at 8 and contains only depth_ok.
    _check(len(summary["top_symbols"]) == 3,
           f"top_symbols length={len(summary['top_symbols'])}")


def test_8_duplicate_loop_noop():
    _section("8. duplicate scheduling does NOT start a second audit loop")
    tmp = Path(tempfile.mkdtemp())
    _redir(tmp)
    A.LOW_VOL_FILE.parent.mkdir(parents=True, exist_ok=True)
    A.LOW_VOL_FILE.write_text("", encoding="utf-8")

    async def driver():
        stop = asyncio.Event()
        runs = {"n": 0}

        def stub():
            runs["n"] += 1
            return {"total_low_volume_candidates": 0, "by_verdict": {}}

        # Two concurrent loops with the same guard.  Second must no-op.
        t1 = asyncio.create_task(S.low_volume_audit_loop(
            stop, interval_s=300, audit_fn=stub, run_immediately=True))
        await asyncio.sleep(0.2)  # let t1 acquire the guard
        t2 = asyncio.create_task(S.low_volume_audit_loop(
            stop, interval_s=300, audit_fn=stub, run_immediately=True))
        await asyncio.sleep(0.4)
        stop.set()
        await asyncio.gather(t1, t2, return_exceptions=True)
        return runs["n"]

    n_runs = asyncio.run(driver())
    # First loop runs once immediately; second should NOT have added another.
    _check(n_runs == 1, f"only one run executed (got {n_runs})")


def test_9_interval_env_configurable():
    _section("9. scheduler interval is configurable via env var")
    # Default
    os.environ.pop("CARRY_LV_AUDIT_INTERVAL_S", None)
    _check(S.get_interval_s() == 1800.0, f"default {S.get_interval_s()}")
    # Override
    os.environ["CARRY_LV_AUDIT_INTERVAL_S"] = "600"
    _check(S.get_interval_s() == 600.0, f"override {S.get_interval_s()}")
    # Below min → clamped
    os.environ["CARRY_LV_AUDIT_INTERVAL_S"] = "10"
    _check(S.get_interval_s() == 300.0,
           f"floor clamp {S.get_interval_s()}")
    # Garbage → fallback to default
    os.environ["CARRY_LV_AUDIT_INTERVAL_S"] = "not_a_number"
    _check(S.get_interval_s() == 1800.0,
           f"garbage fallback {S.get_interval_s()}")
    os.environ.pop("CARRY_LV_AUDIT_INTERVAL_S", None)


def test_10_missing_files_handled_safely():
    _section("10. missing shadow file / empty tail handled safely")
    tmp = Path(tempfile.mkdtemp())
    _redir(tmp)
    # Do NOT create the shadow file at all.
    _check(not A.LOW_VOL_FILE.exists(), "shadow file absent (setup)")
    # Audit returns 0 candidates.
    summary = S.run_one_audit_step(
        lambda: {"total_low_volume_candidates": 0, "by_verdict": {}})
    _check(summary["candidates"] == 0, "candidates=0")
    _check(summary["depth_ok"] == 0, "depth_ok=0")
    _check(summary["errors"] == 0, f"errors={summary['errors']}")
    # All watch-list entries marked absent.
    wl = summary["watch_list"]
    _check(all(v.get("present") is False for v in wl.values()),
           "watch_list entries all marked absent")
    for sym in ("ARM", "CRWV", "PUNDIX"):
        _check(sym in wl, f"watch_list includes {sym}")


# ──────────────────────────────────────────────────────────────────────────
TESTS = [
    test_1_scheduler_calls_audit_no_trading,
    test_2_summary_flags,
    test_3_audit_failure_doesnt_crash,
    test_4_no_executor_imports,
    test_5_strategy_files_unchanged,
    test_6_live_vol_floor_unchanged,
    test_7_verdict_counts_correct,
    test_8_duplicate_loop_noop,
    test_9_interval_env_configurable,
    test_10_missing_files_handled_safely,
]


def main() -> int:
    for t in TESTS:
        try:
            t()
        except Exception:
            import traceback
            global _failed
            _failed += 1
            print(f"  FAIL  test crashed: {t.__name__}")
            traceback.print_exc()
    print(f"\nSUMMARY  passed={_passed}  failed={_failed}")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
