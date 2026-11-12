"""
funding_carry/watchdog.py — auto-restart for the funding_carry paper bot.

Launch from the PARENT directory:
    py -3 -m funding_carry.watchdog
or:
    cd "C:\\Users\\adama\\Documents\\zeph ai projects"
    py -3 funding_carry/watchdog.py

The watchdog itself is intentionally tiny and synchronous. It only
supervises the bot subprocess and emits Telegram alerts. It does NOT
touch trading state, balances or positions.

Telegram alerts on:
  - watchdog started
  - bot started (each attempt)
  - bot crashed (exit code logged)
  - bot restarted
  - 3 consecutive fast-fails (< MIN_ALIVE_S each)

Env vars (optional):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID — for alerts (silent no-op if absent)
    CARRY_WD_RESTART_DELAY_S            — default 15
    CARRY_WD_MAX_RESTARTS               — default 200
    CARRY_WD_MIN_ALIVE_S                — default 60
    CARRY_WD_FAST_FAIL_ALERT_AT         — default 3
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# ── env (.env) ───────────────────────────────────────────────────────────────
# Best-effort load of .env from a priority chain so the same TELEGRAM_*
# token can be shared across news_trader / funding_carry / funding_directional
# without duplicating .env files.
try:
    from dotenv import load_dotenv
    _ROOT = Path(__file__).resolve().parent.parent
    for _cand in (
        _ROOT / ".env",
        _ROOT / "funding_carry" / ".env",
        _ROOT / "funding_directional" / ".env",
        _ROOT / "news_trader" / "news_trader" / ".env",
        _ROOT / "news_trader" / ".env",
    ):
        if _cand.exists():
            load_dotenv(_cand, override=False)
    load_dotenv()           # also CWD if any
except ImportError:
    pass

# ── Config ────────────────────────────────────────────────────────────────────
RESTART_DELAY_S      = int(os.getenv("CARRY_WD_RESTART_DELAY_S", "15"))
MAX_RESTARTS         = int(os.getenv("CARRY_WD_MAX_RESTARTS",    "200"))
MIN_ALIVE_S          = int(os.getenv("CARRY_WD_MIN_ALIVE_S",     "60"))
FAST_FAIL_ALERT_AT   = int(os.getenv("CARRY_WD_FAST_FAIL_ALERT_AT", "3"))

BOT_NAME             = "CARRY"

_HERE      = Path(__file__).resolve().parent          # …/funding_carry
_PARENT    = _HERE.parent                              # …/zeph ai projects
STATE_DIR  = _HERE / "state"
LOGS_DIR   = _HERE / "logs"
LOG_FILE   = STATE_DIR / "watchdog.log"
PID_FILE   = STATE_DIR / "watchdog.pid"
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "") or os.getenv("TG_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID",   "") or os.getenv("TG_CHAT_ID",   "")

# Stdout/stderr UTF-8 fix (Windows cp1252 default crashes on non-ASCII log msgs)
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


class _SafeStreamHandler(logging.StreamHandler):
    """StreamHandler that drops Unicode-encode failures rather than killing
    the watchdog (root cause of the 2026-05 outage in news_trader watchdog)."""
    def emit(self, record):
        try:
            super().emit(record)
        except Exception:
            pass


logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [wd-{BOT_NAME.lower()}] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(str(LOG_FILE), mode="a", encoding="utf-8"),
        _SafeStreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(f"watchdog.{BOT_NAME.lower()}")


# ── Telegram (sync, stdlib only) ─────────────────────────────────────────────

def _tg_alert(text: str) -> None:
    """Best-effort Telegram notification. NEVER raises — alert errors
    must not crash the watchdog loop (warning logged at most)."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       f"[{BOT_NAME}-WD] {text}",
        "parse_mode": "HTML",
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status not in (200, 201):
                log.warning("telegram non-200: %d", resp.status)
    except urllib.error.URLError as exc:
        log.warning("telegram send failed: %s", exc)
    except Exception as exc:
        log.warning("telegram unexpected error: %s", exc)


# ── Bot launch command ───────────────────────────────────────────────────────
# Bot must be launched from the PARENT directory so `-m funding_carry`
# resolves correctly (the package lives there).
_BOT_ARGS = [sys.executable, "-m", "funding_carry"]
_BOT_ARGS += sys.argv[1:]   # forward any flags

_POPEN_KWARGS: dict = {
    "cwd": str(_PARENT),
    "env": os.environ.copy(),
}

if sys.platform == "win32":
    # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    _POPEN_KWARGS["creationflags"] = 0x00000200 | 0x08000000


# ── PID / process helpers ────────────────────────────────────────────────────

def _is_alive(pid: int) -> bool:
    try:
        if pid <= 0 or pid == os.getpid():
            return False
        if sys.platform == "win32":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x100000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _kill(pid: int) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10)
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except Exception:
                os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


