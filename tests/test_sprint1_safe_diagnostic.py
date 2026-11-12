"""
Sprint 1 (2026-05-26) regression tests for funding_carry safe-diagnostic mode.

Run:
    cd "C:\\Users\\adama\\Documents\\zeph ai projects"
    py -3 funding_carry/tests/test_sprint1_safe_diagnostic.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from funding_carry.config import (
    CARRY_SAFE_DIAGNOSTIC_MODE,
    MIN_FEE_COVERAGE_RATIO_SAFE,
    MIN_FUNDING_RATE_PER_PERIOD_SAFE,
    MIN_HOLD_CYCLES_FOR_PROFIT_ZERO,
    MIN_NET_APR_PCT_SAFE,
    SYMBOL_COOLDOWN_AFTER_BAD_EXIT_S,
    TEMP_BLACKLIST_BASES,
)
from funding_carry.data.types import CarryLeg, CarryOpportunity
from funding_carry.diagnostics import (
    CooldownTracker,
    estimate_fee_coverage,
    max_abs_perp_funding_rate,
    normalised_rate_per_8h,
)

_failed = 0
_passed = 0


def _check(cond: bool, msg: str) -> None:
    global _failed, _passed
    if cond:
        _passed += 1
        print(f"  PASS  {msg}")
    else:
        _failed += 1
        print(f"  FAIL  {msg}")


def _section(t: str) -> None:
    print()
    print("=" * 70)
    print(t)
    print("=" * 70)


def _mk_opp(*, base: str, long_rate: float, short_rate: float,
            same_ex: bool = False,
            long_kind: str = "perp", short_kind: str = "perp",
            interval_h: float = 8.0,
            long_ex: str = "binance", short_ex: str = "bybit") -> CarryOpportunity:
    ll = CarryLeg(exchange=long_ex,  leg_kind=long_kind,  direction="LONG",
                  symbol=f"{base}USDT", price=1.0,
                  funding_rate=long_rate,
                  funding_interval_h=interval_h if long_kind == "perp" else 0.0,
                  next_funding_ts=int(time.time() * 1000) + 60_000,
                  volume_usd=1e8)
    sl = CarryLeg(exchange=short_ex, leg_kind=short_kind, direction="SHORT",
                  symbol=f"{base}USDT", price=1.0,
                  funding_rate=short_rate,
                  funding_interval_h=interval_h if short_kind == "perp" else 0.0,
                  next_funding_ts=int(time.time() * 1000) + 60_000,
                  volume_usd=1e8)
    return CarryOpportunity(
        base=base, long_leg=ll, short_leg=sl,
        fetched_ts=time.time(),
    )


# ─── tests ──────────────────────────────────────────────────────────────


def test_funding_rate_too_low():
    _section("1. funding 0.1%/8h -> rejected funding_rate_too_low")
    opp = _mk_opp(base="X", long_rate=0.0, short_rate=0.001)   # 0.1%/8h
    max_rate = max_abs_perp_funding_rate(opp)
    rate_8h = normalised_rate_per_8h(max_rate, 8.0)
    _check(rate_8h == 0.001, f"normalised rate per 8h == 0.001 (got {rate_8h})")
    _check(rate_8h < MIN_FUNDING_RATE_PER_PERIOD_SAFE,
           "rate 0.1%/8h is below MIN_FUNDING_RATE_PER_PERIOD_SAFE (0.3%)")


def test_funding_rate_passes_at_threshold():
    _section("2. funding 0.5%/8h -> passes funding_rate gate")
    opp = _mk_opp(base="Y", long_rate=0.0, short_rate=0.005)   # 0.5%/8h
    max_rate = max_abs_perp_funding_rate(opp)
    rate_8h = normalised_rate_per_8h(max_rate, 8.0)
    _check(rate_8h >= MIN_FUNDING_RATE_PER_PERIOD_SAFE,
           "0.5%/8h passes 0.3% gate")


def test_funding_rate_4h_interval_normalisation():
    _section("3. 4h funding interval normalised correctly")
    # 0.001/4h ~= 0.002/8h equivalent
    opp = _mk_opp(base="Z", long_rate=0.0, short_rate=0.001, interval_h=4.0)
    rate_8h = normalised_rate_per_8h(max_abs_perp_funding_rate(opp), 4.0)
    _check(abs(rate_8h - 0.002) < 1e-9,
           f"0.1%/4h -> 0.2%/8h equivalent (got {rate_8h})")
    _check(rate_8h < MIN_FUNDING_RATE_PER_PERIOD_SAFE,
           "even normalised, 0.1%/4h still under 0.3%/8h floor")


def test_fee_coverage_too_low():
    _section("4. fee coverage < 2x -> rejected fee_coverage_too_low")
    # Weak funding: 0.0003 (0.03%/8h) cross-perp, notional $2000
    opp = _mk_opp(base="A", long_rate=0.0, short_rate=0.0003)
    cov = estimate_fee_coverage(opp=opp, notional_usd=2000.0)
    print(f"     fees=${cov.fees_roundtrip_usd:.3f} "
          f"funding/cycle=${cov.funding_per_cycle_usd:.3f} "
          f"cov={cov.coverage_ratio:.2f}x")
    _check(cov.coverage_ratio < MIN_FEE_COVERAGE_RATIO_SAFE,
           "weak funding -> coverage < 2x rejected")


def test_fee_coverage_strong_passes():
    _section("5. strong funding 0.5%/8h -> coverage >= 2x")
    opp = _mk_opp(base="B", long_rate=0.0, short_rate=0.005)
    cov = estimate_fee_coverage(opp=opp, notional_usd=2000.0)
    print(f"     fees=${cov.fees_roundtrip_usd:.3f} "
          f"funding/cycle=${cov.funding_per_cycle_usd:.3f} "
          f"cov={cov.coverage_ratio:.2f}x")
    _check(cov.coverage_ratio >= MIN_FEE_COVERAGE_RATIO_SAFE,
           "strong funding passes coverage gate")


def test_temp_blacklist_contains_problem_symbols():
    _section("6. temp blacklist contains NVDA + NEAR")
    _check("NVDA" in TEMP_BLACKLIST_BASES, "NVDA blacklisted")
    _check("NEAR" in TEMP_BLACKLIST_BASES, "NEAR blacklisted")


def test_cooldown_lifecycle():
    _section("7. cooldown lifecycle: bad exit -> 24h cooldown")
    # Use throwaway STATE_DIR via env-controlled path? Easier: just call
    # tracker directly. It will persist to real STATE_DIR but that is OK
    # because the file is harmless and tracked symbols are synthetic.
    tracker = CooldownTracker()
    # Synthetic base; ensure no leftover state
    base = "TEST_COOLDOWN_BASE_XYZ"
    # Force-clear
    tracker._until.pop(base, None)
    tracker._save_state_file()
    _check(not tracker.is_on_cooldown(base), "no cooldown initially")
    now = time.time()
    was_bad = tracker.record_close(
        base=base, exit_ts=now,
        realized_pnl_usd=-1.5,
        exit_reason="profit_zero_decayed",
    )
    _check(was_bad, "negative-PnL profit_zero_decayed flagged as bad exit")
    _check(tracker.is_on_cooldown(base, now + 60), "cooldown active 60s later")
    _check(tracker.is_on_cooldown(base, now + 23 * 3600), "cooldown still active at 23h")
    _check(not tracker.is_on_cooldown(base, now + 25 * 3600), "cooldown expired at 25h")
    # cleanup
    tracker._until.pop(base, None)
    tracker._save_state_file()


def test_positive_pnl_not_cooldowned_but_zero_decayed_is():
    _section("8. profit_zero_decayed with positive PnL -> still cooldown")
    tracker = CooldownTracker()
    base = "TEST_POS_PNL_ZERODECAYED"
    tracker._until.pop(base, None)
    was_bad = tracker.record_close(
        base=base, exit_ts=time.time(),
        realized_pnl_usd=+0.5,
        exit_reason="profit_zero_decayed",
    )
    _check(was_bad,
           "exit_reason profit_zero_decayed triggers cooldown even on small win")
    tracker._until.pop(base, None)
    tracker._save_state_file()


def test_clean_exit_no_cooldown():
    _section("9. basis_drift positive PnL exit (winner) -> no cooldown")
    tracker = CooldownTracker()
    base = "TEST_CLEAN_BASE"
    tracker._until.pop(base, None)
    was_bad = tracker.record_close(
        base=base, exit_ts=time.time(),
        realized_pnl_usd=+3.2,
        exit_reason="manual_exit",   # not in BAD_EXIT_REASONS
    )
    _check(not was_bad, "positive PnL + clean reason -> no cooldown")
    _check(not tracker.is_on_cooldown(base), "not on cooldown after clean exit")


def test_config_invariants():
    _section("10. config invariants")
    _check(CARRY_SAFE_DIAGNOSTIC_MODE is True,
           "CARRY_SAFE_DIAGNOSTIC_MODE defaults True")
    _check(MIN_FUNDING_RATE_PER_PERIOD_SAFE == 0.003,
           "MIN_FUNDING_RATE_PER_PERIOD_SAFE = 0.003 (0.3%/8h)")
    _check(MIN_FEE_COVERAGE_RATIO_SAFE == 2.0,
           "MIN_FEE_COVERAGE_RATIO_SAFE = 2.0")
    _check(MIN_NET_APR_PCT_SAFE == 80.0,
           "MIN_NET_APR_PCT_SAFE = 80")
    _check(SYMBOL_COOLDOWN_AFTER_BAD_EXIT_S == 24 * 3600,
           "SYMBOL_COOLDOWN_AFTER_BAD_EXIT_S = 24h")
    _check(MIN_HOLD_CYCLES_FOR_PROFIT_ZERO == 2,
           "MIN_HOLD_CYCLES_FOR_PROFIT_ZERO = 2")


def main() -> int:
    for t in (
        test_funding_rate_too_low,
        test_funding_rate_passes_at_threshold,
        test_funding_rate_4h_interval_normalisation,
        test_fee_coverage_too_low,
        test_fee_coverage_strong_passes,
        test_temp_blacklist_contains_problem_symbols,
        test_cooldown_lifecycle,
        test_positive_pnl_not_cooldowned_but_zero_decayed_is,
        test_clean_exit_no_cooldown,
        test_config_invariants,
    ):
        try:
            t()
        except Exception as exc:
            import traceback
            global _failed
            _failed += 1
            print(f"  FAIL  test crashed: {t.__name__}")
            traceback.print_exc()
    print()
    print("=" * 70)
    print(f"SUMMARY  passed={_passed}  failed={_failed}")
    print("=" * 70)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
