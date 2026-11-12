"""
funding_carry/external/low_volume_audit.py

Sprint 21.3 — Low-volume shadow universe + depth-validation audit.

PURE DIAGNOSTIC.  Does NOT touch live universe filter, does NOT change strategy,
does NOT trade.  Reuses the Sprint 20 free_funding_scanner output (cross-exchange
funding spreads from 5 public exchanges), filters down to the low-volume band
`$500k ≤ min_leg_volume_24h < $10M`, then fetches LIVE orderbooks on both legs
to classify each candidate as `depth_ok | spread_too_wide | depth_too_thin |
fee_coverage_too_low | price_mismatch | unknown`.

The point: answer "are these < $10M opps real at small size, or is the high
APR an illiquidity mirage?"  Output is shadow-only.  We never lower the live
$10M floor based on APR alone — only depth-validated candidates are surfaced
for a hypothetical future micro-paper test.

Usage:
    cd "C:\\Users\\adama\\Documents\\zeph ai projects"
    py -3 -m funding_carry.external.low_volume_audit
"""
from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from funding_carry.config import PERP_TAKER_FEES
from funding_carry.external import free_funding_scanner as F

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = _ROOT / "state"
LOW_VOL_FILE = STATE_DIR / "low_volume_funding_shadow.jsonl"

# Volume bands (USD 24h, quote currency).
VOL_FLOOR_LIVE = 10_000_000.0
VOL_FLOOR_MIN = 500_000.0          # below this we just don't bother

# Depth & spread thresholds for the verdict classifier.
SPREAD_OK_BPS = 10.0
DEPTH_FILL_OK_BPS = 10.0
PRICE_MISMATCH_BPS = 50.0
FEE_COVERAGE_MIN = 2.0

# Default diagnostic notionals.
TARGET_NOTIONALS = (25, 50, 100, 250, 500)

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}
_HTTP_TIMEOUT_S = 15.0


def _now_iso() -> str:
    return (datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def _bucket(vol: float | None) -> str:
    if vol is None:
        return "unknown"
    if vol < 500_000:    return "<500k"
    if vol < 1_000_000:  return "500k-1M"
    if vol < 3_000_000:  return "1M-3M"
    if vol < 5_000_000:  return "3M-5M"
    if vol < 10_000_000: return "5M-10M"
    return "10M+"


def append_jsonl(path: Path, record: dict) -> bool:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False,
                               separators=(",", ":")) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("[low_vol_audit] jsonl write fail: %s", exc)
        return False


# ── Orderbook helpers ──────────────────────────────────────────────────────
@dataclass
class OrderBook:
    exchange: str
    symbol: str
    bids: list[tuple[float, float]] = field(default_factory=list)  # (price, qty)
    asks: list[tuple[float, float]] = field(default_factory=list)
    ts: float = 0.0

    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    def spread_bps(self) -> float | None:
        b, a = self.best_bid(), self.best_ask()
        if b is None or a is None or b <= 0:
            return None
        mid = (a + b) / 2.0
        return (a - b) / mid * 1e4 if mid > 0 else None

    def slippage_to_fill_bps(self, side: str, notional_usd: float) -> float | None:
        """Returns the average-execution slippage from the best price needed
        to fill `notional_usd` worth of size on the given side.

        side="buy" walks asks (we go LONG and pay UP); side="sell" walks bids
        (we go SHORT and receive DOWN).  None when book is empty or notional
        cannot be filled with the visible levels.
        """
        levels = self.asks if side == "buy" else self.bids
        if not levels:
            return None
        ref = levels[0][0]
        if ref <= 0:
            return None
        remaining = notional_usd
        filled_cost = 0.0
        filled_size = 0.0
        for price, qty in levels:
            if price <= 0 or qty <= 0:
                continue
            level_notional = price * qty
            take = min(level_notional, remaining)
            filled_cost += take
            filled_size += take / price
            remaining -= take
            if remaining <= 0:
                break
        if remaining > 0:
            return None  # not enough depth visible
        avg = filled_cost / filled_size if filled_size > 0 else None
        if avg is None:
            return None
        slip = abs(avg - ref) / ref * 1e4
        return slip


