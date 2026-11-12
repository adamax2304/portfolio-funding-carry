"""
funding_carry/strategy/opportunities.py

Build & score carry opportunities — same-exchange AND cross-exchange.

For each base symbol:
  - Collect ALL perp snapshots across exchanges (by base).
  - Collect ALL spot snapshots across exchanges (by base).
  - Enumerate candidate (long_leg, short_leg) combinations:
      1) SAME-EXCHANGE SPOT+PERP : long=spot[ex], short=perp[ex] for each ex
         (perp funding must be > SAME_EXCHANGE_MIN_FUNDING_PERIOD)
      2) CROSS-EXCHANGE SPOT+PERP: long=spot[ex_a], short=perp[ex_b]   ex_a ≠ ex_b
         (perp funding must be > CROSS_SPOT_PERP_MIN_FUNDING_PERIOD)
      3) CROSS-EXCHANGE PERP+PERP: long=perp[ex_a (neg funding)], short=perp[ex_b (pos)]
         (NET funding > CROSS_PERP_PERP_MIN_NET_PERIOD, both ≠ same exchange)
  - Score each by net APR after fees + cross-exchange friction.
  - Drop those failing MIN_NET_APR_PCT.

Returned list is sorted by net APR desc.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ..config import (
    CROSS_EXCHANGE_FRICTION_BPS,
    CROSS_PERP_PERP_MIN_NET_PERIOD,
    CROSS_SPOT_PERP_MIN_FUNDING_PERIOD,
    MIN_NET_APR_PCT,
    PERP_MAKER_FEES,
    PERP_SLIPPAGE_BPS,
    SAME_EXCHANGE_MIN_FUNDING_PERIOD,
    SPOT_MAKER_FEES,
    SPOT_SLIPPAGE_BPS,
)
from ..data.types import (
    CarryLeg,
    CarryOpportunity,
    FundingSnapshot,
    SpotSnapshot,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScoredOpportunity:
    opportunity: CarryOpportunity
    gross_apr_pct: float
    fees_per_cycle_pct: float
    breakeven_days: float
    net_apr_pct_7d: float
    kind: str


# ──────────────────────────────────────────────────────────────────────────


def _leg_fee(leg: CarryLeg) -> float:
    """Return the maker fee for one entry-or-exit fill on this leg."""
    if leg.leg_kind == "spot":
        return SPOT_MAKER_FEES.get(leg.exchange, 0.001)
    return PERP_MAKER_FEES.get(leg.exchange, 0.0002)


def _leg_slip(leg: CarryLeg) -> float:
    if leg.leg_kind == "spot":
        return SPOT_SLIPPAGE_BPS / 1e4
    return PERP_SLIPPAGE_BPS / 1e4


def _opportunity_economics(opp: CarryOpportunity) -> tuple[float, float]:
    """Return (fee_per_cycle_fraction, gross_funding_per_day_fraction).

    fee_per_cycle = enter LONG + enter SHORT + exit LONG + exit SHORT
                    (each = fee + slippage)
                  + cross-exchange friction (if applicable)
    """
    legs = (opp.long_leg, opp.short_leg)
    fee_total = sum((_leg_fee(l) + _leg_slip(l)) for l in legs) * 2  # entry+exit
    if not opp.same_exchange:
        fee_total += CROSS_EXCHANGE_FRICTION_BPS / 1e4

    gross_per_day = opp.net_funding_per_period   # already periods/day adjusted
    return fee_total, gross_per_day


def _score(opp: CarryOpportunity) -> Optional[ScoredOpportunity]:
    fees, gross_per_day = _opportunity_economics(opp)
    if gross_per_day <= 0:
        return None
    gross_apr_pct = gross_per_day * 365 * 100.0
    breakeven_days = fees / gross_per_day
    net_apr_pct_7d = (gross_per_day * 7 - fees) * (365.0 / 7.0) * 100.0
    return ScoredOpportunity(
        opportunity=opp,
        gross_apr_pct=gross_apr_pct,
        fees_per_cycle_pct=fees * 100.0,
        breakeven_days=breakeven_days,
        net_apr_pct_7d=net_apr_pct_7d,
        kind=opp.kind,
    )


def build_opportunities(
    perps: list[FundingSnapshot],
    spots: list[SpotSnapshot],
) -> list[CarryOpportunity]:
    """Enumerate all candidate (long, short) leg combinations grouped by base."""
    now = max((p.fetched_ts for p in perps), default=0.0) or 0.0

    perps_by_base: dict[str, list[FundingSnapshot]] = {}
    for p in perps:
        perps_by_base.setdefault(p.base, []).append(p)
    spots_by_base: dict[str, list[SpotSnapshot]] = {}
    for s in spots:
        spots_by_base.setdefault(s.base, []).append(s)

    bases = set(perps_by_base) | set(spots_by_base)
    out: list[CarryOpportunity] = []

    for base in bases:
        pls = perps_by_base.get(base, [])
        sls = spots_by_base.get(base, [])

        # 1) SAME-EXCHANGE SPOT+PERP (classic)
        spots_by_ex = {s.exchange: s for s in sls}
        for p in pls:
            if p.funding_rate < SAME_EXCHANGE_MIN_FUNDING_PERIOD:
                continue
            sp = spots_by_ex.get(p.exchange)
            if sp is None:
                continue
            opp = CarryOpportunity(
                base=base,
                long_leg=CarryLeg(exchange=sp.exchange, leg_kind="spot",
                                  direction="LONG", symbol=sp.symbol,
                                  price=sp.price, funding_rate=0.0,
                                  funding_interval_h=0.0, next_funding_ts=0,
                                  volume_usd=sp.quote_volume_24h_usd),
                short_leg=CarryLeg(exchange=p.exchange, leg_kind="perp",
                                   direction="SHORT", symbol=p.symbol,
                                   price=p.mark_price, funding_rate=p.funding_rate,
                                   funding_interval_h=p.funding_interval_h,
                                   next_funding_ts=p.next_funding_ts,
                                   volume_usd=p.quote_volume_24h_usd),
                fetched_ts=now,
            )
            out.append(opp)

        # 2) CROSS-EXCHANGE SPOT+PERP
        for p in pls:
            if p.funding_rate < CROSS_SPOT_PERP_MIN_FUNDING_PERIOD:
                continue
            for sp in sls:
                if sp.exchange == p.exchange:
                    continue   # not cross
                out.append(CarryOpportunity(
                    base=base,
                    long_leg=CarryLeg(exchange=sp.exchange, leg_kind="spot",
                                      direction="LONG", symbol=sp.symbol,
                                      price=sp.price, funding_rate=0.0,
                                      funding_interval_h=0.0, next_funding_ts=0,
                                      volume_usd=sp.quote_volume_24h_usd),
                    short_leg=CarryLeg(exchange=p.exchange, leg_kind="perp",
                                       direction="SHORT", symbol=p.symbol,
                                       price=p.mark_price, funding_rate=p.funding_rate,
                                       funding_interval_h=p.funding_interval_h,
                                       next_funding_ts=p.next_funding_ts,
                                       volume_usd=p.quote_volume_24h_usd),
                    fetched_ts=now,
                ))

        # 3) CROSS-EXCHANGE PERP+PERP (funding arb)
        if len(pls) >= 2:
            # Try each (long_candidate, short_candidate) cross pair
            for long_p in pls:
                if long_p.funding_rate >= 0:
                    continue   # we want NEGATIVE on the long leg
                for short_p in pls:
                    if short_p.exchange == long_p.exchange:
                        continue
                    if short_p.funding_rate <= 0:
                        continue   # need POSITIVE on short leg
                    # Net funding (already converted to per-day in property)
                    out.append(CarryOpportunity(
                        base=base,
                        long_leg=CarryLeg(exchange=long_p.exchange, leg_kind="perp",
                                          direction="LONG", symbol=long_p.symbol,
                                          price=long_p.mark_price,
                                          funding_rate=long_p.funding_rate,
                                          funding_interval_h=long_p.funding_interval_h,
                                          next_funding_ts=long_p.next_funding_ts,
                                          volume_usd=long_p.quote_volume_24h_usd),
                        short_leg=CarryLeg(exchange=short_p.exchange, leg_kind="perp",
                                           direction="SHORT", symbol=short_p.symbol,
                                           price=short_p.mark_price,
                                           funding_rate=short_p.funding_rate,
                                           funding_interval_h=short_p.funding_interval_h,
                                           next_funding_ts=short_p.next_funding_ts,
                                           volume_usd=short_p.quote_volume_24h_usd),
                        fetched_ts=now,
                    ))

    return out


def score_opportunities(
    opps: list[CarryOpportunity],
    *,
    min_net_apr_pct: float = MIN_NET_APR_PCT,
) -> list[ScoredOpportunity]:
    out: list[ScoredOpportunity] = []
    for opp in opps:
        # Type-specific min funding gates already applied in build_opportunities,
        # but PERP+PERP needs net funding gate now (after pairing both rates).
        if opp.kind == "perp+perp_cross":
            if opp.net_funding_per_period < CROSS_PERP_PERP_MIN_NET_PERIOD * (24.0 / 8.0):
                continue
        scored = _score(opp)
        if scored is None:
            continue
        if scored.net_apr_pct_7d < min_net_apr_pct:
            continue
        out.append(scored)
    out.sort(key=lambda o: o.net_apr_pct_7d, reverse=True)
    return out
