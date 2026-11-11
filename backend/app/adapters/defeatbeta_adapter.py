"""
DefeatBeta (HuggingFace Parquet) adapter — deep US daily history as a fallback.

The dataset is one 450MB Parquet file of US daily bars served by HuggingFace
(``defeatbeta/yahoo-finance-data``), scanned with DuckDB over httpfs. Facts
measured directly (2026-11-11/12):

* US-only by construction — the dataset is partitioned ``data/US/*.parquet``;
  a HK/JP/CN symbol simply has no rows, which is why this adapter gates on the
  symbol's market before touching the network.
* Daily bars to the early 1990s (AAPL: 8k rows), ``report_date`` VARCHAR dates,
  prices already split-adjusted; no intraday data at all.
* A whole-symbol scan costs ~2-7s (prod-measured 6.8s for KO, cold) — this
  module caches the per-symbol frame for a day so one scan serves every
  request that session, and cools down 10 minutes after a failure so an
  outage does not turn each click into another scan.
* The dataset trails Yahoo by one session (its newest bar is yesterday's
  session while Yahoo already has today's) — acceptable for a resilience
  fallback, not a primary feed.

The role here is narrow: when Yahoo answers the whole-window daily request
with nothing (rate-limited via the Edge disguise, or the symbol predates
Yahoo's coverage), this fills the daily window instead of leaving the chart
blank. Daily keys are served whole by design — there is no daily paging —
so the whole-window fetch IS the daily request; a later successful Yahoo
refresh overwrites the fallback bars through the normal cache cycle. The
dataset has no coarser bars, so 1wk/1mo windows are aggregated from the
same daily frame, stamped to Yahoo's measured conventions (see
_aggregate_weekly/_aggregate_monthly).

Timestamps: the dataset stamps each session at 00:00 UTC, but Yahoo stamps
its dailies at the session open (prod-measured 13:30 UTC summer / 14:30 UTC
winter — i.e. 9:30 America/New_York). Bars are restamped to that convention
so the two sources are interchangeable on the daily key, and day-range
guard logic that expects open-stamped dailies keeps working.

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
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.models.trading import Bar

logger = logging.getLogger(__name__)

_DATASET_URL = (
    "https://huggingface.co/datasets/defeatbeta/yahoo-finance-data"
    "/resolve/main/data/US/stock_prices.parquet"
)
# One symbol's full daily history (~8k rows) cached for a day — the dataset
# itself updates daily, so a longer TTL would serve stale bars after an
# overnight session, a shorter one would re-scan per request.
_DEFEATBETA_TTL_SECONDS = 24 * 3600.0
_DEFEATBETA_COOLDOWN_SECONDS = 600.0
_MAX_CACHED_SYMBOLS = 64
# A cold scan is a few seconds; a hung HuggingFace connection must not pin a
# caller thread forever (duckdb 1.5.x exposes no http timeout setting, so the
# bound lives here). 30s is ~4x the prod-measured worst case.
_SCAN_TIMEOUT_SECONDS = 30.0
# Yahoo daily bars are stamped at the session open; the dataset stamps the
# same session at 00:00 UTC. Restamping to the open convention keeps the
# two sources interchangeable on the daily key (see module docstring).
_SESSION_OPEN_TZ = ZoneInfo("America/New_York")

_cache_lock = threading.Lock()
_frame_cache: Dict[str, Tuple[float, List[Bar]]] = {}
_cooldown: Dict[str, float] = {}
_last_error: str = ""


def _session_open_epoch(report_date: str) -> int:
    """Epoch of 9:30 America/New_York on that session date.

    DST-aware: 13:30 UTC in summer, 14:30 UTC in winter — the same instant
    Yahoo stamps the session's daily bar with.
    """
    day = datetime.strptime(str(report_date)[:10], "%Y-%m-%d")
    return int(day.replace(hour=9, minute=30, tzinfo=_SESSION_OPEN_TZ).timestamp())


def _rows_to_bars(rows: list) -> List[Bar]:
    """Dataset rows → Bars, oldest first.

    Any row with a missing OHLC field is skipped (a partial row is not a
    candle), mirroring the Yahoo adapter's row discipline — one malformed
    row must not fail the whole symbol's history.
    """
    bars: List[Bar] = []
    for report_date, o, h, low, c, v in rows:
        if None in (o, h, low, c):
            continue
        bars.append(Bar(
            time=_session_open_epoch(report_date),
            open=round(float(o), 4),
            high=round(float(h), 4),
            low=round(float(low), 4),
            close=round(float(c), 4),
            volume=float(v or 0),
        ))
    return bars


def _load_daily_bars(symbol: str) -> List[Bar]:
    """Split-adjusted US daily bars for ``symbol``, oldest first.

    Raises on upstream failure so the caller can distinguish "no such symbol"
    (empty list) from "cannot reach the dataset" (exception + cooldown).
    """
    # Imported lazily: duckdb is a heavy binary wheel and the fallback must
    # not tax boot or the test loop for work most sessions never do.
    import duckdb

    sym = symbol.upper()
    rows = duckdb.execute(
        f"""
        SELECT report_date, open, high, low, close, volume
        FROM '{_DATASET_URL}'
        WHERE symbol = ?
        ORDER BY report_date
        """,
        [sym],
    ).fetchall()
    return _rows_to_bars(rows)


def _load_daily_bars_bounded(symbol: str) -> List[Bar]:
    """``_load_daily_bars`` with a wall-clock bound.

    duckdb has no configurable http timeout, so the scan runs on a helper
    thread the caller stops waiting on after ``_SCAN_TIMEOUT_SECONDS``. The
    orphaned scan keeps no lock (the caller's cache lock is released before
    this runs) and its result is discarded; the cooldown then keeps further
    scans from piling up on a dead upstream.
    """
    outcome: List[Tuple[str, object]] = []

    def _run() -> None:
        try:
            outcome.append(("ok", _load_daily_bars(symbol)))
        except BaseException as exc:  # propagate everything, incl. duckdb errors
            outcome.append(("error", exc))

    scanner = threading.Thread(target=_run, daemon=True, name="defeatbeta-scan")
    scanner.start()
    scanner.join(_SCAN_TIMEOUT_SECONDS)
    if scanner.is_alive():
        raise TimeoutError(f"DefeatBeta scan for {symbol} exceeded {_SCAN_TIMEOUT_SECONDS:.0f}s")
    kind, value = outcome[0]
    if kind == "error":
        raise value
    return value  # type: ignore[return-value]


def fetch_daily_history(symbol: str) -> Optional[Tuple[List[Bar], float]]:
    """Full split-adjusted US daily history for ``symbol``, oldest first.

    Returns ``(bars, fetched_at)`` or ``None`` when upstream failed (the
    caller keeps its own cooldown). The per-symbol frame is cached for a
    day, so one dataset scan serves every request that session — including
    the empty result of a symbol the dataset does not list. Callers get a
    copy: the cached frame must stay immutable behind the lock.
    """
    global _last_error
    key = symbol.upper()
    now = time.time()
    with _cache_lock:
        hit = _frame_cache.get(key)
        if hit and now - hit[0] < _DEFEATBETA_TTL_SECONDS:
            return list(hit[1]), now
        if now - _cooldown.get(key, 0.0) < _DEFEATBETA_COOLDOWN_SECONDS:
            return None
    # The scan runs OUTSIDE the lock: it is seconds of network IO, and a hung
    # upstream must not serialize every other symbol's fallback behind it.
    try:
        all_bars = _load_daily_bars_bounded(key)
    except Exception as exc:
        with _cache_lock:
            _cooldown[key] = time.time()
            _last_error = str(exc)[:200]
        logger.warning("DefeatBeta daily fetch failed for %s: %s", key, exc)
        return None
    with _cache_lock:
        _frame_cache[key] = (time.time(), all_bars)
        if len(_frame_cache) > _MAX_CACHED_SYMBOLS:
            oldest = min(_frame_cache, key=lambda k: _frame_cache[k][0])
            _frame_cache.pop(oldest, None)
    return list(all_bars), time.time()


# Bucket keys the dataset can answer: dailies directly, weekly/monthly by
# aggregation (the dataset has no coarser bars). The feed's fallback gate
# checks this set, so the adapter stays the single source of what it serves.
SUPPORTED_TIMEFRAMES = ("1d", "1wk", "1mo")


def fetch_history(symbol: str, timeframe: str) -> Optional[Tuple[List[Bar], float]]:
    """Whole-history US bars for ``timeframe`` in SUPPORTED_TIMEFRAMES.

    Dailies come straight from the dataset; weekly/monthly buckets are
    aggregated from the same daily frame, stamped to Yahoo's conventions
    (see _aggregate). Returns the same contract as ``fetch_daily_history``.
    """
    tf = (timeframe or "").lower()
    if tf not in SUPPORTED_TIMEFRAMES:
        return None
    daily = fetch_daily_history(symbol)
    if daily is None:
        return None
    bars, fetched_at = daily
    if tf == "1d":
        return bars, fetched_at
    if tf == "1wk":
        return _aggregate_weekly(bars), fetched_at
    return _aggregate_monthly(bars), fetched_at


def _aggregate_weekly(bars: List[Bar]) -> List[Bar]:
    """Trading-week buckets from daily bars, oldest first.

    Yahoo stamps a weekly bar at midnight America/New_York of the bucket's
    Monday — calendar Monday, even when the Monday is a holiday
    (prod-measured 2026-11-12: the MLK and Presidents' Day weeks are stamped
    Mon 05:00Z). OHLCV aggregates across the week's sessions.
    """
    return _aggregate_buckets(
        bars,
        key=lambda d: d.isocalendar()[:2],
        stamp=lambda d: _midnight_et(d - timedelta(days=d.weekday())),
    )


def _aggregate_monthly(bars: List[Bar]) -> List[Bar]:
    """Calendar-month buckets from daily bars, oldest first.

    Yahoo stamps the monthly bar at midnight America/New_York of the month's
    first calendar day — the 1st, holiday or not (prod-measured: KO's
    1985-01-01 stamp sits on New Year's Day).
    """
    return _aggregate_buckets(
        bars,
        key=lambda d: (d.year, d.month),
        stamp=lambda d: _midnight_et(d.replace(day=1)),
    )


def _midnight_et(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=_SESSION_OPEN_TZ).timestamp())


def _aggregate_buckets(
    bars: List[Bar],
    key: Callable[[date], object],
    stamp: Callable[[date], int],
) -> List[Bar]:
    """Group daily bars by ``key(session_date)`` and fold each group into one
    Bar stamped at ``stamp(session_date)`` of the group's first session.

    Bars arrive open-stamped (9:30 ET), so the ET calendar date of ``time``
    is the session date; groups are emitted oldest first.
    """
    buckets: Dict[object, List[Bar]] = {}
    order: List[object] = []
    for b in bars:
        day = datetime.fromtimestamp(b.time, tz=_SESSION_OPEN_TZ).date()
        k = key(day)
        if k not in buckets:
            buckets[k] = []
            order.append(k)
        buckets[k].append(b)

    aggregated: List[Bar] = []
    for k in order:
        chunk = buckets[k]
        first_day = datetime.fromtimestamp(chunk[0].time, tz=_SESSION_OPEN_TZ).date()
        aggregated.append(Bar(
            time=stamp(first_day),
            open=chunk[0].open,
            high=round(max(b.high for b in chunk), 4),
            low=round(min(b.low for b in chunk), 4),
            close=chunk[-1].close,
            volume=round(sum(b.volume for b in chunk), 2),
        ))
    return aggregated


def stats() -> Dict[str, object]:
    """Ops counters for the health payload and the vault's observability."""
    with _cache_lock:
        return {"cached_symbols": len(_frame_cache), "last_error": _last_error or None}
