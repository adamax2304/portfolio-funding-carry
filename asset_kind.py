"""
funding_carry/asset_kind.py

Sprint 53 (2026-06-10) — Funding-side asset-kind classifier.

Standalone, NO IMPORTS from news_trader.  Mirrors the Sprint 42/52 v1.3
EQUITY_TICKER_BLOCKLIST used by the listing detector, applied here to
funding-side candidates (FC main, spread-builder diagnostics, aggressive
paper lane).

Goal per Sprint 53 §7: stop funding analysis from being polluted by
equity/ETF/pre-IPO contamination.  Used DIAGNOSTIC-ONLY (no live trading
behavior change in this sprint).

Sprint 50–52 evidence of contamination:
  * SOXL  trade lost  −$11.12 (Sprint 50)
  * SKHYNIX trade lost −$14.77 (Sprint 52)
  * combined           −$25.89
  * conservative FC cumulative PnL ≈ −$104 over 26 trades, 25% of which
    is the 2 equity trades (5 of 26 = 19% are flagged equity in audit).

This module is the shared classifier.  It is meant to be imported by:
  * `aggressive_paper.py` (already has its own copy via Sprint 51; this
    module can be the source of truth)
  * `spread_based_builder.py` diagnostic (future Sprint to filter)
  * any new paper-side funding pipeline

NEVER imports executor, PaperBook, signal bus, or live API code.
"""
from __future__ import annotations

import re
from typing import Tuple


#: Equity / ETF / pre-IPO tickers that must NEVER be treated as crypto
#: in funding-side paper analysis.  Synced with listing parser v1.3
#: blocklist; expand here when new leaks appear.
EQUITY_TICKER_BLOCKLIST = frozenset({
    # Sprint 50/52 funding-side direct contamination
    "SOXL", "SKHYNIX",
    # Sprint 52 listing IR leaks
    "AAOI", "IREN", "ONDS", "QNTX", "URNM",
    # Sprint 38/42 listing detector
    "DELL", "SAMSUNG", "HYUNDAI", "AMAT", "NOKIA", "IBM",
    # US mega caps + semis
    "NVDA", "TSLA", "AAPL", "MSFT", "META", "GOOGL", "GOOG", "AMZN",
    "NFLX", "AMD", "INTC", "HPE", "SMCI", "AVGO", "TSM", "ASML",
    "ORCL", "SAP", "SONY", "TOYOTA", "HONDA", "BABA", "TENCENT",
    "QCOM", "CSCO", "MU", "TXN", "JPM", "BAC", "WMT", "PEP",
    "KO", "DIS", "BA", "GE", "F", "GM", "FORD", "XOM", "CVX",
    "COIN", "MSTR", "AMDSTOCK", "NBIS", "RKLB", "BMNR", "BE", "WDC",
    "SKYAI1",
    # ETF / index / leveraged
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "SOXS",
    "TQQQ", "SQQQ", "TLT", "GLD", "SLV", "USO",
    "URA", "URNJ",
    # Sector ETFs (SPDR family)
    "XLE", "XLF", "XLK", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU",
    "XLRE",
    # MSCI country ETFs.  Note: EWT excluded — Energy Web Token crypto.
    "EWJ", "EWG", "EWZ", "EWY", "EWU", "EWA",
    "FXI", "MCHI", "EEM", "IEMG", "VWO",
    # ARK Invest ETFs
    "ARKK", "ARKG", "ARKW",
    # Crypto-wrapper / trust products (not the crypto itself)
    "BITO", "BITI", "GBTC", "ETHE", "BITB",
    # Bond ETFs
    "USHY", "HYG", "JNK", "LQD",
    # Other US single-name equities
    "LLY", "PFE", "JNJ", "MRK", "ABBV", "UNH", "V", "MA",
    "PYPL", "SQ", "SHOP", "NET", "ZM", "SNOW", "PLTR",
    "RIVN", "LCID", "BLK", "GS", "MS", "C", "WFC",
    "CRM", "ADBE", "INTU", "WDAY",
    # Sprint 54 v1.4 additions (sync with news_trader parser_v12.py).
    # UVXY leaked as crypto in IR shadow on 2026-06-11.
    "UVXY", "VXX",
    "CIEN", "DKNG", "OUST", "PL", "EBAY",
})

#: Suffix patterns marking explicit equity-style perps.
_EQUITY_SUFFIX_RE = re.compile(
    r"(?i)(STOCK|STOCKS|SHARES?|EQUITY|EQUITIES)\b")