def _http_json(url: str) -> tuple[int | None, Any, str | None]:
    try:
        import httpx
    except Exception as exc:  # noqa: BLE001
        return None, None, f"httpx_unavailable:{exc}"
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True,
                          headers=_BROWSER_HEADERS) as c:
            r = c.get(url)
            if r.status_code != 200:
                return r.status_code, None, f"http_{r.status_code}"
            return r.status_code, r.json(), None
    except Exception as exc:  # noqa: BLE001
        return None, None, str(exc)[:200]


def _parse_levels(items, price_key=0, qty_key=1, limit=50):
    out: list[tuple[float, float]] = []
    for it in items[:limit]:
        try:
            if isinstance(it, (list, tuple)):
                p, q = float(it[price_key]), float(it[qty_key])
            else:
                p, q = float(it.get("price") or it.get("p")), float(it.get("size") or it.get("q") or it.get("qty"))
            if p > 0 and q > 0:
                out.append((p, q))
        except Exception:
            continue
    return out


def fetch_orderbook_binance(symbol: str) -> OrderBook | None:
    _, data, err = _http_json(
        f"https://fapi.binance.com/fapi/v1/depth?symbol={symbol}&limit=50")
    if err or not isinstance(data, dict):
        return None
    return OrderBook(exchange="binance", symbol=symbol,
                      bids=_parse_levels(data.get("bids") or []),
                      asks=_parse_levels(data.get("asks") or []),
                      ts=time.time())


def fetch_orderbook_bybit(symbol: str) -> OrderBook | None:
    _, data, err = _http_json(
        f"https://api.bybit.com/v5/market/orderbook?"
        f"category=linear&symbol={symbol}&limit=50")
    if err or not isinstance(data, dict):
        return None
    result = data.get("result") or {}
    return OrderBook(exchange="bybit", symbol=symbol,
                      bids=_parse_levels(result.get("b") or []),
                      asks=_parse_levels(result.get("a") or []),
                      ts=time.time())


def fetch_orderbook_bitget(symbol: str) -> OrderBook | None:
    _, data, err = _http_json(
        f"https://api.bitget.com/api/v2/mix/market/merge-depth?"
        f"symbol={symbol}&productType=USDT-FUTURES&limit=50")
    if err or not isinstance(data, dict):
        return None
    result = data.get("data") or {}
    return OrderBook(exchange="bitget", symbol=symbol,
                      bids=_parse_levels(result.get("bids") or []),
                      asks=_parse_levels(result.get("asks") or []),
                      ts=time.time())


def fetch_orderbook_mexc(symbol: str) -> OrderBook | None:
    # MEXC contract uses underscore symbols: BTC_USDT
    sym_us = symbol if "_" in symbol else (
        f"{symbol[:-4]}_USDT" if symbol.endswith("USDT") else symbol)
    _, data, err = _http_json(
        f"https://contract.mexc.com/api/v1/contract/depth/{sym_us}")
    if err or not isinstance(data, dict):
        return None
    result = data.get("data") or {}
    return OrderBook(exchange="mexc", symbol=sym_us,
                      bids=_parse_levels(result.get("bids") or []),
                      asks=_parse_levels(result.get("asks") or []),
                      ts=time.time())


_FETCHERS: dict[str, Callable[[str], OrderBook | None]] = {
    "binance": fetch_orderbook_binance,
    "bybit": fetch_orderbook_bybit,
    "bitget": fetch_orderbook_bitget,
    "mexc": fetch_orderbook_mexc,
}


