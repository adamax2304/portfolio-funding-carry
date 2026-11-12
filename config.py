"""
funding_carry/config.py

Delta-neutral carry strategy — same-exchange AND cross-exchange.

Strategy variants
=================

A) SAME-EXCHANGE CASH-AND-CARRY
   LONG  spot  on venue X
   SHORT perp  on venue X (funding > +0.03%/8h)
   → no transfer risk, simplest book-keeping

B) CROSS-EXCHANGE SPOT–PERP
   LONG  spot  on venue X (cheap fees, e.g. MEXC)
   SHORT perp  on venue Y (funding > +0.05%/8h, e.g. Hyperliquid)
   → capture wider spreads, but two-venue execution risk

C) CROSS-EXCHANGE PERP–PERP FUNDING ARB
   LONG  perp  on venue X (funding < -0.03% — we get paid as long)
   SHORT perp  on venue Y (funding > +0.05% — we get paid as short)
   → pure funding arbitrage, no spot involved (5× capital efficient)

Safety gates (all variants)
---------------------------
  • Net funding APR after fees ≥ MIN_NET_APR_PCT
  • Price match between legs at entry: |buy_px − sell_px| / mid ≤ MAX_LEG_PRICE_DIFF_BPS
  • Orderbook depth on BOTH legs: ≥ DEPTH_MULTIPLIER × notional within ENTRY_SLIP_PCT
  • Spread on each leg ≤ MAX_SPREAD_BPS
  • Hold UNTIL one of:
      (a) Funding profitability decays to zero (exit cost > remaining funding income)
      (b) Basis blows out beyond entry_match ± MAX_BASIS_DRIFT_BPS  → SL

Leverage: fixed 5× on perp legs.
"""
import os

# ─── data layer ──────────────────────────────────────────────────────────────
POLL_INTERVAL_S = 60.0            # 1-min precision needed for 5-min entry window
VOLUME_FLOOR_USD_24H = 10_000_000
SYMBOL_BLACKLIST = {
    "USDCUSDT", "BUSDUSDT", "FDUSDUSDT", "TUSDUSDT", "DAIUSDT",
    "USDPUSDT", "EURUSDT", "GBPUSDT",
}

# ─── opportunity gates ───────────────────────────────────────────────────────
# Per-period (typically 8h) funding minimums BY OPPORTUNITY TYPE
SAME_EXCHANGE_MIN_FUNDING_PERIOD = 0.00030    # 0.03%/8h ≈ 32% APR gross
CROSS_SPOT_PERP_MIN_FUNDING_PERIOD = 0.00040  # cross has more friction
CROSS_PERP_PERP_MIN_NET_PERIOD = 0.00050      # need higher edge for 2 perp legs

# Net APR floor after all fees
MIN_NET_APR_PCT = 10.0

# Price-match check at entry (long-leg buy vs short-leg sell, in bps of mid)
MAX_LEG_PRICE_DIFF_BPS = 15.0   # 0.15% — tighter when crypto markets sync

# Orderbook gates
DEPTH_MULTIPLIER = 2.0
ENTRY_SLIP_PCT = 0.001          # consider top 10 bps of book
MAX_SPREAD_BPS = 10.0

# ─── entry timing ────────────────────────────────────────────────────────────
# Enter exactly 5 minutes before the next funding settlement so we are
# already filled when settlement occurs and collect funding immediately.
ENTRY_LEAD_TIME_S   = 300.0     # target: 5 min before settlement
ENTRY_WINDOW_HALF_S =  60.0     # accept ±60 s around target (240 s – 360 s window)

# Hold/Exit
MIN_HOLD_S = 8 * 3600           # at least one funding cycle
MAX_HOLD_S = 30 * 24 * 3600     # safety upper bound
MAX_BASIS_DRIFT_BPS = 40.0      # absolute drift from entry → SL exit

# Hold-until-profit-zero exit: when projected_remaining_funding_to_exit <= exit_cost
EXIT_PROFIT_ZERO_BUFFER = 1.0   # multiplier on exit_cost (≥1.0)

# ─── allocation ──────────────────────────────────────────────────────────────
PAPER_STARTING_BALANCE_USD = 10_000.0
LEVERAGE = 5                    # fixed 5× on perp legs
MAX_CONCURRENT_POSITIONS = 5
ALLOCATION_PCT_PER_TRADE = 0.20
MAX_ALLOCATION_PER_BASE = 0.30

