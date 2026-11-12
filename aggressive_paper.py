"""
funding_carry/aggressive_paper.py

Sprint 51 (2026-06-08) — Aggressive Funding Paper Lane.

DESIGN ONLY.  DISABLED BY DEFAULT.

Sprint 50 audit showed the conservative FC strategy executes ~1 trade/week
with a 100% loss rate (25/25 trades, total realized PnL ≈ −$89).  This
gives almost no data for evaluation.  We need MORE trades while keeping
risk strictly bounded — but we can't loosen the conservative strategy
because that would also loosen its safety profile.

This module implements a **completely isolated** aggressive paper lane:
  * separate ledger (NEVER touches `paper_book.json`)
  * separate events file
  * separate trades file
  * separate summary file
  * crypto-only universe (hard-blocks the equity-perp universe
    contamination seen in Sprint 50: SOXL/SKHYNIX/INTC/TSM/EWY)
  * realistic atomic two-leg execution simulation
  * 2x default / 3x max paper leverage
  * portfolio + per-position kill-switches

Activation requires a future explicit sprint:
    AGGRESSIVE_FUNDING_PAPER_ENABLED=true

Safety contract
---------------
  * NEVER calls executor / order placement / live API
  * NEVER mutates the main FC PaperBook (`paper_book.json`)
  * NEVER mutates conservative FC config (vol floor, funding threshold)
  * NEVER opens an executor signal bus event
  * NEVER raises into the caller — all exceptions swallowed
  * When the env flag is unset, `evaluate_candidate()` is a no-op that
    returns `{"action": "disabled"}`
"""
from __future__ import annotations

import json
import logging
import os
import re
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("funding_carry.aggressive")

_HERE  = Path(__file__).resolve().parent
_STATE = _HERE / "state"

#: Isolated state files.  These names are deliberately distinct from
#: the conservative `paper_book.json` / `trades.jsonl`.
LEDGER_FILE   = _STATE / "aggressive_paper_book.json"
EVENTS_FILE   = _STATE / "aggressive_paper_events.jsonl"
TRADES_FILE   = _STATE / "aggressive_paper_trades.jsonl"
SUMMARY_FILE  = _STATE / "aggressive_paper_summary.jsonl"

ENV_FLAG_NAME = "AGGRESSIVE_FUNDING_PAPER_ENABLED"

# ── Relaxed-but-bounded thresholds (chosen profile: BALANCED-AGGRESSIVE) ─

#: Universe / depth gate.
MIN_VOLUME_24H_USD    = 500_000.0   # 1/20th of conservative $10M floor
MIN_DEPTH_USD_100BPS  = 15_000.0
MIN_DEPTH_USD_50BPS   = 7_500.0
MIN_FEE_COVERAGE      = 1.30
MIN_NET_EDGE_BPS      = 6.0
MAX_ORDERBOOK_SPREAD_BPS = 25.0
MAX_SLIPPAGE_BPS_PER_LEG = 15.0
MAX_ENTRY_BASIS_BPS   = 30.0
MAX_QUOTE_SKEW_MS     = 1500.0

#: Sizing.
MAX_POSITION_NOTIONAL_USD = 100.0
DEFAULT_LEVERAGE          = 2.0
MAX_LEVERAGE              = 3.0
MARGIN_PER_TRADE_USD      = 15.0    # 100 notional / 6.67 leverage ≈ $15
START_BALANCE_USD         = 100.0
MAX_OPEN_POSITIONS        = 2
MAX_NEW_POSITIONS_PER_DAY = 10
MAX_HOLD_HOURS            = 12

#: Per-position stops.
MAX_BASIS_DRIFT_BPS               = 60.0
MAX_UNREALIZED_LOSS_PCT_ON_MARGIN = 20.0
MAX_UNREALIZED_LOSS_USD           = 3.0
MAX_QUOTE_STALE_S                 = 20.0
CLOSE_IF_FUNDING_FLIPS            = True
CLOSE_IF_NET_EDGE_DECAYS_BELOW_ZERO = True

