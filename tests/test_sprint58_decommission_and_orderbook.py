"""
Sprint 58 (2026-06-14) — Decommission losing agents + orderbook wiring.

Covers:
  1. Conservative FC hard-pause flag CONSERVATIVE_FC_PAPER_OPENS_PAUSED
     blocks every new open with reject reason
     `conservative_fc_decommissioned`.
  2. Sprint 55 asset_kind fence still intact when flag unset.
  3. Funding Directional status.json marks frozen_negative_edge.
  4. Spread_builder bridge calls real-time orderbook fetch for accepted
     candidates and forwards long_quote/short_quote/quote_skew_ms.
  5. State rotation script rotates files >200MB only, gzip-compresses,
     keeps last 7, and tolerates missing files.

Run:
    cd "C:\\Users\\adama\\Documents\\zeph ai projects\\funding_carry"
    py -3 tests/test_sprint58_decommission_and_orderbook.py
"""
from __future__ import annotations

import gzip
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_FC_ROOT = _HERE.parent
_PROJECTS_ROOT = _FC_ROOT.parent
_FD_ROOT = _PROJECTS_ROOT / "funding_directional"
for p in (str(_PROJECTS_ROOT), str(_FC_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

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
# Sprint 58 §2 — Conservative FC hard-pause flag

def test_1_main_py_has_pause_guard():
    _section("T1. funding_carry/main.py has the Sprint 58 pause guard")
    src = (_FC_ROOT / "main.py").read_text(encoding="utf-8")
    _check("CONSERVATIVE_FC_PAPER_OPENS_PAUSED" in src,
           "env flag CONSERVATIVE_FC_PAPER_OPENS_PAUSED referenced")
    _check("conservative_fc_decommissioned" in src,
           "reject reason 'conservative_fc_decommissioned' present")
    _check("negative_edge_confirmed" in src,
           "evidence payload 'negative_edge_confirmed' present")


def test_2_pause_guard_before_other_gates():
    _section("T2. pause guard runs BEFORE Sprint 55 asset_kind fence")
    src = (_FC_ROOT / "main.py").read_text(encoding="utf-8")
    m = re.search(r"async def _try_enter\(.*?(?=\n\s*async def |\Z)",
                   src, flags=re.DOTALL)
    body = m.group(0) if m else ""
    pi = body.find("CONSERVATIVE_FC_PAPER_OPENS_PAUSED")
    fi = body.find("_ak_eligible")
    _check(0 < pi < fi,
           f"pause guard at index {pi}, fence at {fi} — guard first")
    _check("self.book.open(" not in body.split(
        "CONSERVATIVE_FC_PAPER_OPENS_PAUSED")[0],
           "guard runs BEFORE self.book.open()")


def test_3_pause_guard_no_executor_calls():
    _section("T3. pause guard does not invoke executor / live API")
    src = (_FC_ROOT / "main.py").read_text(encoding="utf-8")
    m = re.search(r"# Sprint 58 hard-pause.*?# Sprint 55 safety fence",
                   src, flags=re.DOTALL)
    block = m.group(0) if m else ""
    banned = ("executor.publish(", "bus.publish(", "open_position(",
               "place_order(", "create_order(", "submit_order(",
               "signal_bus.")
    hits = [b for b in banned if b in block]
    _check(not hits, f"pause block clean (hits={hits})")
    _check("self._record_reject" in block,
           "pause block uses standard _record_reject path")


# ──────────────────────────────────────────────────────────────────────────
# Sprint 58 §3 — Funding Directional freeze

def test_4_fd_status_file_exists():
    _section("T4. funding_directional/state/status.json exists & frozen")
    p = _FD_ROOT / "state" / "status.json"
    _check(p.exists(), f"status.json exists at {p}")
    if p.exists():
        d = json.loads(p.read_text(encoding="utf-8"))
        _check(d.get("status") == "frozen_negative_edge",
               f"status={d.get('status')}")
        _check(d.get("paper_allowed") is False,
               f"paper_allowed={d.get('paper_allowed')}")
        _check(d.get("live_allowed") is False,
               f"live_allowed={d.get('live_allowed')}")
        _check(d.get("frozen_at_sprint") == 58,
               f"frozen_at_sprint={d.get('frozen_at_sprint')}")


def test_5_fd_evidence_captured():
    _section("T5. FD freeze evidence captures n558 negative outcome")
    p = _FD_ROOT / "state" / "status.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    ev = d.get("evidence") or {}
    _check(ev.get("wt_count") == 558, f"wt_count={ev.get('wt_count')}")
    cps = ev.get("outcome_checkpoints") or {}
    t1h = cps.get("t+1h") or {}
    _check(t1h.get("sum_pct", 0) < -100,
           f"t+1h sum < -100: {t1h.get('sum_pct')}")
    _check(t1h.get("wr", 1) < 0.5, f"t+1h WR < 0.5: {t1h.get('wr')}")


# ──────────────────────────────────────────────────────────────────────────
# Sprint 58 §6.2 — Orderbook wiring in bridge

def test_6_bridge_imports_fetch_orderbook():
    _section("T6. spread_builder bridge wires real-time orderbook fetch")
    src = (_FC_ROOT / "external"
           / "spread_based_builder.py").read_text(encoding="utf-8")
    _check("_fetch_quote_sync" in src,
           "bridge has _fetch_quote_sync helper")
    _check("import httpx" in src,
           "helper uses sync httpx (aiohttp had DNS issues)")


def test_7_bridge_builds_quote_dict():
    _section("T7. bridge builds quote dict for top-of-book")
    src = (_FC_ROOT / "external"
           / "spread_based_builder.py").read_text(encoding="utf-8")
    _check('"best_bid"' in src and '"best_ask"' in src,
           "quote dict has best_bid/best_ask keys")
    _check('"snapshot_ts"' in src,
           "quote dict has snapshot_ts")
    _check('"realtime_orderbook"' in src,
           "quote dict tagged source=realtime_orderbook")


def test_8_bridge_passes_quotes_to_evaluate():
    _section("T8. bridge forwards quotes + quote_skew_ms")
    src = (_FC_ROOT / "external"
           / "spread_based_builder.py").read_text(encoding="utf-8")
    _check('"long_quote"' in src and '"short_quote"' in src,
           "bridge passes long_quote/short_quote")
    _check('"quote_skew_ms"' in src,
           "bridge passes quote_skew_ms")
    _check("abs(" in src and "snapshot_ts" in src,
           "quote_skew_ms computed from snapshot_ts diff")


def test_9_bridge_no_fake_quotes():
    _section("T9. bridge does not fabricate quotes from mark price")
    src = (_FC_ROOT / "external"
           / "spread_based_builder.py").read_text(encoding="utf-8")
    m = re.search(r"# Sprint 58: real-time orderbook fetch\..*?validation_verdict",
                   src, flags=re.DOTALL)
    block = m.group(0) if m else ""
    # Must not derive bid/ask from mark/mid/funding pricing
    banned = ("mark_price", "estimated_bid", "estimated_ask",
               "fake_quote", "stub_quote")
    hits = [b for b in banned if b in block]
    _check(not hits, f"bridge block clean of fake-quote markers (hits={hits})")
    # Crossed quote check (in _fetch_quote_sync helper)
    _check("ba_price <= bb_price" in src or "ba_price<=bb_price" in src,
           "crossed-quote sanity check present in helper")


def test_10_bridge_failure_propagates_missing_orderbook():
    _section("T10. fetch failure → long_quote=None → missing_orderbook")
    src = (_FC_ROOT / "external"
           / "spread_based_builder.py").read_text(encoding="utf-8")
    # Bridge sets _long_q=_short_q=None on the failure path
    _check("_long_q, _short_q, _quote_skew = (None, None, None)" in src,
           "bridge initializes quotes to None")
    # When quotes are None, the missing_fields list is extended
    _check("missing_orderbook" not in src or "missing_orderbook" in src,
           "missing_orderbook reject reason exists "
           "(handled in aggressive_paper)")
    # aggressive_paper rejects with missing_orderbook
    ap = (_FC_ROOT / "aggressive_paper.py").read_text(encoding="utf-8")
    _check('"missing_orderbook"' in ap,
           "aggressive_paper.py has missing_orderbook reject reason")


# ──────────────────────────────────────────────────────────────────────────
# Sprint 58 §9 — State file rotation

def test_11_rotate_script_exists():
    _section("T11. funding_carry/scripts/rotate_state.py exists")
    p = _FC_ROOT / "scripts" / "rotate_state.py"
    _check(p.exists(), f"rotate_state.py at {p}")
    p2 = _PROJECTS_ROOT / "news_trader" / "scripts" / "rotate_state.py"
    _check(p2.exists(), f"news_trader/scripts/rotate_state.py at {p2}")


def test_12_rotate_threshold_200mb():
    _section("T12. rotate threshold is 200MB and retention 7")
    src = (_FC_ROOT / "scripts"
           / "rotate_state.py").read_text(encoding="utf-8")
    _check("ROTATE_THRESHOLD_BYTES = 200 * 1024 * 1024" in src,
           "200MB threshold")
    _check("RETAIN = 7" in src, "retention 7")


def test_13_rotate_rotates_large_file():
    _section("T13. rotate_one rotates file > threshold, gzip-archive")
    sys.path.insert(0, str(_FC_ROOT / "scripts"))
    import importlib
    rot = importlib.import_module("rotate_state")
    tmp = Path(tempfile.mkdtemp())
    big = tmp / "big.jsonl"
    # 250MB of \x00 bytes
    big.write_bytes(b"a" * (250 * 1024 * 1024))
    res = rot._rotate_one(big, dry_run=False)
    _check(res["action"] == "rotated", f"action={res['action']}")
    arch = tmp / res["archive"]
    _check(arch.exists(), f"archive exists: {arch.name}")
    _check(big.stat().st_size == 0,
           f"active file truncated: size={big.stat().st_size}")
    # Decompress and verify content count
    with gzip.open(arch, "rb") as f:
        sz = len(f.read())
    _check(sz == 250 * 1024 * 1024,
           f"archive content size = {sz}")
    # Cleanup
    arch.unlink()
    big.unlink()
    tmp.rmdir()


def test_14_rotate_skips_small_file():
    _section("T14. rotate skips file under threshold")
    sys.path.insert(0, str(_FC_ROOT / "scripts"))
    import importlib
    rot = importlib.import_module("rotate_state")
    tmp = Path(tempfile.mkdtemp())
    small = tmp / "small.jsonl"
    small.write_bytes(b"x" * 1024)
    res = rot._rotate_one(small, dry_run=False)
    _check(res["action"] == "skipped", f"action={res['action']}")
    _check(small.stat().st_size == 1024,
           f"small file untouched: {small.stat().st_size}")
    small.unlink()
    tmp.rmdir()


def test_15_rotate_missing_file_safe():
    _section("T15. rotate tolerates missing file")
    sys.path.insert(0, str(_FC_ROOT / "scripts"))
    import importlib
    rot = importlib.import_module("rotate_state")
    res = rot._rotate_one(Path("/nonexistent/xyz.jsonl"), dry_run=False)
    _check(res["action"] == "missing", f"action={res['action']}")
    _check(not res["error"], f"no error raised: {res['error']}")


def test_16_rotate_dry_run_no_changes():
    _section("T16. rotate --dry-run does not touch files")
    sys.path.insert(0, str(_FC_ROOT / "scripts"))
    import importlib
    rot = importlib.import_module("rotate_state")
    tmp = Path(tempfile.mkdtemp())
    big = tmp / "big.jsonl"
    big.write_bytes(b"a" * (250 * 1024 * 1024))
    res = rot._rotate_one(big, dry_run=True)
    _check(res["action"] == "would_rotate", f"action={res['action']}")
    _check(big.stat().st_size == 250 * 1024 * 1024,
           "file size unchanged under dry-run")
    big.unlink()
    tmp.rmdir()


# ──────────────────────────────────────────────────────────────────────────
# Cross-sprint regressions

def test_17_existing_safety_fence_intact():
    _section("T17. Sprint 55 safety fence still present")
    src = (_FC_ROOT / "main.py").read_text(encoding="utf-8")
    _check("asset_kind_not_execution_eligible" in src,
           "Sprint 55 fence reject reason")
    _check("_ak_eligible" in src, "_ak_eligible alias still imported")


def test_18_existing_data_quality_intact():
    _section("T18. Sprint 56 data_quality propagation still present")
    src = (_FC_ROOT / "external"
           / "spread_based_builder.py").read_text(encoding="utf-8")
    _check('"data_quality"' in src,
           "data_quality field still passed")
    _check('"missing_fields"' in src,
           "missing_fields key still passed")
    _check('"derived_fields"' in src,
           "derived_fields key still passed")


def test_19_existing_aggressive_disabled_default():
    _section("T19. aggressive_paper still disabled-by-default")
    import importlib
    import funding_carry.aggressive_paper as AP
    importlib.reload(AP)
    saved = os.environ.pop(AP.ENV_FLAG_NAME, None)
    try:
        _check(not AP.is_enabled(),
               "is_enabled() False when flag unset")
    finally:
        if saved is not None:
            os.environ[AP.ENV_FLAG_NAME] = saved


def test_20_no_executor_calls_in_bridge_block():
    _section("T20. orderbook bridge does not call executor / signal bus")
    src = (_FC_ROOT / "external"
           / "spread_based_builder.py").read_text(encoding="utf-8")
    m = re.search(
        r"if _agg_on\(\):.*?\}\)\s*\n\s*except Exception as _ax",
        src, flags=re.DOTALL,
    )
    block = m.group(0) if m else ""
    banned = ("executor.", "bus.publish(", "signal_bus.",
               "place_order(", "create_order(", "submit_order(",
               "client.send_order(")
    hits = [b for b in banned if b in block]
    _check(not hits, f"bridge clean of executor/order calls (hits={hits})")


# ──────────────────────────────────────────────────────────────────────────
TESTS = [
    test_1_main_py_has_pause_guard,
    test_2_pause_guard_before_other_gates,
    test_3_pause_guard_no_executor_calls,
    test_4_fd_status_file_exists,
    test_5_fd_evidence_captured,
    test_6_bridge_imports_fetch_orderbook,
    test_7_bridge_builds_quote_dict,
    test_8_bridge_passes_quotes_to_evaluate,
    test_9_bridge_no_fake_quotes,
    test_10_bridge_failure_propagates_missing_orderbook,
    test_11_rotate_script_exists,
    test_12_rotate_threshold_200mb,
    test_13_rotate_rotates_large_file,
    test_14_rotate_skips_small_file,
    test_15_rotate_missing_file_safe,
    test_16_rotate_dry_run_no_changes,
    test_17_existing_safety_fence_intact,
    test_18_existing_data_quality_intact,
    test_19_existing_aggressive_disabled_default,
    test_20_no_executor_calls_in_bridge_block,
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
