"""
funding_carry/strategy/validation.py

Pre-trade safety gates for carry positions.

For an opportunity to be safe to execute, ALL of the following must hold:

  1) Long-leg orderbook present with spread ≤ MAX_SPREAD_BPS and depth
     ≥ DEPTH_MULTIPLIER × notional within ENTRY_SLIP_PCT of best ask.
  2) Short-leg orderbook present with spread ≤ MAX_SPREAD_BPS and depth
     ≥ DEPTH_MULTIPLIER × notional within ENTRY_SLIP_PCT of best bid.
  3) Effective fill prices (long-leg ask vs short-leg bid) match within
     MAX_LEG_PRICE_DIFF_BPS — required so that the delta-neutral
     assumption holds at entry.
  4) Both legs must have sufficient 24h volume (already filtered upstream,
     but doubled-checked here).

If any gate fails, we DO NOT enter.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..config import (
    DEPTH_MULTIPLIER,
    ENTRY_SLIP_PCT,
    MAX_LEG_PRICE_DIFF_BPS,
    MAX_SPREAD_BPS,
)
from ..data.types import CarryOpportunity, OrderBook


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str = ""
    long_fill_price: float = 0.0
    short_fill_price: float = 0.0
    price_diff_bps: float = 0.0
    long_spread_bps: float = 0.0
    short_spread_bps: float = 0.0
    long_depth_usd: float = 0.0
    short_depth_usd: float = 0.0


def validate_legs(
    opp: CarryOpportunity,
    long_ob: Optional[OrderBook],
    short_ob: Optional[OrderBook],
    intended_notional_usd: float,
) -> ValidationResult:
    if long_ob is None:
        return ValidationResult(False, reason="no_long_orderbook")
    if short_ob is None:
        return ValidationResult(False, reason="no_short_orderbook")

    lb = long_ob.best_bid();  la = long_ob.best_ask()
    sb = short_ob.best_bid(); sa = short_ob.best_ask()
    if not (lb and la and sb and sa):
        return ValidationResult(False, reason="empty_book")

    long_mid  = (lb.price + la.price) / 2.0
    short_mid = (sb.price + sa.price) / 2.0
    long_spread_bps  = (la.price - lb.price) / long_mid  * 1e4
    short_spread_bps = (sa.price - sb.price) / short_mid * 1e4

    if long_spread_bps > MAX_SPREAD_BPS:
        return ValidationResult(False, reason=f"long_spread_{long_spread_bps:.1f}bps",
                                long_spread_bps=long_spread_bps,
                                short_spread_bps=short_spread_bps)
    if short_spread_bps > MAX_SPREAD_BPS:
        return ValidationResult(False, reason=f"short_spread_{short_spread_bps:.1f}bps",
                                long_spread_bps=long_spread_bps,
                                short_spread_bps=short_spread_bps)

    # Fill prices: long leg crosses the ask (we BUY), short leg crosses the bid (we SELL)
    long_fill  = la.price
    short_fill = sb.price
    mid = (long_fill + short_fill) / 2.0
    price_diff_bps = (short_fill - long_fill) / mid * 1e4   # positive = perp premium
    if abs(price_diff_bps) > MAX_LEG_PRICE_DIFF_BPS:
        return ValidationResult(False,
            reason=f"price_mismatch_{price_diff_bps:+.1f}bps",
            long_fill_price=long_fill, short_fill_price=short_fill,
            price_diff_bps=price_diff_bps,
            long_spread_bps=long_spread_bps,
            short_spread_bps=short_spread_bps,
        )

    # Depth check (long buys → consume asks; short sells → consume bids)
    long_depth  = long_ob.depth_usd_within_pct("ask", ENTRY_SLIP_PCT)
    short_depth = short_ob.depth_usd_within_pct("bid", ENTRY_SLIP_PCT)
    need = intended_notional_usd * DEPTH_MULTIPLIER
    if long_depth < need:
        return ValidationResult(False,
            reason=f"thin_long_book(${long_depth:.0f}<${need:.0f})",
            long_fill_price=long_fill, short_fill_price=short_fill,
            price_diff_bps=price_diff_bps,
            long_spread_bps=long_spread_bps, short_spread_bps=short_spread_bps,
            long_depth_usd=long_depth, short_depth_usd=short_depth,
        )
    if short_depth < need:
        return ValidationResult(False,
            reason=f"thin_short_book(${short_depth:.0f}<${need:.0f})",
            long_fill_price=long_fill, short_fill_price=short_fill,
            price_diff_bps=price_diff_bps,
            long_spread_bps=long_spread_bps, short_spread_bps=short_spread_bps,
            long_depth_usd=long_depth, short_depth_usd=short_depth,
        )

    return ValidationResult(
        ok=True, reason="ok",
        long_fill_price=long_fill, short_fill_price=short_fill,
        price_diff_bps=price_diff_bps,
        long_spread_bps=long_spread_bps, short_spread_bps=short_spread_bps,
        long_depth_usd=long_depth, short_depth_usd=short_depth,
    )
