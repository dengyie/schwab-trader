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

# Historical span ceiling per timeframe, in days. These are Yahoo's hard
# limits, not preferences: past them the upstream API has no data, and the
# chart shows a "data begins here" edge instead of pretending otherwise.
# None = cursor paging is not offered for that timeframe (daily and coarser
# buckets are fetched whole already; paging them would only multiply keys).
TIMEFRAME_SPAN_DAYS: Dict[str, Optional[int]] = {
    "1m": 7,
    "5m": 60,
    "15m": 60,
    "1h": 730,
    "4h": 730,
    "1d": None,
    "1wk": None,
    "1mo": None,
}

# Bars served per historical segment (one "load older" page). Minute buckets
# carry ~200KB per 2000 bars; daily+ buckets change once per session so a
# smaller page is plenty.
HIST_SEGMENT_BARS = 2000
HIST_SEGMENT_BARS_DAILY = 400

# History is immutable: a segment fetched once stays true for a week, so its
# TTL dwarfs the fresh-window TTLs above. Refreshing it more often is pure
# upstream waste.
HIST_SEGMENT_TTL_SECONDS = 7 * 24 * 3600.0

# At most this many historical segments are cached per (symbol, timeframe):
# the edge segment plus a few pages back. A 4th segment evicts the oldest.
MAX_HIST_SEGMENTS_PER_KEY = 3

# Bars per historical page by bucket granularity (daily+ buckets change once
# per session, so a smaller page is plenty; minute buckets fall back to
# HIST_SEGMENT_BARS). Shared by the Yahoo and Alpaca feeds — this is the
# paging contract, not a per-feed preference.
HIST_PAGE_BARS: Dict[str, int] = {
    "1d": HIST_SEGMENT_BARS_DAILY,
    "1wk": HIST_SEGMENT_BARS_DAILY,
    "1mo": HIST_SEGMENT_BARS_DAILY,
}
# A page fetches a window wide enough to plausibly fill the page size; the
# result is trimmed to the page contract afterwards.
HIST_PAGE_SPAN_SECONDS = 366 * 86400

# Yahoo Finance (interval, range) pairs per timeframe.
# A ``None`` range means "whole listed history via explicit epoch bounds":
# Yahoo coarsens the interval server-side when range=max (measured
# 2026-11-12: interval=1mo + range=max returns 4 bars/year — quarterly
# buckets mislabeled as monthly — while every explicit range stays true
# monthly), so whole-history keys must not ask for "max".
YAHOO_TIMEFRAME_MAP: Dict[str, Tuple[str, Optional[str]]] = {
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
    "1mo": ("1mo", None),
}

# Whole-window span in seconds per bucket, mirroring the ranges above — the
# defeatbeta fallback trims its bars to the same window Yahoo serves, so the
# chart does not change width when the fallback hands back to a recovered
# Yahoo (``None`` = unbounded, same as the ``None`` range). If a range above
# ever changes, change this with it.
WHOLE_WINDOW_SPAN_SECONDS: Dict[str, Optional[int]] = {
    "1d": 5 * 366 * 86400,
    "1wk": 10 * 366 * 86400,
    "1mo": None,
}
