"""
Yahoo Finance Rapid Market Data Adapter.
Provides real-time multi-timeframe OHLCV historical bars
with zero authentication, high depth, and consolidated US equity volume.
"""
from __future__ import annotations
import logging
import threading
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple, Dict
from zoneinfo import ZoneInfo
import httpx
from app.models.trading import Bar
from app.core.timeframes import (
    FRESH_WINDOW_SPAN_SECONDS,
    TIMEFRAME_SECONDS,
    YAHOO_TIMEFRAME_MAP,
)

logger = logging.getLogger(__name__)

# US market session for intraday bucket alignment (09:30 ET open).
ET = ZoneInfo("America/New_York")


class YahooFinanceAdapter:
    """
    Rapid market data provider utilizing Yahoo Finance Chart API.
    Acts as a high-speed multi-timeframe historical bar provider and fallback.
    """

    BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json"
    }

    TIMEFRAME_MAP: Dict[str, Tuple[str, Optional[str]]] = dict(YAHOO_TIMEFRAME_MAP)

    _shared_client: Optional[httpx.Client] = None

    @classmethod
    def get_client(cls) -> httpx.Client:
        # Reuse the pooled client unless it is gone or was closed. ``is_closed``
        # only exists on the real object, so anything else cached here (a test
        # double that leaked in, for instance) is discarded rather than
        # trusted -- a stale non-client would break every later fetch.
        client = cls._shared_client
        if client is None or not isinstance(client, httpx.Client) or client.is_closed:
            cls._shared_client = httpx.Client(
                headers=cls.HEADERS,
                timeout=httpx.Timeout(connect=2.5, read=3.5, write=2.5, pool=5.0),
                limits=httpx.Limits(max_keepalive_connections=15, max_connections=30),
                http2=False
            )
        return cls._shared_client

    @classmethod
    def close_client(cls) -> None:
        if cls._shared_client is not None and not cls._shared_client.is_closed:
            try:
                cls._shared_client.close()
            except Exception:
                pass
            cls._shared_client = None

    # -- IP throttling observability ---------------------------------------
    # Yahoo has no official rate-limit contract: it throttles per IP, and a
    # 429 disguises itself as "possibly delisted" / an Edge error inside an
    # HTTP 200 (yfinance #2284/#2297/#2721). The feed's cache discipline keeps
    # request volume far below the community thresholds, but when the VPS
    # exit IP does get throttled the whole intl feed goes quiet at once, so
    # the signal must be countable and visible, not just a log line.
    # fetch_bars runs on several feed thread pools at once, so the counter
    # is guarded: a torn increment would under-report exactly during the
    # storm this counter exists to observe.
    _rate_limit = {"count": 0, "consecutive": 0, "last_ts": 0.0}
    _rate_limit_lock = threading.Lock()

    @classmethod
    def record_rate_limit(cls, detail: str) -> None:
        """Count one throttled upstream response, real or disguised."""
        with cls._rate_limit_lock:
            stats = cls._rate_limit
            stats["count"] += 1
            stats["consecutive"] += 1
            stats["last_ts"] = time.time()
        logger.warning("Yahoo rate limit signal (%s): %s", stats["count"], detail)

    @classmethod
    def record_success(cls) -> None:
        with cls._rate_limit_lock:
            cls._rate_limit["consecutive"] = 0
        with cls._http_errors_lock:
            cls._http_errors["consecutive"] = 0

    @classmethod
    def rate_limit_stats(cls) -> Dict[str, object]:
        with cls._rate_limit_lock:
            return dict(cls._rate_limit)

    # -- HTTP failure observability ----------------------------------------
    # The fresh-window spans (FRESH_WINDOW_SPAN_SECONDS) are a *measured*
    # upstream contract, not a documented one: the day Yahoo tightens the
    # 59d ceiling, every 5m/15m fresh fetch starts 422-ing and those charts
    # go blank — quietly, since a failed fetch only leaves a log line. This
    # counter (consecutive, so a one-off blip reads as noise) makes that
    # drift visible in health the same way the 429 counter makes throttling
    # visible. HTTP-level failures only: an empty-but-200 answer is a data
    # question (delisting, coverage), not a contract break.
    _http_errors = {"count": 0, "consecutive": 0, "last_ts": 0.0}
    _http_errors_lock = threading.Lock()

    @classmethod
    def record_http_error(cls, status: int, detail: str) -> None:
        with cls._http_errors_lock:
            stats = cls._http_errors
            stats["count"] += 1
            stats["consecutive"] += 1
            stats["last_ts"] = time.time()
        logger.warning("Yahoo HTTP %s (%s): %s", status, stats["consecutive"], detail)

    @classmethod
    def http_error_stats(cls) -> Dict[str, object]:
        with cls._http_errors_lock:
            return dict(cls._http_errors)

    @staticmethod
    def _looks_rate_limited(error: Dict[str, object]) -> bool:
        """A chart.error payload that is the 429 disguise, not a bad symbol.

        Yahoo serves HTTP 200 with ``chart.error`` when throttled; the tell is
        the Edge/Too-Many-Requests wording (the same text yfinance's crumb
        path surfaces). A genuinely delisted symbol yields "Not Found" etc.
        """
        code = str(error.get("code", ""))
        description = str(error.get("description", ""))
        text = f"{code} {description}".lower()
        return "too many requests" in text or "edge" in text

    @classmethod
    def fetch_meta(
        cls, symbol: str, client: Optional[httpx.Client] = None
    ) -> Optional[Dict[str, object]]:
        """Latest quote metadata for any Yahoo symbol.

        Returns the chart ``meta`` block (price, previous close, day range,
        volume, currency, exchange timezone) or ``None`` when the symbol is
        unknown or the provider is unreachable. No price is ever invented.
        """
        sym = symbol.upper().replace("/", "-")
        try:
            cli = client or cls.get_client()
            resp = cli.get(
                f"{cls.BASE_URL}/{sym}",
                params={"interval": "1m", "range": "1d", "includePrePost": "false"},
            )
            resp.raise_for_status()
            payload = resp.json()
            results = payload.get("chart", {}).get("result") or []
            if not results:
                # HTTP 200 but no result: either a dead symbol or the 429
                # disguise — chart.error tells them apart.
                error = payload.get("chart", {}).get("error") or {}
                if cls._looks_rate_limited(error):
                    cls.record_rate_limit(f"meta {sym}: {error.get('code')}: {error.get('description')}")
                else:
                    cls.record_success()
                return None
            meta = results[0].get("meta") or {}
            cls.record_success()
            return meta if meta else None
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                cls.record_rate_limit(f"meta {sym}: HTTP 429")
            logger.warning("YahooFinanceAdapter fetch_meta error for %s: %s", sym, exc)
            return None
        except Exception as exc:
            logger.warning("YahooFinanceAdapter fetch_meta error for %s: %s", sym, exc)
            return None

    @classmethod
    def fetch_bars(
        cls,
        symbol: str,
        timeframe: str = "15m",
        client: Optional[httpx.Client] = None,
        tz: Optional[ZoneInfo] = None,
        period1: Optional[int] = None,
        period2: Optional[int] = None,
    ) -> List[Bar]:
        sym = symbol.upper().replace("/", "-")
        tf_key = timeframe.lower()
        if tf_key not in TIMEFRAME_SECONDS:
            # Never silently substitute another interval for an unknown key.
            logger.warning("Unsupported timeframe %r requested for %s", timeframe, sym)
            return []
        interval, range_param = cls.TIMEFRAME_MAP[tf_key]

        url = f"{cls.BASE_URL}/{sym}"
        params: Dict[str, object] = {
            "interval": interval,
            # Pre/post hours are thin, unrepresentative prints. Measured on SPY
            # 15m they carried lows of 691.53 and highs of 781.05 against a
            # 735.29 close -- a +-6.5% range that no session ever traded. The
            # chart and its stats are about the session, so only it is fetched.
            "includePrePost": "false",
            "events": "div,splits"
        }
        if range_param is None:
            # Whole listed history. Yahoo coarsens the interval when asked for
            # range=max (measured 2026-11-12: interval=1mo + range=max returns
            # 4 bars/year — quarterly buckets mislabeled as monthly — while an
            # explicit epoch window keeps true monthly), so whole-history keys
            # pass explicit bounds instead.
            params["period1"] = 0
            params["period2"] = int(time.time())
        elif tf_key in FRESH_WINDOW_SPAN_SECONDS:
            # The range enum undershoots this timeframe's real coverage
            # (15m/5m: "1mo" ≈ 31d vs a measured 59d ceiling, "3mo" 422s), so
            # the fresh window is an explicit epoch window spanning the whole
            # available history — one request, no truncated seed to page back.
            span = FRESH_WINDOW_SPAN_SECONDS[tf_key]
            params["period1"] = int(time.time()) - span
            params["period2"] = int(time.time())
        else:
            params["range"] = range_param

        try:
            cli = client or cls.get_client()
            if period1 is not None:
                # Historical-segment fetch: an explicit epoch window instead of
                # a trailing "range". Yahoo returns bars with time < period2,
                # so the caller pages with period2 = the oldest bar it has.
                # Without period1 the behavior above is byte-identical to the
                # pre-paging adapter.
                params.pop("range", None)
                params["period1"] = int(period1)
                params["period2"] = int(period2 if period2 is not None else int(time.time()))
            resp = cli.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

            chart = data.get("chart", {})
            result_list = chart.get("result")
            if not result_list:
                # HTTP 200 but no result: a dead symbol — or the 429
                # disguise, which chart.error distinguishes.
                error = chart.get("error") or {}
                if cls._looks_rate_limited(error):
                    cls.record_rate_limit(f"bars {sym} {timeframe}: {error.get('code')}: {error.get('description')}")
                else:
                    cls.record_success()
                return []
            cls.record_success()

            res = result_list[0]
            timestamps = res.get("timestamp", [])
            indicators = res.get("indicators", {})
            quotes = indicators.get("quote", [{}])[0]

            opens = quotes.get("open", [])
            highs = quotes.get("high", [])
            lows = quotes.get("low", [])
            closes = quotes.get("close", [])
            volumes = quotes.get("volume", [])

            raw_bars: List[Bar] = []
            for i in range(len(timestamps)):
                t = timestamps[i]
                o = opens[i] if i < len(opens) else None
                h = highs[i] if i < len(highs) else None
                l = lows[i] if i < len(lows) else None
                c = closes[i] if i < len(closes) else None
                v = volumes[i] if (i < len(volumes) and volumes[i] is not None) else 0

                if None in (t, o, h, l, c):
                    continue

                raw_bars.append(
                    Bar(
                        time=int(t),
                        # 4 decimals, matching the defeatbeta source and the
                        # sub-penny US tick (0.0001) + HK 0.001 tick: rounding
                        # Yahoo to 2 places silently mangled sub-dollar and
                        # low-priced HK names (2026-11-23 review P3).
                        open=round(float(o), 4),
                        high=round(float(h), 4),
                        low=round(float(l), 4),
                        close=round(float(c), 4),
                        volume=float(v)
                    )
                )

            raw_bars = cls._drop_forming_tail(raw_bars, interval)

            # If 4h requested, aggregate 1h bars into 4h bars aligned to the
            # exchange's own session rather than to UTC midnight.
            if tf_key == "4h" and raw_bars:
                return cls._aggregate_bars(
                    raw_bars, bucket_seconds=14400, tz=tz or ET,
                    session_open_seconds=cls._market_open_seconds(tz or ET),
                )

            return raw_bars
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else 0
            # The explicit throttle response, before it blends into the
            # generic warning below.
            if status == 429:
                cls.record_rate_limit(f"bars {sym} {timeframe}: HTTP 429")
            elif status:
                cls.record_http_error(status, f"bars {sym} {timeframe}")
            logger.warning("YahooFinanceAdapter fetch_bars error for %s (%s): %s", sym, timeframe, e)
            return []
        except Exception as e:
            logger.warning("YahooFinanceAdapter fetch_bars error for %s (%s): %s", sym, timeframe, e)
            return []

    @staticmethod
    def _drop_forming_tail(bars: List[Bar], interval: str) -> List[Bar]:
        """Drop a trailing bar that is not an interval of real trading.

        Yahoo marks the still-forming candle with the *wall clock* instead of
        its bucket start (measured: SPY 15m bars every 900s, then one at
        19:59:52 ET with open == high == low == close). Published as data that
        bar stretches the x-axis past the close and its flat price drags the
        change calculation, so it is dropped. The guard is deliberately narrow:
        only a bar that both lands off the interval grid and has no range is
        removed, so a genuinely traded final bar of an irregular session stays.

        Grid alignment is judged relative to the previous bar, not by an
        absolute modulo. Daily bars are anchored to the session open (a US
        13:30 UTC open gives ``time % 86400 == 52200``) and US/CN/HK hourly
        buckets start at :30, so an absolute test would classify every genuine
        bar as off-grid and silently drop the flat last day of a suspended
        stock.
        """
        if len(bars) < 2:
            return bars
        step = TIMEFRAME_SECONDS.get(interval)
        if not step:
            return bars

        last = bars[-1]
        # Weekly/monthly spacing (7/30 nominal days vs real calendar weeks and
        # months, plus DST-shifted weeks) never sits on the naive modulo grid,
        # so the grid test degenerates to "always off-grid" and a genuine flat
        # month (suspension) got deleted with it (2026-11-23 review). For
        # these buckets the precise forming test is temporal: the tail is
        # forming only while wall-clock time is still inside its bucket.
        if interval in ("1wk", "1mo"):
            now_ts = int(datetime.now(tz=timezone.utc).timestamp())
            forming = last.time + step > now_ts
            is_flat = (
                last.open == last.high == last.low == last.close
            )
            if forming and is_flat:
                return bars[:-1]
            return bars
        if (last.time - bars[-2].time) % step == 0:
            return bars
        if last.open != last.high or last.open != last.low or last.open != last.close:
            return bars

        return bars[:-1]

    @staticmethod
    def _market_open_seconds(tz: Optional[ZoneInfo]) -> Optional[int]:
        """Local-midnight offset of the session open for the market whose
        timezone matches ``tz``; None when no catalog market matches."""
        try:
            from app.core.markets import MARKETS

            zone = str(tz)
            for market in MARKETS.all():
                if market.timezone == zone and market.sessions:
                    first = market.sessions[0].start
                    return first.hour * 3600 + first.minute * 60 + first.second
        except Exception:
            return None
        return None

    @staticmethod
    def _aggregate_bars(
        bars: List[Bar], bucket_seconds: int = 14400, tz: Optional[ZoneInfo] = None,
        session_open_seconds: Optional[int] = None,
    ) -> List[Bar]:
        """
        Aggregates 1h bars into 4h bars aligned to the exchange's trading day.

        Buckets are anchored to each bar's local wall clock relative to the
        session open (when the market is known), not to a running per-day
        sequence: Yahoo nulls out individual hourly prints routinely, and the
        old sequence-based bucketing shifted EVERY later bucket of the day
        after one dropped bar — 10:30 missing turned bucket0 into a 4.5-hour
        candle (2026-11-23 review). A bar that prints just before the open
        (auction edges) clamps into the first bucket. Without a known market
        the day's own sequence is kept as a fallback.
        """
        if not bars:
            return []

        local_tz = tz or ET
        bars_per_bucket = max(1, int(bucket_seconds // 3600))

        buckets: Dict[str, List[Bar]] = {}
        seq_per_day: Dict[str, int] = {}
        for b in bars:
            dt = datetime.fromtimestamp(b.time, tz=local_tz)
            day_key = dt.strftime("%Y-%m-%d")
            if session_open_seconds is not None:
                secs = dt.hour * 3600 + dt.minute * 60 + dt.second
                bucket_idx = max(0, (secs - session_open_seconds) // bucket_seconds)
                bucket_key = f"{day_key}/{bucket_idx}"
            else:
                seq = seq_per_day.get(day_key, 0)
                bucket_key = f"{day_key}/{seq // bars_per_bucket}"
            buckets.setdefault(bucket_key, []).append(b)
            seq_per_day[day_key] = seq_per_day.get(day_key, 0) + 1

        aggregated: List[Bar] = []
        for chunk in buckets.values():
            chunk.sort(key=lambda b: b.time)
            aggregated.append(
                Bar(
                    time=chunk[0].time,
                    open=chunk[0].open,
                    high=round(max(b.high for b in chunk), 2),
                    low=round(min(b.low for b in chunk), 2),
                    close=chunk[-1].close,
                    volume=round(sum(b.volume for b in chunk), 2)
                )
            )
        aggregated.sort(key=lambda b: b.time)
        return aggregated
