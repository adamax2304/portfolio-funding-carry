"""
Sprint 19 (2026-05-31) — CoinGlass scanner tests.

  1.  API response parser maps fields correctly.
  2.  Missing fields handled gracefully.
  3.  No-API-key path does not crash.
  4.  HTML/WebFetch failure does not crash.
  5.  Normalizer stores raw record.
  6.  Scanner writes active_trading=False.
  7.  Scanner never calls order/open_position.
  8.  Comparison detects matched opportunity.
  9.  Comparison detects missing exchange.
  10. Comparison detects symbol-mapping issue.
  11. Exchange universe table builds.
  12. Top opportunity validator handles orderbook failure.

Run:
    cd "C:\\Users\\adama\\Documents\\zeph ai projects\\funding_carry"
    py -3 tests/test_sprint19_coinglass.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_ROOT.parent))

from funding_carry.external import coinglass_scanner as C  # noqa: E402

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


# ─────────────────────────────────────────────────────────────────────────
def test_1_parser_maps_fields():
    _section("1. parser maps CoinGlass fields correctly")
    raw = {
        "symbol": "BTCUSDT", "baseCoin": "BTC",
        "longExchange": "Binance", "shortExchange": "Bybit",
        "longFundingRate": 0.0001, "shortFundingRate": 0.0008,
        "apr": 36.5, "annualRevenue": 365.0,
        "threeDayRevenue": 3.0, "pnl": 12.5,
    }
    rec = C.normalize_record(raw, rank=1)
    _check(rec["normalized_ok"] is True, "normalized_ok=True")
    _check(rec["base"] == "BTC", f"base=BTC (got {rec['base']})")
    _check(rec["long_exchange"] == "binance", "long_exchange lowercased")
    _check(rec["short_exchange"] == "bybit", "short_exchange lowercased")
    _check(rec["funding_rate_long"] == 0.0001, "funding rate long preserved")
    _check(abs(rec["apr"] - 36.5) < 1e-9, "apr preserved")
    _check(rec["coinglass_rank"] == 1, "rank preserved")
    _check(rec["mode"] == "shadow", "mode=shadow")
    _check(rec["active_trading"] is False, "active_trading=False")


def test_2_missing_fields_graceful():
    _section("2. missing fields handled gracefully")
    rec = C.normalize_record({"symbol": "FOOUSDT"})
    _check(rec["normalized_ok"] is True, "normalized_ok=True even with sparse input")
    _check(rec["funding_rate_long"] is None, "missing fields -> None")
    _check(rec["apr"] is None, "missing apr -> None")
    _check(rec["base"] == "FOO", "base derived from symbol")
    # totally malformed input must not raise
    rec2 = C.normalize_record({})
    _check(rec2["normalized_ok"] is True, "empty dict ok")
    rec3 = C.normalize_record(["not", "a", "dict"])
    _check(rec3["normalized_ok"] is False, "non-dict -> normalized_ok=False")


def test_3_no_api_key_clean():
    _section("3. no-API-key path is clean (no crash)")
    # Wipe env keys
    saved = {k: os.environ.pop(k, None) for k in
             ("COINGLASS_API_KEY", "CG_API_KEY", "COINGLASS_KEY")}
    try:
        # scan_once with write=False so we don't touch state files
        summary = C.scan_once(write=False)
        _check(summary["api_key_present"] is False, "api_key_present=False")
        _check("fetched" in summary, "summary has 'fetched' key")
        _check(summary["records"] == 0, "0 records when unauthenticated")
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_4_html_fallback_no_crash():
    _section("4. HTML fallback failure does not crash")
    # Monkeypatch _fetch_with_httpx to simulate connection error
    orig = C._fetch_with_httpx
    C._fetch_with_httpx = lambda url, params=None, headers=None: (None, None, "ConnectionError")
    try:
        r = C.fetch_html_page()
        _check(r.ok is False, "ok=False on network error")
        _check(r.error is not None, "error message captured")
    finally:
        C._fetch_with_httpx = orig


def test_5_normalizer_stores_raw():
    _section("5. normalizer preserves raw record")
    raw = {"symbol": "XYZ", "weirdField": [1, 2, 3], "nested": {"a": 1}}
    rec = C.normalize_record(raw)
    _check(rec["raw"] == raw, "raw payload preserved verbatim")


def test_6_scanner_writes_active_trading_false():
    _section("6. scanner writes active_trading=False")
    tmp = Path(tempfile.mkdtemp())
    orig_shadow, orig_compare = C.SHADOW_FILE, C.COMPARE_FILE
    C.SHADOW_FILE = tmp / "shadow.jsonl"
    C.COMPARE_FILE = tmp / "compare.jsonl"
    # Monkeypatch fetch_coinglass to inject synthetic data
    orig_fetch = C.fetch_coinglass
    fake_records = [
        {"symbol": "BTCUSDT", "baseCoin": "BTC", "longExchange": "Binance",
         "shortExchange": "Bybit", "apr": 50.0},
    ]
    C.fetch_coinglass = lambda **kw: C.FetchResult("api_v4", True, 200, fake_records)
    # also wipe env keys to keep test deterministic
    saved = {k: os.environ.pop(k, None) for k in
             ("COINGLASS_API_KEY", "CG_API_KEY", "COINGLASS_KEY")}
    try:
        summary = C.scan_once(write=True)
        lines = C.SHADOW_FILE.read_text(encoding="utf-8").strip().splitlines()
        _check(len(lines) == 1, f"1 shadow record written ({len(lines)})")
        rec = json.loads(lines[0])
        _check(rec["active_trading"] is False, "active_trading=False")
        _check(rec["mode"] == "shadow", "mode=shadow")
        _check(summary["fetched"] is True, "summary.fetched=True")
    finally:
        C.SHADOW_FILE, C.COMPARE_FILE = orig_shadow, orig_compare
        C.fetch_coinglass = orig_fetch
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_7_no_order_calls():
    _section("7. scanner source contains no trading calls")
    src = Path(C.__file__).read_text(encoding="utf-8")
    # Strip docstrings/comments before scanning so we only catch real code.
    import re as _re
    code = _re.sub(r'""".*?"""', "", src, flags=_re.DOTALL)
    code = _re.sub(r"'''.*?'''", "", code, flags=_re.DOTALL)
    code = _re.sub(r"#.*", "", code)
    # Look for CALL patterns, not bare substrings (docstrings list them as
    # negations).  A call pattern is "<name>(" or ".<name>(".
    banned = ["open_position(", ".open_position(", "place_order(",
              ".place_order(", "create_order(", ".create_order(",
              "submit_order(", "ExecutorAgent(", "paper_book.open",
              "execution.paper.open", "bus.publish("]
    hits = [b for b in banned if b in code]
    _check(not hits, f"no trading call sites in scanner code (hits: {hits})")
    # And the executor module is NOT imported.
    _check("from funding_carry.execution" not in src
           and "import execution" not in src,
           "scanner does not import execution layer")


def test_8_comparison_matched():
    _section("8. comparison detects matched opportunity")
    opps = {"BTC": {"base": "BTC", "long_ex": "binance", "short_ex": "bybit",
                    "gross_apr": 48.0, "ts": 1.0, "price_diff_bps": 3.5}}
    rejects = {}
    cg = {"base": "BTC", "long_exchange": "binance", "short_exchange": "bybit",
          "apr": 50.0}
    comp = C.compare_to_internal(cg, opps, rejects)
    _check(comp["verdict"] == "matched", f"matched (got {comp['verdict']})")
    _check(comp["executable"] is True, "executable=True for binance/bybit")
    _check(comp["spread_bps"] == 3.5, "spread copied from internal")


def test_9_comparison_missing_exchange():
    _section("9. comparison detects missing exchange")
    cg = {"base": "FOO", "long_exchange": "okx", "short_exchange": "binance",
          "apr": 100.0}
    comp = C.compare_to_internal(cg, opps={}, rejects={})
    _check(comp["verdict"].startswith("missing_exchange"),
           f"missing_exchange verdict (got {comp['verdict']})")
    _check("okx" in comp["verdict"], "okx flagged as missing")
    _check(comp["executable"] is False, "executable=False")


def test_10_comparison_symbol_mapping():
    _section("10. comparison detects no recent internal data")
    cg = {"base": "RAREBASE", "long_exchange": "binance",
          "short_exchange": "bybit", "apr": 80.0}
    comp = C.compare_to_internal(cg, opps={}, rejects={})
    _check(comp["verdict"] == "symbol_mapping_or_no_recent_data",
           f"symbol mapping verdict (got {comp['verdict']})")
    _check(comp["internal_seen"] is False, "internal_seen=False")


def test_11_exchange_universe():
    _section("11. exchange universe sets are well-formed")
    _check(C._INTERNAL_VENUES == {"binance","bybit","bitget","mexc","hyperliquid","aster"},
           f"internal venues match ({sorted(C._INTERNAL_VENUES)})")
    _check(C._EXECUTABLE_VENUES.issubset(C._INTERNAL_VENUES),
           "executable subset of internal")
    _check("binance" in C._EXECUTABLE_VENUES and "bybit" in C._EXECUTABLE_VENUES,
           "binance + bybit are executable")
    _check("hyperliquid" not in C._EXECUTABLE_VENUES,
           "hyperliquid NOT executable (matches Sprint 18 finding)")


def test_12_api_error_envelope_detected():
    _section("12. CoinGlass error envelope (success:false) detected")
    # The real v2 endpoint returns {"code":"30001","msg":"API key missing","success":false}
    err = C._check_api_error({"code": "30001", "msg": "API key missing.",
                              "success": False})
    _check(err and "api_key_required" in err, f"api_key_required label ({err})")
    err2 = C._check_api_error({"code": "0", "data": []})
    _check(err2 is None, "success envelope -> None")
    err3 = C._check_api_error({"code": "401", "msg": "API key missing."})
    _check(err3 and "api_key_required" in err3, "v4 401 also recognized")


def main() -> int:
    for t in (
        test_1_parser_maps_fields, test_2_missing_fields_graceful,
        test_3_no_api_key_clean, test_4_html_fallback_no_crash,
        test_5_normalizer_stores_raw, test_6_scanner_writes_active_trading_false,
        test_7_no_order_calls, test_8_comparison_matched,
        test_9_comparison_missing_exchange, test_10_comparison_symbol_mapping,
        test_11_exchange_universe, test_12_api_error_envelope_detected,
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
