"""
funding_carry/external/spread_based_builder.py

Sprint 39 (2026-06-06) — Spread-based perp/perp builder DIAGNOSTIC.

READ-ONLY.  Reads the Sprint 32 low_volume_funding_shadow.jsonl + audit
summaries, classifies per-symbol persistence (Sprint 21.4 style), then
for each `persistent_depth_ok` candidate composes a "would-build" record
showing the optimal venue route, funding pattern, costs, and net edge.

Does NOT touch the live universe filter, the order placement path, the
PaperBook, or executor.  Does NOT change any trading threshold.  Output
files are isolated:
  state/spread_builder_diagnostic.jsonl
  state/spread_builder_summary.jsonl

The point: prove (or disprove) that the old internal scanner's sign-gate
logic causes it to miss opportunities that a pure-spread builder would
recognise as buildable at low size.  No trades opened.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
STATE_DIR = _ROOT / "state"

#: Inputs.
LOW_VOL_FILE = STATE_DIR / "low_volume_funding_shadow.jsonl"
AUDIT_SUMMARY_FILE = STATE_DIR / "low_volume_audit_summary.jsonl"
#: Outputs (isolated — DO NOT REUSE strategy files).
DIAG_FILE    = STATE_DIR / "spread_builder_diagnostic.jsonl"
SUMMARY_FILE = STATE_DIR / "spread_builder_summary.jsonl"

#: Eligibility thresholds (Sprint 38 derived).
MIN_APPEARANCES_PER_SYMBOL_ROUTE = 5
MIN_DEPTH_OK_RATE = 0.60
MIN_FEE_COVERAGE  = 2.0
MAX_ORDERBOOK_SPREAD_BPS = 10.0
MAX_SLIPPAGE_50_BPS  = 15.0  # at $50 target notional
MAX_SLIPPAGE_100_BPS = 25.0  # at $100 target notional

#: Persistence verdicts that immediately disqualify.
DISQUALIFYING_VERDICTS = frozenset({
    "spread_too_wide", "fee_edge_disappears", "price_mismatch",
    "unstable_depth", "too_thin", "transient_spike",
    "insufficient_n",
})

#: Sprint 40 — recurring loop default interval.
DEFAULT_INTERVAL_S = 3600.0  # 60 min
MIN_INTERVAL_S     = 600.0   # 10 min floor (anti-spam)


#: Reject reasons — pinned for tests.
REJECT_REASONS = (
    "not_persistent_depth_ok",
    "insufficient_appearances",
    "depth_ok_rate_low",
    "fee_coverage_low",
    "spread_too_wide",
    "slippage_too_high",
    "price_mismatch",
    "net_edge_nonpositive",
    "missing_leg_data",
    "unstable_depth",
    "duplicate_route",
    "unknown",
)


# ── Helpers ───────────────────────────────────────────────────────────────

def _utc_iso(ts: Optional[float] = None) -> str:
    try:
        t = ts if ts is not None else time.time()
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")
    except Exception:
        return str(ts or time.time())


def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _safe_load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception as exc:
        log.debug("[spread_builder] load %s failed: %s", path.name, exc)
    return out


def _funding_pattern(long_f8: Optional[float],
                     short_f8: Optional[float]) -> str:
    if long_f8 is None or short_f8 is None:
        return "unknown"
    lp = long_f8 > 0.0001
    ln = long_f8 < -0.0001
    sp = short_f8 > 0.0001
    sn = short_f8 < -0.0001
    if lp and sp:
        return "both_positive"
    if ln and sn:
        return "both_negative"
    if lp and sn:
        return "mixed_positive_negative"
    if ln and sp:
        return "mixed_negative_positive"
    return "zero_mixed"


# ── Per-(symbol, route) persistence builder ───────────────────────────────

def group_shadow_by_route(records: list[dict]) -> dict:
    """Group shadow records by (base, long_exchange, short_exchange).

    Returns:
        {(base, long_ex, short_ex): [shadow_record, ...]}
    """
    grouped: dict[tuple, list] = defaultdict(list)
    for r in records:
        b = r.get("base")
        le = r.get("long_exchange")
        se = r.get("short_exchange")
        if not (b and le and se):
            continue
        grouped[(b, le, se)].append(r)
    return grouped


def classify_route_persistence(records: list[dict]) -> dict:
    """Compute Sprint 21.4-style persistence stats for one route.

    Returns dict with: appearances, depth_ok_count, depth_ok_rate,
    median_apr, median_fee_coverage, median_ob_spread_bps, verdict.
    """
    apps = len(records)
    do = [r for r in records if r.get("validation_verdict") == "depth_ok"]
    do_count = len(do)
    do_rate = (do_count / apps) if apps else 0.0
    verdict_counter = Counter(r.get("validation_verdict") for r in records)
    aprs = [r.get("gross_apr") for r in records]
    fees = [r.get("fee_coverage_ratio") for r in records]
    obs  = [r.get("orderbook_spread_bps") for r in records]

    # Verdict — bias toward dominant pattern when not persistent.
    if apps < 3:
        v = "insufficient_n"
    elif do_rate >= 0.6:
        v = "persistent_depth_ok"
    elif do_rate >= 0.3:
        v = "unstable_depth"
    elif verdict_counter.get("spread_too_wide", 0) >= apps / 2:
        v = "spread_too_wide"
    elif verdict_counter.get("fee_coverage_too_low", 0) >= apps / 2:
        v = "fee_edge_disappears"
    elif verdict_counter.get("price_mismatch", 0) >= apps / 2:
        v = "price_mismatch"
    elif do_rate > 0:
        v = "transient_spike"
    else:
        v = "too_thin"

    return {
        "appearances": apps,
        "depth_ok_count": do_count,
        "depth_ok_rate": round(do_rate, 4),
        "median_apr": _med(aprs),
        "median_fee_coverage": _med(fees),
        "median_ob_spread_bps": _med(obs),
        "verdict": v,
        "verdict_counts": dict(verdict_counter),
    }


# ── Edge calculation ──────────────────────────────────────────────────────

def compute_edge(latest: dict) -> dict:
    """Compute net-edge metrics from one shadow record (the latest snapshot
    for the (symbol, route) combination).

    Returns:
        {
          "long_rate_8h": float,
          "short_rate_8h": float,
          "funding_spread_8h": float,
          "fees_bps": float,
          "spread_bps": float,
          "slippage_bps_50": float,
          "slippage_bps_100": float,
          "gross_edge_bps": float,
          "net_edge_bps": float,
          "fee_coverage": float,
          "estimated_net_usd_50": float,
          "estimated_net_usd_100": float,
          "pattern": str,
          "price_mismatch_bps": float,
        }

    Convention: shadow record's `funding_spread_8h` is
    `short_funding_8h - long_funding_8h` per Sprint 21.3 builder
    (already positive when long pays / short receives).  We use the
    absolute value for the per-cycle capture magnitude and surface the
    sign separately as the funding pattern.
    """
    long_f8  = latest.get("long_funding_8h")
    short_f8 = latest.get("short_funding_8h")
    spread8  = latest.get("funding_spread_8h")
    fees_rt  = latest.get("fees_roundtrip_frac") or 0.0
    ob_bps   = latest.get("orderbook_spread_bps") or 0.0
    slip_long  = latest.get("depth_slip_long_bps") or 0.0
    slip_short = latest.get("depth_slip_short_bps") or 0.0
    fee_cov  = latest.get("fee_coverage_ratio") or 0.0
    pm_bps   = latest.get("price_mismatch_bps") or 0.0

    pattern = _funding_pattern(long_f8, short_f8)

    # Convert: one funding 8h cycle on each leg generates
    # |funding_spread_8h| of gross edge as a fraction of notional.
    # 1.0 == 100 % == 10_000 bps.
    if spread8 is None:
        gross_edge_bps = 0.0
    else:
        gross_edge_bps = abs(float(spread8)) * 10_000.0

    fees_bps  = float(fees_rt) * 10_000.0
    slip50_bps = (max(slip_long, slip_short) if (slip_long or slip_short)
                  else 0.0)
    # Existing audit captures slippage at $100 notional; reuse it for the
    # $100 target; project to $50 by scaling depth_slip linearly.  This is
    # a coarse estimate.
    slip100_bps = slip50_bps
    slip50_bps  = slip50_bps * 0.5  # halve depth-driven slip at half size

    net_edge_bps = gross_edge_bps - fees_bps - ob_bps - slip100_bps

    estimated_net_usd_50  = 50.0  * net_edge_bps / 10_000.0
    estimated_net_usd_100 = 100.0 * net_edge_bps / 10_000.0

    return {
        "long_rate_8h":  long_f8,
        "short_rate_8h": short_f8,
        "funding_spread_8h": spread8,
        "fees_bps":          round(fees_bps, 3),
        "spread_bps":        round(ob_bps, 3),
        "slippage_bps_50":   round(slip50_bps, 3),
        "slippage_bps_100":  round(slip100_bps, 3),
        "gross_edge_bps":    round(gross_edge_bps, 3),
        "net_edge_bps":      round(net_edge_bps, 3),
        "fee_coverage":      round(float(fee_cov), 4),
        "estimated_net_usd_50":  round(estimated_net_usd_50, 4),
        "estimated_net_usd_100": round(estimated_net_usd_100, 4),
        "pattern":           pattern,
        "price_mismatch_bps": round(float(pm_bps), 3),
    }


# ── Eligibility gate ──────────────────────────────────────────────────────

def evaluate_route(persistence: dict, edge: dict, latest: dict) -> tuple[bool, str]:
    """Return (would_build, reject_reason).  reject_reason is "" on accept."""
    if persistence["verdict"] in DISQUALIFYING_VERDICTS:
        # Map verdict to a specific reject reason where possible.
        if persistence["verdict"] == "unstable_depth":
            return False, "unstable_depth"
        if persistence["verdict"] == "spread_too_wide":
            return False, "spread_too_wide"
        if persistence["verdict"] == "fee_edge_disappears":
            return False, "fee_coverage_low"
        if persistence["verdict"] == "price_mismatch":
            return False, "price_mismatch"
        if persistence["verdict"] in ("too_thin", "transient_spike",
                                       "insufficient_n"):
            return False, "not_persistent_depth_ok"

    if persistence["verdict"] != "persistent_depth_ok":
        return False, "not_persistent_depth_ok"

    if persistence["appearances"] < MIN_APPEARANCES_PER_SYMBOL_ROUTE:
        return False, "insufficient_appearances"

    if persistence["depth_ok_rate"] < MIN_DEPTH_OK_RATE:
        return False, "depth_ok_rate_low"

    mfc = persistence.get("median_fee_coverage") or 0
    if mfc < MIN_FEE_COVERAGE:
        return False, "fee_coverage_low"

    mob = persistence.get("median_ob_spread_bps")
    if mob is None or mob > MAX_ORDERBOOK_SPREAD_BPS:
        return False, "spread_too_wide"

    if edge.get("long_rate_8h") is None or edge.get("short_rate_8h") is None:
        return False, "missing_leg_data"

    if (edge.get("price_mismatch_bps") or 0) > 50.0:
        return False, "price_mismatch"

    s50  = edge.get("slippage_bps_50") or 0
    s100 = edge.get("slippage_bps_100") or 0
    if s50 > MAX_SLIPPAGE_50_BPS or s100 > MAX_SLIPPAGE_100_BPS:
        return False, "slippage_too_high"

    if (edge.get("net_edge_bps") or 0) <= 0:
        return False, "net_edge_nonpositive"

    return True, ""


# ── Record builder + writer ───────────────────────────────────────────────

def build_record(symbol: str, route: tuple[str, str], latest: dict,
                 persistence: dict, edge: dict, decision: str,
                 reject_reason: Optional[str],
                 run_id: Optional[str] = None) -> dict:
    now = time.time()
    return {
        "ts":     round(now, 3),
        "ts_iso": _utc_iso(now),
        "run_id": run_id or "",
        "symbol": symbol,
        "mode":   "diagnostic",
        "active_trading": False,
        "paper_trade":    False,
        "executor_emit":  False,
        "selected_route": {
            "long_venue":  route[0],
            "short_venue": route[1],
            "long_symbol":  latest.get("symbol") or f"{symbol}USDT",
            "short_symbol": latest.get("symbol") or f"{symbol}USDT",
        },
        "funding": {
            "long_rate_8h":      edge["long_rate_8h"],
            "short_rate_8h":     edge["short_rate_8h"],
            "funding_spread_8h": edge["funding_spread_8h"],
            "pattern":           edge["pattern"],
        },
        "costs": {
            "fees_bps":         edge["fees_bps"],
            "spread_bps":       edge["spread_bps"],
            "slippage_bps_50":  edge["slippage_bps_50"],
            "slippage_bps_100": edge["slippage_bps_100"],
        },
        "edge": {
            "gross_edge_bps":         edge["gross_edge_bps"],
            "net_edge_bps":           edge["net_edge_bps"],
            "fee_coverage":           edge["fee_coverage"],
            "estimated_net_usd_50":   edge["estimated_net_usd_50"],
            "estimated_net_usd_100":  edge["estimated_net_usd_100"],
        },
        "persistence": persistence,
        "decision": decision,
        "reject_reason": reject_reason,
    }


def append_diagnostic(rec: dict, path: Optional[Path] = None) -> bool:
    p = path if path is not None else DIAG_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:
        log.debug("[spread_builder] diag write failed: %s (path=%s)", exc, p)
        return False


def append_summary(rec: dict, path: Optional[Path] = None) -> bool:
    p = path if path is not None else SUMMARY_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:
        log.debug("[spread_builder] summary write failed: %s (path=%s)", exc, p)
        return False


# ── Sprint 58 — sync orderbook fetch for aggressive paper bridge ────────
#
# Reads top of orderbook from each venue.  Returns LegQuote-shaped dict
# or None on any error.  Uses httpx sync — aiohttp had transient DNS
# issues in this Python install; httpx works reliably.  Short 2s timeout
# avoids stalling the spread_builder cycle.

def _fetch_quote_sync(venue: str, symbol: str,
                       leg_kind: str = "perp") -> Optional[dict]:
    """Fetch best bid/ask for one venue+symbol leg synchronously.

    Returns {venue, symbol, best_bid, best_ask, bid_size, ask_size,
              snapshot_ts, source} or None.
    """
    try:
        import httpx as _httpx
    except Exception:
        return None

    venue = (venue or "").lower()
    try:
        if venue == "binance":
            url = ("https://fapi.binance.com/fapi/v1/depth"
                    if leg_kind == "perp"
                    else "https://api.binance.com/api/v3/depth")
            r = _httpx.get(url, params={"symbol": symbol, "limit": 10},
                            timeout=2.0)
            if r.status_code != 200:
                return None
            d = r.json()
            bids = d.get("bids") or []
            asks = d.get("asks") or []
        elif venue == "bybit":
            cat = "linear" if leg_kind == "perp" else "spot"
            r = _httpx.get(
                "https://api.bybit.com/v5/market/orderbook",
                params={"category": cat, "symbol": symbol, "limit": 10},
                timeout=2.0)
            if r.status_code != 200:
                return None
            d = r.json()
            if d.get("retCode") != 0:
                return None
            res = d.get("result") or {}
            bids = res.get("b") or []
            asks = res.get("a") or []
        elif venue == "mexc":
            if leg_kind == "perp":
                r = _httpx.get(
                    f"https://contract.mexc.com/api/v1/contract/depth/"
                    f"{symbol}",
                    params={"limit": 10}, timeout=2.0)
                if r.status_code != 200:
                    return None
                d = r.json()
                if not d.get("success"):
                    return None
                data = d.get("data") or {}
                bids = data.get("bids") or []
                asks = data.get("asks") or []
            else:
                r = _httpx.get("https://api.mexc.com/api/v3/depth",
                                params={"symbol": symbol, "limit": 10},
                                timeout=2.0)
                if r.status_code != 200:
                    return None
                d = r.json()
                bids = d.get("bids") or []
                asks = d.get("asks") or []
        elif venue == "bitget":
            if leg_kind == "perp":
                url = ("https://api.bitget.com/api/v2/mix/market/"
                        "merge-depth")
                params = {"productType": "USDT-FUTURES",
                           "symbol":      symbol,
                           "precision":   "scale0",
                           "limit":       "10"}
            else:
                url = ("https://api.bitget.com/api/v2/spot/market/"
                        "merge-depth")
                params = {"symbol": symbol, "precision": "scale0",
                           "limit": "10"}
            r = _httpx.get(url, params=params, timeout=2.0)
            if r.status_code != 200:
                return None
            d = r.json()
            if d.get("code") != "00000":
                return None
            data = d.get("data") or {}
            bids = data.get("bids") or []
            asks = data.get("asks") or []
        else:
            return None

        if not bids or not asks:
            return None
        best_bid = bids[0]
        best_ask = asks[0]
        # Parse [price_str, size_str]
        bb_price = float(best_bid[0])
        ba_price = float(best_ask[0])
        bb_size  = float(best_bid[1]) if len(best_bid) > 1 else 0.0
        ba_size  = float(best_ask[1]) if len(best_ask) > 1 else 0.0
        if bb_price <= 0 or ba_price <= 0:
            return None
        if ba_price <= bb_price:
            return None  # crossed quote
        return {
            "venue":       venue,
            "symbol":      symbol,
            "best_bid":    bb_price,
            "best_ask":    ba_price,
            "bid_size":    bb_size,
            "ask_size":    ba_size,
            "snapshot_ts": time.time(),
            "source":      "realtime_orderbook",
        }
    except Exception:
        return None


# ── Main entry point ──────────────────────────────────────────────────────

def _generate_run_id(ts: Optional[float] = None) -> str:
    """Stable identifier for one builder run.

    Format: ``sb-{epoch_int}-{4hex}`` where the hex is a deterministic
    hash of the epoch (no random calls — keeps the journal reproducible).
    """
    t = ts if ts is not None else time.time()
    epoch = int(t)
    suffix = hashlib.sha1(str(epoch).encode("ascii")).hexdigest()[:4]
    return f"sb-{epoch}-{suffix}"


def run_once(*, low_vol_records: Optional[list] = None,
             write: bool = True,
             run_id: Optional[str] = None) -> dict:
    """One pass over the low-volume shadow data.

    Returns:
        {would_build: int, rejects: int, by_symbol: {...},
         by_pattern: {...}, by_reject_reason: {...}, records: [...],
         median_net_edge_bps: float, median_fee_coverage: float}

    NEVER raises — on any internal error returns a summary with
    `errors >= 1`.  Tests may pass `low_vol_records` to inject fixture
    data instead of reading from disk.
    """
    errors = 0
    run_id = run_id or _generate_run_id()
    try:
        records = (low_vol_records if low_vol_records is not None
                   else _safe_load_jsonl(LOW_VOL_FILE))
        grouped = group_shadow_by_route(records)
    except Exception as exc:
        log.warning("[spread_builder] grouping failed: %s", exc)
        es = _empty_summary(errors=1)
        es["run_id"] = run_id
        if write:
            append_summary(es)
        return es

    accepted: list[dict] = []
    rejected: list[dict] = []
    seen_routes: set = set()

    # Sort routes: dominant routes first by appearance count (stable order)
    for (base, le, se), recs in sorted(
        grouped.items(), key=lambda kv: -len(kv[1])
    ):
        try:
            route_key = (base, le, se)
            persistence = classify_route_persistence(recs)
            latest = max(recs, key=lambda r: r.get("ts") or 0)
            edge = compute_edge(latest)

            # Dedup: same (symbol, route) twice in one run → second is dup.
            if route_key in seen_routes:
                rec = build_record(base, (le, se), latest, persistence, edge,
                                   decision="reject",
                                   reject_reason="duplicate_route",
                                   run_id=run_id)
                rejected.append(rec)
                if write:
                    append_diagnostic(rec)
                continue
            seen_routes.add(route_key)

            ok, reason = evaluate_route(persistence, edge, latest)
            decision = "would_build" if ok else "reject"
            reject_reason = None if ok else reason
            rec = build_record(base, (le, se), latest, persistence, edge,
                               decision=decision,
                               reject_reason=reject_reason,
                               run_id=run_id)
            if ok:
                accepted.append(rec)
                # Sprint 55: pipe every accepted spread-builder candidate
                # to the aggressive funding paper lane.  Lane is gated by
                # AGGRESSIVE_FUNDING_PAPER_ENABLED env flag — when unset,
                # evaluate_candidate is a no-op.  Crypto-only via shared
                # asset_kind classifier (funding_carry/asset_kind.py).
                # Bid/ask quote propagation deferred to Sprint 56; for now
                # candidates without orderbook snapshots will reject as
                # missing_orderbook at the atomic-fill simulator gate.
                try:
                    from ..aggressive_paper import (
                        evaluate_candidate as _agg_eval,
                        is_enabled as _agg_on,
                    )
                    if _agg_on():
                        # Sprint 58: real-time orderbook fetch.
                        # Uses sync httpx (the bot already routes
                        # successful HTTP via httpx — aiohttp had DNS
                        # resolver issues in standalone test under this
                        # Python install).  Runs inside a worker thread
                        # (run_once is dispatched via asyncio.to_thread
                        # by the scheduler loop), so sync HTTP is safe.
                        # No fabricated mid-price quotes — fetch failure
                        # propagates as None → existing `missing_orderbook`
                        # reject path.
                        _long_q, _short_q, _quote_skew = (None, None, None)
                        try:
                            _route = (rec.get("selected_route")
                                       or {}) if rec else {}
                            _long_sym  = _route.get("long_symbol",
                                                     f"{base}USDT")
                            _short_sym = _route.get("short_symbol",
                                                     f"{base}USDT")
                            _long_q  = _fetch_quote_sync(
                                le, _long_sym, "perp")
                            _short_q = _fetch_quote_sync(
                                se, _short_sym, "perp")
                            if (_long_q is not None
                                    and _short_q is not None):
                                _quote_skew = abs(
                                    _long_q["snapshot_ts"]
                                    - _short_q["snapshot_ts"]
                                ) * 1000.0
                        except Exception as _ox:
                            log.debug(
                                "[spread_builder] orderbook fetch fail "
                                "for %s: %s", base, _ox)
                        # Sprint 56: wire real fields from shadow record.
                        # `latest` is a low_volume_funding_shadow.jsonl row
                        # with: min_leg_volume_24h_usd, long_funding_8h,
                        # short_funding_8h, depth_slip_*_bps,
                        # orderbook_spread_bps, mark_spread_bps,
                        # validation_verdict, fee_coverage_ratio,
                        # fees_roundtrip_frac.
                        _missing: list[str] = []
                        _derived: list[str] = []

                        _vol = latest.get("min_leg_volume_24h_usd")
                        if _vol is None:
                            _missing.append("volume_24h_usd")

                        # depth_usd_100bps is not in shadow; use the
                        # upstream validation_verdict as authoritative
                        # ("depth_ok" → upstream proved $100 trade can
                        # be filled without exceeding slip thresholds).
                        _vv = latest.get("validation_verdict") or ""
                        _depth_proxy = None
                        if _vv == "depth_ok":
                            # mark as derived; aggressive paper treats
                            # this as "depth check delegated upstream".
                            _depth_proxy = "delegated_depth_ok"
                            _derived.append("depth_usd_100bps")
                        else:
                            _missing.append("depth_usd_100bps")

                        # Sprint 58: real-time orderbook fetch now
                        # populates bid/ask/snapshot_ts.  If fetch failed
                        # (timeout / API error / empty orderbook /
                        # crossed quote), _long_q or _short_q remains
                        # None and the agg gate fires `missing_orderbook`
                        # with the missing_fields list still populated.
                        if _long_q is None or _short_q is None:
                            _missing.extend([
                                "long_bid", "long_ask",
                                "short_bid", "short_ask",
                                "long_orderbook_ts",
                                "short_orderbook_ts",
                                "quote_skew_ms",
                            ])
                        else:
                            _derived.append("realtime_orderbook")

                        _agg_eval({
                            "symbol":              base,
                            "volume_24h_usd":      (
                                float(_vol) if _vol is not None else None),
                            "fee_coverage":        float(
                                edge.get("fee_coverage") or 0),
                            "net_edge_bps":        float(
                                edge.get("net_edge_bps") or 0),
                            "ob_spread_bps":       float(
                                edge.get("spread_bps") or 0),
                            # depth_usd_100bps: use sentinel string
                            # "delegated_depth_ok" to indicate upstream
                            # proxy; None when not validated upstream.
                            "depth_usd_100bps":    _depth_proxy,
                            "depth_slip_long_bps":  latest.get(
                                "depth_slip_long_bps"),
                            "depth_slip_short_bps": latest.get(
                                "depth_slip_short_bps"),
                            "depth_usd_target":    float(latest.get(
                                "depth_usd_target") or 100.0),
                            "long_funding_8h":     edge.get("long_rate_8h"),
                            "short_funding_8h":    edge.get("short_rate_8h"),
                            "funding_spread_8h":   edge.get(
                                "funding_spread_8h"),
                            "fees_bps":            float(
                                edge.get("fees_bps") or 0),
                            "slippage_bps_50":     float(
                                edge.get("slippage_bps_50") or 0),
                            "slippage_bps_100":    float(
                                edge.get("slippage_bps_100") or 0),
                            "mark_spread_bps":     latest.get(
                                "mark_spread_bps"),
                            "persistence_verdict": (persistence.get(
                                "verdict") or ""),
                            "price_mismatch_bps":  float(
                                edge.get("price_mismatch_bps") or 0),
                            "notional_usd":        100.0,
                            "route": {"long_venue": le,
                                      "short_venue": se},
                            "validation_verdict":  _vv,
                            # Sprint 58 realtime quotes
                            "long_venue":          le,
                            "short_venue":         se,
                            "long_quote":          _long_q,
                            "short_quote":         _short_q,
                            "quote_skew_ms":       _quote_skew,
                            "data_quality": {
                                "missing_fields": _missing,
                                "derived_fields": _derived,
                            },
                        })
                except Exception as _ax:
                    log.debug(
                        "[spread_builder] aggressive bridge fail: %s", _ax)
            else:
                rejected.append(rec)
            if write:
                append_diagnostic(rec)
        except Exception as exc:
            errors += 1
            log.debug("[spread_builder] route %s failed: %s",
                      (base, le, se), exc)

    # ── Summary ───────────────────────────────────────────────────────
    by_sym_accept = Counter(r["symbol"] for r in accepted)
    by_pattern    = Counter(r["funding"]["pattern"] for r in accepted)
    by_reject     = Counter(r["reject_reason"] for r in rejected)

    net_edges = [r["edge"]["net_edge_bps"] for r in accepted]
    fcs       = [r["edge"]["fee_coverage"] for r in accepted]
    top_ops = sorted(accepted, key=lambda r: -(r["edge"]["net_edge_bps"]
                                                or 0))[:8]

    summary = {
        "ts":     round(time.time(), 3),
        "ts_iso": _utc_iso(),
        "run_id": run_id,
        "source": "spread_builder_summary",
        "candidates":  len(accepted) + len(rejected),
        "would_build": len(accepted),
        "rejects":     len(rejected),
        "by_symbol_accepted": dict(by_sym_accept),
        "by_pattern_accepted": dict(by_pattern),
        "by_reject_reason":   dict(by_reject),
        "median_net_edge_bps": _med(net_edges),
        "median_fee_coverage": _med(fcs),
        "top_opportunities": [
            {
                "symbol":           t["symbol"],
                "route":            t["selected_route"],
                "net_edge_bps":     t["edge"]["net_edge_bps"],
                "fee_coverage":     t["edge"]["fee_coverage"],
                "pattern":          t["funding"]["pattern"],
                "depth_ok_rate":    t["persistence"]["depth_ok_rate"],
                "appearances":      t["persistence"]["appearances"],
            } for t in top_ops
        ],
        "errors":         errors,
        "mode":           "diagnostic",
        "active_trading": False,
        "paper_trade":    False,
        "executor_emit":  False,
    }
    if write:
        append_summary(summary)
    summary["records"] = accepted + rejected  # for tests / programmatic use
    return summary


def _empty_summary(errors: int = 0) -> dict:
    return {
        "ts":     round(time.time(), 3),
        "ts_iso": _utc_iso(),
        "source": "spread_builder_summary",
        "candidates": 0, "would_build": 0, "rejects": 0,
        "by_symbol_accepted": {}, "by_pattern_accepted": {},
        "by_reject_reason": {},
        "median_net_edge_bps": None, "median_fee_coverage": None,
        "top_opportunities": [],
        "errors": errors,
        "mode": "diagnostic", "active_trading": False,
        "paper_trade": False, "executor_emit": False,
        "records": [],
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    s = run_once()
    print(f"[spread_builder] candidates={s['candidates']} "
          f"would_build={s['would_build']} rejects={s['rejects']} "
          f"errors={s['errors']}")
    if s.get("by_symbol_accepted"):
        print(f"  by_symbol_accepted: {s['by_symbol_accepted']}")
    if s.get("by_pattern_accepted"):
        print(f"  by_pattern_accepted: {s['by_pattern_accepted']}")
    print(f"  median_net_edge_bps: {s.get('median_net_edge_bps')}")
    print(f"  median_fee_coverage: {s.get('median_fee_coverage')}")
    if s.get("top_opportunities"):
        print("  top opportunities:")
        for t in s["top_opportunities"]:
            r = t["route"]
            print(f"    {t['symbol']:>10} {r['long_venue']}->{r['short_venue']:7} "
                  f"net_edge={t['net_edge_bps']:.1f}bps "
                  f"fee_cov={t['fee_coverage']:.2f} pattern={t['pattern']} "
                  f"depth_ok={int(t['depth_ok_rate']*100):>3}% "
                  f"apps={t['appearances']}")
    print(f"  by_reject_reason: {s.get('by_reject_reason')}")
    return 0


# ── Sprint 40 — recurring diagnostic loop ────────────────────────────────

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}


def get_interval_s() -> float:
    """Resolve interval at call time (testable).

    Reads CARRY_SPREAD_BUILDER_INTERVAL_S; falls back to DEFAULT_INTERVAL_S;
    clamps below MIN_INTERVAL_S; falls back to default on parse error.
    """
    raw = os.environ.get("CARRY_SPREAD_BUILDER_INTERVAL_S")
    if raw:
        try:
            v = float(raw)
            if v < MIN_INTERVAL_S:
                log.warning(
                    "[spread_builder_sched] env interval %.0fs < min %.0fs"
                    " — clamping to min", v, MIN_INTERVAL_S)
                return float(MIN_INTERVAL_S)
            return v
        except ValueError:
            log.warning(
                "[spread_builder_sched] bad CARRY_SPREAD_BUILDER_INTERVAL_S"
                "=%r — fallback to default", raw)
    return float(DEFAULT_INTERVAL_S)


class _SingletonGuard:
    def __init__(self):
        self._active = False

    def acquire(self) -> bool:
        if self._active:
            return False
        self._active = True
        return True

    def release(self) -> None:
        self._active = False

    @property
    def active(self) -> bool:
        return self._active


_LOOP_GUARD = _SingletonGuard()


def is_loop_active() -> bool:
    return _LOOP_GUARD.active


def reset_guard_for_tests() -> None:
    _LOOP_GUARD.release()


async def spread_builder_loop(stop_event: asyncio.Event,
                              interval_s: Optional[float] = None,
                              builder_fn: Optional[Callable[[], dict]] = None,
                              run_immediately: bool = False) -> None:
    """Recurring scheduler — drop into main bot's asyncio.create_task().

    Single-instance guarded.  Offloads each builder step to a worker thread
    so the bot's event loop is never blocked.  All exceptions inside the
    step are swallowed by run_once(); to_thread itself is also wrapped.
    Stops cleanly when stop_event is set.

    Args:
        stop_event:    cooperative cancellation.
        interval_s:    explicit override; otherwise resolved via env/default.
        builder_fn:    test stub; production uses run_once.
        run_immediately: kick the first run at t0 instead of t0 + interval.
    """
    if not _LOOP_GUARD.acquire():
        log.info("[spread_builder_sched] already active — refusing duplicate")
        return
    iv = float(interval_s) if interval_s is not None else get_interval_s()
    if iv < MIN_INTERVAL_S:
        iv = float(MIN_INTERVAL_S)
    fn = builder_fn or (lambda: run_once(write=True))
    log.info("[spread_builder_sched] starting — interval=%.0fs (run_imm=%s)",
             iv, run_immediately)
    try:
        if run_immediately:
            try:
                await asyncio.to_thread(fn)
            except Exception as exc:
                log.warning("[spread_builder_sched] immediate run failed: %s",
                            exc)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=iv)
                return  # stop requested
            except asyncio.TimeoutError:
                pass
            try:
                await asyncio.to_thread(fn)
            except Exception as exc:
                log.warning("[spread_builder_sched] step failed: %s", exc)
    finally:
        _LOOP_GUARD.release()
        log.info("[spread_builder_sched] stopped")


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())
