"""
funding_carry/external/

Sprint 19 — external data sources (read-only, NEVER triggers trades).

`coinglass_scanner` polls CoinGlass funding-rate arbitrage as an external
shadow view of the carry universe.  It writes JSONL only; no orders, no
executor calls, no live-trading code paths.
"""
