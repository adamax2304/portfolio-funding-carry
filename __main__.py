"""
funding_carry package entry point: `py -3 -m funding_carry`.

Loads .env from a priority chain BEFORE importing the rest of the package
so that module-top env captures (safety.TELEGRAM_TOKEN etc.) see the values.
Search order:
  1. <project_root>/.env
  2. <project_root>/funding_carry/.env
  3. <project_root>/funding_directional/.env  (shared funding env)
  4. <project_root>/news_trader/news_trader/.env  (existing token lives here)
"""
from pathlib import Path

try:
    from dotenv import load_dotenv
    _ROOT = Path(__file__).resolve().parent.parent
    for _candidate in (
        _ROOT / ".env",
        _ROOT / "funding_carry" / ".env",
        _ROOT / "funding_directional" / ".env",
        _ROOT / "news_trader" / "news_trader" / ".env",
        _ROOT / "news_trader" / ".env",
    ):
        if _candidate.exists():
            load_dotenv(_candidate, override=False)
except ImportError:
    pass

from .main import _main
import asyncio
asyncio.run(_main())
