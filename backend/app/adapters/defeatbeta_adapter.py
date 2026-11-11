"""
DefeatBeta (HuggingFace Parquet) adapter — deep US daily history as a fallback.

The dataset is one 450MB Parquet file of US daily bars served by HuggingFace
(``defeatbeta/yahoo-finance-data``), scanned with DuckDB over httpfs. Facts
measured directly (2026-11-11):

* US-only by construction — the dataset is partitioned ``data/US/*.parquet``;
  a HK/JP/CN symbol simply has no rows, which is why this adapter gates on the
  symbol's market before touching the network.
* Daily bars to the early 1990s (AAPL: 8k rows), ``report_date`` VARCHAR dates,
  prices already split-adjusted; no intraday data at all.
* A whole-symbol scan costs ~2s server-side (server-side projection prunes the
  file); this module caches the per-symbol frame for a day so one scan serves
  every paging click that session, and cools down 10 minutes after a failure
  so a network partition does not turn each click into another 2s stall.

The role here is narrow: when Yahoo answers the whole-window daily request
with nothing (rate-limited via the Edge disguise, or the symbol predates
Yahoo's coverage), this fills the daily window instead of leaving the chart
blank. Daily keys are served whole by design — there is no daily paging —
so the whole-window fetch IS the daily request; a later successful Yahoo
refresh overwrites the fallback bars through the normal cache cycle.

DuckDB is queried directly instead of via the defeatbeta-api package: the
package's 0.0.63 requires Python >=3.11 (prod runs 3.10) while its older
versions still point at the pre-partitioned dataset layout and 404. The
interesting logic here is one SQL WHERE clause; a second dependency chain
(duckdb + pandas + ipython + openai transitively) bought nothing.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from app.models.trading import Bar

logger = logging.getLogger(__name__)

_DATASET_URL = (
    "https://huggingface.co/datasets/defeatbeta/yahoo-finance-data"
    "/resolve/main/data/US/stock_prices.parquet"
)
# One symbol's full daily history (~8k rows) cached for a day — the dataset
# itself updates daily, so a longer TTL would serve stale bars after an
# overnight session, a shorter one would re-scan per paging click.
_DEFEATBETA_TTL_SECONDS = 24 * 3600.0
_DEFEATBETA_COOLDOWN_SECONDS = 600.0
_MAX_CACHED_SYMBOLS = 64

_cache_lock = threading.Lock()
_frame_cache: Dict[str, Tuple[float, List[Bar]]] = {}
_cooldown: Dict[str, float] = {}
_last_error: str = ""


def _load_daily_bars(symbol: str) -> List[Bar]:
    """Split-adjusted US daily bars for ``symbol``, oldest first.

    Raises on upstream failure so the caller can distinguish "no such symbol"
    (empty list) from "cannot reach the dataset" (exception + cooldown).
    """
    # Imported lazily: duckdb is a heavy binary wheel and the fallback must
    # not tax boot or the test loop for work most sessions never do.
    import duckdb

    sym = symbol.upper()
    rows = duckdb.sql(
        f"""
        SELECT report_date, open, high, low, close, volume
        FROM '{_DATASET_URL}'
        WHERE symbol = '{sym}'
        ORDER BY report_date
        """
    ).fetchall()

    bars: List[Bar] = []
    for report_date, o, h, low, c, v in rows:
        # report_date is a VARCHAR session date; the daily bucket is midnight
        # UTC of that session — the same convention the chart's daily key
        # already uses (Yahoo dailies are stamped the same way).
        day = datetime.strptime(str(report_date)[:10], "%Y-%m-%d")
        ts = int(day.replace(tzinfo=timezone.utc).timestamp())
        if o is None or c is None:
            continue
        bars.append(Bar(
            time=ts,
            open=round(float(o), 4),
            high=round(float(h), 4),
            low=round(float(low), 4),
            close=round(float(c), 4),
            volume=float(v or 0),
        ))
    return bars


def fetch_daily_history(symbol: str) -> Optional[Tuple[List[Bar], float]]:
    """Full split-adjusted US daily history for ``symbol``, oldest first.

    Returns ``(bars, fetched_at)`` or ``None`` when upstream failed (the
    caller keeps its own cooldown). The per-symbol frame is cached for a
    day, so one dataset scan serves every request that session — including
    the empty result of a symbol the dataset does not list.
    """
    global _last_error
    key = symbol.upper()
    now = time.time()
    with _cache_lock:
        hit = _frame_cache.get(key)
        if hit and now - hit[0] < _DEFEATBETA_TTL_SECONDS:
            return hit[1], time.time()
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
        if len(_frame_cache) > _MAX_CACHED_SYMBOLS:
            oldest = min(_frame_cache, key=lambda k: _frame_cache[k][0])
            _frame_cache.pop(oldest, None)
    return all_bars, time.time()


def cooldown_remaining(symbol: str) -> float:
    key = symbol.upper()
    with _cache_lock:
        return max(0.0, _DEFEATBETA_COOLDOWN_SECONDS - (time.time() - _cooldown.get(key, 0.0)))


def stats() -> Dict[str, object]:
    """Ops counters for the health payload and the vault's observability."""
    with _cache_lock:
        return {"cached_symbols": len(_frame_cache), "last_error": _last_error or None}
