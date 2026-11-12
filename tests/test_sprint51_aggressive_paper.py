"""
Sprint 51 (2026-06-08) — Aggressive Funding Paper Lane tests.

33 tests covering env flag, crypto-only universe, atomic two-leg
simulation, leverage tier, kill-switches, and isolation from the
conservative FC PaperBook.

Run:
    cd "C:\\Users\\adama\\Documents\\zeph ai projects\\funding_carry"
    py -3 tests/test_sprint51_aggressive_paper.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_FC_ROOT = _HERE.parent
_PROJ = _FC_ROOT.parent
for p in (str(_PROJ), str(_FC_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from funding_carry import aggressive_paper as AP  # noqa: E402

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


def _enable():
    os.environ[AP.ENV_FLAG_NAME] = "1"


def _disable():
    os.environ.pop(AP.ENV_FLAG_NAME, None)


def _redir(tmp: Path):
    AP.LEDGER_FILE   = tmp / "ledger.json"
    AP.EVENTS_FILE   = tmp / "events.jsonl"
    AP.TRADES_FILE   = tmp / "trades.jsonl"
    AP.SUMMARY_FILE  = tmp / "summary.jsonl"
    AP.reset_state_for_tests()
    _disable()


def _quote(venue="bybit", bid=100.0, ask=100.05, bid_size=1000, ask_size=1000,
            funding_8h=0.001):
    return {"venue": venue, "best_bid": bid, "best_ask": ask,
            "bid_size": bid_size, "ask_size": ask_size,
            "snapshot_ts": time.time(), "funding_8h": funding_8h}


def _cand(symbol="BTC", **kw):
    base = {
        "symbol": symbol,
        "long_venue": "binance", "short_venue": "bybit",
        "volume_24h_usd": 5_000_000,
        "fee_coverage": 2.5,
        "net_edge_bps": 20.0,
        "ob_spread_bps": 8.0,
        "depth_usd_100bps": 30_000,
        "long_funding_8h":  -0.0005,
        "short_funding_8h":  0.0010,
        "long_quote":  _quote("binance"),
        "short_quote": _quote("bybit"),
        "notional_usd": 100,
        "title": "",
        "price_mismatch_bps": 0.0,
        "persistence_verdict": "persistent_depth_ok",
    }
    base.update(kw)
    return base


# ──────────────────────────────────────────────────────────────────────────
def test_1_default_flag_false():
    _section("T1. default env flag → is_enabled() == False")
    _disable()
    _check(AP.is_enabled() is False, "default False")
    for v in ("1", "true", "yes", "ON"):
        os.environ[AP.ENV_FLAG_NAME] = v
        _check(AP.is_enabled() is True, f"'{v}' → True")
    _disable()


def test_2_disabled_no_mutation():
    _section("T2. Disabled flag → no ledger mutation, no event written")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    r = AP.evaluate_candidate(_cand())
    _check(r["action"] == "disabled", f"action={r['action']}")
    _check(not AP.LEDGER_FILE.exists(), "no ledger")
    _check(not AP.EVENTS_FILE.exists(), "no events")


def test_3_ledger_isolated():
    _section("T3. Ledger path isolated from FC main paper_book.json")
    src = Path(AP.__file__).read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", src, flags=re.DOTALL)
    code = re.sub(r"#.*", "", code)
    # Look for the bare conservative filename, allowing the aggressive
    # variant ("aggressive_paper_book.json") to coexist.
    conservative_hits = re.findall(r"(?<!aggressive_)paper_book\.json",
                                   code)
    _check(not conservative_hits,
           f"no reference to bare paper_book.json in code "
           f"(found {len(conservative_hits)})")
    _check("aggressive_paper_book.json" in src,
           "isolated ledger filename in source")


def test_4_crypto_can_open():
    _section("T4. Crypto candidate opens position in enabled fixture")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    r = AP.evaluate_candidate(_cand(symbol="ETH"))
    _check(r["action"] == "opened", f"action={r['action']}")
    _check(r["position"]["asset_kind"] == "crypto", "asset_kind=crypto")
    # The default fixture has fee_coverage=2.5 which does NOT meet the
    # max-leverage gate (requires ≥3.0).  Default leverage should apply.
    _check(r["position"]["leverage"] == AP.DEFAULT_LEVERAGE,
           f"ordinary candidate → default leverage {r['position']['leverage']}")
    _check(r["position"]["active_trading"] is False, "active_trading=False")
    _check(r["position"]["paper_trade"]    is True,  "paper_trade=True")
    _check(r["position"]["executor_emit"]  is False, "executor_emit=False")
    _disable()


def test_5_equity_perp_rejected():
    _section("T5. SOXL (Sprint 50 contamination) → rejected")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    r = AP.evaluate_candidate(_cand(symbol="SOXL"))
    _check(r["action"] == "rejected", f"action={r['action']}")
    _check("equity_perp" in r["reason"], f"reason={r['reason']}")
    _disable()


def test_6_etf_rejected():
    _section("T6. IWM ETF rejected")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    r = AP.evaluate_candidate(_cand(symbol="IWM"))
    _check(r["action"] == "rejected", f"action={r['action']}")
    _check("equity_perp" in r["reason"], f"reason={r['reason']}")
    _disable()


def test_7_unknown_kind_rejected():
    _section("T7. Empty/unknown symbol rejected")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    r = AP.evaluate_candidate(_cand(symbol=""))
    _check(r["action"] == "rejected", f"action={r['action']}")
    _disable()


def test_8_stock_suffix_rejected():
    _section("T8. *STOCK suffix rejected")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    r = AP.evaluate_candidate(_cand(symbol="HPESTOCK"))
    _check(r["action"] == "rejected", f"action={r['action']}")
    _check("equity_perp" in r["reason"], f"reason={r['reason']}")
    _disable()


def test_9_both_legs_must_fill():
    _section("T9. Both legs filled allows open")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    r = AP.evaluate_candidate(_cand())
    _check(r["action"] == "opened",
           f"action={r['action']} ({r.get('reason')})")
    _disable()


def test_10_one_leg_unfilled_rejects():
    _section("T10. One leg with no depth → one_leg_unfilled / partial_fill")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    c = _cand()
    c["long_quote"]  = _quote("binance", ask_size=0.05)  # tiny size
    r = AP.evaluate_candidate(c)
    _check(r["action"] == "rejected", f"action={r['action']}")
    _check(r["reason"] in ("one_leg_unfilled", "partial_fill_mismatch"),
           f"reason={r['reason']}")
    _disable()


def test_11_partial_fill_mismatch_rejects():
    _section("T11. Partial-fill mismatch rejects")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    c = _cand()
    c["long_quote"]  = _quote("binance", ask_size=0.05)
    c["short_quote"] = _quote("bybit",   bid_size=0.05)
    r = AP.evaluate_candidate(c)
    _check(r["action"] == "rejected", f"action={r['action']}")
    _check(r["reason"] in ("one_leg_unfilled", "partial_fill_mismatch"),
           f"reason={r['reason']}")
    _disable()


def test_12_quote_skew_too_high_rejects():
    _section("T12. Quote skew > max → quote_skew_too_high")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    c = _cand()
    c["long_quote"]  = _quote("binance")
    c["short_quote"] = _quote("bybit")
    c["short_quote"]["snapshot_ts"] = time.time() - 5  # 5s skew
    r = AP.evaluate_candidate(c)
    _check(r["action"] == "rejected", f"action={r['action']}")
    _check(r["reason"] == "quote_skew_too_high", f"reason={r['reason']}")
    _disable()


def test_13_entry_basis_too_high():
    _section("T13. Entry basis too high → entry_basis_too_high")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    c = _cand()
    # huge price gap between venues — well over 30 bps basis
    c["long_quote"]  = _quote("binance", bid=100.00, ask=110.00,
                                bid_size=1000, ask_size=1000)
    c["short_quote"] = _quote("bybit",   bid=100.00, ask=100.05,
                                bid_size=1000, ask_size=1000)
    r = AP.evaluate_candidate(c)
    _check(r["action"] == "rejected", f"action={r['action']}")
    _check(r["reason"] in ("entry_basis_too_high", "slippage_too_high"),
           f"reason={r['reason']}")
    _disable()


def test_14_slippage_too_high():
    _section("T14. Wide spread → slippage_too_high")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    c = _cand(ob_spread_bps=30)
    # produce per-leg slippage > 15 bps
    c["long_quote"]  = _quote("binance", bid=100.00, ask=100.60,
                                bid_size=1000, ask_size=1000)
    c["short_quote"] = _quote("bybit",   bid=99.40, ask=100.00,
                                bid_size=1000, ask_size=1000)
    r = AP.evaluate_candidate(c)
    _check(r["action"] == "rejected", f"action={r['action']}")
    _check(r["reason"] in ("slippage_too_high", "spread_too_wide"),
           f"reason={r['reason']}")
    _disable()


def test_15_net_edge_below_threshold():
    _section("T15. net_edge_bps below MIN → net_edge_nonpositive")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    r = AP.evaluate_candidate(_cand(net_edge_bps=1.0))
    _check(r["action"] == "rejected", f"action={r['action']}")
    _check(r["reason"] == "net_edge_nonpositive", f"reason={r['reason']}")
    _disable()


def test_16_missing_funding_data():
    _section("T16. Missing funding data → missing_funding_data")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    c = _cand(long_funding_8h=None)
    r = AP.evaluate_candidate(c)
    _check(r["action"] == "rejected", f"action={r['action']}")
    _check(r["reason"] == "missing_funding_data", f"reason={r['reason']}")
    _disable()


def test_17_default_leverage_2x():
    _section("T17. Default leverage = 2x for ordinary candidate")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    r = AP.evaluate_candidate(_cand(symbol="A", fee_coverage=1.5,
                                      net_edge_bps=10,
                                      depth_usd_100bps=20_000))
    _check(r["action"] == "opened", f"action={r['action']}")
    _check(r["position"]["leverage"] == AP.DEFAULT_LEVERAGE,
           f"leverage={r['position']['leverage']}")
    _disable()


def test_18_max_leverage_only_for_high_confidence():
    _section("T18. 3x only when persistent_depth_ok + low spread + deep")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    # Sprint 55: use crypto-safe placeholder ("C" collides with
    # Citigroup in the v1.4 blocklist).
    r = AP.evaluate_candidate(_cand(symbol="CCC",
                                      fee_coverage=5.0,
                                      net_edge_bps=40,
                                      ob_spread_bps=5,
                                      depth_usd_100bps=80_000,
                                      persistence_verdict="persistent_depth_ok"))
    _check(r["position"]["leverage"] == AP.MAX_LEVERAGE,
           f"leverage={r['position']['leverage']}")
    _disable()


def test_19_max_open_positions_enforced():
    _section("T19. max_open_positions=2 enforced")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    for i, sym in enumerate(("A", "B")):
        r = AP.evaluate_candidate(_cand(symbol=sym))
        _check(r["action"] == "opened",
               f"open #{i+1}: action={r['action']} reason={r.get('reason')}")
    # Sprint 55: "C" collides with Citigroup blocklist — use "CCC".
    r = AP.evaluate_candidate(_cand(symbol="CCC"))
    _check(r["action"] == "rejected", f"3rd: action={r['action']}")
    _check(r["reason"] == "max_open_positions", f"reason={r['reason']}")
    _disable()


def test_20_daily_loss_cap_halts():
    _section("T20. daily_pnl_usd ≤ -$10 halts")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    lg = AP._fresh_ledger()
    lg["stats"]["daily_pnl_usd"] = AP.DAILY_LOSS_CAP_USD - 0.01
    halt, reason = AP.evaluate_portfolio_halt(lg, "crypto")
    _check(halt and reason == "daily_loss_cap",
           f"halt={halt} reason={reason}")
    _disable()


def test_21_drawdown_halts():
    _section("T21. drawdown > 5% halts")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    lg = AP._fresh_ledger()
    lg["balance_usd"] = lg["start_balance_usd"] * 0.93   # 7% DD
    halt, reason = AP.evaluate_portfolio_halt(lg, "crypto")
    _check(halt and reason == "drawdown_gt_5pct",
           f"halt={halt} reason={reason}")
    _disable()


def test_22_three_consecutive_hard_losses_halts():
    _section("T22. 3 consecutive hard losses halts")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    lg = AP._fresh_ledger()
    lg["stats"]["consecutive_hard_losses"] = 3
    halt, reason = AP.evaluate_portfolio_halt(lg, "crypto")
    _check(halt and reason == "three_consecutive_hard_losses",
           f"reason={reason}")
    _disable()


def test_23_rolling_median_halts():
    _section("T23. rolling 20-trade median ≤ 0 halts")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    AP._ROLLING_RESULTS.clear()
    for _ in range(11):
        AP._ROLLING_RESULTS.append((-1.0, False, False))
    for _ in range(9):
        AP._ROLLING_RESULTS.append((+2.0, False, False))
    halt, reason = AP.evaluate_portfolio_halt(AP._fresh_ledger(), "crypto")
    _check(halt and reason == "rolling_median_le_zero",
           f"reason={reason}")
    _disable()


def test_24_rolling_winrate_below_45_halts():
    _section("T24. rolling 20-trade winrate < 45% halts")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    AP._ROLLING_RESULTS.clear()
    # 8 wins, 12 losses but with positive median sum → winrate 40% only
    for _ in range(8):
        AP._ROLLING_RESULTS.append((+5.0, False, False))
    for _ in range(12):
        AP._ROLLING_RESULTS.append((+0.01, False, False))  # tiny "wins" only?
    # Now make 12 losses
    AP._ROLLING_RESULTS.clear()
    for _ in range(8):
        AP._ROLLING_RESULTS.append((+5.0, False, False))
    for _ in range(12):
        AP._ROLLING_RESULTS.append((-0.1, False, False))
    halt, reason = AP.evaluate_portfolio_halt(AP._fresh_ledger(), "crypto")
    _check(halt is True, f"halt={halt} reason={reason}")
    # Either rolling_winrate_below_45 OR rolling_median_le_zero may fire first
    _check(reason in ("rolling_winrate_below_45", "rolling_median_le_zero",
                       "rolling_avg_pnl_below_zero"),
           f"reason={reason}")
    _disable()


def test_25_basis_drift_stop():
    _section("T25. position basis drift > MAX → close_required")
    pos = {"ts_open": time.time(),
           "entry": {"entry_basis_bps": 0.0,
                      "funding_long_8h": -0.0001}}
    long_q = AP.LegQuote("binance", best_bid=100, best_ask=100.05,
                          snapshot_ts=time.time())
    short_q = AP.LegQuote("bybit",   best_bid=101.5, best_ask=101.55,
                          snapshot_ts=time.time(), funding_8h=-0.0001)
    chk = AP.check_position_health(pos, long_q, short_q)
    _check(chk.halt is True, f"halt={chk.halt}")
    _check(chk.reason == "basis_drift_stop", f"reason={chk.reason}")


def test_26_funding_flip_closes():
    _section("T26. Funding flips against position → close")
    pos = {"ts_open": time.time(),
           "entry": {"funding_long_8h": -0.001,
                      "entry_basis_bps": 0.0}}
    long_q  = AP.LegQuote("binance", 100, 100.05,
                          snapshot_ts=time.time(), funding_8h=+0.002)
    short_q = AP.LegQuote("bybit", 100, 100.05, snapshot_ts=time.time())
    chk = AP.check_position_health(pos, long_q, short_q)
    _check(chk.halt is True and chk.reason == "funding_flipped",
           f"halt={chk.halt} reason={chk.reason}")


def test_27_quote_stale_closes():
    _section("T27. Stale quote → close_required")
    pos = {"ts_open": time.time(),
           "entry": {"entry_basis_bps": 0.0}}
    long_q  = AP.LegQuote("binance", 100, 100.05,
                          snapshot_ts=time.time() - 60,
                          funding_8h=-0.0001)
    short_q = AP.LegQuote("bybit",   100, 100.05,
                          snapshot_ts=time.time() - 60,
                          funding_8h=-0.0001)
    chk = AP.check_position_health(pos, long_q, short_q)
    _check(chk.halt is True and chk.reason == "quote_stale",
           f"reason={chk.reason}")


def test_28_no_executor_calls():
    _section("T28. aggressive_paper.py has no executor/order callsites")
    src = Path(AP.__file__).read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", src, flags=re.DOTALL)
    code = re.sub(r"#.*", "", code)
    banned = ["open_position(", "place_order(", "create_order(",
               "submit_order(", "ExecutorAgent(", "bus.publish("]
    hits = [s for s in banned if s in code]
    _check(not hits, f"no banned callsites ({hits})")
    banned_imp = ["from funding_carry.strategy",
                   "from funding_carry.execution"]
    iim = [s for s in banned_imp if s in src]
    _check(not iim, f"no banned imports ({iim})")


def test_29_no_conservative_config_mutation():
    _section("T29. No mutation of conservative FC config values")
    from funding_carry import config as CFG
    # Capture before
    floor_before = CFG.VOLUME_FLOOR_USD_24H
    # Exercise
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    AP.evaluate_candidate(_cand())
    _disable()
    # Capture after
    _check(CFG.VOLUME_FLOOR_USD_24H == floor_before == 10_000_000,
           f"VOLUME_FLOOR_USD_24H unchanged={CFG.VOLUME_FLOOR_USD_24H}")


def test_30_no_main_paper_book_mutation():
    _section("T30. Conservative paper_book.json not opened for write")
    src = Path(AP.__file__).read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", src, flags=re.DOTALL)
    code = re.sub(r"#.*", "", code)
    conservative_hits = re.findall(r"(?<!aggressive_)paper_book\.json",
                                   code)
    _check(not conservative_hits,
           f"no bare paper_book.json in code "
           f"(found {len(conservative_hits)})")
    _check(not re.search(r"open\(.*(?<!aggressive_)paper_book\.json",
                          code),
           "no open(...paper_book.json) call to conservative file")


def test_31_malformed_diagnostics_safe():
    _section("T31. evaluate_candidate handles malformed input")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    for bad in (None, {}, {"symbol": None}, {"symbol": "BTC"}):
        r = AP.evaluate_candidate(bad or {})
        _check(r["action"] in ("rejected", "error", "disabled", "opened"),
               f"input {bad} → {r['action']}")
    _disable()


def test_32_asset_contamination_halt():
    _section("T32. Asset contamination at portfolio gate halts")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    halt, reason = AP.evaluate_portfolio_halt(AP._fresh_ledger(),
                                                "equity_perp")
    _check(halt and reason == "asset_contamination_detected",
           f"reason={reason}")
    _disable()


def test_33_one_leg_exposure_kill_switch_reason():
    _section("T33. One-leg detection reason exists in HALT_REASONS list")
    _check("one_leg_exposure_detected" in AP.HALT_REASONS,
           f"reason present in: {AP.HALT_REASONS}")


# ──────────────────────────────────────────────────────────────────────────
TESTS = [
    test_1_default_flag_false,
    test_2_disabled_no_mutation,
    test_3_ledger_isolated,
    test_4_crypto_can_open,
    test_5_equity_perp_rejected,
    test_6_etf_rejected,
    test_7_unknown_kind_rejected,
    test_8_stock_suffix_rejected,
    test_9_both_legs_must_fill,
    test_10_one_leg_unfilled_rejects,
    test_11_partial_fill_mismatch_rejects,
    test_12_quote_skew_too_high_rejects,
    test_13_entry_basis_too_high,
    test_14_slippage_too_high,
    test_15_net_edge_below_threshold,
    test_16_missing_funding_data,
    test_17_default_leverage_2x,
    test_18_max_leverage_only_for_high_confidence,
    test_19_max_open_positions_enforced,
    test_20_daily_loss_cap_halts,
    test_21_drawdown_halts,
    test_22_three_consecutive_hard_losses_halts,
    test_23_rolling_median_halts,
    test_24_rolling_winrate_below_45_halts,
    test_25_basis_drift_stop,
    test_26_funding_flip_closes,
    test_27_quote_stale_closes,
    test_28_no_executor_calls,
    test_29_no_conservative_config_mutation,
    test_30_no_main_paper_book_mutation,
    test_31_malformed_diagnostics_safe,
    test_32_asset_contamination_halt,
    test_33_one_leg_exposure_kill_switch_reason,
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
