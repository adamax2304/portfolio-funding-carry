"""
Sprint 20 (2026-05-31) — Free Funding Universe Scanner tests.

  1.  Binance parser maps fields correctly.
  2.  Bybit parser maps fields correctly.
  3.  OKX deferred — adapter slot is absent (documented).
  4.  Gate parser handles seconds-based interval.
  5.  Funding interval normalization (4h vs 8h → per_8h).
  6.  Symbol normalization handles all formats.
  7.  1000PEPE NOT merged with PEPE.
  8.  Opportunity builder picks min-funding=LONG, max=SHORT.
  9.  Gross APR calc (spread_8h × 1095 × 100).
  10. Missing exchange handled gracefully (adapter failure → skip + continue).
  11. Unsupported exchange (gate) marked data-only.
  12. JSONL record has active_trading=False.
  13. Scanner never calls executor/order/open_position.
  14. Internal comparison detects missing exchange.
  15. Internal comparison detects symbol mapping issue.
  16. Regime summary builds.
  17. API failure does not crash.

Run:
    cd "C:\\Users\\adama\\Documents\\zeph ai projects\\funding_carry"
    py -3 tests/test_sprint20_free_funding.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_ROOT.parent))

from funding_carry.external import free_funding_scanner as F  # noqa: E402

_failed = 0
_passed = 0


def _check(c, m):
    global _failed, _passed
    if c:
        _passed += 1; print(f"  PASS  {m}")
    else:
        _failed += 1; print(f"  FAIL  {m}")


def _section(t):
    print("\n" + "=" * 70 + f"\n{t}\n" + "=" * 70)


# ──────────────────────────────────────────────────────────────────────────
def test_1_binance_parser():
    _section("1. Binance parser maps premiumIndex fields")
    raw = {"symbol": "BTCUSDT", "markPrice": "60000",
           "indexPrice": "60010", "lastFundingRate": "0.0001",
           "nextFundingTime": 9999999999000, "time": 0}
    orig = F._http_json
    F._http_json = lambda url: (200, [raw], None)
    try:
        recs, err = F.fetch_binance()
    finally:
        F._http_json = orig
    _check(err is None and len(recs) == 1, f"1 record parsed (err={err})")
    r = recs[0]
    _check(r.exchange == "binance" and r.base == "BTC" and r.quote == "USDT",
           f"venue/base/quote ({r.exchange}/{r.base}/{r.quote})")
    _check(r.funding_rate == 0.0001, "funding_rate preserved")
    _check(r.mark_price == 60000.0, "mark_price preserved")


def test_2_bybit_parser():
    _section("2. Bybit parser maps tickers fields")
    raw = {"symbol": "BTCUSDT", "lastPrice": "60000", "markPrice": "60005",
           "fundingRate": "0.00005", "fundingIntervalHour": "4",
           "nextFundingTime": "9999999999000", "turnover24h": "1234567",
           "openInterestValue": "987654"}
    orig = F._http_json
    F._http_json = lambda url: (200, {"result": {"list": [raw]}}, None)
    try:
        recs, err = F.fetch_bybit()
    finally:
        F._http_json = orig
    _check(err is None and len(recs) == 1, f"1 record parsed (err={err})")
    r = recs[0]
    _check(r.funding_interval_h == 4.0, "4h interval parsed")
    _check(r.volume_24h_usd == 1234567.0, "volume parsed")
    _check(r.open_interest_usd == 987654.0, "open interest parsed")


def test_3_okx_deferred_documented():
    _section("3. OKX adapter deferred (no adapter slot)")
    _check("okx" not in F._ADAPTERS,
           "okx not in adapters (documented Sprint 20.1+)")


def test_4_gate_parser_seconds_interval():
    _section("4. Gate parser handles seconds-based interval")
    raw = {"name": "BTC_USDT", "funding_rate": "0.0002",
           "funding_interval": 28800, "funding_next_apply": 9999999999,
           "mark_price_round": "60000", "in_delisting": False}
    orig = F._http_json
    F._http_json = lambda url: (200, [raw], None)
    try:
        recs, err = F.fetch_gate()
    finally:
        F._http_json = orig
    _check(len(recs) == 1, "1 record")
    _check(recs[0].funding_interval_h == 8.0,
           f"28800s -> 8h ({recs[0].funding_interval_h})")


def test_5_interval_normalization():
    _section("5. funding_rate_8h normalization (4h doubles)")
    r4 = F.FundingRecord("x", "X", "X", "USDT", "linear_perp",
                          funding_rate=0.001, funding_interval_h=4.0,
                          next_funding_ts=None, mark_price=None)
    _check(abs(r4.funding_rate_8h - 0.002) < 1e-9, "4h × (8/4) = 0.002")
    r8 = F.FundingRecord("x", "X", "X", "USDT", "linear_perp",
                          funding_rate=0.001, funding_interval_h=8.0,
                          next_funding_ts=None, mark_price=None)
    _check(r8.funding_rate_8h == 0.001, "8h stays")
    # APR
    _check(abs(r8.annualized_apr_pct - 0.001 * 1095 * 100) < 1e-9, "APR calc")


def test_6_symbol_normalization():
    _section("6. symbol normalization handles all formats")
    for raw, expected in [
        ("BTCUSDT", "BTCUSDT"),
        ("BTC/USDT", "BTCUSDT"),
        ("BTC-USDT-SWAP", "BTCUSDT"),
        ("BTC_USDT", "BTCUSDT"),
        ("BTCUSDTM", "BTCUSDT"),     # KuCoin M suffix
        ("ETHUSDC", "ETHUSDC"),
    ]:
        r = F.normalize_symbol(raw)
        _check(r and r["normalized_symbol"] == expected,
               f"{raw} -> {expected} (got {r['normalized_symbol'] if r else None})")
    # Excluded
    for raw in ("SPYUSDT", "LLYUSDT", "AMDSTOCKUSDT", "BTCUSDT-240329",
                "BTCUSD"):
        r = F.normalize_symbol(raw)
        _check(r is None, f"excluded: {raw}")


def test_7_1000pepe_distinct():
    _section("7. 1000PEPE NOT merged with PEPE")
    a = F.normalize_symbol("1000PEPEUSDT")
    b = F.normalize_symbol("PEPEUSDT")
    _check(a and b and a["base"] != b["base"],
           f"1000PEPE base ({a['base']}) != PEPE base ({b['base']})")
    _check(a["base"] == "1000PEPE", "1000PEPE preserved verbatim")
    # 1MBABYDOGE similarly
    c = F.normalize_symbol("1MBABYDOGEUSDT")
    _check(c and c["base"] == "1MBABYDOGE", "1MBABYDOGE preserved")


def _mk(exchange, base, fr_8h_target, interval=8.0, mark=100.0):
    return F.FundingRecord(exchange=exchange, raw_symbol=f"{base}USDT",
                           base=base, quote="USDT", contract_type="linear_perp",
                           funding_rate=fr_8h_target * (interval / 8.0),
                           funding_interval_h=interval, next_funding_ts=None,
                           mark_price=mark)


def test_8_opportunity_builder():
    _section("8. opportunity builder picks LONG=min, SHORT=max")
    universe = {"BTC": {
        "binance": _mk("binance", "BTC", -0.0005),  # long pays (negative)
        "bybit":   _mk("bybit",   "BTC",  0.0010),  # short pays (positive)
        "bitget":  _mk("bitget",  "BTC",  0.0003),
    }}
    opps = F.build_opportunities(universe)
    _check(len(opps) == 1, f"1 opportunity built ({len(opps)})")
    o = opps[0]
    _check(o["long_exchange"] == "binance" and o["short_exchange"] == "bybit",
           f"long=binance, short=bybit (got {o['long_exchange']}/{o['short_exchange']})")
    _check(abs(o["long_funding_8h"] - (-0.0005)) < 1e-9, "long_funding correct")
    _check(abs(o["short_funding_8h"] - 0.0010) < 1e-9, "short_funding correct")
    _check(o["both_executable"] is True, "both executable")


def test_9_gross_apr_calc():
    _section("9. gross APR calculation")
    # spread 0.0015 per 8h -> 0.0015 × 1095 × 100 = 164.25%
    universe = {"X": {"binance": _mk("binance", "X", 0.0),
                      "bybit":   _mk("bybit",   "X", 0.0015)}}
    o = F.build_opportunities(universe)[0]
    _check(abs(o["gross_apr"] - 164.25) < 1e-6,
           f"APR = 164.25% (got {o['gross_apr']:.4f})")


def test_10_missing_exchange_graceful():
    _section("10. adapter failure -> skip + continue")
    orig = dict(F._ADAPTERS)
    F._ADAPTERS["binance"] = lambda: ([], "simulated_error")
    try:
        summary = F.scan_once(write=False, exchanges=("binance", "bybit"))
        _check(summary["fetch_status"]["binance"]["ok"] is False,
               "binance marked failed")
        # bybit live call may or may not work in test; main check: no crash
        _check("fetch_status" in summary, "summary has fetch_status")
    finally:
        F._ADAPTERS.clear()
        F._ADAPTERS.update(orig)


def test_11_data_only_gate():
    _section("11. gate marked data-only (not executable)")
    _check("gate" not in F._EXECUTABLE_VENUES, "gate not executable")
    _check("gate" in F._DATA_ONLY_EXCHANGES, "gate in data-only set")
    universe = {"BTC": {"gate": _mk("gate", "BTC", -0.001),
                        "binance": _mk("binance", "BTC", 0.001)}}
    o = F.build_opportunities(universe)[0]
    _check(o["both_executable"] is False,
           f"opp with gate not executable ({o['both_executable']})")
    _check("gate" in o["data_only_exchanges"], "gate listed in data_only")


def test_12_active_trading_false():
    _section("12. all records active_trading=False")
    universe = {"BTC": {"binance": _mk("binance", "BTC", 0.0),
                        "bybit":   _mk("bybit",   "BTC", 0.001)}}
    o = F.build_opportunities(universe)[0]
    _check(o["active_trading"] is False, "active_trading=False")
    _check(o["mode"] == "shadow", "mode=shadow")
    _check(o["source"] == "free_funding_scanner", "source set")


def test_13_no_executor_calls():
    _section("13. scanner source contains no trading call sites")
    import re as _re
    src = Path(F.__file__).read_text(encoding="utf-8")
    code = _re.sub(r'""".*?"""', "", src, flags=_re.DOTALL)
    code = _re.sub(r"#.*", "", code)
    banned = ["open_position(", ".open_position(", "place_order(",
              "create_order(", "submit_order(", "ExecutorAgent(",
              "bus.publish(", "paper_book.open"]
    hits = [b for b in banned if b in code]
    _check(not hits, f"no trading calls (hits: {hits})")
    _check("from funding_carry.execution" not in src,
           "scanner does not import execution layer")


def test_14_compare_missing_exchange():
    _section("14. comparison detects missing exchange (gate)")
    opp = {"base": "BTC", "long_exchange": "gate", "short_exchange": "binance",
           "gross_apr": 100, "funding_spread_8h": 0.001,
           "normalized_symbol": "BTCUSDT", "both_executable": False}
    c = F.compare_to_internal(opp, internal_opps={}, internal_rejects={})
    _check(c["verdict"].startswith("missing_exchange") and "gate" in c["verdict"],
           f"missing_exchange:gate ({c['verdict']})")


def test_15_compare_symbol_mapping():
    _section("15. comparison detects symbol_mapping_or_no_recent_data")
    opp = {"base": "RAREBASE", "long_exchange": "binance",
           "short_exchange": "bybit", "gross_apr": 200,
           "funding_spread_8h": 0.002, "normalized_symbol": "RAREBASEUSDT",
           "both_executable": True}
    c = F.compare_to_internal(opp, internal_opps={}, internal_rejects={})
    _check(c["verdict"] == "symbol_mapping_or_no_recent_data",
           f"verdict for unseen base ({c['verdict']})")


def test_16_regime_summary():
    _section("16. regime summary builds")
    opps = [
        {"base": "A", "gross_apr": 250, "funding_spread_8h": 0.0025,
         "both_executable": True, "long_exchange": "binance",
         "short_exchange": "bybit"},
        {"base": "B", "gross_apr": 50, "funding_spread_8h": 0.0005,
         "both_executable": False, "long_exchange": "gate",
         "short_exchange": "binance"},
    ]
    s = F.regime_summary(opps)
    _check(s["total_opportunities"] == 2, "total=2")
    _check(s["gross_apr_gt_200pct"] == 1, "1 opp >200% APR")
    _check(s["executable_clean"] == 1, "1 executable clean >100%")
    _check(s["unique_symbols"] == 2, "2 unique symbols")


def test_17_api_failure_no_crash():
    _section("17. API failure does not crash")
    orig = F._http_json
    F._http_json = lambda url: (500, None, "http_500")
    try:
        recs, err = F.fetch_binance()
        _check(recs == [] and err is not None, f"empty + error (err={err})")
        recs, err = F.fetch_bybit()
        _check(recs == [] and err is not None, "bybit graceful")
        recs, err = F.fetch_gate()
        _check(recs == [] and err is not None, "gate graceful")
    finally:
        F._http_json = orig


def main() -> int:
    for t in (
        test_1_binance_parser, test_2_bybit_parser, test_3_okx_deferred_documented,
        test_4_gate_parser_seconds_interval, test_5_interval_normalization,
        test_6_symbol_normalization, test_7_1000pepe_distinct,
        test_8_opportunity_builder, test_9_gross_apr_calc,
        test_10_missing_exchange_graceful, test_11_data_only_gate,
        test_12_active_trading_false, test_13_no_executor_calls,
        test_14_compare_missing_exchange, test_15_compare_symbol_mapping,
        test_16_regime_summary, test_17_api_failure_no_crash,
    ):
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
