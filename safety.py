"""
funding_carry/safety.py

Production-safety layer shared by the carry runner.

Components
----------
CircuitBreaker
    Monitors equity drawdown from peak.  When drawdown >= MAX_DD_PCT the
    circuit trips and no new positions are opened.  Auto-resets after
    COOLDOWN_S once equity climbs back above the threshold.

TelegramAlerter
    Thin async wrapper around the Telegram Bot sendMessage API.
    Silently no-ops if TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars
    are absent — safe to use unconditionally.

ErrorTracker
    Counts consecutive cycle failures.  After ALERT_AFTER_N_ERRORS
    consecutive failures it sends a Telegram alert and optionally trips
    the circuit breaker.

Usage
-----
    Set env vars (e.g. in a .env file loaded with python-dotenv):
        TELEGRAM_BOT_TOKEN=123456:ABC...
        TELEGRAM_CHAT_ID=-1001234567890
        CARRY_MAX_DD_PCT=15        (optional, default 15 %)
        CARRY_CB_COOLDOWN_S=3600   (optional, default 1 h)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

# ── env-configurable defaults ─────────────────────────────────────────────────
MAX_DD_PCT         = float(os.getenv("CARRY_MAX_DD_PCT",   "15.0"))
CB_COOLDOWN_S      = float(os.getenv("CARRY_CB_COOLDOWN_S", "3600.0"))
ALERT_AFTER_N_ERRS = int(os.getenv("CARRY_ALERT_ERRORS",  "3"))

# Accept either TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID (funding convention) or
# the existing news_trader naming TG_BOT_TOKEN/TG_CHAT_ID — same physical bot.
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "") or os.getenv("TG_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID",   "") or os.getenv("TG_CHAT_ID",   "")


# ─────────────────────────────────────────────────────────────────────────────
class CircuitBreaker:
    """
    Drawdown-based circuit breaker.

    Call `.update(equity)` every cycle to track the equity peak.
    Call `.check(equity)` before opening any new position — if it returns
    True the circuit is open and you should skip new entries.
    """

    def __init__(
        self,
        equity_start: float,
        max_dd_pct: float = MAX_DD_PCT,
        cooldown_s: float = CB_COOLDOWN_S,
    ) -> None:
        self._peak       = equity_start
        self._max_dd_pct = max_dd_pct
        self._cooldown_s = cooldown_s
        self._tripped_at: float = 0.0

    # public ──────────────────────────────────────────────────────────────────

    def update(self, equity: float) -> None:
        """Advance peak equity; clear trip flag on a new all-time high."""
        if equity > self._peak:
            self._peak = equity
            self._tripped_at = 0.0     # new high resets the breaker

    def check(self, equity: float) -> tuple[bool, float]:
        """
        Returns (is_tripped, drawdown_pct).

        is_tripped=True means do NOT open new positions.
        """
        dd_pct = (self._peak - equity) / max(self._peak, 1.0) * 100.0

        # DD threshold exceeded → trip
        if dd_pct >= self._max_dd_pct:
            if not self._tripped_at:
                self._tripped_at = time.time()
                log.warning(
                    "[circuit_breaker] TRIPPED — DD %.1f%% >= %.1f%% (peak=$%.2f)",
                    dd_pct, self._max_dd_pct, self._peak,
                )
            return True, dd_pct

        # Still inside cooldown after a previous trip
        if self._tripped_at:
            elapsed = time.time() - self._tripped_at
            if elapsed < self._cooldown_s:
                return True, dd_pct
            # Cooldown elapsed — allow trading again
            log.info("[circuit_breaker] reset after %.0fs cooldown", elapsed)
            self._tripped_at = 0.0

        return False, dd_pct

    @property
    def peak(self) -> float:
        return self._peak


# ─────────────────────────────────────────────────────────────────────────────
async def _telegram_post(
    session: aiohttp.ClientSession,
    text: str,
) -> None:
    """Fire-and-forget Telegram message. Never raises."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with session.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=aiohttp.ClientTimeout(total=8.0),
        ) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                log.debug("[telegram] non-200 %d: %s", resp.status, body[:120])
    except Exception as exc:
        log.debug("[telegram] send failed: %s", exc)


