"""
funding_carry/external/low_volume_persistence.py

Sprint 21.4 — Persistence analyzer over the accumulated low-volume shadow
audit records.  Reads `state/low_volume_funding_shadow.jsonl` and answers:
  - which symbols repeatedly pass depth_ok across multiple audit runs?
  - which "depth_ok" verdicts were one-time transient spikes?
  - how stable are funding_spread, fee_coverage, orderbook_spread per symbol?
  - which (if any) symbols would qualify for a future controlled micro-paper
    trial under criteria: ≥3 depth_ok runs, fee_cov ≥ 2x consistently,
    ob_spread ≤ 10 bps consistently?

READ-ONLY.  No state mutation.  No trading.  No live changes.
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
SHADOW_FILE = _ROOT / "state" / "low_volume_funding_shadow.jsonl"

DEPTH_OK_PERSIST_MIN_RUNS = 3        # appears depth_ok ≥ this many times → persistent
PERSISTENT_FEE_COV = 2.0
PERSISTENT_SPREAD_BPS = 10.0
MIN_TOTAL_APPEARANCES = 3            # noise floor for any verdict statement


def _med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 4) if xs else None


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.fmean(xs), 4) if xs else None


def load_records(path: Path = SHADOW_FILE) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def group_by_symbol(records: list[dict]) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        b = r.get("base") or "?"
        by[b].append(r)
    return by


def per_symbol_stats(items: list[dict]) -> dict:
    """Per-symbol persistence stats."""
    verdicts = Counter(r.get("validation_verdict") for r in items)
    depth_ok_runs = verdicts.get("depth_ok", 0)
    aprs = [r.get("gross_apr") for r in items]
    feecovs = [r.get("fee_coverage_ratio") for r in items]
    obs = [r.get("orderbook_spread_bps") for r in items]
    sl_long = [r.get("depth_slip_long_bps") for r in items]
    sl_short = [r.get("depth_slip_short_bps") for r in items]
    vols = [r.get("min_leg_volume_24h_usd") for r in items]
    pairs = Counter((r.get("long_exchange"), r.get("short_exchange"))
                    for r in items)
    bucket = next((r.get("volume_bucket") for r in items), None)
    return {
        "total_runs": len(items),
        "depth_ok_runs": depth_ok_runs,
        "depth_ok_ratio": (round(depth_ok_runs / len(items), 3)
                           if items else None),
        "verdict_dist": dict(verdicts),
        "median_apr": _med(aprs),
        "mean_apr": _mean(aprs),
        "median_fee_cov": _med(feecovs),
        "median_ob_spread_bps": _med(obs),
        "median_slip_long_bps": _med(sl_long),
        "median_slip_short_bps": _med(sl_short),
        "median_vol_usd": _med(vols),
        "venue_pairs": dict(pairs),
        "bucket": bucket,
    }


def classify_persistence(s: dict) -> str:
    """Symbol-level persistence verdict (per spec §3)."""
    n = s["total_runs"]
    if n < MIN_TOTAL_APPEARANCES:
        return "needs_more_data"
    ok = s["depth_ok_runs"]
    if ok == 0:
        return "fee_edge_disappears"  # never passed in this window
    if ok == 1 and n >= 2:
        return "transient_spike"
    if ok >= DEPTH_OK_PERSIST_MIN_RUNS:
        fc = s["median_fee_cov"]
        sp = s["median_ob_spread_bps"]
        if (fc is not None and fc >= PERSISTENT_FEE_COV
                and sp is not None and sp <= PERSISTENT_SPREAD_BPS):
            return "persistent_depth_ok"
        return "unstable_depth"
    # 2 or some but not 3+
    return "unstable_depth"


def summarize(records: list[dict]) -> dict:
    by_sym = group_by_symbol(records)
    per_sym: dict[str, dict] = {}
    for sym, items in by_sym.items():
        st = per_symbol_stats(items)
        st["persistence_verdict"] = classify_persistence(st)
        per_sym[sym] = st
    # Aggregate
    n_runs = len(set((r.get("ts") or 0) // 1 for r in records))  # rough run count
    total_records = len(records)
    all_verdicts = Counter(r.get("validation_verdict") for r in records)
    persistence_classes = Counter(s["persistence_verdict"]
                                  for s in per_sym.values())
    persistent_syms = sorted([(s, v) for s, v in per_sym.items()
                              if v["persistence_verdict"] == "persistent_depth_ok"],
                             key=lambda kv: -(kv[1]["depth_ok_runs"] or 0))
    return {
        "records_loaded": total_records,
        "approx_audit_runs": int(round(total_records / max(1, len(by_sym)) * 1.0)),
        "unique_symbols": len(by_sym),
        "verdict_distribution_all": dict(all_verdicts),
        "persistence_classes": dict(persistence_classes),
        "persistent_symbols": [{"symbol": s, **v} for s, v in persistent_syms],
        "per_symbol": per_sym,
        "generated_iso": (datetime.now(timezone.utc)
                          .isoformat(timespec="milliseconds")
                          .replace("+00:00", "Z")),
    }


def main() -> int:
    recs = load_records()
    if not recs:
        print("[persistence] no records found")
        return 1
    s = summarize(recs)
    print(f"records: {s['records_loaded']}  unique_symbols: {s['unique_symbols']}")
    print(f"all-verdict distribution: {s['verdict_distribution_all']}")
    print(f"persistence classes: {s['persistence_classes']}")
    print()
    print(f"=== PERSISTENT depth_ok symbols ({len(s['persistent_symbols'])}) ===")
    for r in s["persistent_symbols"]:
        print(f"  {r['symbol']:10} runs={r['total_runs']:2} depth_ok={r['depth_ok_runs']:2} "
              f"({100*r['depth_ok_ratio']:.0f}%)  med_APR={r['median_apr']:>7.0f}%  "
              f"med_fee_cov={r['median_fee_cov']}x  med_ob_spread={r['median_ob_spread_bps']}bps  "
              f"pairs={dict(r['venue_pairs'])}")
    print()
    print("=== TARGET symbols (Sprint 21.3 callouts) ===")
    targets = ("SIGN", "AIGENSYN", "PUNDIX", "ARM", "STEEM", "HOME", "MEME",
               "GENIUS", "CBRS", "POWR", "M", "ID", "ALLO", "HEI")
    for t in targets:
        if t in s["per_symbol"]:
            v = s["per_symbol"][t]
            print(f"  {t:10} runs={v['total_runs']:2} depth_ok={v['depth_ok_runs']:2}  "
                  f"verdict={v['persistence_verdict']:25}  "
                  f"med_APR={v['median_apr']}%  med_feecov={v['median_fee_cov']}x  "
                  f"med_spread={v['median_ob_spread_bps']}bps")
        else:
            print(f"  {t:10} NOT IN DATASET")
    return 0


if __name__ == "__main__":
    sys.exit(main())
