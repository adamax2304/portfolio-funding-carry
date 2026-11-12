"""
funding_carry/execution/paper.py

Paper book for DELTA-NEUTRAL carry — same-exchange OR cross-exchange,
spot+perp OR perp+perp, with 5× leverage on perp legs.

Exit triggers
-------------
(a) SL — basis between long-leg and short-leg drifts beyond
    MAX_BASIS_DRIFT_BPS from entry-match.
(b) PROFIT-ZERO — projected remaining funding income until next settlement,
    minus exit-cost, drops below EXIT_PROFIT_ZERO_BUFFER × exit_cost.
    (i.e. it stops being profitable to keep the position open.)
(c) MAX_HOLD_S — hard timeout.

Funding accounting at settlement
--------------------------------
At each perp-leg settlement that lands while the position is open, we credit:
  short_perp_leg  → +funding_rate × notional   (if rate > 0)
  long_perp_leg   → +|funding_rate| × notional (if rate < 0)
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

from ..config import (
    CROSS_EXCHANGE_FRICTION_BPS,
    EXIT_PROFIT_ZERO_BUFFER,
    FUNDING_HISTORY_FILE,
    LEVERAGE,
    MAX_BASIS_DRIFT_BPS,
    MAX_HOLD_S,
    MIN_HOLD_S,
    PAPER_BOOK_FILE,
    PAPER_STARTING_BALANCE_USD,
    PERP_MAKER_FEES,
    PERP_SLIPPAGE_BPS,
    SPOT_MAKER_FEES,
    SPOT_SLIPPAGE_BPS,
    TRADE_LOG_FILE,
    # Sprint 1 (2026-05-26) — minimum cycles before profit_zero exit
    CARRY_SAFE_DIAGNOSTIC_MODE,
    HOLD_EXTENDED_LOG_TAG,
    MIN_HOLD_CYCLES_FOR_PROFIT_ZERO,
)
from ..data.types import CarryLeg, CarryOpportunity

log = logging.getLogger(__name__)


@dataclass
class CarryPosition:
    pos_id: str
    base: str
    kind: str                      # "spot+perp_same" | "spot+perp_cross" | "perp+perp_cross"

    long_exchange: str
    long_leg_kind: str             # "spot" or "perp"
    long_symbol: str
    long_entry_price: float
    long_qty: float

    short_exchange: str
    short_leg_kind: str
    short_symbol: str
    short_entry_price: float
    short_qty: float               # positive magnitude (direction is implicit)

    notional_usd: float
    capital_locked_usd: float

    # Funding state
    long_funding_rate_at_entry: float
    long_funding_interval_h: float
    long_next_funding_ts: float
    short_funding_rate_at_entry: float
    short_funding_interval_h: float
    short_next_funding_ts: float

    entry_ts: float
    entry_basis_bps: float
    fees_paid_usd: float = 0.0
    funding_collected_usd: float = 0.0

    last_credit_long_ts:  float = 0.0
    last_credit_short_ts: float = 0.0

    # Filled on close
    exit_ts: Optional[float] = None
    exit_reason: Optional[str] = None
    long_exit_price: Optional[float] = None
    short_exit_price: Optional[float] = None
    realized_pnl_usd: Optional[float] = None


class PaperBook:
    def __init__(self) -> None:
        self.balance_usd: float = PAPER_STARTING_BALANCE_USD
        self.equity_usd:  float = PAPER_STARTING_BALANCE_USD
        self.open_positions: dict[str, CarryPosition] = {}
        self.closed_count: int = 0
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not os.path.exists(PAPER_BOOK_FILE):
            return
        try:
            with open(PAPER_BOOK_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.balance_usd = float(d.get("balance_usd", PAPER_STARTING_BALANCE_USD))
            self.equity_usd  = float(d.get("equity_usd", self.balance_usd))
            self.closed_count = int(d.get("closed_count", 0))
            for pid, raw in (d.get("open_positions") or {}).items():
                self.open_positions[pid] = CarryPosition(**raw)
            log.info("[paper] loaded: equity=$%.2f open=%d closed=%d",
                     self.equity_usd, len(self.open_positions), self.closed_count)
        except Exception as exc:
            log.warning("[paper] load failed: %s", exc)

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(PAPER_BOOK_FILE), exist_ok=True)
            with open(PAPER_BOOK_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "balance_usd": self.balance_usd,
                    "equity_usd":  self.equity_usd,
                    "closed_count": self.closed_count,
                    "open_positions": {pid: asdict(p) for pid, p in self.open_positions.items()},
                }, f, indent=2)
        except Exception as exc:
            log.warning("[paper] save failed: %s", exc)

    # ── helpers ──────────────────────────────────────────────────────────────

    def capital_deployed_usd(self) -> float:
        return sum(p.capital_locked_usd for p in self.open_positions.values())

    def already_open(self, opp: CarryOpportunity) -> bool:
        """Detect duplicate (base, kind, long_exchange, short_exchange) positions."""
        for p in self.open_positions.values():
            if (p.base == opp.base and p.kind == opp.kind
                and p.long_exchange == opp.long_leg.exchange
                and p.short_exchange == opp.short_leg.exchange):
                return True
        return False

    def _leg_fee(self, leg: CarryLeg) -> float:
        if leg.leg_kind == "spot":
            return SPOT_MAKER_FEES.get(leg.exchange, 0.001)
        return PERP_MAKER_FEES.get(leg.exchange, 0.0002)

    def _cycles_completed(self, p: CarryPosition, now_ts: float) -> int:
        """Sprint 1: number of funding cycles elapsed since entry.

        Strategy uses time-based estimation against MIN_HOLD_S (=8h, one
        cycle). A cycle is counted as complete when `now_ts - entry_ts`
        has passed an integer multiple of MIN_HOLD_S. This is robust to
        4h vs 8h funding intervals and avoids reliance on
        next_funding_ts deltas (which represent time-until-next, not
        the leg's natural interval).

        Additionally we OR in the credited-settlement evidence: if a
        leg's last_credit_*_ts has advanced past entry_ts, that proves
        at least one cycle has settled even if the wall-clock math is
        off (e.g. position opened seconds before settlement).
        """
        elapsed_s = max(0.0, now_ts - p.entry_ts)
        time_cycles = int(elapsed_s // max(1.0, float(MIN_HOLD_S)))

        credited = 0
        if p.long_leg_kind == "perp" and p.last_credit_long_ts > p.entry_ts:
            credited = max(credited, 1)
        if p.short_leg_kind == "perp" and p.last_credit_short_ts > p.entry_ts:
            credited = max(credited, 1)
        # Reading-credited is binary (0 or 1) -- we don't know how many
        # individual settlements happened beyond that, so only use it as
        # a lower-bound check.

        return max(time_cycles, credited)

    def _leg_slip(self, leg: CarryLeg) -> float:
        return (SPOT_SLIPPAGE_BPS if leg.leg_kind == "spot" else PERP_SLIPPAGE_BPS) / 1e4

    def _exit_cost_fraction(self, p: CarryPosition) -> float:
        """Fraction-of-notional cost to close BOTH legs right now."""
        # Reconstruct "leg" objects shallowly enough for fee lookup
        long_fee  = (SPOT_MAKER_FEES if p.long_leg_kind == "spot"
                     else PERP_MAKER_FEES).get(p.long_exchange, 0.0002)
        short_fee = (SPOT_MAKER_FEES if p.short_leg_kind == "spot"
                     else PERP_MAKER_FEES).get(p.short_exchange, 0.0002)
        long_slip  = (SPOT_SLIPPAGE_BPS if p.long_leg_kind == "spot"
                      else PERP_SLIPPAGE_BPS) / 1e4
        short_slip = (SPOT_SLIPPAGE_BPS if p.short_leg_kind == "spot"
                      else PERP_SLIPPAGE_BPS) / 1e4
        cross_friction = (CROSS_EXCHANGE_FRICTION_BPS / 1e4
                          if p.long_exchange != p.short_exchange else 0.0)
        return long_fee + short_fee + long_slip + short_slip + cross_friction

    # ── open ─────────────────────────────────────────────────────────────────

    def open(
        self,
        opp: CarryOpportunity,
        notional_usd: float,
        capital_locked_usd: float,
        long_fill_price: float,
        short_fill_price: float,
    ) -> Optional[CarryPosition]:
        if self.already_open(opp):
            log.info("[paper] skip %s — duplicate position", opp.base)
            return None
        if notional_usd <= 0 or capital_locked_usd <= 0:
            return None

        long_qty  = notional_usd / long_fill_price  if long_fill_price  > 0 else 0.0
        short_qty = notional_usd / short_fill_price if short_fill_price > 0 else 0.0
        if long_qty <= 0 or short_qty <= 0:
            return None

        # Entry fees (maker on both legs)
        entry_fees = notional_usd * (self._leg_fee(opp.long_leg) + self._leg_fee(opp.short_leg))
        # Cross-exchange friction added once at entry
        if not opp.same_exchange:
            entry_fees += notional_usd * (CROSS_EXCHANGE_FRICTION_BPS / 1e4) * 0.5
        self.balance_usd -= entry_fees

        pos = CarryPosition(
            pos_id=str(uuid.uuid4())[:8],
            base=opp.base, kind=opp.kind,
            long_exchange=opp.long_leg.exchange, long_leg_kind=opp.long_leg.leg_kind,
            long_symbol=opp.long_leg.symbol, long_entry_price=long_fill_price,
            long_qty=long_qty,
            short_exchange=opp.short_leg.exchange, short_leg_kind=opp.short_leg.leg_kind,
            short_symbol=opp.short_leg.symbol, short_entry_price=short_fill_price,
            short_qty=short_qty,
            notional_usd=notional_usd, capital_locked_usd=capital_locked_usd,
            long_funding_rate_at_entry=opp.long_leg.funding_rate,
            long_funding_interval_h=opp.long_leg.funding_interval_h,
            long_next_funding_ts=float(opp.long_leg.next_funding_ts),
            short_funding_rate_at_entry=opp.short_leg.funding_rate,
            short_funding_interval_h=opp.short_leg.funding_interval_h,
            short_next_funding_ts=float(opp.short_leg.next_funding_ts),
            entry_ts=time.time(),
            entry_basis_bps=opp.price_diff_bps,
            fees_paid_usd=entry_fees,
        )
        self.open_positions[pos.pos_id] = pos
        self._save()
        log.info(
            "[paper] OPEN %s carry %s notional=$%.0f capital=$%.0f (lev=%dx) — "
            "LONG %s/%s @ %.6f, SHORT %s/%s @ %.6f, basis=%+.1fbps",
            pos.kind, pos.base, notional_usd, capital_locked_usd, LEVERAGE,
            pos.long_leg_kind, pos.long_exchange, long_fill_price,
            pos.short_leg_kind, pos.short_exchange, short_fill_price,
            pos.entry_basis_bps,
        )
        return pos

    # ── mark ─────────────────────────────────────────────────────────────────

    def mark(
        self,
        p: CarryPosition,
        long_current_price: float,
        short_current_price: float,
        long_current_funding_rate: float,
        short_current_funding_rate: float,
        long_next_funding_ts: float,
        short_next_funding_ts: float,
    ) -> Optional[CarryPosition]:
        """Apply latest prices & funding; trigger exit if conditions met.
        Returns position if closed this call.
        """
        now_ts = time.time()

        # 1) Credit funding for any settlement that landed
        for which in ("long", "short"):
            leg_kind = p.long_leg_kind if which == "long" else p.short_leg_kind
            if leg_kind != "perp":
                continue
            settle_ts = p.long_next_funding_ts if which == "long" else p.short_next_funding_ts
            last_credit = p.last_credit_long_ts if which == "long" else p.last_credit_short_ts
            if settle_ts <= 0:
                continue
            settle_s = settle_ts / 1000.0
            if settle_s <= now_ts and last_credit < settle_s:
                # Credit using CURRENT rate as best estimate (live would query history)
                rate = (long_current_funding_rate if which == "long"
                        else short_current_funding_rate)
                # We RECEIVE if we hold the side opposite to crowded:
                #  short leg + rate>0 → receive  rate × notional
                #  long  leg + rate<0 → receive |rate| × notional
                income = 0.0
                if which == "short" and rate > 0:
                    income = p.notional_usd * rate
                elif which == "long" and rate < 0:
                    income = p.notional_usd * abs(rate)
                p.funding_collected_usd += income
                self.balance_usd += income
                # advance & remember
                if which == "long":
                    p.last_credit_long_ts = settle_s
                    p.long_next_funding_ts = long_next_funding_ts
                else:
                    p.last_credit_short_ts = settle_s
                    p.short_next_funding_ts = short_next_funding_ts
                self._log_funding(p, which, rate, income, now_ts)
                if income != 0:
                    log.info(
                        "[paper] FUNDING %s leg of %s on %s: rate=%+.4f%% income=$%+.4f",
                        which, p.base,
                        p.long_exchange if which == "long" else p.short_exchange,
                        rate * 100, income,
                    )

        # 2) Compute current basis
        mid = (long_current_price + short_current_price) / 2.0
        cur_basis_bps = (short_current_price - long_current_price) / mid * 1e4 if mid > 0 else 0.0
        basis_drift_bps = abs(cur_basis_bps - p.entry_basis_bps)

        age_s = now_ts - p.entry_ts
        exit_reason: Optional[str] = None

        # 3) SL — basis drift: NO minimum hold — if legs diverge badly right
        #    after entry we must exit immediately, not wait 8 hours.
        if basis_drift_bps > MAX_BASIS_DRIFT_BPS:
            exit_reason = f"basis_drift_{basis_drift_bps:.0f}bps"

        # 4) PROFIT-ZERO — projected remaining funding to next settlement
        #    minus exit cost would be net-negative.
        #    Enforce MIN_HOLD_S here only: we must collect at least one cycle
        #    before declaring profit-zero (otherwise we'd exit too eagerly
        #    in the gap between entry and first settlement).
        #
        # Sprint 1 (2026-05-26): in CARRY_SAFE_DIAGNOSTIC_MODE we require
        # MIN_HOLD_CYCLES_FOR_PROFIT_ZERO (default 2) funding cycles before
        # profit_zero_decayed can fire. Empirical: 8h single-cycle holds
        # collected $0-1 funding but paid $2.6 fees → -EV by construction.
        # Basis_drift, max_hold and emergency exits are unaffected.
        if exit_reason is None and age_s >= MIN_HOLD_S:
            exit_cost_pct = self._exit_cost_fraction(p)
            exit_cost_usd = p.notional_usd * exit_cost_pct
            # Project income at next perp settlement(s) using current rate
            projected_remaining = 0.0
            if p.short_leg_kind == "perp" and short_current_funding_rate > 0:
                projected_remaining += p.notional_usd * short_current_funding_rate
            if p.long_leg_kind == "perp" and long_current_funding_rate < 0:
                projected_remaining += p.notional_usd * abs(long_current_funding_rate)
            if projected_remaining < exit_cost_usd * EXIT_PROFIT_ZERO_BUFFER:
                cycles_completed = self._cycles_completed(p, now_ts)
                if (CARRY_SAFE_DIAGNOSTIC_MODE
                        and cycles_completed < MIN_HOLD_CYCLES_FOR_PROFIT_ZERO):
                    # Extend hold: not enough funding cycles yet to recover
                    # fees. Basis_drift gate above is unaffected.
                    fee_coverage = (p.funding_collected_usd / exit_cost_usd
                                    if exit_cost_usd > 0 else 0.0)
                    log.info(
                        "[paper] %s %s held=%.1fh cycles=%d/%d "
                        "funding_so_far=$%.4f exit_cost=$%.4f cov=%.2fx "
                        "-- profit_zero deferred",
                        HOLD_EXTENDED_LOG_TAG, p.base,
                        age_s / 3600.0,
                        cycles_completed, MIN_HOLD_CYCLES_FOR_PROFIT_ZERO,
                        p.funding_collected_usd, exit_cost_usd, fee_coverage,
                    )
                else:
                    exit_reason = "profit_zero_decayed"

        # 5) Hard timeout
        if age_s >= MAX_HOLD_S:
            exit_reason = exit_reason or "max_hold"

        if exit_reason:
            self._close(p, long_current_price, short_current_price, exit_reason)
            self._save()
            return p
        return None

    def _close(
        self, p: CarryPosition,
        long_exit_price: float, short_exit_price: float, reason: str,
    ) -> None:
        # Apply exit slippage on both legs
        long_slip  = (SPOT_SLIPPAGE_BPS if p.long_leg_kind == "spot"
                      else PERP_SLIPPAGE_BPS) / 1e4
        short_slip = (SPOT_SLIPPAGE_BPS if p.short_leg_kind == "spot"
                      else PERP_SLIPPAGE_BPS) / 1e4
        # Long EXIT = sell (cross bid) at slip
        long_fill  = long_exit_price * (1 - long_slip)
        # Short EXIT = buy back (cross ask) at slip
        short_fill = short_exit_price * (1 + short_slip)

        long_fee_rate  = (SPOT_MAKER_FEES if p.long_leg_kind == "spot"
                          else PERP_MAKER_FEES).get(p.long_exchange, 0.0002)
        short_fee_rate = (SPOT_MAKER_FEES if p.short_leg_kind == "spot"
                          else PERP_MAKER_FEES).get(p.short_exchange, 0.0002)
        exit_fees = p.notional_usd * (long_fee_rate + short_fee_rate)
        if p.long_exchange != p.short_exchange:
            exit_fees += p.notional_usd * (CROSS_EXCHANGE_FRICTION_BPS / 1e4) * 0.5

        # Leg PnL
        long_pnl  = (long_fill - p.long_entry_price) * p.long_qty
        short_pnl = (p.short_entry_price - short_fill) * p.short_qty

        p.fees_paid_usd += exit_fees
        self.balance_usd -= exit_fees
        self.balance_usd += long_pnl + short_pnl

        p.long_exit_price  = long_fill
        p.short_exit_price = short_fill
        p.exit_ts = time.time()
        p.exit_reason = reason
        p.realized_pnl_usd = (long_pnl + short_pnl + p.funding_collected_usd
                              - p.fees_paid_usd)

        self.equity_usd = self.balance_usd
        self.closed_count += 1
        del self.open_positions[p.pos_id]

        try:
            os.makedirs(os.path.dirname(TRADE_LOG_FILE), exist_ok=True)
            with open(TRADE_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(p)) + "\n")
        except Exception:
            pass

        held_h = (p.exit_ts - p.entry_ts) / 3600.0
        annual_pct = (p.realized_pnl_usd / max(p.capital_locked_usd, 1.0)) \
                     * (8760.0 / max(held_h, 0.01)) * 100.0
        log.info(
            "[paper] CLOSE %s %s [%s] — held %.1fh — funding=$%.4f long_pnl=$%.4f "
            "short_pnl=$%.4f fees=$%.4f net=$%.4f (annual %+.1f%% on capital)",
            p.kind, p.base, reason, held_h,
            p.funding_collected_usd, long_pnl, short_pnl, p.fees_paid_usd,
            p.realized_pnl_usd, annual_pct,
        )

    def _log_funding(self, p: CarryPosition, which: str,
                     rate: float, income: float, ts: float) -> None:
        try:
            os.makedirs(os.path.dirname(FUNDING_HISTORY_FILE), exist_ok=True)
            with open(FUNDING_HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": ts, "pos_id": p.pos_id, "base": p.base, "kind": p.kind,
                    "leg": which,
                    "exchange": p.long_exchange if which == "long" else p.short_exchange,
                    "rate": rate, "income_usd": income,
                    "cumulative": p.funding_collected_usd,
                }) + "\n")
        except Exception:
            pass

    # ── stats ────────────────────────────────────────────────────────────────

    def summary(self) -> dict:
        return {
            "balance_usd":   round(self.balance_usd, 4),
            "equity_usd":    round(self.equity_usd, 4),
            "open":          len(self.open_positions),
            "closed":        self.closed_count,
            "deployed_usd":  round(self.capital_deployed_usd(), 2),
            "open_legs":     [f"{p.base}[{p.kind}]" for p in self.open_positions.values()],
        }
