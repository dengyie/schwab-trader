"""
DefeatBeta (HuggingFace Parquet) adapter — deep US daily history as a fallback.

The dataset is one Parquet file of US daily bars served by HuggingFace, scanned
with DuckDB. Facts measured on 0.0.63 (2026-11-11):

* US-only by construction — the library hardcodes ``data/US/{table}.parquet``;
  a HK/JP/CN symbol returns an empty frame, which is why this adapter gates on
  the symbol's market before touching upstream.
* Daily bars to the early 1990s (AAPL: 8k rows), ``report_date`` date strings,
  prices already split-adjusted; no intraday data at all.
* A full per-symbol scan costs ~1.5-2s; the library caches the fetched frame
  in-process, so repeat calls are free. One bar request per user paging click
  is therefore acceptable, and a module-level TTL cache below keeps even that
  off the hot path.

The role here is narrow: when Yahoo answers a daily paging request with
nothing (rate-limited, or the symbol simply predates Yahoo's coverage), this
fills the segment instead of storing a false "data begins here" edge.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from app.models.trading import Bar

logger = logging.getLogger(__name__)

# The full history is one Parquet row group away, so "paging" defeatbeta means
# serving the whole series once and letting BarCache keep the segment. The
# fetch is heavy (~2s) but in-process cached by the library; this module adds
# a failure cooldown so a dead dataset or a network partition does not turn
# every paging click into another 2s stall.
_DEFEATBETA_TTL_SECONDS = 24 * 3600.0
_DEFEATBETA_COOLDOWN_SECONDS = 600.0

_cache_lock = threading.Lock()
_frame_cache: Dict[str, Tuple[float, List[Bar]]] = {}
_cooldown: Dict[str, float] = {}
_last_error: str = ""


def _load_daily_bars(symbol: str) -> List[Bar]:
    """Split-adjusted US daily bars for ``symbol``, oldest first.

    Raises on upstream failure so the caller can distinguish "no such symbol"
    (empty list) from "cannot reach the dataset" (exception + cooldown).
    """
    # Imported lazily: defeatbeta-api pulls DuckDB, pandas and friends —
    # several seconds of import — and the backend must not pay that at boot
    # for a fallback most sessions never use.
    from defeatbeta_api.data.ticker import Ticker

    df = Ticker(symbol.upper().replace(".", "-"), log_level=None).price()
    bars: List[Bar] = []
    if df is None or df.empty:
        return bars
    for row in df.itertuples(index=False):
        # report_date is a date string (or datetime) at the session bucket;
        # defeatbeta has no intraday, so the bucket is midnight ET of that
        # session — the same convention the chart's daily key already uses.
        raw = row.report_date
        if isinstance(raw, str):
            day = datetime.strptime(raw[:10], "%Y-%m-%d")
        else:
            day = datetime(raw.year, raw.month, raw.day)
        ts = int(day.replace(tzinfo=timezone.utc).timestamp())
        if row.open is None or row.close is None:
            continue
        bars.append(Bar(
            time=ts,
            open=round(float(row.open), 4),
            high=round(float(row.high), 4),
            low=round(float(row.low), 4),
            close=round(float(row.close), 4),
            volume=float(row.volume or 0),
        ))
    bars.sort(key=lambda b: b.time)
    return bars


def fetch_daily_history(symbol: str, before: int, page_size: int) -> Optional[Tuple[List[Bar], float]]:
    """Bars for one daily-history page strictly older than ``before``.

    Returns ``(bars, fetched_at)`` or ``None`` when upstream failed (caller
    keeps its existing cooldown behavior). Bars are trimmed to ``page_size``
    newest-oldest, honoring the segment contract.
    """
    global _last_error
    key = symbol.upper()
    now = time.time()
    with _cache_lock:
        hit = _frame_cache.get(key)
        if hit and now - hit[0] < _DEFEATBETA_TTL_SECONDS:
            all_bars = hit[1]
        else:
            if now - _cooldown.get(key, 0.0) < _DEFEATBETA_COOLDOWN_SECONDS:
                return None
            try:
                all_bars = _load_daily_bars(key)
            except Exception as exc:
                _cooldown[key] = now
                _last_error = str(exc)
                logger.warning("DefeatBeta daily fetch failed for %s: %s", key, exc)
                return None
            _frame_cache[key] = (now, all_bars)
            # Bound the module cache: one symbol's 30y of dailies is ~8k Bars.
            if len(_frame_cache) > 64:
                oldest = min(_frame_cache, key=lambda k: _frame_cache[k][0])
                _frame_cache.pop(oldest, None)

    older = [b for b in all_bars if b.time < before]
    if len(older) > page_size:
        older = older[-page_size:]
    return older, time.time()


def cooldown_remaining(symbol: str) -> float:
    key = symbol.upper()
    with _cache_lock:
        return max(0.0, _DEFEATBETA_COOLDOWN_SECONDS - (time.time() - _cooldown.get(key, 0.0)))


def stats() -> Dict[str, object]:
    """Ops counters for the health payload and the vault's observability."""
    with _cache_lock:
        return {"cached_symbols": len(_frame_cache), "last_error": _last_error or None}
