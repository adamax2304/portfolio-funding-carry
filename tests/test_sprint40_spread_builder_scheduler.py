"""
Sprint 40 (2026-06-06) — Spread Builder recurring scheduler tests.

15 tests covering interval config, single-instance guard, exception
isolation, output safety flags, and main-bot integration safety.

Run:
    cd "C:\\Users\\adama\\Documents\\zeph ai projects\\funding_carry"
    py -3 tests/test_sprint40_spread_builder_scheduler.py
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
_ROOT = _HERE.parent
_PROJ = _ROOT.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from funding_carry.external import spread_based_builder as B  # noqa: E402
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


def _disable_env():
    os.environ.pop("CARRY_SPREAD_BUILDER_INTERVAL_S", None)


def _redir(tmp: Path):
    B.DIAG_FILE = tmp / "diag.jsonl"
    B.SUMMARY_FILE = tmp / "summary.jsonl"
    B.LOW_VOL_FILE = tmp / "low_vol.jsonl"
    B.reset_guard_for_tests()


# ──────────────────────────────────────────────────────────────────────────
def test_1_default_interval():
    _section("1. scheduler default interval == 3600s")
    _disable_env()
    _check(B.get_interval_s() == 3600.0, f"default {B.get_interval_s()}")


def test_2_env_override():
    _section("2. CARRY_SPREAD_BUILDER_INTERVAL_S env override works")
    os.environ["CARRY_SPREAD_BUILDER_INTERVAL_S"] = "1800"
    _check(B.get_interval_s() == 1800.0, f"override {B.get_interval_s()}")
    _disable_env()


def test_3_min_clamp():
    _section("3. interval below 600s clamps to 600s")
    os.environ["CARRY_SPREAD_BUILDER_INTERVAL_S"] = "30"
    _check(B.get_interval_s() == 600.0, f"clamp {B.get_interval_s()}")
    os.environ["CARRY_SPREAD_BUILDER_INTERVAL_S"] = "garbage"
    _check(B.get_interval_s() == 3600.0,
           f"garbage → default {B.get_interval_s()}")
    _disable_env()


def test_4_run_immediately():
    _section("4. loop fires builder once with run_immediately=True")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    runs = {"n": 0}

    def stub():
        runs["n"] += 1
        return {"candidates": 0, "would_build": 0, "rejects": 0,
                "errors": 0, "records": []}

    async def driver():
        stop = asyncio.Event()
        t = asyncio.create_task(B.spread_builder_loop(
            stop, interval_s=600, builder_fn=stub, run_immediately=True))
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.gather(t, return_exceptions=True)
        return runs["n"]
    n = asyncio.run(driver())
    _check(n == 1, f"builder called exactly once ({n})")


def test_5_singleton_guard():
    _section("5. second concurrent loop is no-op")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    runs = {"n": 0}

    def stub():
        runs["n"] += 1
        return {"candidates": 0, "would_build": 0, "rejects": 0,
                "errors": 0, "records": []}

    async def driver():
        stop = asyncio.Event()
        t1 = asyncio.create_task(B.spread_builder_loop(
            stop, interval_s=600, builder_fn=stub, run_immediately=True))
        await asyncio.sleep(0.2)
        t2 = asyncio.create_task(B.spread_builder_loop(
            stop, interval_s=600, builder_fn=stub, run_immediately=True))
        await asyncio.sleep(0.4)
        stop.set()
        await asyncio.gather(t1, t2, return_exceptions=True)
        return runs["n"]
    n = asyncio.run(driver())
    _check(n == 1, f"only first instance ran ({n})")


def test_6_exception_does_not_crash():
    _section("6. builder exception is swallowed by loop")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)

    def crashing():
        raise RuntimeError("simulated builder failure")

    async def driver():
        stop = asyncio.Event()
        t = asyncio.create_task(B.spread_builder_loop(
            stop, interval_s=600, builder_fn=crashing,
            run_immediately=True))
        await asyncio.sleep(0.3)
        stop.set()
        try:
            await asyncio.gather(t, return_exceptions=True)
            return "ok"
        except Exception as e:
            return f"crashed: {e}"
    res = asyncio.run(driver())
    _check(res == "ok", f"loop survived ({res})")


def test_7_summary_error_on_grouping_failure():
    _section("7. summary records errors when grouping fails")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    # Pass a record that triggers exception inside grouping by monkey-
    # patching group_shadow_by_route.
    orig = B.group_shadow_by_route

    def boom(_):
        raise RuntimeError("inject")
    B.group_shadow_by_route = boom
    try:
        s = B.run_once(low_vol_records=[{"base": "X", "long_exchange": "a",
                                          "short_exchange": "b"}],
                        write=True)
        _check(s["errors"] >= 1, f"errors={s['errors']}")
        # summary file should still receive the error record
        _check(B.SUMMARY_FILE.exists(), "summary file written on error")
    finally:
        B.group_shadow_by_route = orig


def test_8_output_safety_flags():
    _section("8. records have active_trading/paper_trade/executor_emit=False")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    # Inject a single persistent_depth_ok fixture
    rs = []
    for i in range(12):
        rs.append({
            "ts": 1_000_000 + i, "base": "ARM", "symbol": "ARMUSDT",
            "long_exchange": "bybit", "short_exchange": "bitget",
            "long_funding_8h": -0.04, "short_funding_8h": -0.016,
            "funding_spread_8h": 0.024,
            "gross_apr": 400.0,
            "orderbook_spread_bps": 5.3,
            "depth_slip_long_bps": 0.1, "depth_slip_short_bps": 0.1,
            "price_mismatch_bps": 2.0,
            "fee_coverage_ratio": 3.91,
            "fees_roundtrip_frac": 0.0005,
            "validation_verdict": "depth_ok" if i < 11 else "spread_too_wide",
        })
    s = B.run_once(low_vol_records=rs, write=True)
    rows = [json.loads(l) for l in
            B.DIAG_FILE.read_text(encoding="utf-8").splitlines() if l]
    _check(rows, f"diag rows ({len(rows)})")
    for r in rows:
        _check(r["active_trading"] is False, "active_trading=False")
        _check(r["paper_trade"] is False, "paper_trade=False")
        _check(r["executor_emit"] is False, "executor_emit=False")
    sum_rows = [json.loads(l) for l in
                B.SUMMARY_FILE.read_text(encoding="utf-8").splitlines() if l]
    _check(sum_rows[-1]["active_trading"] is False,
           "summary active_trading=False")
    _check(sum_rows[-1]["paper_trade"] is False, "summary paper_trade=False")
    _check(sum_rows[-1]["executor_emit"] is False,
           "summary executor_emit=False")


def test_9_no_paperbook():
    _section("9. no PaperBook references in scheduler/builder")
    src = Path(B.__file__).read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", src, flags=re.DOTALL)
    code = re.sub(r"#.*", "", code)
    banned = [
        "from funding_carry.strategy", "import PaperBook",
        "PaperBook(", " paper_book.", "(paper_book.",
        "self.book.",
    ]
    hits = [s for s in banned if s in code]
    _check(not hits, f"no PaperBook refs ({hits})")


def test_10_no_executor():
    _section("10. no executor / order / open_position callsites")
    src = Path(B.__file__).read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", src, flags=re.DOTALL)
    code = re.sub(r"#.*", "", code)
    banned = [
        "open_position(", ".open_position(", "place_order(",
        "create_order(", "submit_order(", "execute_trade(",
    ]
    hits = [s for s in banned if s in code]
    _check(not hits, f"no trading calls ({hits})")
    banned_imports = [
        "from funding_carry.execution",
        "from funding_carry.strategy.executor",
    ]
    iim = [s for s in banned_imports if s in src]
    _check(not iim, f"no execution imports ({iim})")


def test_11_strategy_files_unchanged():
    _section("11. strategy / execution / safety files not edited by Sprint 40")
    marker = "Sprint 40"
    for f in ("safety.py", "watchdog.py", "config.py", "diagnostics.py"):
        p = _ROOT / f
        if p.exists():
            txt = p.read_text(encoding="utf-8")
            _check(marker not in txt, f"{f} not touched by Sprint 40")
    for d in ("strategy", "execution"):
        dp = _ROOT / d
        if dp.exists():
            edited = []
            for f in dp.rglob("*.py"):
                try:
                    if marker in f.read_text(encoding="utf-8"):
                        edited.append(f.name)
                except Exception:
                    pass
            _check(not edited, f"{d}/ untouched ({edited})")
    # main.py: Sprint 40 markers must be inside diagnostic-loop region only.
    main_src = (_ROOT / "main.py").read_text(encoding="utf-8")
    cycle_line = next((i for i, l in enumerate(main_src.splitlines(), 1)
                        if "async def _cycle" in l), None)
    sprint40_locs = [i for i, l in enumerate(main_src.splitlines(), 1)
                     if "Sprint 40" in l]
    _check(cycle_line is not None and
           all(loc < cycle_line for loc in sprint40_locs),
           f"Sprint 40 edits only in diagnostic block "
           f"(locs={sprint40_locs}, _cycle@{cycle_line})")


def test_12_live_vol_floor_unchanged():
    _section("12. live VOLUME_FLOOR_USD_24H still $10M")
    _check(CFG.VOLUME_FLOOR_USD_24H == 10_000_000,
           f"config {CFG.VOLUME_FLOOR_USD_24H}")
    # Also low_volume_audit constant.
    from funding_carry.external import low_volume_audit as LV
    _check(LV.VOL_FLOOR_LIVE == 10_000_000.0,
           f"audit VOL_FLOOR_LIVE {LV.VOL_FLOOR_LIVE}")


def test_13_independent_of_low_volume_audit():
    _section("13. scheduler does NOT interfere with low_volume_audit scheduler")
    from funding_carry.external import low_volume_audit_scheduler as LV
    # Different guard objects
    _check(LV._GUARD is not B._LOOP_GUARD,
           "guards are independent objects")
    # Different summary files
    _check(LV.SUMMARY_FILE != B.SUMMARY_FILE,
           f"different summary paths ({LV.SUMMARY_FILE.name} vs "
           f"{B.SUMMARY_FILE.name})")


def test_14_missing_files_safe():
    _section("14. missing input file → no crash")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    # B.LOW_VOL_FILE points to nonexistent path in tmp
    s = B.run_once(write=True)
    _check(s["candidates"] == 0, f"candidates=0 ({s['candidates']})")
    _check(s["errors"] == 0, f"errors={s['errors']}")
    _check(B.SUMMARY_FILE.exists(), "summary written")


def test_15_run_id_included():
    _section("15. run_id is included in diagnostic + summary records")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    rs = []
    for i in range(12):
        rs.append({
            "ts": 1_000_000 + i, "base": "ARM", "symbol": "ARMUSDT",
            "long_exchange": "bybit", "short_exchange": "bitget",
            "long_funding_8h": -0.04, "short_funding_8h": -0.016,
            "funding_spread_8h": 0.024, "gross_apr": 400.0,
            "orderbook_spread_bps": 5.3,
            "depth_slip_long_bps": 0.1, "depth_slip_short_bps": 0.1,
            "price_mismatch_bps": 2.0,
            "fee_coverage_ratio": 3.91, "fees_roundtrip_frac": 0.0005,
            "validation_verdict": "depth_ok" if i < 11 else "spread_too_wide",
        })
    s = B.run_once(low_vol_records=rs, write=True, run_id="sb-test-1234")
    _check(s.get("run_id") == "sb-test-1234",
           f"summary run_id={s.get('run_id')}")
    rows = [json.loads(l) for l in
            B.DIAG_FILE.read_text(encoding="utf-8").splitlines() if l]
    _check(rows, f"rows written ({len(rows)})")
    _check(all(r.get("run_id") == "sb-test-1234" for r in rows),
           "all diag rows carry the run_id")
    sum_rows = [json.loads(l) for l in
                B.SUMMARY_FILE.read_text(encoding="utf-8").splitlines() if l]
    _check(sum_rows[-1].get("run_id") == "sb-test-1234",
           f"summary-file row run_id={sum_rows[-1].get('run_id')}")


# ──────────────────────────────────────────────────────────────────────────
TESTS = [
    test_1_default_interval,
    test_2_env_override,
    test_3_min_clamp,
    test_4_run_immediately,
    test_5_singleton_guard,
    test_6_exception_does_not_crash,
    test_7_summary_error_on_grouping_failure,
    test_8_output_safety_flags,
    test_9_no_paperbook,
    test_10_no_executor,
    test_11_strategy_files_unchanged,
    test_12_live_vol_floor_unchanged,
    test_13_independent_of_low_volume_audit,
    test_14_missing_files_safe,
    test_15_run_id_included,
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
