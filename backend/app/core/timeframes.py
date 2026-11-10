"""
Single source of truth for supported chart timeframes.

Keeping the whitelist, cache TTLs and upstream (Yahoo) mapping in one place
prevents the API, the market feed and the adapter from drifting apart.
"""
from __future__ import annotations
from typing import Dict, Tuple

# Supported timeframe keys accepted by the API / chart UI.
TIMEFRAME_SECONDS: Dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
    # Weekly / monthly candles exist so a chart can reach back years: 1d is
    # capped at five years of daily bars, and everything older needs a coarser
    # bucket. Keys avoid a bare "1M" on purpose: the API lowercases input, so
    # "1M" would collide with the 1-minute key.
    "1wk": 604800,
    "1mo": 2592000,
}

# Freshness window for cached bars per timeframe.
TIMEFRAME_TTLS: Dict[str, float] = {
    "1m": 15.0,
    "5m": 60.0,
    "15m": 120.0,
    "1h": 300.0,
    "4h": 600.0,
    "1d": 1800.0,
    # Weekly and monthly series move once per session at most; an hour of
    # freshness is far more than any chart needs and keeps upstream traffic
    # negligible.
    "1wk": 3600.0,
    "1mo": 3600.0,
}

# Yahoo Finance (interval, range) pairs per timeframe.
YAHOO_TIMEFRAME_MAP: Dict[str, Tuple[str, str]] = {
    "1m": ("1m", "7d"),
    "5m": ("5m", "1mo"),
    "15m": ("15m", "1mo"),
    "1h": ("1h", "6mo"),
    "4h": ("1h", "1y"),
    "1d": ("1d", "5y"),
    # A week bucket over ten years and a month bucket over the whole listed
    # history: together with 1d this covers the "one year / five years / as
    # far back as it goes" chart asks.
    "1wk": ("1wk", "10y"),
    "1mo": ("1mo", "max"),
}
