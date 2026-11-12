"""
funding_carry/strategy/sizing.py

5× leverage on perp legs.  Allocate up to ALLOCATION_PCT_PER_TRADE of equity
to each opportunity, capped at MAX_ALLOCATION_PER_BASE per base.

With 5× lev on the perp leg, the dollar capital lock per position is:
  - spot leg: full notional locked in spot wallet
  - perp leg: notional / 5 of margin

For PERP+PERP opportunities, both legs use 5× — capital = 2 × notional / 5.
"""
from __future__ import annotations

from ..config import (
    ALLOCATION_PCT_PER_TRADE,
    LEVERAGE,
    MAX_ALLOCATION_PER_BASE,
)


def compute_allocation(
    equity_usd: float,
    capital_already_deployed_usd: float,
    n_open_positions: int,
    max_concurrent: int,
    opportunity_kind: str,
) -> tuple[float, float]:
    """
    Return (notional_per_leg_usd, total_capital_locked_usd).

    For spot+perp: capital = notional × (1 + 1/LEVERAGE) = notional × 1.2 (at 5×)
    For perp+perp: capital = notional × (2 / LEVERAGE)   = notional × 0.4 (at 5×)
    """
    if n_open_positions >= max_concurrent:
        return 0.0, 0.0

    target_notional = equity_usd * ALLOCATION_PCT_PER_TRADE
    cap_notional    = equity_usd * MAX_ALLOCATION_PER_BASE

    notional = min(target_notional, cap_notional)

    if opportunity_kind == "perp+perp_cross":
        cap_per_position = notional * (2.0 / LEVERAGE)
    else:
        # spot full notional + perp at 1/LEVERAGE
        cap_per_position = notional * (1.0 + 1.0 / LEVERAGE)

    free_capital = max(0.0, equity_usd - capital_already_deployed_usd)
    if cap_per_position > free_capital:
        # Scale notional down to fit free capital
        scale = free_capital / cap_per_position if cap_per_position > 0 else 0
        notional *= scale
        cap_per_position *= scale

    return notional, cap_per_position