def _acquire_watchdog_instance() -> None:
    """Singleton guard — kill any older watchdog instance writing the same PID file."""
    my_pid = os.getpid()
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip() or "0")
            if old_pid > 0 and old_pid != my_pid and _is_alive(old_pid):
                log.warning("Another watchdog detected (pid=%d) -- killing it...", old_pid)
                _kill(old_pid)
                time.sleep(2)
        except (ValueError, OSError):
            pass
    PID_FILE.write_text(str(my_pid))

    @atexit.register
    def _cleanup_pid() -> None:
        try:
            if PID_FILE.exists() and PID_FILE.read_text().strip() == str(my_pid):
                PID_FILE.unlink()
        except Exception:
            pass


# ── Graceful shutdown ────────────────────────────────────────────────────────
_stop = False
_current_proc: subprocess.Popen | None = None


def _handle_stop(sig, frame):
    global _stop
    log.info("Signal %s received -- stopping watchdog", sig)
    _stop = True
    if _current_proc and _current_proc.poll() is None:
        log.info("Terminating bot pid=%d...", _current_proc.pid)
        try:
            _current_proc.terminate()
        except Exception:
            pass


signal.signal(signal.SIGINT, _handle_stop)
try:
    signal.signal(signal.SIGTERM, _handle_stop)
except (AttributeError, OSError):
    pass


# ── Main loop ────────────────────────────────────────────────────────────────

def run_watchdog():
    global _current_proc
    restarts = 0
    delay = RESTART_DELAY_S
    consecutive_fast_fails = 0

    _acquire_watchdog_instance()
    log.info("=" * 50)
    log.info("Watchdog started  pid=%d", os.getpid())
    log.info("Bot cmd: %s", " ".join(_BOT_ARGS))
    log.info("CWD:     %s", _PARENT)
    log.info("=" * 50)
    _tg_alert(f"watchdog started  pid={os.getpid()}")

    while not _stop and restarts < MAX_RESTARTS:
        start_ts = time.monotonic()
        log.info("[%s] Starting bot (attempt #%d)...",
                 datetime.now().strftime("%H:%M:%S"), restarts)

        try:
            proc = subprocess.Popen(_BOT_ARGS, **_POPEN_KWARGS)
        except Exception as exc:
            log.error("Failed to start bot: %s -- retry in %ds", exc, delay)
            _tg_alert(f"failed to launch: <code>{exc}</code> -- retry {delay}s")
            time.sleep(delay)
            restarts += 1
            continue

        _current_proc = proc
        log.info("Bot launched  pid=%d", proc.pid)
        if restarts == 0:
            _tg_alert(f"bot started  pid={proc.pid}")

        # Wait
        while True:
            try:
                proc.wait(timeout=5)
                break
            except subprocess.TimeoutExpired:
                if _stop:
                    try:
                        proc.terminate()
                        proc.wait(timeout=15)
                    except Exception:
                        proc.kill()
                    log.info("Watchdog stopped.")
                    _tg_alert("watchdog stopped (graceful)")
                    return

        _current_proc = None
        alive_s = time.monotonic() - start_ts
        code = proc.returncode

        # Make sure child tree is dead before respawn
        _kill(proc.pid)

        log.warning(
            "Bot exited  code=%s  uptime=%.0fs  attempt=#%d",
            code, alive_s, restarts,
        )
        _tg_alert(f"bot crashed  code={code}  uptime={alive_s:.0f}s")

        if _stop:
            break

        restarts += 1

        if alive_s < MIN_ALIVE_S:
            consecutive_fast_fails += 1
            delay = min(delay * 2, 300)
            log.warning("Fast-fail #%d -- restart delay -> %ds",
                        consecutive_fast_fails, delay)
            if consecutive_fast_fails == FAST_FAIL_ALERT_AT:
                _tg_alert(
                    f"⚠️ {FAST_FAIL_ALERT_AT} fast-fails in a row "
                    f"(each uptime <{MIN_ALIVE_S}s) -- investigate."
                )
        else:
            if consecutive_fast_fails > 0:
                log.info("Fast-fail streak cleared after stable run %.0fs", alive_s)
            consecutive_fast_fails = 0
            delay = RESTART_DELAY_S

        log.info("Restarting in %ds...", delay)
        _tg_alert(f"restarting in {delay}s (attempt #{restarts})")
        # Interruptible sleep
        for _ in range(int(delay)):
            if _stop:
                break
            time.sleep(1)

    if restarts >= MAX_RESTARTS:
        log.error("MAX_RESTARTS=%d reached -- watchdog giving up!", MAX_RESTARTS)
        _tg_alert(f"⚠️ MAX_RESTARTS={MAX_RESTARTS} reached -- giving up")
    else:
        log.info("Watchdog stopped cleanly.")


if __name__ == "__main__":
    run_watchdog()
