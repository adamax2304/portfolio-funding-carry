"""
Sprint 39 (2026-06-06) — Spread-based builder DIAGNOSTIC tests.

20 tests covering eligibility gates, edge logic, isolation from trading,
and summary metrics.

Run:
    cd "C:\\Users\\adama\\Documents\\zeph ai projects\\funding_carry"
    py -3 tests/test_sprint39_spread_based_builder.py
"""
from __future__ import annotations

import json
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
    B.DIAG_FILE = tmp / "diag.jsonl"
    B.SUMMARY_FILE = tmp / "summary.jsonl"


def _mk(base="ARM", le="bybit", se="bitget",
        verdict="depth_ok", long_f8=-0.04, short_f8=-0.016,
        spread8=0.024, ob_bps=5.3, fee_cov=3.91, fees_rt=0.0005,
        slip_l=0.1, slip_s=0.1, pm_bps=2.0, gross_apr=400.0,
        vol=4_000_000, ts=1_000_000.0):
    """Build a shadow record matching the production schema."""
    return {
        "ts": ts, "ts_iso": "2026-06-04T00:00:00.000Z",
        "source": "funding_carry_low_volume_shadow",
        "base": base, "symbol": f"{base}USDT",
        "long_exchange": le, "short_exchange": se,
        "long_funding_8h": long_f8, "short_funding_8h": short_f8,
        "funding_spread_8h": spread8,
        "gross_apr": gross_apr,
        "min_leg_volume_24h_usd": vol, "volume_bucket": "3M-5M",
        "mark_spread_bps": pm_bps * 2, "orderbook_spread_bps": ob_bps,
        "depth_usd_target": 100.0,
        "depth_slip_long_bps": slip_l, "depth_slip_short_bps": slip_s,
        "price_mismatch_bps": pm_bps,
        "fee_coverage_ratio": fee_cov,
        "fees_roundtrip_frac": fees_rt,
        "validation_verdict": verdict,
        "mode": "shadow", "active_trading": False,
    }


def _persistent_route(base, **kwargs):
    """Generate N records of the same route, mostly depth_ok."""
    n = kwargs.pop("n", 12)
    do = kwargs.pop("depth_ok", 10)
    base_kw = {"base": base}
    base_kw.update(kwargs)
    out = []
    for i in range(n):
        v = "depth_ok" if i < do else "spread_too_wide"
        out.append(_mk(verdict=v, ts=1_000_000.0 + i, **base_kw))
    return out


# ──────────────────────────────────────────────────────────────────────────
def test_1_arm_would_build():
    _section("1. ARM-like persistent → would_build")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    rs = _persistent_route("ARM", le="bybit", se="bitget",
                            ob_bps=5.3, fee_cov=3.91, n=12, depth_ok=11,
                            long_f8=-0.04, short_f8=-0.016, spread8=0.024)
    s = B.run_once(low_vol_records=rs, write=False)
    _check(s["would_build"] == 1, f"would_build={s['would_build']}")
    if s["records"]:
        r = s["records"][0]
        _check(r["decision"] == "would_build", f"decision={r['decision']}")
        _check(r["persistence"]["verdict"] == "persistent_depth_ok",
               f"verdict={r['persistence']['verdict']}")
        _check(r["edge"]["net_edge_bps"] > 0,
               f"net_edge_bps={r['edge']['net_edge_bps']}")


def test_2_home_would_build():
    _section("2. HOME-like persistent → would_build (mixed pattern)")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    rs = _persistent_route("HOME", le="bybit", se="bitget",
                            ob_bps=7.8, fee_cov=6.16, n=12, depth_ok=10,
                            long_f8=0.03, short_f8=-0.02,
                            spread8=-0.05)  # short – long negative ⟶ |.| = +
    s = B.run_once(low_vol_records=rs, write=False)
    _check(s["would_build"] == 1, f"would_build={s['would_build']}")
    if s["records"]:
        r = s["records"][0]
        _check(r["funding"]["pattern"] == "mixed_positive_negative",
               f"pattern={r['funding']['pattern']}")


