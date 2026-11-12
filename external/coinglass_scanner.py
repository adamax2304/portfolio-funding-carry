"""
funding_carry/external/coinglass_scanner.py

Sprint 19 — CoinGlass funding-rate arbitrage external scanner (SHADOW-only).

HARD GUARANTEES:
  * Reads only.  NEVER calls executor / order / open_position.
  * NEVER trades on CoinGlass-derived signals — they pass through depth/spread
    validation against our internal scanner before any future use.
  * Graceful no-API-key path: prints a clean message and exits 0.
  * Graceful endpoint failures: each tier is best-effort, no crash.

Output:
  funding_carry/state/coinglass_arbitrage_shadow.jsonl   (one record per opp)
  funding_carry/state/coinglass_internal_compare.jsonl   (CG-vs-internal join)

Usage:
  py -3 -m funding_carry.external.coinglass_scanner
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = _ROOT / "state"
SHADOW_FILE = STATE_DIR / "coinglass_arbitrage_shadow.jsonl"
COMPARE_FILE = STATE_DIR / "coinglass_internal_compare.jsonl"
INTERNAL_OPP_FILE = STATE_DIR / "opportunities.jsonl"
INTERNAL_REJECT_FILE = STATE_DIR / "rejects.jsonl"

# API endpoints (best-effort, may change upstream).
_API_V4 = ("https://open-api-v4.coinglass.com/api/futures/funding-rate/"
           "arbitrage")
_API_V2 = ("https://open-api.coinglass.com/public/v2/futures/"
           "fundingRateArbitrage")
_PAGE_URL = "https://www.coinglass.com/FrArbitrage"

_USD_NOTIONAL = 10_000
_DEFAULT_EXCHANGES = ("Binance", "OKX", "Bybit", "Bitget", "MEXC", "Gate",
                      "KuCoin", "HTX", "Hyperliquid", "WhiteBIT", "Aster",
                      "LBank")

_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 "
                   "Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

_HTTP_TIMEOUT_S = 15.0
_MAX_RECORDS = 200


# ── helpers ────────────────────────────────────────────────────────────────
def _api_key() -> str | None:
    for k in ("COINGLASS_API_KEY", "CG_API_KEY", "COINGLASS_KEY"):
        v = os.environ.get(k)
        if v and len(v) > 5:
            return v
    return None


def _now_iso() -> str:
    return (datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def append_jsonl(path: Path, record: dict) -> bool:
    """Append one JSONL record; never raise. Returns True on success."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("[coinglass] jsonl write failed: %s", exc)
        return False


# ── parsing / normalization ────────────────────────────────────────────────
def _digit_get(d: dict, *keys: str) -> Any:
    """First non-None value among the given keys."""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return None


def normalize_record(raw: dict, *, usd: int = _USD_NOTIONAL,
                     rank: int | None = None) -> dict:
    """Map a CoinGlass arbitrage row to our canonical shadow schema.

    Resilient to schema drift across v4 / v2 / scraped responses.
    Missing fields stay None; raw is preserved.
    """
    if not isinstance(raw, dict):
        return {"normalized_ok": False, "raw": raw, "mode": "shadow",
                "active_trading": False}
    symbol = _digit_get(raw, "symbol", "symbolName", "symbolList", "instrument")
    if isinstance(symbol, list):
        symbol = symbol[0] if symbol else None
    base = _digit_get(raw, "base", "baseCoin", "baseAsset", "coin")
    if base is None and isinstance(symbol, str):
        base = symbol.replace("USDT", "").replace("USD", "").rstrip("/-_")
    portfolio = _digit_get(raw, "portfolio", "portfolioName", "strategy")
    long_ex = _digit_get(raw, "longExchange", "long_exchange", "longExchangeName")
    short_ex = _digit_get(raw, "shortExchange", "short_exchange", "shortExchangeName")
    fr_long = _digit_get(raw, "longFundingRate", "long_funding_rate",
                         "fundingRateLong")
    fr_short = _digit_get(raw, "shortFundingRate", "short_funding_rate",
                          "fundingRateShort")
    funding_diff = _digit_get(raw, "fundingDifference", "funding_diff",
                              "fundingRateDifference")
    pnl = _digit_get(raw, "pnl", "expectedPnl")
    threed = _digit_get(raw, "threeDayCumFunding", "three_day_cum_funding",
                        "cumFunding3d", "totalReturn")
    threed_rev = _digit_get(raw, "threeDayRevenue", "three_day_revenue",
                            "revenue3d")
    apr = _digit_get(raw, "apr", "aprPct", "annualizedApr", "yearlyApr")
    annual_rev = _digit_get(raw, "annualRevenue", "annual_revenue",
                            "yearlyRevenue")

    return {
        "ts": time.time(),
        "ts_iso": _now_iso(),
        "source": "coinglass",
        "usd": usd,
        "symbol": symbol,
        "base": (base or "").upper() if isinstance(base, str) else None,
        "portfolio": portfolio,
        "long_exchange": (long_ex or "").lower() if isinstance(long_ex, str) else None,
        "short_exchange": (short_ex or "").lower() if isinstance(short_ex, str) else None,
        "funding_rate_long": _coerce_float(fr_long),
        "funding_rate_short": _coerce_float(fr_short),
        "funding_diff": _coerce_float(funding_diff),
        "pnl": _coerce_float(pnl),
        "three_day_cum_funding": _coerce_float(threed),
        "three_day_revenue": _coerce_float(threed_rev),
        "apr": _coerce_float(apr),
        "annual_revenue": _coerce_float(annual_rev),
        "raw": raw,
        "coinglass_rank": rank,
        "normalized_ok": True,
        "mode": "shadow",
        "active_trading": False,
    }


