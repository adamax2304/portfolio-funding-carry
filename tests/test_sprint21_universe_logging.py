"""
Sprint 21.1 (2026-05-31) — Universe silent-drop observability tests.

  1.  below_vol_floor logged.
  2.  symbol_blacklist logged.
  3.  base_blacklist logged.
  4.  funding_outlier logged.
  5.  active_trading=False on every record.
  6.  Logger failure does not crash classifier.
  7.  Filtered OUTPUT count unchanged (kept-lists identical pre/post).
  8.  No executor/order/open_position imports or call sites.
  9.  Valid JSONL written (one record per line, json-parseable).
  10. Missing fields (None volume / None funding) handled safely.
  11. Dedup window: same (exchange, symbol, reason) NOT re-logged within 1h.

Run:
    cd "C:\\Users\\adama\\Documents\\zeph ai projects\\funding_carry"
    py -3 tests/test_sprint21_universe_logging.py
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

from funding_carry.data import universe as U                  # noqa: E402
from funding_carry.data.types import FundingSnapshot, SpotSnapshot  # noqa: E402

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


def _mk_perp(exchange="binance", symbol="BTCUSDT", base="BTC",
             funding_rate=0.0001, mark=60000.0, volume=20_000_000.0):
    return FundingSnapshot(
        exchange=exchange, symbol=symbol, base=base,
        funding_rate=funding_rate, mark_price=mark,
        next_funding_ts=9_999_999_999, funding_interval_h=8.0,
        quote_volume_24h_usd=volume, fetched_ts=0.0,
    )


def _mk_spot(exchange="binance", symbol="BTCUSDT", base="BTC",
             price=60000.0, volume=20_000_000.0):
    return SpotSnapshot(
        exchange=exchange, symbol=symbol, base=base, price=price,
        quote_volume_24h_usd=volume, fetched_ts=0.0,
    )


def _fresh_fetcher(tmp: Path, vol_floor=10_000_000.0,
                   blacklist=None) -> U.UniverseFetcher:
    U.FILTERED_FILE = tmp / "universe_filtered.jsonl"
    U._STATE_DIR = tmp
    return U.UniverseFetcher(volume_floor_usd=vol_floor,
                              symbol_blacklist=set(blacklist or []))


def _read(tmp: Path) -> list[dict]:
    f = tmp / "universe_filtered.jsonl"
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()
            if l.strip()]


# ──────────────────────────────────────────────────────────────────────────
def test_1_below_vol_floor():
    _section("1. below_vol_floor is logged")
    tmp = Path(tempfile.mkdtemp())
    f = _fresh_fetcher(tmp)
    ok = f._log_filtered(exchange="binance", symbol="STEEMUSDT", base="STEEM",
                          reason="below_vol_floor",
                          quote_volume_24h_usd=1_400_000.0,
                          funding_rate=0.0001, mark_price=0.2)
    _check(ok, "logged")
    recs = _read(tmp)
    _check(len(recs) == 1 and recs[0]["reason"] == "below_vol_floor",
           f"1 below_vol_floor record ({len(recs)})")
    _check(recs[0]["quote_volume_24h_usd"] == 1_400_000.0, "volume preserved")
    _check(recs[0]["vol_floor_usd"] == 10_000_000.0, "vol_floor preserved")
    _check(recs[0]["base"] == "STEEM", "base preserved")


def test_2_symbol_blacklist():
    _section("2. symbol_blacklist logged")
    tmp = Path(tempfile.mkdtemp())
    f = _fresh_fetcher(tmp, blacklist={"BLKSYMUSDT"})
    _classify = U._classify_perp_drop
    reason = _classify(_mk_perp(symbol="BLKSYMUSDT", base="BLKSYM"),
                       f._blacklist, f._vol_floor)
    _check(reason == "symbol_blacklist",
           f"classifier returns symbol_blacklist ({reason})")
    f._log_filtered(exchange="binance", symbol="BLKSYMUSDT", base="BLKSYM",
                    reason="symbol_blacklist",
                    quote_volume_24h_usd=99_000_000.0,
                    funding_rate=0.0, mark_price=1.0)
    recs = _read(tmp)
    _check(recs and recs[0]["reason"] == "symbol_blacklist", "logged")


def test_3_base_blacklist():
    _section("3. base_blacklist logged")
    tmp = Path(tempfile.mkdtemp())
    f = _fresh_fetcher(tmp, blacklist={"BLKBASE"})
    reason = U._classify_perp_drop(_mk_perp(symbol="BLKBASEUSDT", base="BLKBASE"),
                                    f._blacklist, f._vol_floor)
    _check(reason == "base_blacklist", f"classifier ({reason})")
    f._log_filtered(exchange="bybit", symbol="BLKBASEUSDT", base="BLKBASE",
                    reason="base_blacklist",
                    quote_volume_24h_usd=50_000_000.0,
                    funding_rate=0.0, mark_price=1.0)
    recs = _read(tmp)
    _check(recs and recs[0]["reason"] == "base_blacklist", "logged")


def test_4_funding_outlier():
    _section("4. funding_outlier logged (|funding|>1.0)")
    tmp = Path(tempfile.mkdtemp())
    f = _fresh_fetcher(tmp)
    reason = U._classify_perp_drop(_mk_perp(funding_rate=2.5), f._blacklist,
                                    f._vol_floor)
    _check(reason == "funding_outlier", f"classifier ({reason})")
    f._log_filtered(exchange="binance", symbol="WEIRDUSDT", base="WEIRD",
                    reason="funding_outlier",
                    quote_volume_24h_usd=20_000_000.0,
                    funding_rate=2.5, mark_price=0.001)
    recs = _read(tmp)
    _check(recs and recs[0]["funding_rate"] == 2.5, "funding 2.5 preserved")


def test_5_active_trading_false():
    _section("5. all records active_trading=False, mode=shadow")
    tmp = Path(tempfile.mkdtemp())
    f = _fresh_fetcher(tmp)
    for r in ("below_vol_floor", "symbol_blacklist", "base_blacklist",
              "funding_outlier"):
        f._log_filtered(exchange="binance", symbol=f"X{r}USDT", base=f"X{r}",
                        reason=r, quote_volume_24h_usd=0.0,
                        funding_rate=0.0, mark_price=1.0)
    recs = _read(tmp)
    _check(len(recs) == 4, f"4 records ({len(recs)})")
    _check(all(r["active_trading"] is False for r in recs),
           "active_trading=False on all")
    _check(all(r["mode"] == "shadow" for r in recs), "mode=shadow on all")
    _check(all(r["source"] == "funding_carry_universe" for r in recs),
           "source set on all")


def test_6_logger_failure_no_crash():
    _section("6. logger failure does not crash classifier")
    tmp = Path(tempfile.mkdtemp())
    f = _fresh_fetcher(tmp)
    # Force a write failure: point file at a directory path (write fails).
    U.FILTERED_FILE = tmp / "does" / "not" / "exist" / "x.jsonl"
    # Also break the parent so mkdir cannot save us:
    bad_dir = tmp / "blocker"
    bad_dir.touch()                          # a FILE blocks mkdir
    U._STATE_DIR = bad_dir / "deeper"
    ok = f._log_filtered(exchange="binance", symbol="BUSDT", base="B",
                         reason="below_vol_floor",
                         quote_volume_24h_usd=0.0,
                         funding_rate=0.0, mark_price=1.0)
    _check(ok is False, f"returns False on write failure (got {ok})")


def test_7_filter_output_unchanged():
    _section("7. classification loop produces same kept-sets as pre-patch comp")
    tmp = Path(tempfile.mkdtemp())
    f = _fresh_fetcher(tmp, blacklist={"NVDA", "NEARUSDT"})
    perps = [
        _mk_perp(symbol="BTCUSDT", base="BTC", volume=50_000_000),   # keep
        _mk_perp(symbol="STEEMUSDT", base="STEEM", volume=1_400_000),  # below
        _mk_perp(symbol="NEARUSDT", base="NEAR", volume=50_000_000),  # sym BL
        _mk_perp(symbol="NVDAUSDT", base="NVDA", volume=50_000_000),  # base BL
        _mk_perp(symbol="BIGUSDT", base="BIG", volume=99_000_000,
                  funding_rate=2.5),                                  # outlier
    ]
    expected_pre_patch = [p for p in perps
                          if p.symbol not in f._blacklist
                          and p.base not in f._blacklist
                          and p.quote_volume_24h_usd >= f._vol_floor
                          and abs(p.funding_rate) <= 1.0]
    kept = []
    for p in perps:
        reason = U._classify_perp_drop(p, f._blacklist, f._vol_floor)
        if reason is None:
            kept.append(p)
    _check([p.symbol for p in kept] == [p.symbol for p in expected_pre_patch],
           f"kept symbols match pre-patch ({[p.symbol for p in kept]})")
    _check(len(kept) == 1 and kept[0].symbol == "BTCUSDT",
           "only BTCUSDT survives")


def test_8_no_executor_calls():
    _section("8. no executor / order / open_position in universe.py")
    import re as _re
    src = Path(U.__file__).read_text(encoding="utf-8")
    code = _re.sub(r'""".*?"""', "", src, flags=_re.DOTALL)
    code = _re.sub(r"#.*", "", code)
    banned = ["open_position(", ".open_position(", "place_order(",
              "create_order(", "submit_order(", "ExecutorAgent(",
              "paper_book.open", "bus.publish("]
    hits = [b for b in banned if b in code]
    _check(not hits, f"no trading calls in universe.py (hits: {hits})")
    _check("from funding_carry.execution" not in src
           and "from ..execution" not in src,
           "no execution-layer import")


def test_9_valid_jsonl():
    _section("9. JSONL is valid (one JSON object per line)")
    tmp = Path(tempfile.mkdtemp())
    f = _fresh_fetcher(tmp)
    for sym in ("STEEMUSDT", "HOMEUSDT", "MEMEUSDT"):
        f._log_filtered(exchange="bitget", symbol=sym, base=sym[:-4],
                        reason="below_vol_floor",
                        quote_volume_24h_usd=1_000_000.0,
                        funding_rate=-0.001, mark_price=1.0)
    lines = (tmp / "universe_filtered.jsonl").read_text(encoding="utf-8").splitlines()
    _check(len(lines) == 3, f"3 lines ({len(lines)})")
    for ln in lines:
        j = json.loads(ln)
        _check("reason" in j and "ts_iso" in j, f"required fields present")


def test_10_missing_fields_safe():
    _section("10. missing fields (None volume/funding) handled safely")
    tmp = Path(tempfile.mkdtemp())
    f = _fresh_fetcher(tmp)
    ok = f._log_filtered(exchange="mexc", symbol="NONEUSDT", base="NONE",
                         reason="below_vol_floor",
                         quote_volume_24h_usd=None,
                         funding_rate=None, mark_price=None)
    _check(ok is True, "None fields do not raise")
    rec = _read(tmp)[0]
    _check(rec["quote_volume_24h_usd"] is None, "volume=None preserved")
    _check(rec["funding_rate"] is None, "funding_rate=None preserved")


def test_11_dedup_window():
    _section("11. dedup window: same key not re-logged within 1h")
    tmp = Path(tempfile.mkdtemp())
    f = _fresh_fetcher(tmp)
    a = f._log_filtered(exchange="bitget", symbol="DUPUSDT", base="DUP",
                        reason="below_vol_floor", quote_volume_24h_usd=0.0,
                        funding_rate=0.0, mark_price=1.0)
    b = f._log_filtered(exchange="bitget", symbol="DUPUSDT", base="DUP",
                        reason="below_vol_floor", quote_volume_24h_usd=0.0,
                        funding_rate=0.0, mark_price=1.0)
    _check(a is True and b is False, f"first=True, second=False ({a}/{b})")
    recs = _read(tmp)
    _check(len(recs) == 1, f"only 1 record on disk ({len(recs)})")
    # Different reason on same symbol -> not deduped
    c = f._log_filtered(exchange="bitget", symbol="DUPUSDT", base="DUP",
                        reason="symbol_blacklist", quote_volume_24h_usd=0.0,
                        funding_rate=0.0, mark_price=1.0)
    _check(c is True, "different reason logs again")


def main() -> int:
    for t in (
        test_1_below_vol_floor, test_2_symbol_blacklist, test_3_base_blacklist,
        test_4_funding_outlier, test_5_active_trading_false,
        test_6_logger_failure_no_crash, test_7_filter_output_unchanged,
        test_8_no_executor_calls, test_9_valid_jsonl,
        test_10_missing_fields_safe, test_11_dedup_window,
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
