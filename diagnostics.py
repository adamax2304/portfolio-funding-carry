"""
funding_carry/diagnostics.py

Sprint 1 (2026-05-26) — pure-helper module for the safe-diagnostic mode.

* CooldownTracker
    Tracks per-base "do not re-open until" timestamps. Initially seeded
    from trades.jsonl so cooldown state survives restarts. Adds a new
    cooldown when a position closes with a bad exit (negative realized
    PnL OR exit_reason in BAD_EXIT_REASONS).

* estimate_fee_coverage()
    Given a ScoredOpportunity and a notional, project how many funding
    cycles' worth of income covers the round-trip fees. Used by the
    stricter entry gate to reject low-coverage opportunities.

Safety contract
---------------
* No state mutation outside its own JSON tracker file.
* All public functions are exception-safe; never crashes _try_enter.
* No network calls.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from .config import (
    BAD_EXIT_PNL_THRESHOLD_USD,
    BAD_EXIT_REASONS,
    PERP_MAKER_FEES,
    PERP_SLIPPAGE_BPS,
    SPOT_MAKER_FEES,
    SPOT_SLIPPAGE_BPS,
    STATE_DIR,
    SYMBOL_COOLDOWN_AFTER_BAD_EXIT_S,
    TEMP_BLACKLIST_BASES,
    TRADE_LOG_FILE,
    CROSS_EXCHANGE_FRICTION_BPS,
)

log = logging.getLogger(__name__)

COOLDOWN_STATE_FILE = os.path.join(STATE_DIR, "carry_cooldown_state.json")


# ──────────────────────────────────────────────────────────────────────────
# Cooldown tracker
# ──────────────────────────────────────────────────────────────────────────


class CooldownTracker:
    """Per-base "next-open-allowed-at" timestamps. Persisted across restarts.

    Mutates only its own JSON file at STATE_DIR/carry_cooldown_state.json.
    Used by main._try_enter to skip recently bad-exit bases.
    """

    def __init__(self) -> None:
        self._until: dict[str, float] = {}
        self._load_state_file()
        self._seed_from_trades()

    # ── persistence ───────────────────────────────────────────────────────

    def _load_state_file(self) -> None:
        try:
            if not os.path.exists(COOLDOWN_STATE_FILE):
                return
            with open(COOLDOWN_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for base, ts in data.items():
                    try:
                        self._until[str(base)] = float(ts)
                    except Exception:
                        continue
        except Exception as exc:
            log.debug("[cooldown] state load failed: %s", exc)

    def _save_state_file(self) -> None:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = COOLDOWN_STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._until, f, ensure_ascii=False)
            os.replace(tmp, COOLDOWN_STATE_FILE)
        except Exception as exc:
            log.debug("[cooldown] state save failed: %s", exc)

    # ── seed from history ─────────────────────────────────────────────────

    def _seed_from_trades(self) -> None:
        """Walk trades.jsonl once at startup and apply cooldowns for any
        recent bad exit whose cooldown is still live."""
        if not os.path.exists(TRADE_LOG_FILE):
            return
        now_ts = time.time()
        cutoff = now_ts - SYMBOL_COOLDOWN_AFTER_BAD_EXIT_S
        try:
            with open(TRADE_LOG_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    exit_ts = rec.get("exit_ts")
                    if not exit_ts or exit_ts < cutoff:
                        continue
                    base = rec.get("base")
                    if not base:
                        continue
                    pnl = float(rec.get("realized_pnl_usd") or 0.0)
                    reason = (rec.get("exit_reason") or "").lower()
                    if _is_bad_exit(pnl, reason):
                        ts_open_allowed = float(exit_ts) + SYMBOL_COOLDOWN_AFTER_BAD_EXIT_S
                        prev = self._until.get(base, 0.0)
                        if ts_open_allowed > prev:
                            self._until[base] = ts_open_allowed
        except Exception as exc:
            log.debug("[cooldown] seed-from-trades failed: %s", exc)
        log.info("[cooldown] seeded from history: %d bases on cooldown",
                 sum(1 for ts in self._until.values() if ts > now_ts))

    # ── public API ────────────────────────────────────────────────────────

    def is_on_cooldown(self, base: str, now_ts: Optional[float] = None) -> bool:
        ts = time.time() if now_ts is None else now_ts
        return self._until.get(base, 0.0) > ts

    def seconds_until_open(self, base: str, now_ts: Optional[float] = None) -> float:
        ts = time.time() if now_ts is None else now_ts
        return max(0.0, self._until.get(base, 0.0) - ts)

    def record_close(
        self, *,
        base: str,
        exit_ts: float,
        realized_pnl_usd: float,
        exit_reason: str,
    ) -> bool:
        """Returns True if a cooldown was added (bad exit)."""
        if _is_bad_exit(realized_pnl_usd, (exit_reason or "").lower()):
            open_allowed_at = float(exit_ts) + SYMBOL_COOLDOWN_AFTER_BAD_EXIT_S
            prev = self._until.get(base, 0.0)
            if open_allowed_at > prev:
                self._until[base] = open_allowed_at
                self._save_state_file()
            return True
        return False

    def bases_on_cooldown(self) -> list[tuple[str, float]]:
        """Returns [(base, seconds_remaining)] sorted by remaining desc."""
        now_ts = time.time()
        out = [(b, max(0.0, ts - now_ts))
               for b, ts in self._until.items() if ts > now_ts]
        out.sort(key=lambda kv: -kv[1])
        return out


def _is_bad_exit(pnl_usd: float, exit_reason_lower: str) -> bool:
    """Return True if the close should trigger a cooldown."""
    if pnl_usd <= BAD_EXIT_PNL_THRESHOLD_USD:
        return True
    # Even on small positive PnL, certain exit reasons still mean the
    # opportunity was structurally weak (e.g. profit_zero_decayed but
    # eked out a small win) -- treat as bad.
    for bad in BAD_EXIT_REASONS:
        if bad in exit_reason_lower:
            return True
    return False


# ──────────────────────────────────────────────────────────────────────────
# Fee coverage estimator
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FeeCoverage:
    """Result of fee-coverage estimation for an opportunity at a given notional."""
    fees_roundtrip_usd:    float
    funding_per_cycle_usd: float   # what we expect to receive per 8h cycle
    funding_2_cycles_usd:  float   # 2× per_cycle
    coverage_ratio:        float   # funding_2_cycles / fees_roundtrip
    funding_interval_h:    float


def _leg_fee_rate(leg_kind: str, exchange: str) -> float:
    if leg_kind == "spot":
        return SPOT_MAKER_FEES.get(exchange, 0.001)
    return PERP_MAKER_FEES.get(exchange, 0.0002)


def _leg_slip_rate(leg_kind: str) -> float:
    return (SPOT_SLIPPAGE_BPS if leg_kind == "spot" else PERP_SLIPPAGE_BPS) / 1e4


def estimate_fee_coverage(*,
                          opp,
                          notional_usd: float) -> FeeCoverage:
    """Compute expected fee coverage for `opp` at `notional_usd`.

    Funding income per cycle:
      * SHORT perp leg pays funding when rate > 0:  income = notional × rate
      * LONG  perp leg pays funding when rate < 0:  income = notional × |rate|
      * Spot legs do not produce funding income.

    Roundtrip fees include enter + exit for both legs + cross-exchange
    friction (if applicable).
    """
    legs = (opp.long_leg, opp.short_leg)
    fee_rate_sum = sum(
        _leg_fee_rate(l.leg_kind, l.exchange) + _leg_slip_rate(l.leg_kind)
        for l in legs
    )
    # × 2 for enter + exit
    fees_rt = notional_usd * fee_rate_sum * 2.0
    if not opp.same_exchange:
        fees_rt += notional_usd * (CROSS_EXCHANGE_FRICTION_BPS / 1e4)

    # Per-cycle income (use the larger absolute rate of the perp legs that
    # actually pay us — i.e. SHORT perp with rate>0 OR LONG perp with rate<0).
    per_cycle = 0.0
    for l in legs:
        if l.leg_kind != "perp":
            continue
        rate = float(getattr(l, "funding_rate", 0.0) or 0.0)
        if l.direction == "SHORT" and rate > 0:
            per_cycle += notional_usd * rate
        elif l.direction == "LONG" and rate < 0:
            per_cycle += notional_usd * abs(rate)

    interval_h = 8.0
    for l in legs:
        if l.leg_kind == "perp" and getattr(l, "funding_interval_h", 0.0):
            interval_h = float(l.funding_interval_h)
            break

    two_cycles = per_cycle * 2.0
    coverage = (two_cycles / fees_rt) if fees_rt > 0 else 0.0
    return FeeCoverage(
        fees_roundtrip_usd=fees_rt,
        funding_per_cycle_usd=per_cycle,
        funding_2_cycles_usd=two_cycles,
        coverage_ratio=coverage,
        funding_interval_h=interval_h,
    )


def max_abs_perp_funding_rate(opp) -> float:
    """Maximum |funding_rate| across the perp legs of the opportunity.

    For SAME-EXCHANGE cash-and-carry this is the perp leg's rate; for
    PERP+PERP cross this is the larger of the two side-specific rates we
    actually receive on (SHORT perp w/ rate>0, LONG perp w/ rate<0)."""
    best = 0.0
    for l in (opp.long_leg, opp.short_leg):
        if l.leg_kind != "perp":
            continue
        r = abs(float(getattr(l, "funding_rate", 0.0) or 0.0))
        if r > best:
            best = r
    return best


def normalised_rate_per_8h(rate_per_interval: float, interval_h: float) -> float:
    """Normalise a per-interval funding rate to per-8h equivalents.
    If interval_h is 0 or invalid, return rate_per_interval unchanged."""
    try:
        if interval_h and interval_h > 0:
            return float(rate_per_interval) * (8.0 / float(interval_h))
    except Exception:
        pass
    return float(rate_per_interval)