def _coerce_float(v) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# ── extraction helpers (resilient to schema drift) ─────────────────────────
def _iter_record_lists(obj: Any, depth: int = 0):
    """Yield lists whose elements look like opportunity dicts."""
    if depth > 6:
        return
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict):
            yield obj
        for v in obj:
            yield from _iter_record_lists(v, depth + 1)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_record_lists(v, depth + 1)


def extract_records(data: Any) -> list[dict]:
    """Pick the largest record list (resilient to JSON shape changes)."""
    best: list[dict] = []
    for lst in _iter_record_lists(data):
        if len(lst) > len(best):
            best = lst
    return best


# ── fetchers ───────────────────────────────────────────────────────────────
@dataclass
class FetchResult:
    source: str          # "api_v4" | "api_v2" | "html_page" | "none"
    ok: bool
    status: int | None
    records: list[dict] = field(default_factory=list)
    error: str | None = None


def _fetch_with_httpx(url: str, params: dict | None = None,
                       headers: dict | None = None) -> tuple[int | None, Any, str | None]:
    try:
        import httpx
    except Exception as exc:  # noqa: BLE001
        return None, None, f"httpx_unavailable:{exc}"
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True,
                          headers=headers or _BROWSER_HEADERS) as c:
            r = c.get(url, params=params)
            ct = r.headers.get("content-type", "")
            if "application/json" in ct or r.text.lstrip().startswith(("{", "[")):
                try:
                    return r.status_code, r.json(), None
                except Exception:
                    return r.status_code, r.text, None
            return r.status_code, r.text, None
    except Exception as exc:  # noqa: BLE001
        return None, None, str(exc)[:200]


def _check_api_error(data) -> str | None:
    """Detect CoinGlass error envelopes that arrive as HTTP 200 + JSON body.
    Returns a human-readable error label or None on success."""
    if not isinstance(data, dict):
        return None
    msg = (data.get("msg") or data.get("message") or "").strip()
    code = str(data.get("code") or "").strip()
    success = data.get("success")
    if success is False or "api key" in msg.lower():
        return f"api_key_required (code={code}, msg={msg or '?'})"
    if code and code not in ("0", "200", "000000", ""):
        return f"api_error (code={code}, msg={msg or '?'})"
    return None


def fetch_api_v4(api_key: str,
                 exchanges: tuple[str, ...] = _DEFAULT_EXCHANGES,
                 usd: int = _USD_NOTIONAL) -> FetchResult:
    headers = {**_BROWSER_HEADERS, "CG-API-KEY": api_key, "coinglassSecret": api_key}
    params = {"usd": usd, "exchange_list": ",".join(exchanges)}
    status, data, err = _fetch_with_httpx(_API_V4, params=params, headers=headers)
    if err or status is None:
        return FetchResult("api_v4", False, status, [], err or "no_response")
    if status != 200:
        return FetchResult("api_v4", False, status, [], f"http_{status}")
    if not isinstance(data, (dict, list)):
        return FetchResult("api_v4", False, status, [], "non_json_response")
    api_err = _check_api_error(data)
    if api_err:
        return FetchResult("api_v4", False, status, [], api_err)
    recs = extract_records(data)
    return FetchResult("api_v4", bool(recs), status, recs[:_MAX_RECORDS],
                       None if recs else "no_records")


