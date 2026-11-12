"""
Sprint 53 (2026-06-10) — Funding-side asset_kind classifier tests.

Verifies the shared funding equity-contamination filter blocks all
Sprint 50-52 confirmed leaks (SOXL/SKHYNIX/AAOI/IREN/ONDS/QNTX/URNM/NVDA)
while preserving real crypto.

Run:
    cd "C:\\Users\\adama\\Documents\\zeph ai projects\\funding_carry"
    py -3 tests/test_sprint53_asset_kind.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_FC_ROOT = _HERE.parent
for p in (str(_FC_ROOT.parent), str(_FC_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from asset_kind import (  # noqa: E402
    classify_asset_kind, is_execution_eligible,
    audit_trades, EQUITY_TICKER_BLOCKLIST, AMBIGUOUS_TICKERS,
)

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


# ──────────────────────────────────────────────────────────────────────────

def test_1_sprint50_52_leaks_blocked():
    _section("T1. All Sprint 50-52 confirmed leak tickers → equity_perp")
    for sym in ("SOXL", "SKHYNIX", "NVDA", "AAOI", "IREN", "ONDS",
                "QNTX", "URNM"):
        kind, reason = classify_asset_kind(sym)
        _check(kind == "equity_perp", f"{sym} → {kind} ({reason})")


def test_2_us_equity_blocklist():
    _section("T2. Major US equity tickers → equity_perp")
    for sym in ("TSLA", "AAPL", "MSFT", "META", "GOOGL", "AMZN",
                "INTC", "AMD", "AVGO"):
        kind, _ = classify_asset_kind(sym)
        _check(kind == "equity_perp", f"{sym} → {kind}")


def test_3_etfs_blocked():
    _section("T3. ETFs → equity_perp")
    for sym in ("SPY", "QQQ", "IWM", "URNM", "URA", "URNJ",
                "XLE", "XLF", "ARKK", "BITO", "GBTC"):
        kind, _ = classify_asset_kind(sym)
        _check(kind == "equity_perp", f"{sym} → {kind}")


def test_4_msci_country_etfs():
    _section("T4. MSCI country ETFs → equity_perp (EWT excluded for crypto)")
    for sym in ("EWJ", "EWG", "EWZ", "EWY", "EWU", "EWA"):
        kind, _ = classify_asset_kind(sym)
        _check(kind == "equity_perp", f"{sym} → {kind}")
    # EWT in AMBIGUOUS_TICKERS — no equity context → crypto
    kind, _ = classify_asset_kind("EWT")
    _check(kind == "crypto", f"EWT (no context) → {kind}")


def test_5_crypto_preserved():
    _section("T5. Real crypto tokens preserved")
    for sym in ("BTC", "ETH", "SOL", "AAVE", "EDEN", "ZORA", "HOME",
                "SIGN", "ARM", "CTR", "PHAROS", "ID", "VTHO", "HIVE"):
        kind, _ = classify_asset_kind(sym)
        _check(kind == "crypto", f"{sym} → {kind}")


def test_6_pre_ipo_context():
    _section("T6. Pre-IPO context → pre_ipo")
    kind, _ = classify_asset_kind(
        "FOO", "Binance Will Launch FOOUSDT Pre-IPO Perpetual Contract")
    _check(kind == "pre_ipo", f"context-driven pre_ipo: {kind}")


def test_7_etf_context_only():
    _section("T7. ETF context → etf_perp for non-blocklisted symbol")
    kind, _ = classify_asset_kind(
        "FOO", "Some ETF perpetual contract launching")
    _check(kind == "etf_perp", f"context-driven etf_perp: {kind}")


def test_8_suffix_pattern():
    _section("T8. *STOCK / *SHARES suffix → equity_perp")
    for sym in ("HPESTOCK", "AMDSTOCK", "NVDASHARES"):
        kind, _ = classify_asset_kind(sym)
        _check(kind == "equity_perp", f"{sym} → {kind}")


def test_9_unknown_safe():
    _section("T9. Empty / malformed → unknown")
    for inp in ("", None, "   "):
        kind, _ = classify_asset_kind(inp or "")
        _check(kind == "unknown", f"input {inp!r} → {kind}")


def test_10_eligibility_predicate():
    _section("T10. is_execution_eligible")
    for sym, expected in (("BTC", True), ("ETH", True),
                            ("SOXL", False), ("SKHYNIX", False),
                            ("NVDA", False), ("URNM", False),
                            ("AAOI", False), ("CTR", True)):
        ok, info = is_execution_eligible(sym)
        _check(ok == expected,
               f"{sym} eligible={ok} (expected {expected}, "
               f"kind={info['asset_kind']})")


def test_11_no_executor_imports():
    _section("T11. asset_kind.py has no executor / PaperBook imports")
    src = Path("../funding_carry/asset_kind.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", src, flags=re.DOTALL)
    code = re.sub(r"#.*", "", code)
    banned = ["open_position(", "place_order(", "create_order(",
               "submit_order(", "ExecutorAgent(", "PaperBook(",
               "bus.publish("]
    hits = [s for s in banned if s in code]
    _check(not hits, f"no banned callsites ({hits})")
    banned_imp = ["from funding_carry.strategy",
                   "from funding_carry.execution",
                   "from news_trader"]
    # Strip docstrings/comments before scanning import statements
    iim = [s for s in banned_imp if s in code]
    _check(not iim, f"no banned imports ({iim})")


def test_12_audit_function():
    _section("T12. audit_trades summary on fixture data")
    fixture = [
        {"base": "BTC",  "realized_pnl_usd": +5.0, "funding_collected_usd": +1.0},
        {"base": "ETH",  "realized_pnl_usd": -2.0, "funding_collected_usd": +0.5},
        {"base": "SOXL", "realized_pnl_usd": -11.12, "funding_collected_usd": 0,
         "exit_reason": "basis_drift_54bps", "ts_iso": "2026-06-08T..."},
        {"base": "SKHYNIX","realized_pnl_usd": -14.77, "funding_collected_usd": 0,
         "exit_reason": "basis_drift_41bps", "ts_iso": "2026-06-09T..."},
    ]
    a = audit_trades(fixture)
    _check(a["total_trades"] == 4, f"total={a['total_trades']}")
    _check(a["by_kind"]["crypto"]["count"] == 2,
           f"crypto count={a['by_kind']['crypto']['count']}")
    _check(a["by_kind"]["equity_perp"]["count"] == 2,
           f"equity count={a['by_kind']['equity_perp']['count']}")
    _check(a["contaminated_count"] == 2,
           f"contaminated={a['contaminated_count']}")
    # Sum check
    _check(abs(a["by_kind"]["equity_perp"]["pnl_usd"] - (-25.89)) < 0.01,
           f"equity pnl ≈ -$25.89: {a['by_kind']['equity_perp']['pnl_usd']}")


def test_13_real_fc_trades_audit():
    _section("T13. Audit the real FC trades.jsonl file")
    trades_file = _FC_ROOT / "state" / "trades.jsonl"
    if not trades_file.exists():
        _check(False, "trades.jsonl not found"); return
    trades = [json.loads(l) for l in
              trades_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    a = audit_trades(trades)
    _check(a["total_trades"] >= 25,
           f"FC has ≥25 trades (audited {a['total_trades']})")
    # Contamination should include SOXL + SKHYNIX (Sprint 50-52 trades)
    contaminated_symbols = {c["base"] for c in a["contaminated"]}
    _check("SOXL" in contaminated_symbols,
           f"SOXL in contaminated: {contaminated_symbols}")
    _check("SKHYNIX" in contaminated_symbols,
           f"SKHYNIX in contaminated: {contaminated_symbols}")
    # PnL on equity should be negative
    eq_pnl = a["by_kind"].get("equity_perp", {}).get("pnl_usd", 0)
    _check(eq_pnl < 0, f"equity perp PnL is negative: ${eq_pnl:.2f}")


def test_14_blocklist_includes_all_required():
    _section("T14. Sprint 53 §7 required blocklist tickers present")
    required = ("SOXL", "SKHYNIX", "EWY", "INTC", "TSM", "AAOI",
                 "IREN", "ONDS", "URNM", "QNTX", "DELL", "IBM",
                 "NVDA", "TSLA", "AAPL", "MSFT", "AMAT", "NOKIA",
                 "SAMSUNG", "HYUNDAI", "IWM", "SPY", "QQQ")
    missing = [s for s in required if s not in EQUITY_TICKER_BLOCKLIST]
    _check(not missing, f"all required in blocklist (missing: {missing})")


def test_15_ambiguous_ticker_handling():
    _section("T15. Ambiguous tickers respect context")
    # ARM as crypto context (default)
    kind, _ = classify_asset_kind("ARM",
                                    "New listing: ARMUSDT Perpetual Contract")
    _check(kind == "crypto", f"ARM no-context → {kind}")
    # ARM with equity context
    kind, _ = classify_asset_kind("ARM",
                                    "Arm Holdings ARM Stock Perpetual on Bybit")
    _check(kind == "equity_perp", f"ARM with stock context → {kind}")
    # EWT (Energy Web Token vs MSCI Taiwan ETF)
    kind, _ = classify_asset_kind("EWT")
    _check(kind == "crypto", f"EWT default → {kind}")


# ──────────────────────────────────────────────────────────────────────────
TESTS = [
    test_1_sprint50_52_leaks_blocked,
    test_2_us_equity_blocklist,
    test_3_etfs_blocked,
    test_4_msci_country_etfs,
    test_5_crypto_preserved,
    test_6_pre_ipo_context,
    test_7_etf_context_only,
    test_8_suffix_pattern,
    test_9_unknown_safe,
    test_10_eligibility_predicate,
    test_11_no_executor_imports,
    test_12_audit_function,
    test_13_real_fc_trades_audit,
    test_14_blocklist_includes_all_required,
    test_15_ambiguous_ticker_handling,
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
