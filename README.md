# funding_carry

Delta-neutral **cash-and-carry** paper trader on Binance + Bybit + MEXC.

## Strategy

For each USDT-base that has BOTH a spot pair AND a perp on the SAME exchange:

1. If perp funding rate ≥ +0.03% per 8h (≈ 32% APR gross) → **opportunity**.
2. Open LONG spot + SHORT perp at equal notional (delta-neutral).
3. Collect funding every settlement (short perp receives funding when rate > 0).
4. Exit when funding decays below 0.01% per 8h, flips negative, or basis drifts
   beyond ±30 bps from entry.

Net APR after roundtrip fees:
- **MEXC**: ~0.14% roundtrip (cheapest) → break-even in ~1 funding cycle
- **Binance**: ~0.28% roundtrip
- **Bybit**: ~0.31% roundtrip

Refused if projected 7-day-hold net APR < 8%.

## Why it works

Positive funding = leveraged longs are paying shorts. When everyone wants to be
long, the market rewards us for taking the unwanted side. Our spot leg
neutralises the directional risk — we don't care if BTC moves $5000 either way.

## Why it can fail

Catalogued in code comments:
- **Funding flip**: rate goes negative → we start PAYING. Exit trigger handles.
- **Basis drift**: spot vs perp diverges (especially during stress). Drift
  beyond 30 bps from entry → exit.
- **Liquidation**: AVOIDED by 1x leverage (fully collateralised). The perp short
  is backed dollar-for-dollar by the spot long.
- **Fee drag**: roundtrip costs eat into yield. Minimum APR gate keeps these
  trades worth doing.
- **Funding cap**: exchanges cap funding rates. If a coin's rate is "stuck"
  at ±2% intraday, we can't capture more than what posts at settlement.

## How it works (data flow)

```
                    ┌──────────────────────────────────────┐
                    │  fetch perps + spot on all 3 venues  │
                    │      (6 endpoints, parallel)         │
                    └────────────────┬─────────────────────┘
                                     ▼
                              join by base
                                  │
                                  ▼
                          CarryPair candidates
                                  │
                                  ▼
                       score_opportunities()
              ↳ funding ≥ thresh
              ↳ |basis| ≤ 30 bps
              ↳ projected net APR ≥ 8%
                                  │
                                  ▼
                          PaperBook.open()
                  ↳ buy spot at slip
                  ↳ short perp at slip
                  ↳ pay both leg's taker fees
                                  │
              ─────────── every 5 min ──────────
                                  │
                                  ▼
                          PaperBook.mark()
                  ↳ credit funding at each settlement
                  ↳ exit if funding decays / flips / drift / max_hold
```

## Run

```bash
pip install -r requirements.txt
python -m funding_carry.main
```

Outputs:
- `logs/run.log`
- `state/paper_book.json`
- `state/trades.jsonl`
- `state/opportunities.jsonl`
- `state/funding_payments.jsonl`

## Config knobs (`config.py`)

- `ENTRY_FUNDING_THRESH`: min funding per interval to enter (default 0.03%)
- `EXIT_FUNDING_THRESH`: close when rate falls below (default 0.01%)
- `MIN_NET_APR_PCT`: refuse below this projected APR (default 8%)
- `MAX_BASIS_BPS`: max spot/perp divergence in bps (default 30)
- `MIN_HOLD_S` / `MAX_HOLD_S`: hold time bounds (default 8h to 30d)
- `ALLOCATION_PCT_PER_TRADE`: 20% of equity per opportunity
- `MAX_CONCURRENT_POSITIONS`: 5

## Going live

Same as `funding_directional`: paper mode uses only public data. Live would
require:
- Binance API key (spot trade + futures trade permissions)
- Bybit API key (spot + linear unified)
- MEXC API key (spot + futures)

Each exchange's keys can be added at the end as `.env`. Currently NOT required.