def fetch_api_v2(api_key: str | None = None,
                 exchanges: tuple[str, ...] = _DEFAULT_EXCHANGES,
                 usd: int = _USD_NOTIONAL) -> FetchResult:
    headers = {**_BROWSER_HEADERS}
    if api_key:
        headers["coinglassSecret"] = api_key
    params = {"usd": usd, "ex": ",".join(exchanges)}
    status, data, err = _fetch_with_httpx(_API_V2, params=params, headers=headers)
    if err or status is None:
        return FetchResult("api_v2", False, status, [], err or "no_response")
    if status != 200:
        return FetchResult("api_v2", False, status, [], f"http_{status}")
    if not isinstance(data, (dict, list)):
        return FetchResult("api_v2", False, status, [], "non_json_response")
    api_err = _check_api_error(data)
    if api_err:
        return FetchResult("api_v2", False, status, [], api_err)
    recs = extract_records(data)
    return FetchResult("api_v2", bool(recs), status, recs[:_MAX_RECORDS],
                       None if recs else "no_records")


def fetch_html_page() -> FetchResult:
    """Best-effort HTML fallback.  CoinGlass page is JS-rendered + may have
    Cloudflare; expected to often fail.  Returns ok=False without raising."""
    status, body, err = _fetch_with_httpx(_PAGE_URL, headers=_BROWSER_HEADERS)
    if err or status is None:
        return FetchResult("html_page", False, status, [], err or "no_response")
    if status != 200:
        return FetchResult("html_page", False, status, [], f"http_{status}")
    text = body if isinstance(body, str) else ""
    if "cloudflare" in text.lower() or "Just a moment" in text:
        return FetchResult("html_page", False, status, [], "cloudflare_block")
    # Try to find embedded JSON state.  This is a heuristic only.
    marker = '"arbitrageList":'
    if marker in text:
        try:
            i = text.index(marker) + len(marker)
            # crude: take next [...] bracket block
            depth = 0; start = None
            for j, ch in enumerate(text[i:], start=i):
                if ch == "[":
                    if depth == 0:
                        start = j
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0 and start is not None:
                        chunk = text[start:j + 1]
                        try:
                            arr = json.loads(chunk)
                            if isinstance(arr, list):
                                return FetchResult("html_page", True, status, arr,
                                                   None)
                        except Exception:
                            pass
                        break
        except Exception:
            pass
    return FetchResult("html_page", False, status, [], "no_embedded_json")


def fetch_coinglass(*, api_key: str | None = None,
                    exchanges: tuple[str, ...] = _DEFAULT_EXCHANGES,
                    usd: int = _USD_NOTIONAL) -> FetchResult:
    """Try v4 → v2 → HTML page.  Returns first successful FetchResult or the
    last failure if nothing worked.  Never raises."""
    tried: list[FetchResult] = []
    if api_key:
        r = fetch_api_v4(api_key, exchanges, usd)
        tried.append(r)
        if r.ok:
            return r
        r = fetch_api_v2(api_key, exchanges, usd)
        tried.append(r)
        if r.ok:
            return r
    # Try v2 unauthenticated (some endpoints are public).
    r = fetch_api_v2(None, exchanges, usd)
    tried.append(r)
    if r.ok:
        return r
    r = fetch_html_page()
    tried.append(r)
    if r.ok:
        return r
    # All failed — return the last attempt with rolled-up info.
    return FetchResult("none", False, None, [],
                       "; ".join(f"{t.source}:{t.error}" for t in tried))


# ── internal index for comparison ──────────────────────────────────────────
def _load_recent_internal(window_s: int = 6 * 3600) -> tuple[dict, dict]:
    """Build (opp_by_base, reject_by_base) from recent internal records.
    Both maps: BASE -> latest record (or list of reject reasons)."""
    now = time.time()
    opps: dict[str, dict] = {}
    rejects: dict[str, list[str]] = {}
    if INTERNAL_OPP_FILE.exists():
        try:
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
        except Exception as exc:  # noqa: BLE001
            log.debug("[coinglass] internal opp load failed: %s", exc)
    if INTERNAL_REJECT_FILE.exists():
        try:
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
        except Exception as exc:  # noqa: BLE001
            log.debug("[coinglass] internal reject load failed: %s", exc)
    return opps, rejects


_INTERNAL_VENUES = {"binance", "bybit", "bitget", "mexc", "hyperliquid", "aster"}
_EXECUTABLE_VENUES = {"binance", "bybit", "mexc", "bitget"}  # ours have keys/clients