# ─── fees ────────────────────────────────────────────────────────────────────
PERP_TAKER_FEES = {
    "binance":     0.00040, "bybit": 0.00055, "mexc": 0.00020,
    "bitget":      0.00060, "aster": 0.00035, "hyperliquid": 0.00035,
}
PERP_MAKER_FEES = {
    "binance":     0.00020, "bybit": 0.00020, "mexc": 0.00000,
    "bitget":      0.00020, "aster": 0.00010, "hyperliquid": 0.00010,
}
SPOT_TAKER_FEES = {
    "binance": 0.00100, "bybit": 0.00100, "mexc": 0.00050, "bitget": 0.00100,
}
SPOT_MAKER_FEES = {
    "binance": 0.00100, "bybit": 0.00100, "mexc": 0.00000, "bitget": 0.00080,
}

# Slippage model (paper, limit orders)
SPOT_SLIPPAGE_BPS = 4.0
PERP_SLIPPAGE_BPS = 3.0

# Cross-exchange transfer cost (only applied if legs on different venues
# require redistributing collateral — paper just charges a flat 5 bps as
# approximation of basis-arbitrage friction).
CROSS_EXCHANGE_FRICTION_BPS = 5.0

# ─── persistence ─────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR  = os.path.join(_HERE, "state")
LOGS_DIR   = os.path.join(_HERE, "logs")
PAPER_BOOK_FILE     = os.path.join(STATE_DIR, "paper_book.json")
TRADE_LOG_FILE      = os.path.join(STATE_DIR, "trades.jsonl")
OPPORTUNITY_LOG_FILE = os.path.join(STATE_DIR, "opportunities.jsonl")
REJECT_LOG_FILE     = os.path.join(STATE_DIR, "rejects.jsonl")
FUNDING_HISTORY_FILE = os.path.join(STATE_DIR, "funding_payments.jsonl")

PAPER_MODE = True

# ─── Sprint 1 (2026-05-26) — CARRY_SAFE_DIAGNOSTIC_MODE ──────────────────────
# When True, additional stricter entry gates are applied (fee coverage,
# minimum funding rate, net APR), already-open positions continue to be
# managed normally, and live trading remains paper. Set to False to
# fall back to the pre-Sprint-1 (looser) behaviour without code changes.
import os as _os_for_flag
CARRY_SAFE_DIAGNOSTIC_MODE: bool = (
    _os_for_flag.getenv("CARRY_SAFE_DIAGNOSTIC_MODE", "true").lower()
    in ("1", "true", "yes", "y")
)

# Stricter entry thresholds (only used when CARRY_SAFE_DIAGNOSTIC_MODE=True).
# Each is per 8h (one funding settlement) unless noted otherwise.
MIN_FUNDING_RATE_PER_PERIOD_SAFE: float = 0.003   # 0.3%/8h ≈ 327% APR
MIN_FEE_COVERAGE_RATIO_SAFE:      float = 2.0     # expected_funding_2cycles ≥ 2× roundtrip fees
MIN_NET_APR_PCT_SAFE:             float = 80.0    # 80%/yr after fees (7-day projection)

# Symbol cooldown after a bad exit (negative PnL or profit_zero_decayed).
# Same base cannot be re-opened until cooldown expires.
SYMBOL_COOLDOWN_AFTER_BAD_EXIT_S: int   = 24 * 3600           # 24 h
BAD_EXIT_PNL_THRESHOLD_USD:       float = 0.0                 # PnL ≤ 0 counts as bad
BAD_EXIT_REASONS:                 set[str] = {
    "profit_zero_decayed",
    "basis_drift",            # any basis_drift_* exit
}

# Temporary base blacklist (paper diagnostics only — never persisted).
# NVDA: 1 entry, $0 funding income, fees -$2.6 → pure loss.
# NEAR: 2 repeated entries, both profit_zero_decayed, -$5.40 total.
TEMP_BLACKLIST_BASES: set[str] = {"NVDA", "NEAR"}

# Minimum number of funding cycles to collect before profit_zero_decayed
# is allowed to fire. Basis_drift and max_hold can still exit immediately.
MIN_HOLD_CYCLES_FOR_PROFIT_ZERO: int = 2

# Diagnostic log prefix for fee-recovery extended holds.
HOLD_EXTENDED_LOG_TAG = "hold_extended_for_fee_recovery"
