"""
funding_carry/main.py

Entry point for the multi-exchange delta-neutral carry strategy.

Per-cycle pipeline (every POLL_INTERVAL_S = 5 min)
==================================================
  1. Fetch perps + spots from all 6 venues in parallel.
  2. Build candidate opportunities (same-ex spot+perp, cross-ex spot+perp,
     cross-ex perp+perp).
  3. Score by net APR after fees + cross-exchange friction; filter min APR.
  4. For each open position: fetch latest leg prices, credit funding, evaluate
     exit triggers (basis drift, profit-zero, max hold).
  5. For each top opportunity (in order):
       a. Skip if already open or insufficient capital
       b. Allocate sizing (5× lev, capital cap)
       c. Fetch L2 orderbooks on BOTH legs (parallel)
       d. Run safety validation (spread, depth, price-match)
       e. If green → open position at validated fill prices
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal as os_signal
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

from .config import (
    CARRY_SAFE_DIAGNOSTIC_MODE,
    ENTRY_LEAD_TIME_S,
    ENTRY_WINDOW_HALF_S,
    LOGS_DIR,
    MAX_CONCURRENT_POSITIONS,
    MIN_FEE_COVERAGE_RATIO_SAFE,
    MIN_FUNDING_RATE_PER_PERIOD_SAFE,
    MIN_NET_APR_PCT_SAFE,
    OPPORTUNITY_LOG_FILE,
    PAPER_MODE,
    POLL_INTERVAL_S,
    REJECT_LOG_FILE,
    STATE_DIR,
    SYMBOL_BLACKLIST,
    TEMP_BLACKLIST_BASES,
    TRADE_LOG_FILE,
    FUNDING_HISTORY_FILE,
    VOLUME_FLOOR_USD_24H,
)
from .diagnostics import (
    CooldownTracker,
    estimate_fee_coverage,
    max_abs_perp_funding_rate,
    normalised_rate_per_8h,
)
from .data import UniverseFetcher, fetch_orderbook
from .data.types import LegKind
from .data.universe import EXECUTABLE_VENUES
from .execution import PaperBook
from .safety import CircuitBreaker, ErrorTracker, TelegramAlerter
from .asset_kind import is_execution_eligible as _ak_eligible  # Sprint 55
from .strategy import (
    ScoredOpportunity,
    build_opportunities,
    compute_allocation,
    score_opportunities,
    validate_legs,
)

# Cycle must complete within this many seconds or we log a warning and continue
CYCLE_TIMEOUT_S = POLL_INTERVAL_S - 5.0   # give 5s margin


def _setup_logging() -> None:
    os.makedirs(LOGS_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    handler = RotatingFileHandler(
        os.path.join(LOGS_DIR, "run.log"),
        maxBytes=10_000_000, backupCount=3, encoding="utf-8",
    )
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s",
                            datefmt="%H:%M:%S")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)


log = logging.getLogger("funding_carry")


def _within_entry_window(opp) -> tuple[bool, float]:
    """Return (True, secs_to_settle) if at least one perp leg is within the
    entry window [ENTRY_LEAD_TIME_S ± ENTRY_WINDOW_HALF_S] seconds of
    its next funding settlement.

    Both legs (when perp) are checked; we enter when the *nearest* settlement
    is inside the window so that the fill lands before either leg settles.
    """
    now = time.time()
    lo = ENTRY_LEAD_TIME_S - ENTRY_WINDOW_HALF_S   # 240 s
    hi = ENTRY_LEAD_TIME_S + ENTRY_WINDOW_HALF_S   # 360 s

    for leg in (opp.long_leg, opp.short_leg):
        if leg.leg_kind != "perp":
            continue
        if not leg.next_funding_ts:
            continue
        # next_funding_ts is in milliseconds on most exchanges
        ts_s = leg.next_funding_ts / 1000.0
        secs_left = ts_s - now
        if lo <= secs_left <= hi:
            return True, secs_left
    return False, -1.0


def _append_jsonl(path: str, obj: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj) + "\n")
    except Exception:
        pass


class FundingCarryRunner:
    def __init__(self) -> None:
        self.book    = PaperBook()
        self._stop   = asyncio.Event()
        self._cb     = CircuitBreaker(equity_start=self.book.equity_usd)
        self._errors = ErrorTracker()
        self._alerts: TelegramAlerter | None = None   # created after session opens

        # Phase 2 (2026-05-25) — observability (no trading-logic changes).
        # Reject counters: bumped from _record_reject(); reset on hourly log
        # and again on daily summary so each window is independent.
        self._reject_counts:        dict[str, int] = defaultdict(int)
        self._reject_counts_hourly: dict[str, int] = defaultdict(int)
        self._reject_counts_daily:  dict[str, int] = defaultdict(int)
        # Last cycle marker for heartbeat health detection.
        self._last_cycle_ts:   float = 0.0
        self._last_cycle_summary: dict = {}
        self._scored_last_count: int = 0
        # PnL marker for daily summary delta.
        self._daily_anchor_equity: float = self.book.equity_usd
        self._daily_anchor_ts:     float = time.time()
        self._daily_closed_count_anchor: int = 0

        # Sprint 1 (2026-05-26) -- safe-diagnostic mode.
        # Cooldown tracker (per-base "do not re-open until") is always
        # constructed so cooldown state survives restart, even if
        # CARRY_SAFE_DIAGNOSTIC_MODE is later switched off. The gate
        # itself is only enforced when the flag is True (see _try_enter).
        self._safe_mode: bool = CARRY_SAFE_DIAGNOSTIC_MODE
        try:
            self._cooldown_tracker: Optional[CooldownTracker] = CooldownTracker()
            n_cd = len(self._cooldown_tracker.bases_on_cooldown())
            log.info(
                "[Sprint1] safe_mode=%s cooldown_tracker init: %d bases on cooldown "
                "(min_funding=%.4f/8h fee_cov_min=%.1fx min_apr=%.0f%% temp_blacklist=%s)",
                self._safe_mode, n_cd,
                MIN_FUNDING_RATE_PER_PERIOD_SAFE,
                MIN_FEE_COVERAGE_RATIO_SAFE,
                MIN_NET_APR_PCT_SAFE,
                sorted(TEMP_BLACKLIST_BASES),
            )
        except Exception as exc:
            log.warning("[Sprint1] CooldownTracker init failed: %s -- cooldown disabled", exc)
            self._cooldown_tracker = None

    def request_stop(self, *_: object) -> None:
        log.info("stop requested")
        self._stop.set()

    async def run(self) -> None:
        log.info("==== funding_carry starting (paper=%s, lev=5x) ====", PAPER_MODE)
        log.info("starting equity: $%.2f", self.book.equity_usd)
        log.info("executable venues: %s", EXECUTABLE_VENUES)

        async with UniverseFetcher(
            volume_floor_usd=VOLUME_FLOOR_USD_24H,
            symbol_blacklist=SYMBOL_BLACKLIST,
        ) as fetcher:
            self._alerts = TelegramAlerter(fetcher.session, bot_name="carry")
            await self._alerts.started(self.book.equity_usd)

            # Phase 2: side-channel diagnostic loops. They share the same
            # event loop as _cycle() but never block trading — each is a
            # plain asyncio.create_task() with try/except inside.
            hb_task     = asyncio.create_task(self._heartbeat_loop())
            hourly_task = asyncio.create_task(self._hourly_summary_loop())
            daily_task  = asyncio.create_task(self._daily_summary_loop())
            # Sprint 32 (2026-06-02) — recurring low-volume audit scheduler.
            # SHADOW-ONLY: this loop never opens trades, never touches the
            # book, the universe filter, or strategy state.  Guarded by an
            # internal single-instance lock and try/except.
            lv_audit_task = asyncio.create_task(self._low_volume_audit_loop())
            # Sprint 40 (2026-06-06) — recurring spread-builder diagnostic.
            # READ-ONLY: this loop only reads existing audit shadow data
            # and writes its own isolated diagnostic / summary files.
            # Never opens trades, never touches the strategy state, the
            # PaperBook, the universe filter, or the executor.
            sb_task = asyncio.create_task(self._spread_builder_loop())

            try:
                while not self._stop.is_set():
                    try:
                        await asyncio.wait_for(
                            self._cycle(fetcher),
                            timeout=CYCLE_TIMEOUT_S,
                        )
                        self._errors.ok()
                    except asyncio.TimeoutError:
                        log.warning("cycle timed out after %.0fs", CYCLE_TIMEOUT_S)
                    except Exception as exc:
                        log.exception("cycle failed: %s", exc)
                        if self._errors.fail(exc):
                            if self._alerts:
                                await self._alerts.consecutive_errors(
                                    self._errors.count, self._errors.last_error
                                )
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_S)
                    except asyncio.TimeoutError:
                        continue
            finally:
                for t in (hb_task, hourly_task, daily_task, lv_audit_task, sb_task):
                    t.cancel()
                # Drain cancellations; never propagate
                for t in (hb_task, hourly_task, daily_task, lv_audit_task, sb_task):
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

        summary = self.book.summary()
        log.info("==== stopped. final: %s", summary)
        if self._alerts:
            await self._alerts.stopped(summary)

    # ── Phase 2 diagnostic loops — no trading-logic changes ────────────────

    async def _heartbeat_loop(self) -> None:
        """Every 5 min: log + Telegram alive ping with balance/equity/positions."""
        HEARTBEAT_S = 300.0
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=HEARTBEAT_S)
                return
            except asyncio.TimeoutError:
                pass
            try:
                s = self._last_cycle_summary or self.book.summary()
                age = time.time() - self._last_cycle_ts if self._last_cycle_ts else -1.0
                last_err = self._errors.last_error or "(none)"
                msg = (
                    f"heartbeat: equity=${s.get('equity_usd', 0):.2f} "
                    f"bal=${s.get('balance_usd', 0):.2f} "
                    f"open={s.get('open', 0)} closed={s.get('closed', 0)} "
                    f"deployed=${s.get('deployed_usd', 0):.0f} "
                    f"last_cycle_age={age:.0f}s opps_seen={self._scored_last_count}"
                )
                log.info("[heartbeat] %s", msg)
                if self._alerts:
                    await self._alerts.send(
                        f"heartbeat\n"
                        f"equity ${s.get('equity_usd', 0):.2f}  "
                        f"open {s.get('open', 0)}  "
                        f"deployed ${s.get('deployed_usd', 0):.0f}\n"
                        f"last cycle {age:.0f}s ago  opps {self._scored_last_count}\n"
                        f"last err: <code>{last_err[:120]}</code>"
                    )
            except Exception as exc:
                log.warning("heartbeat loop error: %s", exc)

    async def _hourly_summary_loop(self) -> None:
        """Every 60 min: log top reject reasons then reset hourly counter."""
        HOUR_S = 3600.0
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=HOUR_S)
                return
            except asyncio.TimeoutError:
                pass
            try:
                if not self._reject_counts_hourly:
                    log.info("[hourly] no rejects this hour")
                else:
                    top = sorted(
                        self._reject_counts_hourly.items(),
                        key=lambda kv: kv[1], reverse=True,
                    )[:8]
                    summary = " | ".join(f"{k}={v}" for k, v in top)
                    log.info("[hourly] reject summary: %s", summary)
                    if self._alerts:
                        await self._alerts.send(
                            f"hourly rejects ({sum(self._reject_counts_hourly.values())} total)\n"
                            + "\n".join(f"  {k}: {v}" for k, v in top)
                        )
                self._reject_counts_hourly.clear()
            except Exception as exc:
                log.warning("hourly summary error: %s", exc)

    async def _daily_summary_loop(self) -> None:
        """Phase 3.5 daily validation report: realized + unrealized PnL,
        funding, fees, basis drift, opens/closes, top rejects, best/worst
        pair, net APR realized. Pure observability; no trading effects."""
        DAY_S = 86400.0
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=DAY_S)
                return
            except asyncio.TimeoutError:
                pass
            try:
                report = self._build_daily_report()
                log.info("[daily] %s", report["log_line"])
                if self._alerts:
                    try:
                        await self._alerts.send(report["tg_msg"])
                    except Exception as tg_exc:
                        log.warning("daily tg send failed: %s", tg_exc)
                # Reset anchors + daily counters
                self._daily_anchor_equity = report["cur_eq"]
                self._daily_anchor_ts     = time.time()
                self._daily_closed_count_anchor = report["closed_total"]
                self._reject_counts_daily.clear()
            except Exception as exc:
                log.warning("daily summary error: %s", exc)

    def _build_daily_report(self) -> dict:
        """Heavy-lifting routine -- read trades.jsonl + funding_payments.jsonl
        for the last 24h, compute realized/unrealized PnL, fees, funding,
        basis drift, best/worst pair, net APR realized. All file I/O is
        best-effort; on any error we still emit a minimal log line."""
        s = self.book.summary()
        cur_eq = float(s.get("equity_usd", self.book.equity_usd))
        cur_bal = float(s.get("balance_usd", self.book.balance_usd))
        deployed = float(s.get("deployed_usd", 0.0))
        closed_total = int(s.get("closed", 0))
        equity_delta_24h = cur_eq - self._daily_anchor_equity
        closed_24h = closed_total - self._daily_closed_count_anchor

        now_ts = time.time()
        cutoff = now_ts - 86400.0

        # Read 24h slice of trades.jsonl
        trades_24h: list[dict] = []
        try:
            with open(TRADE_LOG_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    ts = rec.get("exit_ts") or rec.get("entry_ts") or 0
                    if ts and ts >= cutoff:
                        trades_24h.append(rec)
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.debug("daily: trades.jsonl read failed: %s", exc)

        realized_24h     = sum(float(t.get("realized_pnl_usd") or 0.0) for t in trades_24h)
        funding_closed   = sum(float(t.get("funding_collected_usd") or 0.0) for t in trades_24h)
        fees_closed      = sum(float(t.get("fees_paid_usd")        or 0.0) for t in trades_24h)
        capital_locked_avg = (
            sum(float(t.get("capital_locked_usd") or 0.0) for t in trades_24h) /
            max(1, len(trades_24h))
        ) if trades_24h else 0.0

        # Open positions: unrealized estimate, funding earned-so-far,
        # entry vs current basis drift (we don't have a live cur_basis
        # here; we approximate "drift" using the magnitude of
        # entry_basis_bps as the implicit cost-to-exit reference).
        open_funding_collected = 0.0
        open_fees              = 0.0
        open_entry_basis_sum_abs = 0.0
        open_count             = len(self.book.open_positions)
        for p in self.book.open_positions.values():
            open_funding_collected += float(getattr(p, "funding_collected_usd", 0.0) or 0.0)
            open_fees              += float(getattr(p, "fees_paid_usd", 0.0) or 0.0)
            open_entry_basis_sum_abs += abs(float(getattr(p, "entry_basis_bps", 0.0) or 0.0))

        unrealized_estimate = (cur_eq - cur_bal)
        # Best / worst pair (closed in 24h)
        best_pair = ("", 0.0)
        worst_pair = ("", 0.0)
        per_base_pnl: dict[str, float] = {}
        for t in trades_24h:
            b = t.get("base") or "?"
            per_base_pnl[b] = per_base_pnl.get(b, 0.0) + float(t.get("realized_pnl_usd") or 0.0)
        if per_base_pnl:
            best_pair  = max(per_base_pnl.items(), key=lambda kv: kv[1])
            worst_pair = min(per_base_pnl.items(), key=lambda kv: kv[1])

        # Realized APR -- annualise realized PnL by mean deployed capital
        denom = max(1.0, capital_locked_avg if capital_locked_avg > 0 else deployed)
        net_apr_realized = (realized_24h / denom) * 365.0 * 100.0 if denom > 0 else 0.0

        # Top rejects
        top_rej = sorted(
            self._reject_counts_daily.items(),
            key=lambda kv: kv[1], reverse=True,
        )[:5]
        rej_txt = ("\n".join(f"  {k}: {v}" for k, v in top_rej)
                   if top_rej else "  (no rejects)")

        # Sprint 1 -- additional metrics: funding/fees ratio, holds before
        # 2 cycles, symbols on cooldown, blacklist.
        ff_ratio = (funding_closed / fees_closed * 100.0) if fees_closed > 0 else 0.0
        cycles_8h = 8 * 3600
        exits_before_2c = sum(
            1 for t in trades_24h
            if ((t.get("exit_ts") or 0) - (t.get("entry_ts") or 0)) < 2 * cycles_8h
        )
        exits_after_2c  = max(0, len(trades_24h) - exits_before_2c)
        hold_hours = [
            ((t.get("exit_ts") or 0) - (t.get("entry_ts") or 0)) / 3600.0
            for t in trades_24h
        ]
        avg_hold_cycles = (
            (sum(hold_hours) / max(1, len(hold_hours))) / 8.0
            if hold_hours else 0.0
        )
        cd_list = (self._cooldown_tracker.bases_on_cooldown()
                   if self._cooldown_tracker else [])
        cd_txt = (", ".join(f"{b}({h/3600:.0f}h)" for b, h in cd_list[:8])
                  if cd_list else "(none)")

        log_line = (
            f"equity=${cur_eq:.2f} delta_24h={equity_delta_24h:+.2f} "
            f"realized_24h={realized_24h:+.2f} unreal={unrealized_estimate:+.2f} "
            f"funding_24h_closed={funding_closed:.3f} fees_24h_closed={fees_closed:.3f} "
            f"ff_ratio={ff_ratio:.0f}% "
            f"opens_now={open_count} closed_24h={closed_24h} "
            f"exits_<2c={exits_before_2c} >=2c={exits_after_2c} "
            f"avg_hold_cycles={avg_hold_cycles:.2f} "
            f"deployed=${deployed:.0f} best={best_pair[0]}:${best_pair[1]:+.2f} "
            f"worst={worst_pair[0]}:${worst_pair[1]:+.2f} apr_real={net_apr_realized:+.1f}% "
            f"on_cooldown={len(cd_list)} blacklist={sorted(TEMP_BLACKLIST_BASES)}"
        )

        tg_msg = (
            f"daily summary (24h) | Sprint1 safe_mode\n"
            f"equity:           ${cur_eq:.2f}\n"
            f"  delta 24h:      ${equity_delta_24h:+.2f}\n"
            f"  realized 24h:   ${realized_24h:+.2f}\n"
            f"  unrealized now: ${unrealized_estimate:+.2f}\n"
            f"  funding 24h:    +${funding_closed:.3f} (closed) "
            f"+${open_funding_collected:.3f} (open)\n"
            f"  fees 24h:       -${fees_closed:.3f} (closed) "
            f"-${open_fees:.3f} (open)\n"
            f"  funding/fees:   {ff_ratio:.0f}% "
            f"({'+EV' if ff_ratio >= 200 else 'low cov' if ff_ratio >= 100 else '-EV'})\n"
            f"positions:        open={open_count}  closed_24h={closed_24h}\n"
            f"  exits <2c:      {exits_before_2c}\n"
            f"  exits >=2c:     {exits_after_2c}\n"
            f"  avg_hold_cyc:   {avg_hold_cycles:.2f}\n"
            f"deployed:         ${deployed:.0f}\n"
            f"best pair 24h:    {best_pair[0]}: ${best_pair[1]:+.2f}\n"
            f"worst pair 24h:   {worst_pair[0]}: ${worst_pair[1]:+.2f}\n"
            f"net APR realized: {net_apr_realized:+.1f}% (on ${denom:.0f} avg cap)\n"
            f"on cooldown ({len(cd_list)}): {cd_txt}\n"
            f"blacklist:        {sorted(TEMP_BLACKLIST_BASES)}\n"
            f"top rejects:\n{rej_txt}"
        )

        return {
            "cur_eq": cur_eq,
            "closed_total": closed_total,
            "log_line": log_line,
            "tg_msg":   tg_msg,
        }

    async def _low_volume_audit_loop(self) -> None:
        """Sprint 32 — recurring low-volume audit scheduler.

        Read-only side-channel diagnostic.  Runs every 30 min (env-overridable
        via CARRY_LV_AUDIT_INTERVAL_S) and writes:
          * `state/low_volume_funding_shadow.jsonl`  (per-candidate, via
            audit_once())
          * `state/low_volume_audit_summary.jsonl`   (per-run summary, via
            this scheduler)

        Guarantees:
          * never opens a trade;
          * never calls executor or open_position;
          * never mutates book / cooldown / strategy state;
          * any failure inside the audit step is swallowed and never
            crashes this loop or the main cycle.
        """
        try:
            from .external.low_volume_audit_scheduler import (
                low_volume_audit_loop as _loop,
            )
        except Exception as exc:
            log.warning("[lv_audit_sched] import failed: %s — loop disabled",
                        exc)
            return
        try:
            # run_immediately=True: kick off one audit at startup so the
            # shadow file gets a fresh datapoint right after a (re)start
            # rather than waiting a full interval.
            await _loop(self._stop, run_immediately=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[lv_audit_sched] loop crashed: %s — restarting once",
                        exc)
            # One-time retry; further failures stay caught by outer try.
            try:
                await _loop(self._stop, run_immediately=True)
            except Exception as exc2:
                log.warning("[lv_audit_sched] retry failed: %s — giving up",
                            exc2)

    async def _spread_builder_loop(self) -> None:
        """Sprint 40 — recurring spread-builder diagnostic scheduler.

        Read-only side-channel diagnostic.  Runs every 60 min by default
        (env-overridable via CARRY_SPREAD_BUILDER_INTERVAL_S, floor 600s)
        and writes:
          * `state/spread_builder_diagnostic.jsonl` — per-route record
          * `state/spread_builder_summary.jsonl`   — per-run summary

        Guarantees:
          * never opens a trade;
          * never calls executor or open_position;
          * never mutates book / cooldown / strategy state / universe
            filter / live vol_floor;
          * any failure inside the builder step is swallowed and never
            crashes this loop or the main cycle.
        """
        try:
            from .external.spread_based_builder import (
                spread_builder_loop as _loop,
            )
        except Exception as exc:
            log.warning("[spread_builder_sched] import failed: %s — disabled",
                        exc)
            return
        try:
            # run_immediately=True: kick off one builder run at startup so
            # we get a fresh datapoint right after (re)start.
            await _loop(self._stop, run_immediately=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[spread_builder_sched] loop crashed: %s — retry once",
                        exc)
            try:
                await _loop(self._stop, run_immediately=True)
            except Exception as exc2:
                log.warning("[spread_builder_sched] retry failed: %s — giving up",
                            exc2)

    async def _cycle(self, fetcher: UniverseFetcher) -> None:
        perps, spots = await fetcher.fetch_all()
        if not perps:
            return

        # ── stale-data guard ───────────────────────────────────────────────
        now_ts = time.time()
        stale_thresh = 120.0   # warn if any exchange data is > 2 min old
        by_exchange: dict[str, float] = {}
        for p in perps:
            age = now_ts - p.fetched_ts
            if age > by_exchange.get(p.exchange, 0):
                by_exchange[p.exchange] = age
        for ex, age in by_exchange.items():
            if age > stale_thresh:
                log.warning("[stale] %s data is %.0fs old", ex, age)
                if self._alerts:
                    await self._alerts.stale_data(ex, age)

        # ── circuit breaker ────────────────────────────────────────────────
        self._cb.update(self.book.equity_usd)
        cb_tripped, dd_pct = self._cb.check(self.book.equity_usd)
        if cb_tripped:
            log.warning("[circuit_breaker] tripped — DD %.1f%% — skipping new entries", dd_pct)
            if self._alerts and dd_pct >= self._cb._max_dd_pct:
                await self._alerts.circuit_breaker_tripped(dd_pct, self.book.equity_usd)
            # Still mark open positions even when breaker is tripped
            await self._mark_open_positions(fetcher, perps, spots)
            log.info("cycle (cb-tripped): %s", self.book.summary())
            return

        # Build & score opportunities
        opps_raw = build_opportunities(perps, spots)
        scored = score_opportunities(opps_raw)

        if scored:
            log.info("found %d viable opportunit%s:",
                     len(scored), "ies" if len(scored) != 1 else "y")
            for s in scored[:8]:
                o = s.opportunity
                log.info(
                    "  %-22s %-8s LONG %-12s SHORT %-12s gross=%+.0f%% "
                    "net7d=%+.0f%% breakeven=%.1fd fees=%.3f%%",
                    s.kind, o.base,
                    f"{o.long_leg.leg_kind}/{o.long_leg.exchange}",
                    f"{o.short_leg.leg_kind}/{o.short_leg.exchange}",
                    s.gross_apr_pct, s.net_apr_pct_7d,
                    s.breakeven_days, s.fees_per_cycle_pct,
                )
                _append_jsonl(OPPORTUNITY_LOG_FILE, {
                    "ts": time.time(),
                    "kind": s.kind, "base": o.base,
                    "long_ex": o.long_leg.exchange, "long_leg": o.long_leg.leg_kind,
                    "short_ex": o.short_leg.exchange, "short_leg": o.short_leg.leg_kind,
                    "long_funding": o.long_leg.funding_rate,
                    "short_funding": o.short_leg.funding_rate,
                    "gross_apr": s.gross_apr_pct, "net7d_apr": s.net_apr_pct_7d,
                    "fees_per_cycle": s.fees_per_cycle_pct,
                    "breakeven_days": s.breakeven_days,
                    "price_diff_bps": o.price_diff_bps,
                })

        # Mark existing positions against latest data
        await self._mark_open_positions(fetcher, perps, spots)

        # Try entering top opportunities
        for s in scored:
            if len(self.book.open_positions) >= MAX_CONCURRENT_POSITIONS:
                break
            await self._try_enter(fetcher, s)

        # Phase 2: cycle health markers (no trading impact)
        self._scored_last_count = len(scored)
        self._last_cycle_ts = time.time()
        self._last_cycle_summary = self.book.summary()

        log.info("cycle: %s", self.book.summary())

    async def _mark_open_positions(self, fetcher, perps, spots) -> None:
        # Build quick indices
        perp_idx = {(p.exchange, p.symbol): p for p in perps}
        spot_idx = {(s.exchange, s.symbol): s for s in spots}

        for pos in list(self.book.open_positions.values()):
            long_p  = (perp_idx.get((pos.long_exchange, pos.long_symbol))
                       if pos.long_leg_kind == "perp"
                       else spot_idx.get((pos.long_exchange, pos.long_symbol)))
            short_p = (perp_idx.get((pos.short_exchange, pos.short_symbol))
                       if pos.short_leg_kind == "perp"
                       else spot_idx.get((pos.short_exchange, pos.short_symbol)))
            if long_p is None or short_p is None:
                continue
            long_price  = getattr(long_p,  "mark_price", None) or getattr(long_p,  "price", 0.0)
            short_price = getattr(short_p, "mark_price", None) or getattr(short_p, "price", 0.0)
            long_rate    = getattr(long_p,  "funding_rate", 0.0)
            short_rate   = getattr(short_p, "funding_rate", 0.0)
            long_next    = getattr(long_p,  "next_funding_ts", 0)
            short_next   = getattr(short_p, "next_funding_ts", 0)
            closed = self.book.mark(
                pos, long_price, short_price,
                long_rate, short_rate, long_next, short_next,
            )
            if closed is not None:
                # Sprint 1 -- record bad exit to cooldown tracker.
                if self._cooldown_tracker is not None:
                    try:
                        was_bad = self._cooldown_tracker.record_close(
                            base=closed.base,
                            exit_ts=closed.exit_ts or time.time(),
                            realized_pnl_usd=closed.realized_pnl_usd or 0.0,
                            exit_reason=closed.exit_reason or "",
                        )
                        if was_bad:
                            log.info(
                                "[Sprint1] cooldown set on %s after %s "
                                "(pnl=%.2f) — open allowed in %.0fh",
                                closed.base, closed.exit_reason,
                                closed.realized_pnl_usd or 0.0,
                                self._cooldown_tracker.seconds_until_open(closed.base) / 3600.0,
                            )
                    except Exception as cx:
                        log.debug("[Sprint1] cooldown record_close error: %s", cx)
                if self._alerts:
                    held_h = ((closed.exit_ts or time.time()) - closed.entry_ts) / 3600.0
                    await self._alerts.position_closed(
                        closed.base, closed.kind,
                        closed.exit_reason or "?", held_h,
                        closed.realized_pnl_usd or 0.0,
                    )

    async def _try_enter(self, fetcher, s: ScoredOpportunity) -> None:
        o = s.opportunity
        if self.book.already_open(o):
            return

        # Sprint 58 hard-pause (2026-06-14) — conservative FC main is
        # decommissioned. Evidence: 29 trades, cumulative PnL -$140,
        # crypto-only WR 12% (3W/22L), last 2 crypto trades STG -$9.97
        # and H -$28.44 (137bps basis drift). Basis drift > funding
        # income by ~8:1. The strategy itself is broken; equity_kind
        # fence (Sprint 55) doesn't fix the crypto-side losses.
        # While CONSERVATIVE_FC_PAPER_OPENS_PAUSED is set, no new opens.
        # Existing open positions can exit naturally — no force-close.
        import os as _os
        if _os.environ.get(
                "CONSERVATIVE_FC_PAPER_OPENS_PAUSED", ""
        ).strip().lower() in ("1", "true", "yes", "on", "y", "t"):
            self._record_reject(s, "conservative_fc_decommissioned", {
                "base":     o.base,
                "reason":   "negative_edge_confirmed",
                "evidence": {
                    "total_pnl_usd": -140,
                    "crypto_pnl_usd": -113,
                    "winrate":       0.12,
                    "basis_drift_exceeds_funding": True,
                },
                "allow_existing_exits": True,
                "sprint": 58,
            })
            return

        # Sprint 55 safety fence (2026-06-11) — block equity / ETF /
        # pre-IPO / TradFi opens at the conservative FC paper candidate
        # gate. Sprint 54 audit found 4 equity perps (NVDA / SOXL /
        # SKHYNIX / SOXL) had reached this book despite being non-crypto;
        # cost was $-26.89 on 4 trades with $0 funding income. Shared
        # classifier (funding_carry/asset_kind.py) mirrors listing
        # parser v1.4. PAPER-only: this gate runs in the paper-book code
        # path. No live/executor path mutation.
        try:
            ak_ok, ak_info = _ak_eligible(o.base or "", "")
        except Exception:
            ak_ok, ak_info = True, {"asset_kind": "unknown",
                                     "asset_kind_reason": "classifier_error"}
        if not ak_ok:
            self._record_reject(s, "asset_kind_not_execution_eligible", {
                "base":               o.base,
                "asset_kind":         ak_info.get("asset_kind", "?"),
                "asset_kind_reason":  ak_info.get("asset_kind_reason", "?"),
                "execution_eligible": False,
            })
            return

        # Sprint 1 (2026-05-26): block any second open on the SAME base
        # (we used to allow if (kind, long_ex, short_ex) was different —
        # but live data showed 2-3× re-entries on NEAR / INJ / BCH all
        # losing money). One open position per base, always.
        if self._safe_mode and self._is_base_already_open(o.base):
            self._record_reject(s, "duplicate_symbol_open",
                                {"base": o.base})
            return
        # Gate: both legs must be on EXECUTABLE venues
        if (o.long_leg.exchange  not in EXECUTABLE_VENUES or
            o.short_leg.exchange not in EXECUTABLE_VENUES):
            self._record_reject(s, "non_executable_venue", {
                "long_ex": o.long_leg.exchange,
                "short_ex": o.short_leg.exchange,
            })
            return

        # Sprint 1 — safe-diagnostic gates (cheap checks first, no I/O).
        if self._safe_mode:
            # 1) Temp blacklist (NVDA, NEAR per diagnostic of -EV history).
            if o.base in TEMP_BLACKLIST_BASES:
                self._record_reject(s, "symbol_temp_blacklist",
                                    {"base": o.base})
                return
            # 2) Cooldown after recent bad exit.
            if self._cooldown_tracker is not None and \
               self._cooldown_tracker.is_on_cooldown(o.base):
                self._record_reject(s, "symbol_cooldown_after_bad_exit", {
                    "base": o.base,
                    "seconds_remaining": round(
                        self._cooldown_tracker.seconds_until_open(o.base), 0
                    ),
                })
                return
            # 3) Minimum funding rate (normalised to 8h).
            try:
                max_rate = max_abs_perp_funding_rate(o)
                interval_h = 8.0
                for _leg in (o.long_leg, o.short_leg):
                    if _leg.leg_kind == "perp" and getattr(
                            _leg, "funding_interval_h", 0.0):
                        interval_h = float(_leg.funding_interval_h)
                        break
                rate_8h = normalised_rate_per_8h(max_rate, interval_h)
                if rate_8h < MIN_FUNDING_RATE_PER_PERIOD_SAFE:
                    self._record_reject(s, "funding_rate_too_low", {
                        "base": o.base,
                        "rate_per_interval": round(max_rate, 6),
                        "interval_h": interval_h,
                        "rate_per_8h": round(rate_8h, 6),
                        "min_required_per_8h": MIN_FUNDING_RATE_PER_PERIOD_SAFE,
                    })
                    return
            except Exception as gx:
                log.debug("[Sprint1] funding_rate gate error: %s", gx)
            # 4) Net APR floor (uses score's pre-computed 7d projection).
            if s.net_apr_pct_7d < MIN_NET_APR_PCT_SAFE:
                self._record_reject(s, "net_apr_too_low", {
                    "base": o.base,
                    "net_apr_pct_7d": round(s.net_apr_pct_7d, 1),
                    "min_required_apr": MIN_NET_APR_PCT_SAFE,
                })
                return

        # Carry is a multi-day delta-neutral hold — no timing gate needed.
        # (Timing gate is only for directional / short-hold settlement bets.)

        # Sizing
        notional, capital_locked = compute_allocation(
            equity_usd=self.book.equity_usd,
            capital_already_deployed_usd=self.book.capital_deployed_usd(),
            n_open_positions=len(self.book.open_positions),
            max_concurrent=MAX_CONCURRENT_POSITIONS,
            opportunity_kind=s.kind,
        )
        if notional < 50.0:   # below this notional, slippage dominates
            return

        # Sprint 1 — fee-coverage gate (needs notional → runs after sizing).
        if self._safe_mode:
            try:
                cov = estimate_fee_coverage(opp=o, notional_usd=notional)
                if cov.coverage_ratio < MIN_FEE_COVERAGE_RATIO_SAFE:
                    self._record_reject(s, "fee_coverage_too_low", {
                        "base": o.base,
                        "notional_usd": round(notional, 0),
                        "fees_roundtrip_usd": round(cov.fees_roundtrip_usd, 3),
                        "funding_per_cycle_usd": round(cov.funding_per_cycle_usd, 3),
                        "funding_2_cycles_usd":  round(cov.funding_2_cycles_usd, 3),
                        "coverage_ratio": round(cov.coverage_ratio, 2),
                        "min_required": MIN_FEE_COVERAGE_RATIO_SAFE,
                    })
                    return
            except Exception as gx:
                log.debug("[Sprint1] fee_coverage gate error: %s", gx)

        # Fetch both orderbooks in parallel
        long_ob, short_ob = await asyncio.gather(
            fetch_orderbook(fetcher.session, o.long_leg.exchange,
                            o.long_leg.symbol, o.long_leg.leg_kind),
            fetch_orderbook(fetcher.session, o.short_leg.exchange,
                            o.short_leg.symbol, o.short_leg.leg_kind),
            return_exceptions=False,
        )

        verdict = validate_legs(o, long_ob, short_ob, notional)
        if not verdict.ok:
            self._record_reject(s, verdict.reason, {
                "long_spread_bps":  verdict.long_spread_bps,
                "short_spread_bps": verdict.short_spread_bps,
                "long_depth_usd":   verdict.long_depth_usd,
                "short_depth_usd":  verdict.short_depth_usd,
                "price_diff_bps":   verdict.price_diff_bps,
                "needed_usd":       notional * 2.0,
            })
            return

        pos = self.book.open(
            o, notional, capital_locked,
            long_fill_price=verdict.long_fill_price,
            short_fill_price=verdict.short_fill_price,
        )
        if pos is not None and self._alerts:
            await self._alerts.position_opened(
                o.base, s.kind,
                o.long_leg.exchange, o.short_leg.exchange,
                notional, s.net_apr_pct_7d,
            )

    def _is_base_already_open(self, base: str) -> bool:
        """Sprint 1: stricter than book.already_open (which is keyed on
        full opportunity signature). One open position per base, period."""
        for p in self.book.open_positions.values():
            if (p.base or "").upper() == (base or "").upper():
                return True
        return False

    def _record_reject(self, s: ScoredOpportunity, reason: str, extra: dict) -> None:
        o = s.opportunity
        log.info("[reject] %s %s [%s] %s — %s %s",
                 o.base, s.kind, f"{o.long_leg.exchange}->{o.short_leg.exchange}",
                 o.long_leg.symbol, reason, extra)
        # Phase 2: counters for hourly/daily summaries
        self._reject_counts[reason]        += 1
        self._reject_counts_hourly[reason] += 1
        self._reject_counts_daily[reason]  += 1
        _append_jsonl(REJECT_LOG_FILE, {
            "ts": time.time(),
            "base": o.base, "kind": s.kind,
            "long_ex": o.long_leg.exchange, "short_ex": o.short_leg.exchange,
            "reason": reason, **extra,
        })


def _install_signals(runner: FundingCarryRunner) -> None:
    try:
        loop = asyncio.get_running_loop()
        for s in (os_signal.SIGINT, os_signal.SIGTERM):
            try:
                loop.add_signal_handler(s, runner.request_stop)
            except NotImplementedError:
                pass
    except RuntimeError:
        pass


async def _main() -> None:
    # Load .env if present (python-dotenv; silently skipped if not installed)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    _setup_logging()
    runner = FundingCarryRunner()
    _install_signals(runner)
    try:
        await runner.run()
    except KeyboardInterrupt:
        runner.request_stop()


if __name__ == "__main__":
    asyncio.run(_main())