#: Portfolio kill-switches.
DAILY_LOSS_CAP_USD          = -10.0
MAX_DRAWDOWN_PCT            = 5.0
CONSECUTIVE_HARD_LOSS_HALT  = 3
ROLLING_WINDOW_TRADES       = 20
ROLLING_MEDIAN_HALT_AT      = 0.0
ROLLING_WINRATE_HALT_AT_PCT = 45.0
ROLLING_AVG_NET_HALT_AT     = 0.0
HALT_REASONS = (
    "rolling_median_le_zero",
    "rolling_winrate_below_45",
    "rolling_avg_pnl_below_zero",
    "three_consecutive_hard_losses",
    "asset_contamination_detected",
    "one_leg_exposure_detected",
    "daily_loss_cap",
    "drawdown_gt_5pct",
    "max_trades_per_day",
    "main_paper_book_drift",  # never expected; defensive
    "manual_halt",
)

# ── Crypto-only asset filter (Sprint 51 §6, unified Sprint 55) ──────────
#
# Sprint 55: classifier is now the shared funding_carry/asset_kind.py
# module.  The previous local EQUITY_TICKER_BLOCKLIST duplicate has been
# removed to prevent drift between this module and the conservative FC
# paper gate (main.py).  All v1.3 + v1.4 additions (UVXY/VXX/CIEN/ZM/
# DKNG/OUST/PL/EBAY/AMC-ambig/SATS-ambig) are inherited automatically.

from . import asset_kind as _ak_mod

# Backwards-compat re-exports for existing Sprint 51 tests.
EQUITY_TICKER_BLOCKLIST = _ak_mod.EQUITY_TICKER_BLOCKLIST
EQUITY_SUFFIX_RE        = _ak_mod._EQUITY_SUFFIX_RE


def classify_asset_kind(symbol: str, title_or_label: str = "") -> tuple[str, str]:
    """Delegates to shared funding_carry/asset_kind.classify_asset_kind."""
    return _ak_mod.classify_asset_kind(symbol, title_or_label)


# ── Env flag gate ────────────────────────────────────────────────────────

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}


def is_enabled() -> bool:
    """Read flag at call time. Default unset → False."""
    return os.environ.get(ENV_FLAG_NAME, "").strip().lower() in _TRUTHY


def reset_state_for_tests() -> None:
    """Test hook — clear module-level state."""
    _ROLLING_RESULTS.clear()
    _PER_SYMBOL_COOLDOWN.clear()


# ── In-memory rolling stats ──────────────────────────────────────────────

_ROLLING_RESULTS: deque = deque(maxlen=ROLLING_WINDOW_TRADES)
_PER_SYMBOL_COOLDOWN: dict = {}


def _utc_iso(ts: Optional[float] = None) -> str:
    try:
        t = ts if ts is not None else time.time()
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")
    except Exception:
        return str(ts or time.time())


def _utc_date_str(ts: Optional[float] = None) -> str:
    t = ts if ts is not None else time.time()
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")


# ── Ledger I/O (isolated) ───────────────────────────────────────────────

def _fresh_ledger() -> dict:
    return {
        "enabled": False,
        "start_balance_usd": float(START_BALANCE_USD),
        "balance_usd":       float(START_BALANCE_USD),
        "open_positions":    [],
        "closed_positions":  [],
        "stats": {
            "daily_pnl_usd":            0.0,
            "trades_today":             0,
            "trades_today_date":        _utc_date_str(),
            "total_pnl_usd":            0.0,
            "rolling_20_median":        None,
            "rolling_20_winrate":       None,
            "consecutive_hard_losses":  0,
            "halted":                   False,
            "halt_reason":              None,
        },
    }


def load_ledger() -> dict:
    p = LEDGER_FILE
    if not p.exists():
        return _fresh_ledger()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return _fresh_ledger()
        base = _fresh_ledger()
        base.update(d)
        if "stats" not in d:
            base["stats"] = _fresh_ledger()["stats"]
        return base
    except Exception:
        return _fresh_ledger()