#: Tickers that overlap between crypto and equity (require explicit
#: equity context to flag).  Sprint 52 v1.3 inherits these.
AMBIGUOUS_TICKERS = frozenset({
    "ARM", "HOME", "SIGN", "EDEN", "ZORA", "DRAM", "WAL",
    "ANIME", "IRYS", "TSM", "EWT", "NOW",
    # Sprint 54: AMC (token + AMC Entertainment) and SATS (Bitcoin Satoshi token + SATS Holdings)
    "AMC", "SATS",
})

#: Title patterns that flag equity context for AMBIGUOUS tickers.
_EQUITY_CONTEXT_RE = re.compile(
    r"(?i)\b("
    r"trad\s*fi|tradfi|"
    r"pre[\s-]?market|pre[\s-]?ipo|"
    r"stock\s+perpetual|stock\s+futures?|stock\s+contract|"
    r"us[\s-]?stocks?|us[\s-]?equities|"
    r"korean?\s+stocks?|chinese?\s+stocks?|japan(ese)?\s+stocks?|"
    r"semiconductor|chipmaker|"
    r"earnings?\s+report|quarterly\s+results?|"
    r"company\s+(news|update|results?)|"
    r"etf|exchange[- ]?traded[- ]?fund"
    r")\b"
)

_PRE_IPO_RE = re.compile(r"(?i)\bpre[\s-]?ipo\b")
_ETF_HINT_RE = re.compile(
    r"(?i)\b(etf|exchange[- ]?traded[- ]?fund)\b")


def classify_asset_kind(symbol: str,
                        context_title: str = "") -> Tuple[str, str]:
    """Classify a funding-candidate symbol.

    Returns (asset_kind, reason).
    asset_kind ∈ {crypto, equity_perp, etf_perp, pre_ipo, unknown}.
    """
    sym = (symbol or "").upper().strip()
    text = context_title or ""
    if not sym:
        return "unknown", "missing_symbol"

    # Hard blocklist
    if sym in EQUITY_TICKER_BLOCKLIST:
        return "equity_perp", f"blocklist:{sym}"

    # Explicit equity suffix on the ticker itself
    if _EQUITY_SUFFIX_RE.search(sym):
        return "equity_perp", "equity_suffix"

    # Pre-IPO / ETF context
    if _PRE_IPO_RE.search(text):
        return "pre_ipo", "pre_ipo_in_title"
    if _ETF_HINT_RE.search(text):
        return "etf_perp", "etf_in_title"

    # Ambiguous overlap — only flip on explicit equity context
    if sym in AMBIGUOUS_TICKERS:
        if _EQUITY_CONTEXT_RE.search(text):
            return "equity_perp", "ambiguous_with_equity_context"
        return "crypto", "ambiguous_default_crypto"

    return "crypto", "default_crypto"


def is_execution_eligible(symbol: str,
                          context_title: str = "") -> Tuple[bool, dict]:
    """Top-level predicate for funding-side execution gating.

    Returns (eligible, info).  `eligible=True` iff `asset_kind == crypto`.
    """
    kind, reason = classify_asset_kind(symbol, context_title)
    info = {
        "asset_kind":         kind,
        "asset_kind_reason":  reason,
        "execution_eligible": kind == "crypto",
    }
    return info["execution_eligible"], info


def audit_trades(trades: list[dict],
                 base_field: str = "base") -> dict:
    """Pure read-only auditor for FC trades.jsonl rows.

    Counts trades, sums PnL, splits by asset_kind.  Caller passes the
    parsed JSONL list.  Returns a summary dict (used by Sprint 53
    report + tests).
    """
    by_kind: dict[str, dict] = {}
    contaminated: list[dict] = []
    for t in trades:
        sym = (t.get(base_field) or "").upper()
        kind, reason = classify_asset_kind(sym)
        bucket = by_kind.setdefault(kind, {"count": 0, "pnl_usd": 0.0,
                                            "funding_usd": 0.0})
        bucket["count"] += 1
        bucket["pnl_usd"]     += float(t.get("realized_pnl_usd") or 0)
        bucket["funding_usd"] += float(t.get("funding_collected_usd") or 0)
        if kind != "crypto":
            contaminated.append({
                "ts_iso":  t.get("ts_iso") or "",
                "base":    sym,
                "kind":    kind,
                "reason":  reason,
                "pnl_usd": float(t.get("realized_pnl_usd") or 0),
                "funding_usd": float(t.get("funding_collected_usd") or 0),
                "exit_reason": (t.get("exit_reason") or "")[:60],
            })
    return {
        "total_trades":       len(trades),
        "by_kind":            by_kind,
        "contaminated":       contaminated,
        "contaminated_count": len(contaminated),
    }