# ── Classifier ─────────────────────────────────────────────────────────────
def classify(long_ob: OrderBook | None, short_ob: OrderBook | None,
             funding_spread_8h: float | None,
             fees_roundtrip: float,
             target_notional: float = 100.0,
             ) -> tuple[str, dict]:
    """Return (verdict, metrics)."""
    metrics: dict = {
        "orderbook_spread_bps": None,
        "depth_slip_long_bps": None,
        "depth_slip_short_bps": None,
        "price_mismatch_bps": None,
        "fee_coverage_ratio": None,
    }
    if long_ob is None or short_ob is None:
        return "unknown", metrics

    long_spread = long_ob.spread_bps()
    short_spread = short_ob.spread_bps()
    spreads = [s for s in (long_spread, short_spread) if s is not None]
    metrics["orderbook_spread_bps"] = max(spreads) if spreads else None

    # Cross-leg price mismatch (mid-mid)
    long_mid = (long_ob.best_bid() + long_ob.best_ask()) / 2.0 \
        if (long_ob.best_bid() and long_ob.best_ask()) else None
    short_mid = (short_ob.best_bid() + short_ob.best_ask()) / 2.0 \
        if (short_ob.best_bid() and short_ob.best_ask()) else None
    if long_mid and short_mid and long_mid > 0:
        metrics["price_mismatch_bps"] = abs(short_mid - long_mid) / long_mid * 1e4

    slip_long = long_ob.slippage_to_fill_bps("buy", target_notional)
    slip_short = short_ob.slippage_to_fill_bps("sell", target_notional)
    metrics["depth_slip_long_bps"] = slip_long
    metrics["depth_slip_short_bps"] = slip_short

    # Fee coverage:
    # funding_spread_8h × 2 cycles  vs  roundtrip fees (entry+exit on both legs)
    if funding_spread_8h is not None and fees_roundtrip > 0:
        expected_2c = abs(funding_spread_8h) * 2.0
        metrics["fee_coverage_ratio"] = expected_2c / fees_roundtrip

    # Verdict priority
    if metrics["orderbook_spread_bps"] is not None \
            and metrics["orderbook_spread_bps"] > SPREAD_OK_BPS:
        return "spread_too_wide", metrics
    if slip_long is None or slip_short is None:
        return "depth_too_thin", metrics
    if slip_long > DEPTH_FILL_OK_BPS or slip_short > DEPTH_FILL_OK_BPS:
        return "depth_too_thin", metrics
    if metrics["price_mismatch_bps"] is not None \
            and metrics["price_mismatch_bps"] > PRICE_MISMATCH_BPS:
        return "price_mismatch", metrics
    if metrics["fee_coverage_ratio"] is not None \
            and metrics["fee_coverage_ratio"] < FEE_COVERAGE_MIN:
        return "fee_coverage_too_low", metrics
    return "depth_ok", metrics


def _roundtrip_fee_frac(long_ex: str, short_ex: str) -> float:
    """Roundtrip = entry+exit on both legs = 4 fills.  Use taker fees (conservative)."""
    lf = PERP_TAKER_FEES.get(long_ex, 0.0006)
    sf = PERP_TAKER_FEES.get(short_ex, 0.0006)
    return (lf + sf) * 2.0  # entry + exit


def _load_opps_from_file() -> list[dict]:
    if not F.OPP_FILE.exists():
        return []
    return [json.loads(l) for l in F.OPP_FILE.read_text(encoding="utf-8")
                                              .splitlines() if l.strip()]


