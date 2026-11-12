"""
Sprint 55 (2026-06-11) — Conservative FC paper safety fence + shared
asset_kind classifier integration.

Verifies:
  1. funding_carry/main.py _try_enter() blocks equity/ETF/pre-IPO bases
     at the conservative FC paper candidate gate via the shared
     funding_carry/asset_kind.is_execution_eligible.
  2. funding_carry/aggressive_paper.py delegates classify_asset_kind
     to the shared module — no local blocklist drift.
  3. v1.4 leak tickers (SOXL/SKHYNIX/UVXY/EWY/INTC/TSM) are blocked
     from both lanes.
  4. Real crypto bases (BTC/ETH/SOL/EDEN/ZORA) pass through.
  5. No live / executor path is touched.

Run:
    cd "C:\\Users\\adama\\Documents\\zeph ai projects\\funding_carry"
    py -3 tests/test_sprint55_conservative_fc_fence.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

_HERE = Path(__file__).resolve().parent
_FC_ROOT = _HERE.parent
for p in (str(_FC_ROOT.parent), str(_FC_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from funding_carry.asset_kind import (  # noqa: E402
    classify_asset_kind, is_execution_eligible,
    EQUITY_TICKER_BLOCKLIST,
)
import funding_carry.aggressive_paper as agg  # noqa: E402

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

def test_1_v14_leak_tickers_blocked():
    _section("T1. v1.4 leak tickers blocked from both lanes")
    leak = ("SOXL", "SKHYNIX", "UVXY", "VXX", "EWY", "INTC", "TSM",
             "NVDA", "AAOI", "IREN", "ONDS", "QNTX", "URNM",
             "CIEN", "ZM", "DKNG", "OUST", "PL", "EBAY")
    for sym in leak:
        ok, info = is_execution_eligible(sym)
        _check(not ok and info["asset_kind"] == "equity_perp",
               f"{sym} ineligible kind={info['asset_kind']}")


def test_2_crypto_bases_allowed():
    _section("T2. Real crypto bases allowed")
    crypto = ("BTC", "ETH", "SOL", "AAVE", "EDEN", "ZORA", "HOME",
              "SIGN", "STABLE", "WAL", "DRAM", "IRYS", "ARM",
              "JOTCHUA", "CTR", "PHAROS")
    for sym in crypto:
        ok, info = is_execution_eligible(sym)
        _check(ok and info["asset_kind"] == "crypto",
               f"{sym} eligible kind={info['asset_kind']}")


def test_3_aggressive_paper_uses_shared_classifier():
    _section("T3. aggressive_paper.py delegates to shared classifier")
    # Spot-check identity: aggressive_paper.classify_asset_kind must
    # return identical (kind, reason) to the shared module.
    from funding_carry import asset_kind as ak
    for sym in ("SOXL", "BTC", "UVXY", "EDEN"):
        agg_k = agg.classify_asset_kind(sym, "")
        ak_k = ak.classify_asset_kind(sym, "")
        _check(agg_k == ak_k,
               f"{sym}: agg={agg_k} shared={ak_k}")


def test_4_aggressive_paper_blocklist_synced():
    _section("T4. aggressive_paper.EQUITY_TICKER_BLOCKLIST is shared")
    _check(agg.EQUITY_TICKER_BLOCKLIST is EQUITY_TICKER_BLOCKLIST,
           "blocklist is identity-shared with shared module")
    for must in ("UVXY", "VXX", "CIEN", "ZM", "DKNG", "OUST", "PL",
                  "EBAY", "SOXL", "SKHYNIX", "NVDA"):
        _check(must in agg.EQUITY_TICKER_BLOCKLIST,
               f"{must} in shared blocklist")


def test_5_main_py_imports_eligibility():
    _section("T5. funding_carry/main.py imports _ak_eligible (Sprint 55)")
    src = (_FC_ROOT / "main.py").read_text(encoding="utf-8")
    _check("from .asset_kind import is_execution_eligible" in src,
           "main.py imports is_execution_eligible")
    _check("asset_kind_not_execution_eligible" in src,
           "main.py reject reason is 'asset_kind_not_execution_eligible'")
    _check("_ak_eligible" in src,
           "main.py uses _ak_eligible alias")


def test_6_main_py_safety_fence_structure():
    _section("T6. _try_enter() safety fence structure")
    src = (_FC_ROOT / "main.py").read_text(encoding="utf-8")
    # Find the _try_enter function body
    m = re.search(r"async def _try_enter\(.*?\n(?=\s*async def|\s*def )",
                   src, flags=re.DOTALL)
    body = m.group(0) if m else ""
    _check("_ak_eligible(o.base" in body,
           "fence calls _ak_eligible(o.base, ...)")
    _check("asset_kind_not_execution_eligible" in body,
           "fence uses 'asset_kind_not_execution_eligible' reject reason")
    _check("self.book.open(" not in body.split("_ak_eligible")[0],
           "fence runs BEFORE self.book.open()")
    # Fence must not invoke executor / order placement.  Inspect the
    # fence block specifically.
    fence_match = re.search(
        r"#\s*Sprint 55 safety fence.*?# Sprint 1 \(2026-05-26\)",
        body, flags=re.DOTALL,
    )
    fence_body = fence_match.group(0) if fence_match else ""
    banned = ("executor.", "executor_emit=True", "bus.publish(",
               "signal_bus.", "executor.publish", "place_order(",
               "create_order(", "submit_order(")
    hits = [b for b in banned if b in fence_body]
    _check(not hits, f"fence has no executor/order calls (hits={hits})")


def test_7_classifier_event_log_shape():
    _section("T7. classifier returns expected info dict shape")
    for sym in ("SOXL", "BTC", "UVXY"):
        ok, info = is_execution_eligible(sym)
        _check("asset_kind" in info and "asset_kind_reason" in info
                and "execution_eligible" in info,
               f"{sym}: info has required keys ({sorted(info.keys())})")


def test_8_aggressive_paper_disabled_by_default():
    _section("T8. aggressive_paper still disabled by default")
    import os
    # at test time the env must respect current shell — if a future
    # test runs after enable in §17 §5, this test is expected to be
    # re-evaluated separately.  Here we just confirm the default is
    # False if flag is unset.
    saved = os.environ.pop(agg.ENV_FLAG_NAME, None)
    try:
        _check(not agg.is_enabled(),
               "is_enabled() False when flag unset")
    finally:
        if saved is not None:
            os.environ[agg.ENV_FLAG_NAME] = saved


def test_9_aggressive_paper_evaluate_disabled_no_ledger():
    _section("T9. evaluate_candidate is no-op when disabled")
    import os
    saved = os.environ.pop(agg.ENV_FLAG_NAME, None)
    try:
        res = agg.evaluate_candidate({"symbol": "SOXL"})
        _check(res.get("action") == "disabled",
               f"action={res.get('action')} when disabled")
        _check(not res.get("ledger_written"),
               "ledger not written when disabled")
    finally:
        if saved is not None:
            os.environ[agg.ENV_FLAG_NAME] = saved


def test_10_ambiguous_amc_sats_no_context_crypto():
    _section("T10. AMC / SATS ambiguous → crypto without equity context")
    for sym in ("AMC", "SATS"):
        ok, info = is_execution_eligible(sym, "")
        _check(ok and info["asset_kind"] == "crypto",
               f"{sym} no-ctx kind={info['asset_kind']}")
    # With equity context, flip
    for sym, ctx in (("AMC", "AMC Entertainment stock perpetual"),
                       ("SATS", "SATS Holdings US-stocks share perp")):
        ok, info = is_execution_eligible(sym, ctx)
        _check(not ok and info["asset_kind"] == "equity_perp",
               f"{sym} with ctx kind={info['asset_kind']}")


def test_11_unknown_blocked_from_paper():
    _section("T11. unknown symbol still gates correctly")
    ok, info = is_execution_eligible("", "")
    _check(not ok and info["asset_kind"] == "unknown",
           f"empty symbol blocked, kind={info['asset_kind']}")


def test_12_no_local_blocklist_leftover():
    _section("T12. aggressive_paper.py has no local frozenset duplicate")
    src = (_FC_ROOT / "aggressive_paper.py").read_text(encoding="utf-8")
    # Strip docstrings + comments before searching
    code = re.sub(r'"""[\s\S]*?"""', "", src)
    code = re.sub(r"#[^\n]*", "", code)
    # Count frozenset definitions in code-only — should not redefine
    # a private EQUITY_TICKER_BLOCKLIST literal (must inherit).
    occ = re.findall(r"EQUITY_TICKER_BLOCKLIST\s*=\s*frozenset\(\{", code)
    _check(not occ,
           f"no local frozenset definition (found {len(occ)})")
    # Module attribute still re-exports via shared module identity.
    _check("EQUITY_TICKER_BLOCKLIST = _ak_mod.EQUITY_TICKER_BLOCKLIST" in src,
           "exports shared blocklist via identity")


# ──────────────────────────────────────────────────────────────────────────
TESTS = [
    test_1_v14_leak_tickers_blocked,
    test_2_crypto_bases_allowed,
    test_3_aggressive_paper_uses_shared_classifier,
    test_4_aggressive_paper_blocklist_synced,
    test_5_main_py_imports_eligibility,
    test_6_main_py_safety_fence_structure,
    test_7_classifier_event_log_shape,
    test_8_aggressive_paper_disabled_by_default,
    test_9_aggressive_paper_evaluate_disabled_no_ledger,
    test_10_ambiguous_amc_sats_no_context_crypto,
    test_11_unknown_blocked_from_paper,
    test_12_no_local_blocklist_leftover,
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
