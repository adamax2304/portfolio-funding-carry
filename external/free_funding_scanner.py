"""
funding_carry/external/free_funding_scanner.py

Sprint 20 — Free cross-exchange funding universe scanner (SHADOW-only).

Polls PUBLIC funding endpoints (no auth) of major perpetual venues, normalizes
symbols, finds cross-exchange funding-spread arbitrage opportunities, validates
which legs are executable, and writes JSONL.  Designed as the free alternative
to CoinGlass after Sprint 19 found CoinGlass requires a paid API key.

Implemented adapters (Priority 1):
  * Binance USDT-perps   (fapi.binance.com)
  * Bybit linear         (api.bybit.com)
  * Gate USDT futures    (api.gateio.ws)
  * MEXC contract        (contract.mexc.com)
  * Bitget USDT-FUTURES  (api.bitget.com)
Deferred (per-instrument calls / TBD):
  * OKX, KuCoin, HTX, BingX, CoinEx, Hyperliquid — Sprint 20.1+

HARD GUARANTEES:
  * Pure read-only.  NEVER calls executor / order / open_position.
  * NEVER triggers trades on CG/free-scanner signals.
  * Graceful per-exchange failures: one venue down → skip + continue.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = _ROOT / "state"
OPP_FILE = STATE_DIR / "free_funding_opportunities_shadow.jsonl"
COMPARE_FILE = STATE_DIR / "free_funding_internal_compare.jsonl"
REGIME_FILE = STATE_DIR / "free_funding_regime_summary.jsonl"

INTERNAL_OPP_FILE = STATE_DIR / "opportunities.jsonl"
INTERNAL_REJECT_FILE = STATE_DIR / "rejects.jsonl"

_HTTP_TIMEOUT_S = 25.0
_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}

# Internal venue universe (mirror Sprint 19 finding).
_INTERNAL_VENUES = {"binance", "bybit", "bitget", "mexc", "hyperliquid", "aster"}
_EXECUTABLE_VENUES = {"binance", "bybit", "mexc", "bitget"}

# Exchanges this scanner *can* read.
_DATA_ONLY_EXCHANGES = {"gate"}   # we can fetch but cannot trade

# ── Common record ──────────────────────────────────────────────────────────
@dataclass
class FundingRecord:
    exchange: str
    raw_symbol: str
    base: str
    quote: str
    contract_type: str            # "linear_perp"
    funding_rate: float | None    # raw, per its native interval
    funding_interval_h: float     # 1, 4, 8 typically
    next_funding_ts: float | None  # epoch seconds
    mark_price: float | None
    index_price: float | None = None
    volume_24h_usd: float | None = None
    open_interest_usd: float | None = None
    ts: float = field(default_factory=time.time)
    raw_keys: list[str] = field(default_factory=list)

    @property
    def funding_rate_8h(self) -> float | None:
        """Normalize to per-8h fraction."""
        if self.funding_rate is None or not self.funding_interval_h:
            return None
        return self.funding_rate * (8.0 / self.funding_interval_h)

    @property
    def annualized_apr_pct(self) -> float | None:
        """Annualized APR in % (3 cycles/day × 365 days = 1095)."""
        f8 = self.funding_rate_8h
        if f8 is None:
            return None
        return f8 * 1095.0 * 100.0


def _now_iso() -> str:
    return (datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


# ── Symbol normalization ───────────────────────────────────────────────────
_QUOTES = ("USDT", "USDC", "USD")
_BANNED_BASES = {
    # ETF / equity perps and known noise — keep out of crypto universe
    "SPY", "QQQ", "IWM", "SOXL", "SOXS", "TQQQ", "SQQQ",
    "LLY", "NVDA", "QCOM", "CSCO", "NBIS", "RKLB", "AMDSTOCK",
    "TSLA", "AAPL", "MSFT", "GOOG", "META", "AMZN", "NFLX", "COIN", "MSTR",
    "MULTIPLE", "STOCK", "STOCKS", "ETF",
}
_DATED_RE = re.compile(r"-\d{6,8}$")          # BTC-USDT-240329
_INVERSE_RE = re.compile(r"USD(?:M|_PERP)?$")  # USD-margined inverse
_PREFIX_RE = re.compile(r"^(1000|1M|100K|10K)([A-Z]+)$")  # 1000PEPE, 1MBABYDOGE


def normalize_symbol(raw: str) -> dict | None:
    """Return {base, quote, normalized_symbol} or None for excluded products.

    Conservative: 1000PEPE stays distinct from PEPE (kept as base="1000PEPE").
    Stock/ETF/dated/inverse-coin-margined are rejected with None.
    """
    if not raw:
        return None
    s = raw.upper().strip()
    # Drop -SWAP suffix (OKX), KuCoin "M" tag, USDT_PERP, etc.
    s = s.replace("-SWAP", "").replace("/", "").replace("_", "").replace("-", "")
    if _DATED_RE.search(raw.upper()):
        return None
    # Strip trailing M (KuCoin contract suffix)
    if s.endswith("USDTM"):
        s = s[:-1]
    base = None; quote = None
    for q in _QUOTES:
        if s.endswith(q):
            base = s[: -len(q)]; quote = q
            break
    if not base or not quote:
        return None
    # Inverse coin-margined products (BTCUSD without USDT/USDC) -> skip
    if quote == "USD" and s.endswith("USD") and not s.endswith("USDT") and not s.endswith("USDC"):
        # purely USD-margined inverse — skip
        return None
    if base in _BANNED_BASES:
        return None
    # length sanity
    if not (1 <= len(base) <= 14):
        return None
    if not re.fullmatch(r"[A-Z0-9]+", base):
        return None
    return {"base": base, "quote": quote, "normalized_symbol": f"{base}{quote}"}


# ── HTTP helper ────────────────────────────────────────────────────────────
def _http_json(url: str) -> tuple[int | None, Any, str | None]:
    try:
        import httpx
    except Exception as exc:  # noqa: BLE001
        return None, None, f"httpx_unavailable:{exc}"
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True,
                          headers=_BROWSER_HEADERS) as c:
            r = c.get(url)
            if r.status_code != 200:
                return r.status_code, None, f"http_{r.status_code}"
            return r.status_code, r.json(), None
    except Exception as exc:  # noqa: BLE001
        return None, None, str(exc)[:200]


def _f(v, default=None):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


# ── Adapters ───────────────────────────────────────────────────────────────
def fetch_binance() -> tuple[list[FundingRecord], str | None]:
    status, data, err = _http_json(
        "https://fapi.binance.com/fapi/v1/premiumIndex")
    if err or not isinstance(data, list):
        return [], err or "non_list"
    out: list[FundingRecord] = []
    now = time.time()
    for it in data:
        if not isinstance(it, dict):
            continue
        sym = it.get("symbol", "")
        norm = normalize_symbol(sym)
        if not norm:
            continue
        nfx = _f(it.get("nextFundingTime"))
        next_ts = nfx / 1000.0 if nfx and nfx > 1e12 else nfx
        interval_h = 8.0
        if next_ts:
            delta_h = (next_ts - now) / 3600
            if 0 < delta_h <= 4.5:
                interval_h = 4.0
            elif 0 < delta_h <= 1.5:
                interval_h = 1.0
        out.append(FundingRecord(
            exchange="binance", raw_symbol=sym,
            base=norm["base"], quote=norm["quote"], contract_type="linear_perp",
            funding_rate=_f(it.get("lastFundingRate")),
            funding_interval_h=interval_h, next_funding_ts=next_ts,
            mark_price=_f(it.get("markPrice")),
            index_price=_f(it.get("indexPrice")),
            raw_keys=list(it.keys()),
        ))
    return out, None


def fetch_bybit() -> tuple[list[FundingRecord], str | None]:
    status, data, err = _http_json(
        "https://api.bybit.com/v5/market/tickers?category=linear")
    if err or not isinstance(data, dict):
        return [], err or "non_dict"
    items = (data.get("result") or {}).get("list") or []
    out: list[FundingRecord] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sym = it.get("symbol", "")
        norm = normalize_symbol(sym)
        if not norm:
            continue
        interval = _f(it.get("fundingIntervalHour"), 8.0) or 8.0
        nfx = _f(it.get("nextFundingTime"))
        next_ts = nfx / 1000.0 if nfx and nfx > 1e12 else nfx
        out.append(FundingRecord(
            exchange="bybit", raw_symbol=sym,
            base=norm["base"], quote=norm["quote"], contract_type="linear_perp",
            funding_rate=_f(it.get("fundingRate")),
            funding_interval_h=interval, next_funding_ts=next_ts,
            mark_price=_f(it.get("markPrice")),
            index_price=_f(it.get("indexPrice")),
            volume_24h_usd=_f(it.get("turnover24h")),
            open_interest_usd=_f(it.get("openInterestValue")),
            raw_keys=list(it.keys()),
        ))
    return out, None


def fetch_gate() -> tuple[list[FundingRecord], str | None]:
    status, data, err = _http_json(
        "https://api.gateio.ws/api/v4/futures/usdt/contracts")
    if err or not isinstance(data, list):
        return [], err or "non_list"
    out: list[FundingRecord] = []
    for it in data:
        if not isinstance(it, dict):
            continue
        if it.get("in_delisting"):
            continue
        name = it.get("name", "")  # e.g. "BTC_USDT"
        norm = normalize_symbol(name)
        if not norm:
            continue
        # funding_interval is in seconds
        interval_s = _f(it.get("funding_interval"))
        interval_h = (interval_s / 3600.0) if interval_s else 8.0
        out.append(FundingRecord(
            exchange="gate", raw_symbol=name,
            base=norm["base"], quote=norm["quote"], contract_type="linear_perp",
            funding_rate=_f(it.get("funding_rate")),
            funding_interval_h=interval_h,
            next_funding_ts=_f(it.get("funding_next_apply")),
            mark_price=_f(it.get("mark_price_round")) or _f(it.get("last_price")),
            index_price=_f(it.get("index_price")),
            raw_keys=list(it.keys()),
        ))
    return out, None


def fetch_mexc() -> tuple[list[FundingRecord], str | None]:
    status, data, err = _http_json(
        "https://contract.mexc.com/api/v1/contract/funding_rate")
    if err or not isinstance(data, dict):
        return [], err or "non_dict"
    items = data.get("data") or []
    out: list[FundingRecord] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sym = it.get("symbol", "")  # e.g. "BTC_USDT"
        norm = normalize_symbol(sym)
        if not norm:
            continue
        # collectCycle in MEXC: typically 8 (hours) — value '8'
        interval = _f(it.get("collectCycle"), 8.0) or 8.0
        nfx = _f(it.get("nextSettleTime"))
        next_ts = nfx / 1000.0 if nfx and nfx > 1e12 else nfx
        out.append(FundingRecord(
            exchange="mexc", raw_symbol=sym,
            base=norm["base"], quote=norm["quote"], contract_type="linear_perp",
            funding_rate=_f(it.get("fundingRate")),
            funding_interval_h=interval, next_funding_ts=next_ts,
            mark_price=_f(it.get("fairPrice")) or _f(it.get("idxPrice")),
            index_price=_f(it.get("idxPrice")),
            raw_keys=list(it.keys()),
        ))
    return out, None


def fetch_bitget() -> tuple[list[FundingRecord], str | None]:
    status, data, err = _http_json(
        "https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES")
    if err or not isinstance(data, dict):
        return [], err or "non_dict"
    items = data.get("data") or []
    out: list[FundingRecord] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sym = it.get("symbol", "")  # e.g. "BTCUSDT"
        norm = normalize_symbol(sym)
        if not norm:
            continue
        # Bitget USDT-FUTURES has 8h funding by default.
        out.append(FundingRecord(
            exchange="bitget", raw_symbol=sym,
            base=norm["base"], quote=norm["quote"], contract_type="linear_perp",
            funding_rate=_f(it.get("fundingRate")),
            funding_interval_h=8.0,
            next_funding_ts=None,
            mark_price=_f(it.get("markPrice")) or _f(it.get("lastPr")),
            volume_24h_usd=_f(it.get("usdtVolume")) or _f(it.get("quoteVolume")),
            raw_keys=list(it.keys()),
        ))
    return out, None


_ADAPTERS: dict[str, Callable[[], tuple[list[FundingRecord], str | None]]] = {
    "binance": fetch_binance, "bybit": fetch_bybit, "gate": fetch_gate,
    "mexc": fetch_mexc, "bitget": fetch_bitget,
}


# ── Cross-exchange opportunity builder ─────────────────────────────────────
def build_universe(records: list[FundingRecord]) -> dict[str, dict[str, FundingRecord]]:
    """{base -> {exchange -> FundingRecord}}.  Only USDT-quoted kept."""
    by_base: dict[str, dict[str, FundingRecord]] = {}
    for r in records:
        if r.quote != "USDT":
            continue
        if r.funding_rate is None:
            continue
        by_base.setdefault(r.base, {})[r.exchange] = r
    return by_base


def build_opportunities(universe: dict[str, dict[str, FundingRecord]],
                        ) -> list[dict]:
    """For each base on 2+ venues, build the BEST long/short pair by funding
    spread.  Direction: long the leg that PAYS the long (funding ≤ 0 typically),
    short the leg that PAYS the short (funding > 0)."""
    opps: list[dict] = []
    for base, by_ex in universe.items():
        if len(by_ex) < 2:
            continue
        venues = list(by_ex.items())
        # Pick min-funding venue as LONG leg, max as SHORT leg.
        long_ex, long_rec = min(venues, key=lambda kv: kv[1].funding_rate_8h or 0)
        short_ex, short_rec = max(venues, key=lambda kv: kv[1].funding_rate_8h or 0)
        if long_ex == short_ex:
            continue
        lf8 = long_rec.funding_rate_8h or 0.0
        sf8 = short_rec.funding_rate_8h or 0.0
        spread8 = sf8 - lf8
        if spread8 <= 0:
            continue
        expected_funding_2c = spread8 * 2.0       # 2 cycles minimum hold
        gross_apr = spread8 * 1095.0 * 100.0
        # Mark price spread (info only)
        mark_spread_bps = None
        if long_rec.mark_price and short_rec.mark_price:
            mp = (long_rec.mark_price + short_rec.mark_price) / 2.0
            if mp > 0:
                mark_spread_bps = abs(long_rec.mark_price - short_rec.mark_price) / mp * 1e4
        vols = [v for v in (long_rec.volume_24h_usd, short_rec.volume_24h_usd)
                if v is not None]
        ois = [v for v in (long_rec.open_interest_usd, short_rec.open_interest_usd)
               if v is not None]
        both_executable = (long_ex in _EXECUTABLE_VENUES
                           and short_ex in _EXECUTABLE_VENUES)
        execution_supported = both_executable
        data_only = [ex for ex in (long_ex, short_ex)
                     if ex not in _EXECUTABLE_VENUES]
        opps.append({
            "ts": time.time(),
            "ts_iso": _now_iso(),
            "source": "free_funding_scanner",
            "base": base,
            "normalized_symbol": f"{base}USDT",
            "long_exchange": long_ex,
            "short_exchange": short_ex,
            "long_funding_8h": lf8,
            "short_funding_8h": sf8,
            "funding_spread_8h": spread8,
            "gross_apr": gross_apr,
            "expected_funding_2_cycles": expected_funding_2c,
            "long_mark": long_rec.mark_price,
            "short_mark": short_rec.mark_price,
            "mark_spread_bps": mark_spread_bps,
            "volume_24h_min": min(vols) if vols else None,
            "open_interest_min": min(ois) if ois else None,
            "both_executable": both_executable,
            "execution_supported": execution_supported,
            "data_only_exchanges": data_only,
            "mode": "shadow",
            "active_trading": False,
            "raw_refs": {
                long_ex: long_rec.raw_symbol,
                short_ex: short_rec.raw_symbol,
            },
        })
    opps.sort(key=lambda o: o["gross_apr"], reverse=True)
    return opps


# ── Internal compare ───────────────────────────────────────────────────────
def _load_recent_internal(window_s: int = 6 * 3600) -> tuple[dict, dict]:
    now = time.time()
    opps: dict[str, dict] = {}
    rejects: dict[str, list[str]] = {}
    if INTERNAL_OPP_FILE.exists():
        for line in INTERNAL_OPP_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                j = json.loads(line)
            except Exception:
                continue
            if (j.get("ts") or 0) < now - window_s:
                continue
            base = (j.get("base") or "").upper()
            if not base:
                continue
            prev = opps.get(base)
            if prev is None or (j.get("ts") or 0) > (prev.get("ts") or 0):
                opps[base] = j
    if INTERNAL_REJECT_FILE.exists():
        for line in INTERNAL_REJECT_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                j = json.loads(line)
            except Exception:
                continue
            if (j.get("ts") or 0) < now - window_s:
                continue
            base = (j.get("base") or "").upper()
            if not base:
                continue
            rejects.setdefault(base, []).append(j.get("reason") or "")
    return opps, rejects


def compare_to_internal(opp: dict, internal_opps: dict,
                        internal_rejects: dict) -> dict:
    base = (opp.get("base") or "").upper()
    long_ex = (opp.get("long_exchange") or "").lower()
    short_ex = (opp.get("short_exchange") or "").lower()
    pair_venues = {long_ex, short_ex}
    missing_in_internal = pair_venues - _INTERNAL_VENUES
    non_executable = pair_venues - _EXECUTABLE_VENUES

    internal = internal_opps.get(base)
    reject_list = internal_rejects.get(base, [])
    seen = internal is not None

    if missing_in_internal:
        verdict = f"missing_exchange:{','.join(sorted(missing_in_internal))}"
    elif non_executable:
        verdict = f"non_executable:{','.join(sorted(non_executable))}"
    elif seen and reject_list:
        verdict = f"internal_filter:{reject_list[-1]}"
    elif seen:
        verdict = "matched"
    else:
        verdict = "symbol_mapping_or_no_recent_data"

    return {
        "ts": time.time(),
        "base": base,
        "normalized_symbol": opp.get("normalized_symbol"),
        "long_exchange": long_ex,
        "short_exchange": short_ex,
        "free_gross_apr": opp.get("gross_apr"),
        "free_spread_8h": opp.get("funding_spread_8h"),
        "internal_seen": seen,
        "internal_gross_apr": (internal or {}).get("gross_apr"),
        "internal_reject_reason": reject_list[-1] if reject_list else None,
        "executable": opp.get("both_executable"),
        "missing_reason": (f"non_executable:{','.join(sorted(non_executable))}"
                           if non_executable else None),
        "verdict": verdict,
    }


# ── Regime summary ─────────────────────────────────────────────────────────
def regime_summary(opps: list[dict]) -> dict:
    import statistics as _st
    aprs = [o["gross_apr"] for o in opps if o["gross_apr"] is not None]
    bases = {o["base"] for o in opps}
    pairs = {(o["long_exchange"], o["short_exchange"]) for o in opps}
    return {
        "ts": time.time(), "ts_iso": _now_iso(),
        "total_opportunities": len(opps),
        "spread_8h_gt_015pct": sum(1 for o in opps if (o["funding_spread_8h"] or 0) > 0.0015),
        "spread_8h_gt_020pct": sum(1 for o in opps if (o["funding_spread_8h"] or 0) > 0.0020),
        "spread_8h_gt_030pct": sum(1 for o in opps if (o["funding_spread_8h"] or 0) > 0.0030),
        "gross_apr_gt_100pct": sum(1 for o in opps if (o["gross_apr"] or 0) > 100),
        "gross_apr_gt_200pct": sum(1 for o in opps if (o["gross_apr"] or 0) > 200),
        "executable_clean": sum(1 for o in opps
                                if o["both_executable"]
                                and (o["gross_apr"] or 0) > 100),
        "unique_symbols": len(bases),
        "unique_exchange_pairs": len(pairs),
        "median_gross_apr": round(_st.median(aprs), 2) if aprs else None,
        "top_apr": round(max(aprs), 2) if aprs else None,
    }


# ── JSONL writer ───────────────────────────────────────────────────────────
def append_jsonl(path: Path, record: dict) -> bool:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False,
                               separators=(",", ":")) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("[free_funding] jsonl write fail: %s", exc)
        return False


# ── Scan orchestration ────────────────────────────────────────────────────
def scan_once(*, write: bool = True,
              exchanges: tuple[str, ...] = ("binance", "bybit", "gate",
                                            "mexc", "bitget"),
              ) -> dict:
    fetch_status: dict[str, dict] = {}
    all_records: list[FundingRecord] = []
    for ex in exchanges:
        adapter = _ADAPTERS.get(ex)
        if not adapter:
            fetch_status[ex] = {"ok": False, "error": "no_adapter", "n": 0}
            continue
        try:
            recs, err = adapter()
        except Exception as exc:  # noqa: BLE001
            fetch_status[ex] = {"ok": False, "error": str(exc)[:160], "n": 0}
            continue
        fetch_status[ex] = {"ok": err is None, "error": err, "n": len(recs)}
        all_records.extend(recs)

    universe = build_universe(all_records)
    opps = build_opportunities(universe)
    internal_opps, internal_rejects = _load_recent_internal()
    compares = [compare_to_internal(o, internal_opps, internal_rejects)
                for o in opps[:200]]
    summary = regime_summary(opps)
    summary["fetch_status"] = fetch_status
    summary["raw_records"] = len(all_records)
    summary["normalized_bases"] = len(universe)

    if write:
        for o in opps:
            append_jsonl(OPP_FILE, o)
        for c in compares:
            append_jsonl(COMPARE_FILE, c)
        append_jsonl(REGIME_FILE, summary)

    print(f"[free_funding] raw={len(all_records)} bases={len(universe)} "
          f"opps={len(opps)} clean_exec={summary['executable_clean']} "
          f"top_apr={summary['top_apr']}")
    for ex, st in fetch_status.items():
        flag = "OK" if st["ok"] else f"FAIL({st['error']})"
        print(f"  {ex:8s} {flag} n={st['n']}")
    return summary


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    summary = scan_once()
    print(json.dumps({k: v for k, v in summary.items() if k != "fetch_status"},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