def test_3_aigensyn_would_build():
    _section("3. AIGENSYN-like persistent → would_build")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    rs = _persistent_route("AIGENSYN", le="bybit", se="bitget",
                            ob_bps=7.1, fee_cov=3.65, n=12, depth_ok=10)
    s = B.run_once(low_vol_records=rs, write=False)
    _check(s["would_build"] == 1, f"would_build={s['would_build']}")


def test_4_pundix_unstable_rejected():
    _section("4. PUNDIX unstable_depth → rejected")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    # depth_ok rate 50% → unstable_depth (>=0.3, <0.6)
    rs = _persistent_route("PUNDIX", le="bybit", se="bitget",
                            ob_bps=9.1, fee_cov=3.16, n=12, depth_ok=6)
    s = B.run_once(low_vol_records=rs, write=False)
    _check(s["would_build"] == 0, "0 would_build")
    if s["records"]:
        r = s["records"][0]
        _check(r["reject_reason"] == "unstable_depth",
               f"reject_reason={r['reject_reason']}")


def test_5_sign_spread_too_wide_rejected():
    _section("5. SIGN spread_too_wide-dominated → rejected")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    rs = []
    for i in range(12):
        v = "spread_too_wide" if i < 9 else "depth_ok"
        rs.append(_mk(base="SIGN", verdict=v, ob_bps=18.8,
                       fee_cov=2.08, ts=1_000_000.0 + i))
    s = B.run_once(low_vol_records=rs, write=False)
    _check(s["records"][0]["reject_reason"] == "spread_too_wide",
           f"reject_reason={s['records'][0]['reject_reason']}")


def test_6_fee_edge_disappears_rejected():
    _section("6. fee_edge_disappears verdict → rejected")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    rs = []
    for i in range(12):
        rs.append(_mk(base="STEEM", verdict="fee_coverage_too_low",
                       fee_cov=1.65, ts=1_000_000.0 + i))
    s = B.run_once(low_vol_records=rs, write=False)
    _check(s["records"][0]["reject_reason"] == "fee_coverage_low",
           f"reject_reason={s['records'][0]['reject_reason']}")


def test_7_price_mismatch_rejected():
    _section("7. price_mismatch-dominated → rejected")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    rs = []
    for i in range(12):
        rs.append(_mk(base="HOME", verdict="price_mismatch",
                       pm_bps=600.0, ts=1_000_000.0 + i))
    s = B.run_once(low_vol_records=rs, write=False)
    _check(s["records"][0]["reject_reason"] == "price_mismatch",
           f"reject_reason={s['records'][0]['reject_reason']}")


def test_8_insufficient_appearances_rejected():
    _section("8. <5 appearances → insufficient_appearances")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    # 4 records all depth_ok → persistence may say insufficient_n; eligibility says insufficient appearances
    rs = _persistent_route("NEWSYM", n=4, depth_ok=4)
    s = B.run_once(low_vol_records=rs, write=False)
    # n<3 → insufficient_n verdict mapped to not_persistent_depth_ok;
    # 3<=n<5 with depth_ok 100% may classify persistent but appearances<5
    r = s["records"][0]
    _check(r["reject_reason"] in ("insufficient_appearances",
                                    "not_persistent_depth_ok"),
           f"reject_reason={r['reject_reason']}")


def test_9_depth_ok_rate_low_rejected():
    _section("9. depth_ok rate below 60% → rejected (unstable_depth)")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    rs = _persistent_route("XX", n=10, depth_ok=4)  # 40% < 60%
    s = B.run_once(low_vol_records=rs, write=False)
    # Will be classified as unstable_depth and rejected
    _check(s["would_build"] == 0, f"would_build={s['would_build']}")
    r = s["records"][0]
    _check(r["reject_reason"] in ("unstable_depth", "depth_ok_rate_low"),
           f"reject_reason={r['reject_reason']}")


def test_10_fee_coverage_low_rejected():
    _section("10. median fee_coverage < 2x → rejected")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    # persistent_depth_ok (10/12 = 83%) but fee_cov 1.5 < 2.0
    rs = _persistent_route("XX", n=12, depth_ok=10, fee_cov=1.5)
    s = B.run_once(low_vol_records=rs, write=False)
    _check(s["records"][0]["reject_reason"] == "fee_coverage_low",
           f"reject_reason={s['records'][0]['reject_reason']}")