def compare_to_internal(cg_record: dict, opps: dict, rejects: dict) -> dict:
    """Build one comparison record for a CoinGlass row."""
    base = (cg_record.get("base") or "").upper()
    cg_long = (cg_record.get("long_exchange") or "").lower()
    cg_short = (cg_record.get("short_exchange") or "").lower()
    internal = opps.get(base)
    reject_list = rejects.get(base, [])
    internal_seen = internal is not None
    internal_reject = reject_list[-1] if reject_list else None
    internal_apr = (internal or {}).get("gross_apr")

    # Verdict logic
    cg_venues = {cg_long, cg_short}
    missing_in_internal = cg_venues - _INTERNAL_VENUES
    non_executable = cg_venues - _EXECUTABLE_VENUES

    if internal_seen and not missing_in_internal:
        if internal_reject:
            verdict = f"internal_filter:{internal_reject}"
        else:
            verdict = "matched"
    elif missing_in_internal:
        verdict = f"missing_exchange:{','.join(sorted(missing_in_internal))}"
    elif not internal_seen:
        verdict = "symbol_mapping_or_no_recent_data"
    else:
        verdict = "needs_manual_review"

    executable = bool(cg_venues and cg_venues.issubset(_EXECUTABLE_VENUES))

    return {
        "ts": time.time(),
        "symbol": cg_record.get("symbol"),
        "base": base or None,
        "coinglass_apr": cg_record.get("apr"),
        "coinglass_pnl": cg_record.get("pnl"),
        "coinglass_long": cg_long,
        "coinglass_short": cg_short,
        "internal_seen": internal_seen,
        "internal_reject_reason": internal_reject,
        "internal_apr": internal_apr,
        "spread_bps": (internal or {}).get("price_diff_bps"),
        "depth_ok": None,
        "fee_coverage_ok": None,
        "executable": executable,
        "missing_reason": ("non_executable:" + ",".join(sorted(non_executable))
                           if non_executable else None),
        "verdict": verdict,
    }


# ── main scan loop (one-shot) ──────────────────────────────────────────────
def scan_once(*, write: bool = True,
              exchanges: tuple[str, ...] = _DEFAULT_EXCHANGES,
              usd: int = _USD_NOTIONAL) -> dict:
    """One-shot CoinGlass scan + internal compare.  Returns a summary dict.

    Side effects (only when `write=True`):
      * append normalized records to coinglass_arbitrage_shadow.jsonl
      * append compare records to coinglass_internal_compare.jsonl
    """
    api_key = _api_key()
    result = fetch_coinglass(api_key=api_key, exchanges=exchanges, usd=usd)

    if not result.ok:
        msg = (f"[coinglass] unavailable (source={result.source}, "
               f"status={result.status}, error={result.error})")
        print(msg)
        return {
            "fetched": False, "source": result.source,
            "status": result.status, "error": result.error,
            "api_key_present": api_key is not None,
            "records": 0, "compare": 0,
        }

    normalized: list[dict] = []
    for idx, raw in enumerate(result.records):
        try:
            normalized.append(normalize_record(raw, usd=usd, rank=idx + 1))
        except Exception as exc:  # noqa: BLE001
            log.debug("[coinglass] normalize failed: %s", exc)

    # Internal compare
    opps, rejects = _load_recent_internal()
    comparisons = [compare_to_internal(r, opps, rejects) for r in normalized]

    if write:
        for r in normalized:
            append_jsonl(SHADOW_FILE, r)
        for c in comparisons:
            append_jsonl(COMPARE_FILE, c)

    # Summary
    matched = sum(1 for c in comparisons if c["verdict"] == "matched")
    missing_ex = sum(1 for c in comparisons
                     if c["verdict"].startswith("missing_exchange"))
    internal_filter = sum(1 for c in comparisons
                          if c["verdict"].startswith("internal_filter"))
    no_recent = sum(1 for c in comparisons
                    if c["verdict"] == "symbol_mapping_or_no_recent_data")
    executable_ct = sum(1 for c in comparisons if c["executable"])

    summary = {
        "fetched": True,
        "source": result.source,
        "status": result.status,
        "api_key_present": api_key is not None,
        "records": len(normalized),
        "compare": len(comparisons),
        "matched": matched,
        "missing_exchange": missing_ex,
        "internal_filter_blocked": internal_filter,
        "no_recent_internal_data": no_recent,
        "executable_pair": executable_ct,
    }
    print(f"[coinglass] OK source={result.source} status={result.status} "
          f"records={len(normalized)} matched={matched} "
          f"missing_ex={missing_ex} internal_filter={internal_filter} "
          f"executable={executable_ct}")
    return summary


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    summary = scan_once()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("fetched") or summary.get("api_key_present") is False else 1


if __name__ == "__main__":
    sys.exit(main())
