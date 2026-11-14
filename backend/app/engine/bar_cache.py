"""
Chart-bar cache with disk persistence, stale-while-revalidate and single-flight.

The in-memory TTL cache that used to live inside the feed had three costs that
showed up as user-visible latency (measured on the VPS, 2026-11-10):

* every deploy/restart started cold — a 1m chart paid 1.0-1.2s for ~2,400
  bars that had already been fetched minutes earlier;
* the moment a TTL expired the *next* request blocked on Yahoo, so a warm
  chart still took ~1.1s;
* N concurrent tabs for the same symbol each fetched it independently.

This cache keeps the bars on disk (they are historical data, they do not stop
being true), serves whatever it has immediately and refreshes in the
background, and lets only one caller fetch a given (symbol, timeframe) at a
time. Fetched data is never discarded while the process lives: a stale chart
is still far more useful than an empty one.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from app.core.timeframes import HIST_SEGMENT_TTL_SECONDS, MAX_HIST_SEGMENTS_PER_KEY
from app.models.trading import Bar

logger = logging.getLogger(__name__)

# Disk entries older than this are pruned: a week-old candle series is not a
# cache hit worth keeping. Runtime staleness is unbounded by design.
MAX_DISK_AGE_SECONDS = 30 * 24 * 3600
# Files kept on disk; the RAM cache is bounded separately. The deep-history
# pre-run writes ~110 segment files per (symbol, timeframe) — 14 US symbols
# x 2 timeframes ≈ 3,000 files (measured 2026-11-14: 857 files / 80MB at
# ~60% coverage). A 400 cap would let the next boot's prune delete exactly
# the oldest — i.e. the hardest-to-refetch deep archive pages.
MAX_DISK_FILES = 4000
# After a failed fetch, do not retry that key for this long. Without it an
# upstream outage turns every request (and every tab) into another attempt.
FAILURE_COOLDOWN_SECONDS = 30.0

# Separator for historical-segment subkeys: "SPY:15m#h1717200000". A "#"
# cannot appear in a real timeframe key, so fresh-window code paths that
# split on ":" still see the original timeframe as the second field.
HIST_SEGMENT_MARK = "#"

# Disk payload format version. Bumped whenever the *semantics* of a stored
# window change, so files written by the old code cannot survive a deploy as
# a "cached" answer written under different rules. Measured 2026-11-14: the
# 15m/5m fresh window moved from Yahoo's range=1mo (~31d, 599 bars) to the
# 59d epoch window (1115 bars), and every default symbol not prewarmed
# (MSFT/AMD/GOOGL/... ) served its old 31d file for days because stale-while-
# revalidate returned it before the first background refresh landed. v2 = the
# 59d fresh-window contract; a file without this marker is a v1 leftover and
# is ignored (re-fetched synchronously on first read).
DISK_FORMAT_VERSION = 2


@dataclass(frozen=True)
class CacheEntry:
    bars: List[Bar]
    fetched_at: float


class BarCache:
    """RAM + disk cache for ``(symbol, timeframe) -> bars``.

    ``bars`` and ``fetched_at`` are exposed with the names the feed has always
    used (``multi_tf_bars`` / ``_bars_cache_ts``) so callers and tests that
    touch them directly keep working.
    """

    def __init__(
        self,
        directory: Optional[str] = None,
        max_entries: int = 300,
        ttl_for: Optional[Callable[[str], float]] = None,
    ):
        self.bars: Dict[str, Dict[str, List[Bar]]] = {}
        self.fetched_at: Dict[str, Dict[str, float]] = {}
        self.max_entries = max_entries
        self._ttl_for = ttl_for or (lambda _tf: 60.0)
        self._order: "OrderedDict[str, None]" = OrderedDict()
        self._lock = threading.RLock()
        self._events: Dict[str, threading.Event] = {}
        self._cooldown_until: Dict[str, float] = {}

        self._directory: Optional[Path] = Path(directory) / "bars" if directory else None
        if self._directory is not None:
            try:
                self._directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("Bar cache dir %s unusable: %s", self._directory, exc)
                self._directory = None
        if self._directory is not None:
            self._prune_disk()

    # -- keys ------------------------------------------------------------
    @staticmethod
    def _key(symbol: str, timeframe: str) -> str:
        return f"{symbol.upper()}:{timeframe.lower()}"

    @staticmethod
    def hist_timeframe(timeframe: str, before: int) -> str:
        """Subkey namespace for a historical segment ending before ``before``.

        The segment lives in the same cache as the fresh window (same disk
        dir, same single-flight machinery) under an extended timeframe key,
        so no code path needs to know about segments beyond formatting.
        """
        return f"{timeframe.lower()}{HIST_SEGMENT_MARK}h{int(before)}"

    @staticmethod
    def is_hist_timeframe(timeframe: str) -> bool:
        return HIST_SEGMENT_MARK in timeframe

    def _path(self, symbol: str, timeframe: str) -> Optional[Path]:
        if self._directory is None:
            return None
        safe_sym = "".join(c for c in symbol.upper() if c.isalnum() or c in ".-_")
        # Historical subkeys carry "#h<epoch>"; a "#" must not reach a file
        # name, so a segment is written as {sym}_{tf}_h{epoch}.json — a name
        # no fresh-window file ever uses, which keeps rollbacks trivial.
        base_tf, before = timeframe.lower(), ""
        if self.is_hist_timeframe(timeframe):
            base_tf, _, before = timeframe.lower().partition(HIST_SEGMENT_MARK)
            base_tf = f"{base_tf}_{before}"
        safe_tf = "".join(c for c in base_tf if c.isalnum() or c == "_")
        return self._directory / f"{safe_sym}_{safe_tf}.json"

    # -- reads -----------------------------------------------------------
    def get(self, symbol: str, timeframe: str) -> Optional[CacheEntry]:
        """Cached bars for a key, falling back to the on-disk copy.

        A stored empty segment (a chart's terminal edge) is a real entry and
        is returned as such; ``None`` means "nothing was ever fetched", which
        is the only state that justifies an upstream round-trip.
        """
        key = self._key(symbol, timeframe)
        with self._lock:
            sym_map = self.bars.get(symbol.upper(), {})
            if timeframe.lower() in sym_map:
                bars = sym_map[timeframe.lower()]
                ts = self.fetched_at.get(symbol.upper(), {}).get(timeframe.lower(), 0.0)
                return CacheEntry(bars=bars, fetched_at=ts)
        return self._load_from_disk(symbol, timeframe)

    def is_fresh(self, entry: Optional[CacheEntry], timeframe: str) -> bool:
        if not entry or not entry.fetched_at:
            return False
        if self.is_hist_timeframe(timeframe):
            # History is immutable: a segment stays true for a week, whatever
            # the fresh-window cadence of its base timeframe is.
            return (time.time() - entry.fetched_at) < HIST_SEGMENT_TTL_SECONDS
        return (time.time() - entry.fetched_at) < self._ttl_for(timeframe)

    def age(self, entry: Optional[CacheEntry]) -> float:
        if not entry or not entry.fetched_at:
            return 0.0
        return max(0.0, time.time() - entry.fetched_at)

    def fetched_at_of(self, symbol: str, timeframe: str) -> float:
        with self._lock:
            return self.fetched_at.get(symbol.upper(), {}).get(timeframe.lower(), 0.0)

    # -- writes ----------------------------------------------------------
    def put(
        self,
        symbol: str,
        timeframe: str,
        bars: List[Bar],
        fetched_at: Optional[float] = None,
    ) -> None:
        sym = symbol.upper()
        tf = timeframe.lower()
        ts = time.time() if fetched_at is None else fetched_at
        with self._lock:
            self.bars.setdefault(sym, {})[tf] = bars
            self.fetched_at.setdefault(sym, {})[tf] = ts
            key = self._key(sym, tf)
            self._order[key] = None
            self._order.move_to_end(key)
            if self.is_hist_timeframe(tf):
                self._evict_oldest_segment_locked(sym, tf)
            self._evict_locked()
        self._write_to_disk(sym, tf, bars, ts)

    def _evict_oldest_segment_locked(self, sym: str, tf: str) -> None:
        """Cap historical segments per (symbol, base-timeframe) family.

        A chart pages back at most a few segments per session; beyond
        MAX_HIST_SEGMENTS_PER_KEY the oldest cursor is dropped from RAM (its
        disk copy remains and can be reloaded if the user really goes there
        again). Segments sort naturally by the epoch in the subkey.
        """
        base = tf.split(HIST_SEGMENT_MARK)[0]
        siblings = sorted(
            (t for t in self.bars.get(sym, {}) if t.split(HIST_SEGMENT_MARK)[0] == base and HIST_SEGMENT_MARK in t),
            key=lambda t: int(t.rsplit("h", 1)[1]),
        )
        for old_tf in siblings[:-MAX_HIST_SEGMENTS_PER_KEY] if len(siblings) > MAX_HIST_SEGMENTS_PER_KEY else []:
            self.bars.get(sym, {}).pop(old_tf, None)
            self.fetched_at.get(sym, {}).pop(old_tf, None)
            self._order.pop(self._key(sym, old_tf), None)

    def _evict_locked(self) -> None:
        while len(self._order) > self.max_entries:
            old_key, _ = self._order.popitem(last=False)
            old_sym, old_tf = old_key.split(":", 1)
            self.bars.get(old_sym, {}).pop(old_tf, None)
            self.fetched_at.get(old_sym, {}).pop(old_tf, None)
            if not self.bars.get(old_sym):
                self.bars.pop(old_sym, None)
                self.fetched_at.pop(old_sym, None)

    # -- single-flight ---------------------------------------------------
    def begin_refresh(self, symbol: str, timeframe: str) -> bool:
        """True when the caller becomes the one allowed to fetch this key."""
        key = self._key(symbol, timeframe)
        with self._lock:
            if key in self._events:
                return False
            self._events[key] = threading.Event()
            return True

    def end_refresh(self, symbol: str, timeframe: str) -> None:
        key = self._key(symbol, timeframe)
        with self._lock:
            event = self._events.pop(key, None)
        if event is not None:
            event.set()

    def wait_for_refresh(self, symbol: str, timeframe: str, timeout: float = 6.0) -> None:
        key = self._key(symbol, timeframe)
        with self._lock:
            event = self._events.get(key)
        if event is not None:
            event.wait(timeout)

    # -- failure cooldown ------------------------------------------------
    def cool_down(self, symbol: str, timeframe: str, seconds: float = FAILURE_COOLDOWN_SECONDS) -> None:
        with self._lock:
            self._cooldown_until[self._key(symbol, timeframe)] = time.time() + seconds

    def is_cooling(self, symbol: str, timeframe: str) -> bool:
        with self._lock:
            until = self._cooldown_until.get(self._key(symbol, timeframe), 0.0)
            return time.time() < until

    # -- disk ------------------------------------------------------------
    def _write_to_disk(self, symbol: str, timeframe: str, bars: List[Bar], ts: float) -> None:
        path = self._path(symbol, timeframe)
        if path is None:
            return
        payload = {
            # Format marker: see DISK_FORMAT_VERSION. Readers ignore files
            # written under an older contract instead of serving their stale
            # window shape after a deploy.
            "v": DISK_FORMAT_VERSION,
            "symbol": symbol,
            "timeframe": timeframe,
            "fetched_at": ts,
            # Arrays instead of objects: same data, roughly half the parse cost
            # on the boot path that reads these files.
            "bars": [[b.time, b.open, b.high, b.low, b.close, b.volume] for b in bars],
        }
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("Bar cache write failed at %s: %s", path, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _load_from_disk(self, symbol: str, timeframe: str) -> Optional[CacheEntry]:
        path = self._path(symbol, timeframe)
        if path is None or not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            # A file from an older format contract describes a window written
            # under different rules (e.g. the pre-59d 31d fresh window). It is
            # not a cache hit — ignore it so the first read re-fetches under
            # the current semantics. Set aside like a corrupt file so it is
            # not re-read on every request.
            if int(payload.get("v", 1)) != DISK_FORMAT_VERSION:
                logger.info(
                    "Bar cache file %s is format v%s (current v%s); ignored",
                    path, payload.get("v", 1), DISK_FORMAT_VERSION,
                )
                try:
                    path.replace(path.with_suffix(".oldformat"))
                except OSError:
                    pass
                return None
            ts = float(payload["fetched_at"])
            bars = [
                Bar(time=int(row[0]), open=float(row[1]), high=float(row[2]),
                    low=float(row[3]), close=float(row[4]), volume=float(row[5]))
                for row in payload["bars"]
            ]
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            # A half-written or hand-edited file must never break a chart; move
            # it aside so the same bad bytes are not re-read on every request.
            logger.warning("Bar cache file %s ignored: %s", path, exc)
            try:
                path.replace(path.with_suffix(".corrupt"))
            except OSError:
                pass
            return None
        if not bars:
            # A stored empty IS a real entry when it carries a fresh
            # timestamp: the writer recorded a terminal fact (the archive
            # floor, or a symbol younger than the window). Rejecting it here
            # would make every reboot re-hit upstream for that answer
            # (measured 2026-11-14: young symbols' floor pages were refetched
            # after every deploy) — and mid-outage that empty answer would
            # route through the 503 path instead of the cacheable 200.
            return CacheEntry(bars=[], fetched_at=ts)
        if time.time() - ts > MAX_DISK_AGE_SECONDS:
            return None
        with self._lock:
            # The disk read happened outside the lock, so a concurrent put()
            # may have stored a newer entry in RAM while we parsed. Promoting
            # unconditionally would regress fetched_at (and with it the ETag);
            # a RAM entry that is at least as new always wins.
            current_ts = self.fetched_at.get(symbol.upper(), {}).get(timeframe.lower(), 0.0)
            if current_ts >= ts and self.bars.get(symbol.upper(), {}).get(timeframe.lower()):
                return CacheEntry(
                    bars=self.bars[symbol.upper()][timeframe.lower()], fetched_at=current_ts
                )
            self.bars.setdefault(symbol.upper(), {})[timeframe.lower()] = bars
            self.fetched_at.setdefault(symbol.upper(), {})[timeframe.lower()] = ts
            key = self._key(symbol, timeframe)
            self._order[key] = None
            self._order.move_to_end(key)
            self._evict_locked()
        return CacheEntry(bars=bars, fetched_at=ts)

    def _prune_disk(self) -> None:
        if self._directory is None:
            return
        cutoff = time.time() - MAX_DISK_AGE_SECONDS
        try:
            files: List[Tuple[float, Path]] = []
            for path in self._directory.glob("*.json"):
                try:
                    files.append((path.stat().st_mtime, path))
                except OSError:
                    continue
            files.sort(reverse=True)
            for index, (mtime, path) in enumerate(files):
                if mtime < cutoff or index >= MAX_DISK_FILES:
                    path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Bar cache prune failed: %s", exc)