def test_11_ob_spread_above_10bps_rejected():
    _section("11. median orderbook_spread_bps > 10 → rejected")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    rs = _persistent_route("XX", n=12, depth_ok=10, ob_bps=15.0, fee_cov=4.0)
    s = B.run_once(low_vol_records=rs, write=False)
    _check(s["records"][0]["reject_reason"] == "spread_too_wide",
           f"reject_reason={s['records'][0]['reject_reason']}")


def test_12_net_edge_nonpositive_rejected():
    _section("12. net_edge ≤ 0 → rejected")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    # Build persistent depth_ok records but funding_spread_8h tiny so net
    # edge after fees/spread/slippage is non-positive.
    rs = _persistent_route("XX", n=12, depth_ok=10,
                            ob_bps=8.0, fee_cov=4.0,
                            spread8=0.0001,  # 1 bps gross
                            fees_rt=0.0005)  # 5 bps fees → net < 0
    s = B.run_once(low_vol_records=rs, write=False)
    r = s["records"][0]
    _check(r["reject_reason"] == "net_edge_nonpositive",
           f"reject_reason={r['reject_reason']}")


def test_13_both_positive_valid():
    _section("13. both_positive pattern → can be valid if net_edge > 0")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    rs = _persistent_route("BP", n=12, depth_ok=10,
                            long_f8=0.005, short_f8=0.035, spread8=0.030,
                            ob_bps=5.0, fee_cov=4.0)
    s = B.run_once(low_vol_records=rs, write=False)
    r = s["records"][0]
    _check(r["decision"] == "would_build",
           f"decision={r['decision']} reject={r.get('reject_reason')}")
    _check(r["funding"]["pattern"] == "both_positive",
           f"pattern={r['funding']['pattern']}")


def test_14_both_negative_valid():
    _section("14. both_negative pattern → can be valid if net_edge > 0")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    rs = _persistent_route("BN", n=12, depth_ok=10,
                            long_f8=-0.05, short_f8=-0.02, spread8=0.030,
                            ob_bps=5.0, fee_cov=4.0)
    s = B.run_once(low_vol_records=rs, write=False)
    r = s["records"][0]
    _check(r["decision"] == "would_build",
           f"decision={r['decision']} reject={r.get('reject_reason')}")
    _check(r["funding"]["pattern"] == "both_negative",
           f"pattern={r['funding']['pattern']}")


def test_15_mixed_pattern_classified():
    _section("15. mixed funding pattern classification")
    p1 = B._funding_pattern(+0.01, +0.02)
    p2 = B._funding_pattern(-0.01, -0.02)
    p3 = B._funding_pattern(+0.01, -0.02)
    p4 = B._funding_pattern(-0.01, +0.02)
    p5 = B._funding_pattern(0.0, 0.0)
    _check(p1 == "both_positive", f"p1={p1}")
    _check(p2 == "both_negative", f"p2={p2}")
    _check(p3 == "mixed_positive_negative", f"p3={p3}")
    _check(p4 == "mixed_negative_positive", f"p4={p4}")
    _check(p5 == "zero_mixed", f"p5={p5}")


def test_16_output_safety_flags():
    _section("16. output rows have active_trading/paper_trade/"
             "executor_emit=False")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    rs = (_persistent_route("ARM", n=12, depth_ok=11)
          + _persistent_route("BAD", n=12, depth_ok=2))
    s = B.run_once(low_vol_records=rs, write=True)
    rows = [json.loads(l) for l in
            B.DIAG_FILE.read_text(encoding="utf-8").splitlines() if l]
    _check(rows, f"diag rows written ({len(rows)})")
    for r in rows:
        _check(r["active_trading"] is False, f"active_trading=False")
        _check(r["paper_trade"]    is False, f"paper_trade=False")
        _check(r["executor_emit"]  is False, f"executor_emit=False")
        _check(r["mode"] == "diagnostic", f"mode=diagnostic")
    # Summary also
    srows = [json.loads(l) for l in
             B.SUMMARY_FILE.read_text(encoding="utf-8").splitlines() if l]
    _check(srows[-1]["active_trading"] is False, "summary active_trading=False")


