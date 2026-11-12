"""
Sprint 56 (2026-06-11) — Spread builder → aggressive paper data wiring.

Sprint 55 showed 0 opens with 100% rejects, but 84% of rejects were
`volume_too_low` — actually an artifact of passing 0 instead of the
real value. Sprint 56:
  * spread_builder bridge now reads `min_leg_volume_24h_usd`,
    `validation_verdict`, `depth_slip_*_bps`, `mark_spread_bps`
    from the shadow record;
  * aggressive_paper distinguishes missing vs low for volume / depth;
  * delegated_depth_ok sentinel accepts upstream depth validation;
  * missing bid/ask produces explicit `missing_orderbook` reject
    (instead of being swallowed by quote-coerce);
  * every event records `data_quality.missing_fields` /
    `data_quality.derived_fields`.

Sprint 57 will fetch real bid/ask snapshots to actually open positions.

Run:
    cd "C:\\Users\\adama\\Documents\\zeph ai projects\\funding_carry"
    py -3 tests/test_sprint56_data_wiring.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_FC_ROOT = _HERE.parent
for p in (str(_FC_ROOT.parent), str(_FC_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import funding_carry.aggressive_paper as AP  # noqa: E402

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
# Fixtures / helpers

def _redir(tmp: Path) -> None:
    AP.LEDGER_FILE   = tmp / "aggressive_paper_book.json"
    AP.EVENTS_FILE   = tmp / "aggressive_paper_events.jsonl"
    AP.TRADES_FILE   = tmp / "aggressive_paper_trades.jsonl"
    AP.SUMMARY_FILE  = tmp / "aggressive_paper_summary.jsonl"


def _enable() -> None:
    os.environ[AP.ENV_FLAG_NAME] = "1"
    AP.reset_state_for_tests()


def _disable() -> None:
    os.environ.pop(AP.ENV_FLAG_NAME, None)


def _read_events(tmp: Path) -> list[dict]:
    p = tmp / "aggressive_paper_events.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def _base_cand(**over) -> dict:
    """Crypto candidate with all real fields present."""
    import time
    _now = time.time()
    c = {
        "symbol":              "EDEN",
        "volume_24h_usd":      5_000_000.0,
        "fee_coverage":        5.0,
        "net_edge_bps":        25.0,
        "ob_spread_bps":       8.0,
        "depth_usd_100bps":    50_000.0,
        "long_funding_8h":     -0.01,
        "short_funding_8h":    +0.01,
        "persistence_verdict": "persistent_depth_ok",
        "price_mismatch_bps":  2.0,
        "notional_usd":        100.0,
        "long_venue":          "bybit",
        "short_venue":         "binance",
        "long_quote":          {"venue": "bybit", "best_bid": 0.5,
                                "best_ask": 0.5005, "bid_size": 5000,
                                "ask_size": 5000, "snapshot_ts": _now},
        "short_quote":         {"venue": "binance", "best_bid": 0.5002,
                                "best_ask": 0.5007, "bid_size": 5000,
                                "ask_size": 5000, "snapshot_ts": _now},
        "data_quality": {"missing_fields": [], "derived_fields": []},
    }
    c.update(over)
    return c


# ──────────────────────────────────────────────────────────────────────────

def test_1_missing_volume_distinct():
    _section("T1. None volume → missing_volume_24h (not volume_too_low)")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    c = _base_cand(volume_24h_usd=None)
    r = AP.evaluate_candidate(c)
    _check(r["action"] == "rejected", f"action={r['action']}")
    _check(r["reason"] == "missing_volume_24h", f"reason={r['reason']}")
    _disable()


def test_2_true_low_volume():
    _section("T2. Real low volume → volume_too_low")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    c = _base_cand(volume_24h_usd=100.0)  # below 500k floor
    r = AP.evaluate_candidate(c)
    _check(r["action"] == "rejected", f"action={r['action']}")
    _check(r["reason"] == "volume_too_low", f"reason={r['reason']}")
    _disable()


def test_3_delegated_depth_ok_passes_depth():
    _section("T3. depth_usd_100bps='delegated_depth_ok' → depth gate passes")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    c = _base_cand(depth_usd_100bps="delegated_depth_ok")
    r = AP.evaluate_candidate(c)
    # Falls through to atomic sim — opens or rejects on simulator detail
    _check(r["reason"] not in ("depth_too_low", "missing_depth"),
           f"reason={r['reason']} (depth gate should not block)")
    _disable()


def test_4_missing_depth_distinct():
    _section("T4. depth=None → missing_depth (not depth_too_low)")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    c = _base_cand(depth_usd_100bps=None)
    r = AP.evaluate_candidate(c)
    _check(r["action"] == "rejected", f"action={r['action']}")
    _check(r["reason"] == "missing_depth", f"reason={r['reason']}")
    _disable()


def test_5_true_low_depth():
    _section("T5. depth below MIN → depth_too_low")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    c = _base_cand(depth_usd_100bps=500.0)  # below 15k floor
    r = AP.evaluate_candidate(c)
    _check(r["action"] == "rejected", f"action={r['action']}")
    _check(r["reason"] == "depth_too_low", f"reason={r['reason']}")
    _disable()


def test_6_missing_orderbook_distinct():
    _section("T6. long_quote=None → missing_orderbook (not coerce error)")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    c = _base_cand(long_quote=None)
    r = AP.evaluate_candidate(c)
    _check(r["action"] == "rejected", f"action={r['action']}")
    _check(r["reason"] == "missing_orderbook", f"reason={r['reason']}")
    _disable()


def test_7_short_quote_missing():
    _section("T7. short_quote=None → missing_orderbook")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    c = _base_cand(short_quote=None)
    r = AP.evaluate_candidate(c)
    _check(r["reason"] == "missing_orderbook", f"reason={r['reason']}")
    _disable()


def test_8_equity_rejected_before_volume():
    _section("T8. Equity candidate rejected before volume check")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    # Even with missing volume, equity must reject as asset_kind
    c = _base_cand(symbol="SOXL", volume_24h_usd=None)
    r = AP.evaluate_candidate(c)
    _check(r["action"] == "rejected", f"action={r['action']}")
    _check("asset_kind" in r["reason"], f"reason={r['reason']}")
    _check(r["reason"] != "missing_volume_24h",
           "did not even reach volume check")
    _disable()


def test_9_crypto_full_data_opens():
    _section("T9. Crypto candidate with full data → opens")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    c = _base_cand()
    r = AP.evaluate_candidate(c)
    _check(r["action"] == "opened",
           f"action={r['action']} reason={r.get('reason')}")
    if r["action"] == "opened":
        _check(r["position"]["asset_kind"] == "crypto",
               f"asset_kind={r['position']['asset_kind']}")
        _check(r["position"]["leverage"] <= AP.MAX_LEVERAGE,
               f"lev={r['position']['leverage']}")
    _disable()


def test_10_data_quality_logged():
    _section("T10. data_quality.missing_fields propagated to events")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    c = _base_cand(
        symbol="ZORA",
        long_quote=None,
        data_quality={
            "missing_fields": ["long_bid", "long_ask", "short_bid",
                                "short_ask", "long_orderbook_ts",
                                "short_orderbook_ts", "quote_skew_ms"],
            "derived_fields": ["depth_usd_100bps"],
        },
    )
    r = AP.evaluate_candidate(c)
    events = _read_events(tmp)
    _check(len(events) >= 1, f"events written: {len(events)}")
    last = events[-1]
    _check(last.get("data_quality", {}).get("missing_fields"),
           f"data_quality.missing_fields = "
           f"{last.get('data_quality', {}).get('missing_fields')}")
    _check("depth_usd_100bps" in last.get("data_quality", {})
            .get("derived_fields", []),
           "derived_fields contains depth_usd_100bps")
    _disable()


def test_11_no_executor_calls():
    _section("T11. evaluate_candidate makes no executor / live API calls")
    src = (_FC_ROOT / "aggressive_paper.py").read_text(encoding="utf-8")
    code = re.sub(r'"""[\s\S]*?"""', "", src)
    code = re.sub(r"#[^\n]*", "", code)
    banned = ("executor.publish(", "executor.open(", "executor.send_order(",
               "open_position(", "place_order(", "create_order(",
               "submit_order(", "signal_bus.publish(", "bus.publish(",
               "send_live(", "client.create_order(")
    hits = [b for b in banned if b in code]
    _check(not hits, f"no banned callsites (hits={hits})")


def test_12_main_paper_book_isolated():
    _section("T12. aggressive paper does NOT touch main paper_book.json")
    src = (_FC_ROOT / "aggressive_paper.py").read_text(encoding="utf-8")
    # Strip docstring / comment references when scanning code.
    code = re.sub(r'"""[\s\S]*?"""', "", src)
    code = re.sub(r"#[^\n]*", "", code)
    _check("from .execution import PaperBook" not in src,
           "no PaperBook import in aggressive_paper.py")
    # paper_book.json (the conservative book) must not appear in CODE,
    # though it may be referenced in docstring/comments.
    _check('"paper_book.json"' not in code
            and "'paper_book.json'" not in code,
           "no paper_book.json reference in code")
    _check('LEDGER_FILE   = _STATE / "aggressive_paper_book.json"' in src,
           "uses isolated aggressive_paper_book.json")


def test_13_malformed_candidate_safe():
    _section("T13. Malformed candidate handled safely")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    # Empty / None / weird types should never raise
    for bad in ({}, {"symbol": None},
                {"symbol": "BTC", "volume_24h_usd": "not_a_number"}):
        try:
            r = AP.evaluate_candidate(bad)
            _check(r["action"] in ("rejected", "error", "disabled"),
                   f"malformed -> {r.get('action')}")
        except Exception as e:
            _check(False, f"raised: {type(e).__name__}: {e}")
    _disable()


def test_14_bridge_passes_volume_field():
    _section("T14. spread_builder bridge maps min_leg_volume_24h_usd → "
              "volume_24h_usd")
    src = (_FC_ROOT / "external"
           / "spread_based_builder.py").read_text(encoding="utf-8")
    _check('latest.get("min_leg_volume_24h_usd")' in src,
           "bridge reads min_leg_volume_24h_usd")
    _check('"volume_24h_usd":' in src,
           "bridge passes volume_24h_usd key to aggressive paper")


def test_15_bridge_passes_depth_proxy():
    _section("T15. bridge passes 'delegated_depth_ok' for depth_ok records")
    src = (_FC_ROOT / "external"
           / "spread_based_builder.py").read_text(encoding="utf-8")
    _check('"delegated_depth_ok"' in src,
           "bridge uses delegated_depth_ok sentinel")
    _check('validation_verdict' in src,
           "bridge reads validation_verdict")


def test_16_bridge_marks_missing_bid_ask():
    _section("T16. bridge marks bid/ask as missing_fields for Sprint 57")
    src = (_FC_ROOT / "external"
           / "spread_based_builder.py").read_text(encoding="utf-8")
    for f in ("long_bid", "long_ask", "short_bid", "short_ask",
                "long_orderbook_ts", "short_orderbook_ts", "quote_skew_ms"):
        _check(f in src, f"bridge marks {f!r} as missing")


def test_17_funding_data_still_required():
    _section("T17. missing_funding_data still fires when funding absent")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    c = _base_cand(long_funding_8h=None, short_funding_8h=None)
    r = AP.evaluate_candidate(c)
    _check(r["reason"] == "missing_funding_data",
           f"reason={r['reason']}")
    _disable()


def test_18_partial_funding_data():
    _section("T18. one funding side missing → missing_funding_data")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    c = _base_cand(short_funding_8h=None)
    r = AP.evaluate_candidate(c)
    _check(r["reason"] == "missing_funding_data",
           f"reason={r['reason']}")
    _disable()


def test_19_conservative_fence_independent():
    _section("T19. Conservative FC safety fence still in main.py")
    src = (_FC_ROOT / "main.py").read_text(encoding="utf-8")
    _check("asset_kind_not_execution_eligible" in src,
           "Sprint 55 fence reject reason still present")
    _check("from .asset_kind import is_execution_eligible" in src,
           "main.py imports shared classifier")


def test_20_event_has_paper_safety_fields():
    _section("T20. Every event has paper_trade=true, executor_emit=false")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp); _enable()
    # Generate a mix of events
    for c in (_base_cand(symbol="SOXL"),
                _base_cand(symbol="ZORA", volume_24h_usd=None),
                _base_cand(symbol="EDEN", long_quote=None)):
        AP.evaluate_candidate(c)
    events = _read_events(tmp)
    _check(events, f"n events: {len(events)}")
    _check(all(e.get("paper_trade") is True for e in events),
           "all paper_trade=True")
    _check(all(e.get("live_trading") is False for e in events),
           "all live_trading=False")
    _check(all(e.get("executor_emit") is False for e in events),
           "all executor_emit=False")
    _check(all(e.get("lane") == "funding_aggressive_paper" for e in events),
           "all lane=funding_aggressive_paper")
    _disable()


# ──────────────────────────────────────────────────────────────────────────
TESTS = [
    test_1_missing_volume_distinct,
    test_2_true_low_volume,
    test_3_delegated_depth_ok_passes_depth,
    test_4_missing_depth_distinct,
    test_5_true_low_depth,
    test_6_missing_orderbook_distinct,
    test_7_short_quote_missing,
    test_8_equity_rejected_before_volume,
    test_9_crypto_full_data_opens,
    test_10_data_quality_logged,
    test_11_no_executor_calls,
    test_12_main_paper_book_isolated,
    test_13_malformed_candidate_safe,
    test_14_bridge_passes_volume_field,
    test_15_bridge_passes_depth_proxy,
    test_16_bridge_marks_missing_bid_ask,
    test_17_funding_data_still_required,
    test_18_partial_funding_data,
    test_19_conservative_fence_independent,
    test_20_event_has_paper_safety_fields,
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
