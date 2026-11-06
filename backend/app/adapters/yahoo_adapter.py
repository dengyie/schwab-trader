"""
Yahoo Finance Rapid Market Data Adapter.
Provides real-time multi-timeframe OHLCV historical bars
with zero authentication, high depth, and consolidated US equity volume.
"""
from __future__ import annotations
import logging
from datetime import datetime
from typing import List, Optional, Tuple, Dict
from zoneinfo import ZoneInfo
import httpx
from app.models.trading import Bar
from app.core.timeframes import TIMEFRAME_SECONDS, YAHOO_TIMEFRAME_MAP

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

    TIMEFRAME_MAP: Dict[str, Tuple[str, str]] = dict(YAHOO_TIMEFRAME_MAP)

    _shared_client: Optional[httpx.Client] = None

    @classmethod
    def get_client(cls) -> httpx.Client:
        if cls._shared_client is None or cls._shared_client.is_closed:
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

    @classmethod
    def fetch_bars(cls, symbol: str, timeframe: str = "15m", client: Optional[httpx.Client] = None) -> List[Bar]:
        sym = symbol.upper().replace("/", "-")
        tf_key = timeframe.lower()
        if tf_key not in TIMEFRAME_SECONDS:
            # Never silently substitute another interval for an unknown key.
            logger.warning("Unsupported timeframe %r requested for %s", timeframe, sym)
            return []
        interval, range_param = cls.TIMEFRAME_MAP[tf_key]

        url = f"{cls.BASE_URL}/{sym}"
        params = {
            "interval": interval,
            "range": range_param,
            "includePrePost": "true",
            "events": "div,splits"
        }

        try:
            cli = client or cls.get_client()
            resp = cli.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

            chart = data.get("chart", {})
            result_list = chart.get("result")
            if not result_list:
                return []

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
                        open=round(float(o), 2),
                        high=round(float(h), 2),
                        low=round(float(l), 2),
                        close=round(float(c), 2),
                        volume=float(v)
                    )
                )

            # If 4h requested, aggregate 1h bars into 4h bars aligned to time buckets
            if tf_key == "4h" and raw_bars:
                return cls._aggregate_bars(raw_bars, bucket_seconds=14400)

            return raw_bars
        except Exception as e:
            logger.warning("YahooFinanceAdapter fetch_bars error for %s (%s): %s", sym, timeframe, e)
            return []

    @staticmethod
    def _aggregate_bars(bars: List[Bar], bucket_seconds: int = 14400) -> List[Bar]:
        """
        Aggregates 1h bars into 4h bars aligned to the US trading session.

        Buckets are anchored to the first traded hour of each ET trading day
        instead of UTC midnight, so candles never merge two trading days and
        boundary bars line up with the 09:30 ET open.
        """
        if not bars:
            return []

        bars_per_bucket = max(1, int(bucket_seconds // 3600))

        buckets: Dict[str, List[Bar]] = {}
        seq_per_day: Dict[str, int] = {}
        for b in bars:
            dt = datetime.fromtimestamp(b.time, tz=ET)
            day_key = dt.strftime("%Y-%m-%d")
            seq = seq_per_day.get(day_key, 0)
            bucket_key = f"{day_key}/{seq // bars_per_bucket}"
            buckets.setdefault(bucket_key, []).append(b)
            seq_per_day[day_key] = seq + 1

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