def test_17_no_paperbook_import():
    _section("17. no PaperBook import / mutation")
    src = Path(B.__file__).read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", src, flags=re.DOTALL)
    code = re.sub(r"#.*", "", code)
    banned = [
        "from funding_carry.strategy", "import PaperBook",
        "PaperBook(", " paper_book.", "(paper_book.",
        "self.book.",
    ]
    hits = [s for s in banned if s in code]
    _check(not hits, f"no PaperBook references ({hits})")


def test_18_no_executor_calls():
    _section("18. no executor / order / open_position callsites")
    src = Path(B.__file__).read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", src, flags=re.DOTALL)
    code = re.sub(r"#.*", "", code)
    banned = [
        "open_position(", ".open_position(", "place_order(",
        "create_order(", "submit_order(", "cancel_order(",
        "execute_trade(",
    ]
    hits = [s for s in banned if s in code]
    _check(not hits, f"no trading callsites ({hits})")
    banned_imports = [
        "from funding_carry.execution",
        "from funding_carry.strategy.executor",
    ]
    iim = [s for s in banned_imports if s in src]
    _check(not iim, f"no execution imports ({iim})")


def test_19_summary_counts():
    _section("19. summary counts by symbol/pattern/reject reason")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    rs = (_persistent_route("ARM", le="bybit", se="bitget", n=12, depth_ok=11)
          + _persistent_route("ARM", le="bybit", se="mexc",
                                ob_bps=15.0, n=12, depth_ok=10)
          + _persistent_route("PUNDIX", n=12, depth_ok=6))
    s = B.run_once(low_vol_records=rs, write=False)
    _check(s["would_build"] == 1, f"would_build={s['would_build']}")
    _check(s["rejects"] == 2, f"rejects={s['rejects']}")
    _check(s["by_symbol_accepted"].get("ARM", 0) == 1,
           f"by_symbol_accepted={s['by_symbol_accepted']}")
    _check(set(s["by_reject_reason"].keys())
           <= set(B.REJECT_REASONS),
           f"by_reject_reason keys ⊆ REJECT_REASONS")
    _check(s["by_reject_reason"].get("spread_too_wide", 0) == 1,
           f"ARM->mexc rejected with spread_too_wide: "
           f"{s['by_reject_reason']}")
    _check(s["by_reject_reason"].get("unstable_depth", 0) == 1,
           f"PUNDIX rejected with unstable_depth")


def test_20_missing_files_safe():
    _section("20. missing input files handled safely")
    tmp = Path(tempfile.mkdtemp()); _redir(tmp)
    # Repoint input file to nonexistent path
    B.LOW_VOL_FILE = tmp / "absent.jsonl"
    s = B.run_once(write=False)
    _check(s["candidates"] == 0, f"candidates=0 ({s['candidates']})")
    _check(s["would_build"] == 0, f"would_build=0")
    _check(s["errors"] == 0, f"errors={s['errors']} (no error on missing)")


# ──────────────────────────────────────────────────────────────────────────
TESTS = [
    test_1_arm_would_build,
    test_2_home_would_build,
    test_3_aigensyn_would_build,
    test_4_pundix_unstable_rejected,
    test_5_sign_spread_too_wide_rejected,
    test_6_fee_edge_disappears_rejected,
    test_7_price_mismatch_rejected,
    test_8_insufficient_appearances_rejected,
    test_9_depth_ok_rate_low_rejected,
    test_10_fee_coverage_low_rejected,
    test_11_ob_spread_above_10bps_rejected,
    test_12_net_edge_nonpositive_rejected,
    test_13_both_positive_valid,
    test_14_both_negative_valid,
    test_15_mixed_pattern_classified,
    test_16_output_safety_flags,
    test_17_no_paperbook_import,
    test_18_no_executor_calls,
    test_19_summary_counts,
    test_20_missing_files_safe,
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