class TelegramAlerter:
    """
    Async Telegram notifier.

    All methods are safe to call even when Telegram is not configured
    (no token/chat-id) — they silently no-op.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        bot_name: str = "carry",
    ) -> None:
        self._session = session
        self._prefix  = f"<b>[{bot_name.upper()}]</b> "
        self._enabled = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
        if not self._enabled:
            log.info("[telegram] alerter disabled — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to enable")

    async def send(self, text: str) -> None:
        await _telegram_post(self._session, self._prefix + text)

    # ── convenience helpers ───────────────────────────────────────────────────

    async def started(self, equity: float) -> None:
        await self.send(f"Bot started — equity <b>${equity:.2f}</b>")

    async def stopped(self, summary: dict) -> None:
        await self.send(
            f"Bot stopped.\n"
            f"equity=${summary.get('equity_usd','?')}  "
            f"open={summary.get('open',0)}  "
            f"closed={summary.get('closed',0)}"
        )

    async def position_opened(
        self, base: str, kind: str,
        long_ex: str, short_ex: str,
        notional: float, net_apr_pct: float,
    ) -> None:
        await self.send(
            f"OPEN <b>{base}</b> {kind}\n"
            f"LONG {long_ex} | SHORT {short_ex}\n"
            f"notional=${notional:.0f}  net APR {net_apr_pct:+.1f}%"
        )

    async def position_closed(
        self, base: str, kind: str,
        reason: str, held_h: float, pnl: float,
    ) -> None:
        sign = "+" if pnl >= 0 else ""
        emoji = "✅" if pnl >= 0 else "❌"
        await self.send(
            f"{emoji} CLOSE <b>{base}</b> {kind}\n"
            f"reason={reason}  held={held_h:.1f}h\n"
            f"pnl={sign}${pnl:.4f}"
        )

    async def circuit_breaker_tripped(self, dd_pct: float, equity: float) -> None:
        await self.send(
            f"⚠️ CIRCUIT BREAKER TRIPPED\n"
            f"drawdown={dd_pct:.1f}%  equity=${equity:.2f}\n"
            f"No new positions until recovery."
        )

    async def consecutive_errors(self, n: int, last_error: str) -> None:
        await self.send(
            f"⚠️ {n} consecutive cycle errors\n"
            f"last: {last_error[:200]}"
        )

    async def stale_data(self, venue: str, age_s: float) -> None:
        await self.send(f"⚠️ Stale data from <b>{venue}</b> — {age_s:.0f}s old")


# ─────────────────────────────────────────────────────────────────────────────
class ErrorTracker:
    """
    Counts consecutive cycle failures.

    After ALERT_AFTER_N_ERRS consecutive failures, fires a Telegram alert
    (only once per streak, not every cycle).
    """

    def __init__(self, alert_after: int = ALERT_AFTER_N_ERRS) -> None:
        self._count       = 0
        self._alert_after = alert_after
        self._alerted_at  = 0   # streak count when we last alerted
        self._last_error  = ""

    def ok(self) -> None:
        """Call on a successful cycle — resets the streak."""
        self._count = 0
        self._alerted_at = 0
        self._last_error = ""

    def fail(self, exc: BaseException) -> bool:
        """
        Call on a cycle failure.
        Returns True if we should fire a Telegram alert right now.
        """
        self._count += 1
        self._last_error = str(exc)
        if (self._count >= self._alert_after
                and self._count != self._alerted_at):
            self._alerted_at = self._count
            return True
        return False

    @property
    def count(self) -> int:
        return self._count

    @property
    def last_error(self) -> str:
        return self._last_error
