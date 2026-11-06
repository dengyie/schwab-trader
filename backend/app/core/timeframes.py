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
}

# Freshness window for cached bars per timeframe.
TIMEFRAME_TTLS: Dict[str, float] = {
    "1m": 15.0,
    "5m": 60.0,
    "15m": 120.0,
    "1h": 300.0,
    "4h": 600.0,
    "1d": 1800.0,
}

# Yahoo Finance (interval, range) pairs per timeframe.
YAHOO_TIMEFRAME_MAP: Dict[str, Tuple[str, str]] = {
    "1m": ("1m", "7d"),
    "5m": ("5m", "1mo"),
    "15m": ("15m", "1mo"),
    "1h": ("1h", "6mo"),
    "4h": ("1h", "1y"),
    "1d": ("1d", "2y"),
}