def save_ledger(ledger: dict) -> bool:
    try:
        LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LEDGER_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(LEDGER_FILE)
        return True
    except Exception:
        return False


# ── Event / summary writers ─────────────────────────────────────────────

def _append_jsonl(path: Path, rec: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def log_event(event: str, *, decision: str = "",
              symbol: str = "", asset_kind: str = "",
              reason: str = "", route: Optional[dict] = None,
              entry: Optional[dict] = None, exit_: Optional[dict] = None,
              risk: Optional[dict] = None, pnl: Optional[dict] = None,
              leverage: Optional[float] = None,
              margin_usd: Optional[float] = None,
              notional_usd: Optional[float] = None,
              data_quality: Optional[dict] = None) -> bool:
    rec = {
        "ts":            round(time.time(), 3),
        "ts_iso":        _utc_iso(),
        "lane":          "funding_aggressive_paper",
        "mode":          "paper",
        "active_trading": False,
        "live_trading":  False,
        "paper_trade":   True,
        "executor_emit": False,
        "event":         event,
        "decision":      decision,
        "symbol":        symbol[:32],
        "asset_kind":    asset_kind[:16],
        "reason":        reason[:200],
        "route":         route or {},
        "entry":         entry or {},
        "exit":          exit_ or {},
        "risk":          risk or {},
        "pnl":           pnl or {},
        "leverage":      leverage,
        "margin_usd":    margin_usd,
        "notional_usd":  notional_usd,
        # Sprint 56: surface missing/derived field provenance.
        "data_quality":  data_quality or {},
    }
    return _append_jsonl(EVENTS_FILE, rec)


# ── Atomic two-leg execution simulation ──────────────────────────────────

@dataclass
class LegQuote:
    """One leg's snapshot used to simulate the open / close fill."""
    venue:            str
    best_bid:         float
    best_ask:         float
    bid_size:         float = 0.0
    ask_size:         float = 0.0
    snapshot_ts:      float = 0.0  # epoch seconds
    funding_8h:       Optional[float] = None
    taker_fee_bps:    float = 5.0


def _mid(q: LegQuote) -> float:
    if q.best_bid <= 0 or q.best_ask <= 0:
        return 0.0
    return 0.5 * (q.best_bid + q.best_ask)


def _ob_spread_bps(q: LegQuote) -> float:
    m = _mid(q)
    if m <= 0:
        return float("inf")
    return abs(q.best_ask - q.best_bid) / m * 10_000.0


def simulate_two_leg_entry(long_q: LegQuote, short_q: LegQuote,
                           notional_usd: float,
                           max_quote_skew_ms: float = MAX_QUOTE_SKEW_MS,
                           max_entry_basis_bps: float = MAX_ENTRY_BASIS_BPS,
                           max_slippage_bps_per_leg: float = MAX_SLIPPAGE_BPS_PER_LEG,
                           ) -> dict:
    """Simulate atomic open of both legs.

    Returns dict with `accepted` (bool) and `reason` plus all entry
    diagnostics.  When `accepted=False`, reason explains.
    """
    out: dict[str, Any] = {
        "accepted":        False,
        "reason":          "",
        "entry_long_price":  None,
        "entry_short_price": None,
        "entry_basis_bps":   None,
        "quote_skew_ms":     None,
        "fill_ratio_long":   None,
        "fill_ratio_short":  None,
        "entry_slippage_bps_long":  None,
        "entry_slippage_bps_short": None,
        "hedge_ratio":       1.0,
        "fees_usd":          0.0,
    }
    if not long_q or not short_q:
        out["reason"] = "missing_orderbook"
        return out
    if (long_q.best_ask <= 0 or short_q.best_bid <= 0
            or long_q.best_bid <= 0 or short_q.best_ask <= 0):
        out["reason"] = "missing_orderbook"
        return out
    skew_ms = abs(long_q.snapshot_ts - short_q.snapshot_ts) * 1000.0
    out["quote_skew_ms"] = round(skew_ms, 1)
    if skew_ms > max_quote_skew_ms:
        out["reason"] = "quote_skew_too_high"
        return out
    # Stale-quote check (treat snapshot older than 30s as stale).
    now = time.time()
    if (now - long_q.snapshot_ts) > 30 or (now - short_q.snapshot_ts) > 30:
        out["reason"] = "quote_stale"
        return out

    # Long leg fills at best_ask + depth slippage
    long_mid = _mid(long_q)
    short_mid = _mid(short_q)
    if long_mid <= 0 or short_mid <= 0:
        out["reason"] = "missing_orderbook"
        return out

    # Approximate slippage as proportional to depth shortfall.
    long_size_usd_avail = long_q.ask_size * long_q.best_ask
    short_size_usd_avail = short_q.bid_size * short_q.best_bid
    fill_long_usd = min(long_size_usd_avail, notional_usd) if long_size_usd_avail > 0 else notional_usd
    fill_short_usd = min(short_size_usd_avail, notional_usd) if short_size_usd_avail > 0 else notional_usd
    out["fill_ratio_long"]  = round(fill_long_usd / notional_usd, 4)
    out["fill_ratio_short"] = round(fill_short_usd / notional_usd, 4)

    if out["fill_ratio_long"] < 0.95 or out["fill_ratio_short"] < 0.95:
        out["reason"] = "one_leg_unfilled" if (out["fill_ratio_long"] < 0.95 and out["fill_ratio_short"] >= 0.95) \
                        or (out["fill_ratio_short"] < 0.95 and out["fill_ratio_long"] >= 0.95) \
                        else "partial_fill_mismatch"
        return out
    if abs(out["fill_ratio_long"] - out["fill_ratio_short"]) > 0.05:
        out["reason"] = "partial_fill_mismatch"
        return out

    # Slippage from mid (paying spread on entry).
    slip_long_bps  = (long_q.best_ask - long_mid)  / long_mid  * 10_000.0
    slip_short_bps = (short_mid - short_q.best_bid) / short_mid * 10_000.0
    out["entry_slippage_bps_long"]  = round(slip_long_bps, 3)
    out["entry_slippage_bps_short"] = round(slip_short_bps, 3)
    if slip_long_bps > max_slippage_bps_per_leg \
            or slip_short_bps > max_slippage_bps_per_leg:
        out["reason"] = "slippage_too_high"
        return out

    # Entry basis (relative price between legs at fill).
    entry_basis_bps = (long_q.best_ask - short_q.best_bid) / short_q.best_bid \
                      * 10_000.0
    out["entry_long_price"]  = float(long_q.best_ask)
    out["entry_short_price"] = float(short_q.best_bid)
    out["entry_basis_bps"]   = round(entry_basis_bps, 3)
    if abs(entry_basis_bps) > max_entry_basis_bps:
        out["reason"] = "entry_basis_too_high"
        return out

    # Fees (taker each leg).
    fees_usd = notional_usd * (long_q.taker_fee_bps + short_q.taker_fee_bps) \
                / 10_000.0
    out["fees_usd"] = round(fees_usd, 4)
    out["hedge_ratio"] = 1.0  # notional-balanced
    out["accepted"] = True
    out["reason"] = "ok"
    return out


# ── Per-position kill-switch (pure) ──────────────────────────────────────

@dataclass
class PositionRiskCheck:
    halt:   bool
    reason: str


def check_position_health(position: dict, current_long: LegQuote,
                          current_short: LegQuote) -> PositionRiskCheck:
    """Returns (close_required, reason).  Pure function."""
    # 1. Stale quote
    now = time.time()
    age = max(now - (current_long.snapshot_ts or 0),
              now - (current_short.snapshot_ts or 0))
    if age > MAX_QUOTE_STALE_S:
        return PositionRiskCheck(True, "quote_stale")
    # 2. Basis drift
    bas_now = ((current_long.best_bid + current_long.best_ask) / 2
               - (current_short.best_bid + current_short.best_ask) / 2)
    bas_entry = position.get("entry", {}).get("entry_basis_bps") or 0
    short_mid = (current_short.best_bid + current_short.best_ask) / 2
    if short_mid > 0:
        bas_now_bps = bas_now / short_mid * 10_000.0
        if abs(bas_now_bps - bas_entry) > MAX_BASIS_DRIFT_BPS:
            return PositionRiskCheck(True, "basis_drift_stop")
    # 3. Funding flip
    if CLOSE_IF_FUNDING_FLIPS:
        f_entry = position.get("entry", {}).get("funding_long_8h")
        f_now   = current_long.funding_8h
        if (f_entry is not None and f_now is not None
                and f_entry * f_now < 0):
            return PositionRiskCheck(True, "funding_flipped")
    # 4. Max hold
    ts_open = position.get("ts_open") or 0
    if ts_open and (now - ts_open) / 3600.0 > MAX_HOLD_HOURS:
        return PositionRiskCheck(True, "max_hold_reached")
    return PositionRiskCheck(False, "ok")


# ── Portfolio kill-switch ────────────────────────────────────────────────

def evaluate_portfolio_halt(ledger: dict,
                             incoming_asset_kind: Optional[str] = None
                             ) -> tuple[bool, str]:
    stats = ledger.get("stats") or {}
    if stats.get("halted"):
        return True, stats.get("halt_reason") or "manual_halt"
    # Daily loss cap
    if (stats.get("daily_pnl_usd") or 0) <= DAILY_LOSS_CAP_USD:
        return True, "daily_loss_cap"
    # Drawdown
    start = float(ledger.get("start_balance_usd") or START_BALANCE_USD)
    bal   = float(ledger.get("balance_usd") or 0.0)
    if start > 0 and 100 * (start - bal) / start > MAX_DRAWDOWN_PCT:
        return True, "drawdown_gt_5pct"
    # Max trades/day
    if int(stats.get("trades_today") or 0) >= MAX_NEW_POSITIONS_PER_DAY:
        return True, "max_trades_per_day"
    # 3 consecutive hard losses
    if int(stats.get("consecutive_hard_losses") or 0) \
            >= CONSECUTIVE_HARD_LOSS_HALT:
        return True, "three_consecutive_hard_losses"
    # Asset contamination protection
    if incoming_asset_kind and incoming_asset_kind != "crypto":
        return True, "asset_contamination_detected"
    # Rolling kill-switches
    if len(_ROLLING_RESULTS) >= ROLLING_WINDOW_TRADES:
        pnls = [r[0] for r in _ROLLING_RESULTS]
        med = statistics.median(pnls)
        if med <= ROLLING_MEDIAN_HALT_AT:
            return True, "rolling_median_le_zero"
        winrate = 100.0 * sum(1 for p in pnls if p > 0) / len(pnls)
        if winrate < ROLLING_WINRATE_HALT_AT_PCT:
            return True, "rolling_winrate_below_45"
        avg = statistics.fmean(pnls)
        if avg < ROLLING_AVG_NET_HALT_AT:
            return True, "rolling_avg_pnl_below_zero"
    return False, ""


# ── Leverage tier selection ─────────────────────────────────────────────

def select_leverage(*, asset_kind: str, fee_coverage: float,
                    net_edge_bps: float, ob_spread_bps: float,
                    quote_skew_ms: float, depth_usd_100bps: float,
                    persistent_route: bool = False,
                    recent_symbol_loss: bool = False,
                    price_mismatch_bps: float = 0.0,
                    equity_contamination: bool = False
                    ) -> float:
    if asset_kind != "crypto" or equity_contamination:
        return 0.0
    if (persistent_route and fee_coverage >= 3.0 and net_edge_bps >= 20
            and ob_spread_bps <= 8.0 and quote_skew_ms <= 500
            and depth_usd_100bps >= 50_000 and not recent_symbol_loss
            and price_mismatch_bps < 3.0):
        return MAX_LEVERAGE
    return DEFAULT_LEVERAGE


# ── Public entry point ──────────────────────────────────────────────────

def evaluate_candidate(candidate: dict) -> dict:
    """Public entry — evaluate one would-be spread-builder candidate.

    Returns `{"action": "opened"|"rejected"|"halted"|"disabled"|"error",
              "reason": str, "ledger_written": bool, "position": dict?}`.
    Never raises.

    `candidate` is expected to carry at minimum:
        symbol, long_venue, short_venue, fee_coverage,
        net_edge_bps, ob_spread_bps, depth_usd_100bps,
        long_quote (LegQuote-shaped dict), short_quote (LegQuote-shaped dict),
        title (optional, for classifier),
        persistence_verdict (optional),
        price_mismatch_bps (optional),
    """
    try:
        if not is_enabled():
            return {"action": "disabled", "reason": "env_flag_off",
                    "ledger_written": False}
        sym = (candidate.get("symbol") or "").upper()
        title = candidate.get("title") or ""
        asset_kind, ak_reason = classify_asset_kind(sym, title)
        if asset_kind != "crypto":
            log_event("candidate_rejected", decision="reject", symbol=sym,
                      asset_kind=asset_kind,
                      reason=f"asset_kind:{asset_kind} ({ak_reason})")
            return {"action": "rejected",
                    "reason": f"asset_kind:{asset_kind}",
                    "ledger_written": False}

        ledger = load_ledger()
        ledger["enabled"] = True

        # Portfolio gate
        halt, halt_reason = evaluate_portfolio_halt(ledger, asset_kind)
        if halt:
            ledger["stats"]["halted"] = True
            ledger["stats"]["halt_reason"] = halt_reason
            save_ledger(ledger)
            log_event("portfolio_halted", decision="halt",
                      symbol=sym, asset_kind=asset_kind, reason=halt_reason)
            return {"action": "halted", "reason": halt_reason,
                    "ledger_written": True}

        # Concurrency
        if len(ledger.get("open_positions") or []) >= MAX_OPEN_POSITIONS:
            log_event("candidate_rejected", decision="reject",
                      symbol=sym, asset_kind=asset_kind,
                      reason="max_open_positions")
            return {"action": "rejected", "reason": "max_open_positions",
                    "ledger_written": False}

        # Per-symbol cooldown after loss
        cd = _PER_SYMBOL_COOLDOWN.get(sym, 0)
        if cd and cd > time.time():
            log_event("candidate_rejected", decision="reject",
                      symbol=sym, asset_kind=asset_kind,
                      reason="symbol_cooldown_after_loss")
            return {"action": "rejected", "reason": "symbol_cooldown_after_loss",
                    "ledger_written": False}

        # Sprint 56: pass data_quality through to all events.
        _dq = candidate.get("data_quality") or {}

        # Threshold gates — distinguish missing vs low (Sprint 56 §5).
        _vol = candidate.get("volume_24h_usd")
        if _vol is None:
            return _reject(sym, asset_kind, "missing_volume_24h",
                            data_quality=_dq)
        if float(_vol) < MIN_VOLUME_24H_USD:
            return _reject(sym, asset_kind, "volume_too_low",
                            data_quality=_dq)

        if (candidate.get("fee_coverage") or 0) < MIN_FEE_COVERAGE:
            return _reject(sym, asset_kind, "fee_coverage_low",
                            data_quality=_dq)
        if (candidate.get("net_edge_bps") or 0) < MIN_NET_EDGE_BPS:
            return _reject(sym, asset_kind, "net_edge_nonpositive",
                            data_quality=_dq)
        if (candidate.get("ob_spread_bps") or 9999) > MAX_ORDERBOOK_SPREAD_BPS:
            return _reject(sym, asset_kind, "spread_too_wide",
                            data_quality=_dq)

        # Depth — three cases:
        #   * "delegated_depth_ok" sentinel: upstream validation_verdict
        #     proved $100 trade fits within slip thresholds → accept
        #   * numeric value < MIN: depth_too_low
        #   * None / 0: missing_depth (or upstream rejected depth)
        _depth = candidate.get("depth_usd_100bps")
        if _depth == "delegated_depth_ok":
            pass  # accepted via upstream
        elif _depth is None:
            return _reject(sym, asset_kind, "missing_depth",
                            data_quality=_dq)
        elif float(_depth) < MIN_DEPTH_USD_100BPS:
            return _reject(sym, asset_kind, "depth_too_low",
                            data_quality=_dq)

        # Funding data must be present.
        long_funding_8h = candidate.get("long_funding_8h")
        short_funding_8h = candidate.get("short_funding_8h")
        if long_funding_8h is None or short_funding_8h is None:
            return _reject(sym, asset_kind, "missing_funding_data",
                            data_quality=_dq)

        # Sprint 56: bid/ask still missing — explicit reject before the
        # simulator tries to coerce None into a quote.  This produces
        # the accurate `missing_orderbook` reject reason for crypto
        # candidates with valid funding/volume/depth but no real-time
        # orderbook snapshot (Sprint 57 wires real-time fetcher).
        if (candidate.get("long_quote") is None
                or candidate.get("short_quote") is None):
            return _reject(sym, asset_kind, "missing_orderbook",
                            data_quality=_dq)

        # Atomic two-leg simulation
        long_q  = _coerce_quote(candidate.get("long_quote"),
                                 funding_8h=long_funding_8h)
        short_q = _coerce_quote(candidate.get("short_quote"),
                                 funding_8h=short_funding_8h)
        notional = float(candidate.get("notional_usd",
                                        MAX_POSITION_NOTIONAL_USD))
        notional = min(notional, MAX_POSITION_NOTIONAL_USD)
        sim = simulate_two_leg_entry(long_q, short_q, notional_usd=notional)
        if not sim["accepted"]:
            return _reject(sym, asset_kind, sim["reason"], entry=sim,
                            data_quality=_dq)

        # Leverage selection — coerce "delegated_depth_ok" sentinel to 0
        # for the 3x rule (which requires >=$50k depth — cannot upgrade
        # off a delegated proxy; default lev only).
        _dep_raw = candidate.get("depth_usd_100bps")
        _dep_num = (0.0 if isinstance(_dep_raw, str)
                     else float(_dep_raw or 0))
        lev = select_leverage(
            asset_kind=asset_kind,
            fee_coverage=float(candidate.get("fee_coverage") or 0),
            net_edge_bps=float(candidate.get("net_edge_bps") or 0),
            ob_spread_bps=float(candidate.get("ob_spread_bps") or 0),
            quote_skew_ms=float(sim["quote_skew_ms"] or 0),
            depth_usd_100bps=_dep_num,
            persistent_route=bool(
                candidate.get("persistence_verdict") == "persistent_depth_ok"),
            recent_symbol_loss=bool(_PER_SYMBOL_COOLDOWN.get(sym, 0)
                                     > time.time()),
            price_mismatch_bps=float(candidate.get("price_mismatch_bps") or 0),
        )

        # Open the position (paper-only)
        now = time.time()
        margin_usd = round(notional / lev, 4) if lev > 0 else 0
        position = {
            "ts_open":     round(now, 3),
            "ts_iso_open": _utc_iso(now),
            "symbol":      sym,
            "asset_kind":  asset_kind,
            "route": {"long_venue":  candidate.get("long_venue"),
                       "short_venue": candidate.get("short_venue")},
            "notional_usd": notional,
            "leverage":    lev,
            "margin_used_usd": margin_usd,
            "entry": {
                **sim,
                "funding_long_8h":  long_funding_8h,
                "funding_short_8h": short_funding_8h,
            },
            "risk": {
                "max_basis_drift_bps":   MAX_BASIS_DRIFT_BPS,
                "max_unrealized_loss_pct_on_margin":
                    MAX_UNREALIZED_LOSS_PCT_ON_MARGIN,
                "max_unrealized_loss_usd": MAX_UNREALIZED_LOSS_USD,
                "max_hold_hours":        MAX_HOLD_HOURS,
            },
            "active_trading": False,
            "paper_trade":    True,
            "executor_emit":  False,
        }
        ledger.setdefault("open_positions", []).append(position)
        save_ledger(ledger)
        log_event("position_opened", decision="open", symbol=sym,
                  asset_kind=asset_kind, reason="ok",
                  route=position["route"], entry=position["entry"],
                  risk=position["risk"], leverage=lev,
                  margin_usd=margin_usd, notional_usd=notional)
        return {"action": "opened", "reason": "ok",
                "ledger_written": True, "position": position}
    except Exception as exc:
        try:
            log.debug("[aggressive] evaluate_candidate failed: %s", exc)
        except Exception:
            pass
        return {"action": "error", "reason": str(exc)[:200],
                "ledger_written": False}


def _coerce_quote(d: Any, funding_8h: Optional[float] = None) -> LegQuote:
    if isinstance(d, LegQuote):
        if funding_8h is not None:
            d.funding_8h = funding_8h
        return d
    if not isinstance(d, dict):
        return LegQuote(venue="", best_bid=0, best_ask=0)
    return LegQuote(
        venue=d.get("venue") or "",
        best_bid=float(d.get("best_bid") or 0),
        best_ask=float(d.get("best_ask") or 0),
        bid_size=float(d.get("bid_size") or 0),
        ask_size=float(d.get("ask_size") or 0),
        snapshot_ts=float(d.get("snapshot_ts") or time.time()),
        funding_8h=funding_8h if funding_8h is not None
                    else d.get("funding_8h"),
        taker_fee_bps=float(d.get("taker_fee_bps") or 5.0),
    )


def _reject(symbol: str, asset_kind: str, reason: str,
            entry: Optional[dict] = None,
            data_quality: Optional[dict] = None) -> dict:
    log_event("candidate_rejected", decision="reject",
              symbol=symbol, asset_kind=asset_kind, reason=reason,
              entry=entry or {}, data_quality=data_quality)
    return {"action": "rejected", "reason": reason,
            "ledger_written": False}


def record_trade_result(pnl_usd: float, *, is_rug: bool = False,
                        is_hard_loss: bool = False,
                        symbol: Optional[str] = None) -> dict:
    """Update rolling stats after a paper trade closes.  Returns updated
    ledger.  Caller saves.
    """
    _ROLLING_RESULTS.append((float(pnl_usd), bool(is_rug), bool(is_hard_loss)))
    if is_hard_loss and symbol:
        # 24-hour symbol cooldown after a hard loss.
        _PER_SYMBOL_COOLDOWN[symbol.upper()] = time.time() + 24 * 3600
    ledger = load_ledger()
    stats = ledger.setdefault("stats", _fresh_ledger()["stats"])
    today = _utc_date_str()
    if stats.get("trades_today_date") != today:
        stats["trades_today_date"] = today
        stats["trades_today"] = 0
        stats["daily_pnl_usd"] = 0.0
    stats["trades_today"] = int(stats.get("trades_today") or 0) + 1
    stats["daily_pnl_usd"] = float(stats.get("daily_pnl_usd") or 0.0) + pnl_usd
    stats["total_pnl_usd"] = float(stats.get("total_pnl_usd") or 0.0) + pnl_usd
    ledger["balance_usd"] = float(ledger.get("balance_usd")
                                  or START_BALANCE_USD) + pnl_usd
    if is_hard_loss:
        stats["consecutive_hard_losses"] = \
            int(stats.get("consecutive_hard_losses") or 0) + 1
    else:
        stats["consecutive_hard_losses"] = 0
    if _ROLLING_RESULTS:
        pnls = sorted(r[0] for r in _ROLLING_RESULTS)
        stats["rolling_20_median"] = pnls[len(pnls) // 2]
        wins = sum(1 for p in pnls if p > 0)
        stats["rolling_20_winrate"] = round(100.0 * wins / len(pnls), 2)
    return ledger