# ── Audit orchestration ────────────────────────────────────────────────────
def audit_once(*, write: bool = True,
               refresh_free_scan: bool = True,
               target_notional: float = 100.0,
               max_candidates: int = 50,
               ) -> dict:
    """One-shot low-volume shadow audit.

    1. (optional) refresh free_funding scanner data;
    2. select cross-exchange opps where `min_leg_volume ∈ [$500k, $10M)`
       AND both legs are in our executable venue set;
    3. fetch live orderbooks on both legs;
    4. classify each candidate and write shadow record.
    """
    if refresh_free_scan:
        try:
            F.scan_once(write=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("[low_vol_audit] free scan refresh failed: %s", exc)

    raw = _load_opps_from_file()
    # latest snapshot per (base, long_ex, short_ex)
    latest: dict[tuple, dict] = {}
    for o in raw:
        k = (o.get("base"), o.get("long_exchange"), o.get("short_exchange"))
        if k[0] is None or k[1] is None or k[2] is None:
            continue
        prev = latest.get(k)
        if prev is None or (o.get("ts") or 0) > (prev.get("ts") or 0):
            latest[k] = o
    opps = list(latest.values())

    # Filter to low-volume executable
    cands: list[dict] = []
    for o in opps:
        vol = o.get("volume_24h_min")
        if vol is None:
            continue
        if not (VOL_FLOOR_MIN <= vol < VOL_FLOOR_LIVE):
            continue
        if not o.get("both_executable"):
            continue
        cands.append(o)
    # Sort by gross_apr desc, cap to max_candidates (avoid rate-limit floods)
    cands.sort(key=lambda x: -(x.get("gross_apr") or 0))
    cands = cands[:max_candidates]

    records: list[dict] = []
    for o in cands:
        long_ex = o["long_exchange"]; short_ex = o["short_exchange"]
        long_sym = o.get("raw_refs", {}).get(long_ex, f"{o['base']}USDT")
        short_sym = o.get("raw_refs", {}).get(short_ex, f"{o['base']}USDT")
        long_ob = _FETCHERS.get(long_ex, lambda s: None)(long_sym)
        short_ob = _FETCHERS.get(short_ex, lambda s: None)(short_sym)
        spread8 = o.get("funding_spread_8h")
        fees = _roundtrip_fee_frac(long_ex, short_ex)
        verdict, metrics = classify(long_ob, short_ob, spread8, fees,
                                     target_notional=target_notional)
        rec = {
            "ts": time.time(), "ts_iso": _now_iso(),
            "source": "funding_carry_low_volume_shadow",
            "base": o["base"],
            "symbol": f"{o['base']}USDT",
            "long_exchange": long_ex, "short_exchange": short_ex,
            "long_funding_8h": o.get("long_funding_8h"),
            "short_funding_8h": o.get("short_funding_8h"),
            "funding_spread_8h": spread8,
            "gross_apr": o.get("gross_apr"),
            "min_leg_volume_24h_usd": o.get("volume_24h_min"),
            "volume_bucket": _bucket(o.get("volume_24h_min")),
            "mark_spread_bps": o.get("mark_spread_bps"),
            "orderbook_spread_bps": metrics["orderbook_spread_bps"],
            "depth_usd_target": target_notional,
            "depth_slip_long_bps": metrics["depth_slip_long_bps"],
            "depth_slip_short_bps": metrics["depth_slip_short_bps"],
            "price_mismatch_bps": metrics["price_mismatch_bps"],
            "estimated_slippage_bps": (
                max(x for x in (metrics["depth_slip_long_bps"],
                                 metrics["depth_slip_short_bps"])
                    if x is not None) if any(
                    x is not None for x in (metrics["depth_slip_long_bps"],
                                             metrics["depth_slip_short_bps"]))
                else None),
            "fee_coverage_ratio": metrics["fee_coverage_ratio"],
            "fees_roundtrip_frac": fees,
            "validation_verdict": verdict,
            "mode": "shadow",
            "active_trading": False,
        }
        records.append(rec)
        if write:
            append_jsonl(LOW_VOL_FILE, rec)

    # Summary
    from collections import Counter
    by_bucket = Counter(r["volume_bucket"] for r in records)
    by_verdict = Counter(r["validation_verdict"] for r in records)
    summary = {
        "ts": time.time(),
        "total_low_volume_candidates": len(records),
        "by_bucket": dict(by_bucket),
        "by_verdict": dict(by_verdict),
        "target_notional_usd": target_notional,
    }
    print(f"[low_vol_audit] candidates={len(records)} verdicts={dict(by_verdict)}")
    for r in records[:8]:
        print(f"  {r['base']:10} {r['long_exchange']}->{r['short_exchange']:8} "
              f"APR={r['gross_apr']:>7.0f}%  vol={r['min_leg_volume_24h_usd']:>10,.0f}  "
              f"slip(L/S)={r['depth_slip_long_bps']}/{r['depth_slip_short_bps']}  "
              f"verdict={r['validation_verdict']}")
    return summary


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    s = audit_once()
    print(json.dumps(s, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
