"""
Schwab Data Protocol Adapter.
Parses Schwab Streamer WebSocket payloads and REST historical bar JSON into normalized domain models.
"""
from __future__ import annotations
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from app.models.trading import Quote, Bar, utc_now


class SchwabDataAdapter:
    @staticmethod
    def parse_streamer_level_one_equity(item: Dict[str, Any]) -> Optional[Quote]:
        """
        Parses Level 1 equity quote from Schwab Streamer payload.
        Typical Schwab Streamer fields:
        - "key": ticker symbol
        - "1": Bid price
        - "2": Ask price
        - "3": Last price
        - "8": Total volume
        - "50": Quote Time (ms)
        Or named JSON fields when using high-level SDKs: "bid", "ask", "last", "volume".
        """
        if not isinstance(item, dict):
            return None

        symbol = item.get("key") or item.get("symbol")
        if not symbol:
            return None

        # Handle both raw numeric index schema and decoded named schema
        bid = float(item.get("1") or item.get("bid") or item.get("bidPrice") or 0.0)
        ask = float(item.get("2") or item.get("ask") or item.get("askPrice") or 0.0)
        last = float(item.get("3") or item.get("last") or item.get("lastPrice") or item.get("regularMarketPrice") or 0.0)
        volume = int(item.get("8") or item.get("volume") or item.get("totalVolume") or 0)

        # A missing last stays unknown (0). A bid/ask midpoint is not a trade,
        # and a synthesized bid/ask around it would be mistaken for a market.
        if last <= 0.0 and bid <= 0.0 and ask <= 0.0:
            return None

        raw_time = item.get("50") or item.get("quoteTime") or item.get("timestamp")
        if raw_time:
            try:
                # If epoch milliseconds
                if isinstance(raw_time, (int, float)) and raw_time > 1e11:
                    ts = datetime.fromtimestamp(raw_time / 1000.0, timezone.utc)
                elif isinstance(raw_time, (int, float)):
                    ts = datetime.fromtimestamp(raw_time, timezone.utc)
                else:
                    ts = utc_now()
            except Exception:
                ts = utc_now()
        else:
            ts = utc_now()

        return Quote(
            symbol=str(symbol).upper(),
            bid=round(bid, 4),
            ask=round(ask, 4),
            last=round(last, 4),
            volume=volume,
            timestamp=ts
        )

    @staticmethod
    def parse_rest_price_history(response_json: Dict[str, Any]) -> List[Bar]:
        """
        Parses Schwab /marketdata/v1/pricehistory response.
        Expected format:
        {
            "candles": [
                { "open": 182.1, "high": 183.0, "low": 181.9, "close": 182.5, "volume": 120300, "datetime": 1700000000000 }
            ],
            "empty": false
        }
        """
        candles = response_json.get("candles") or []
        bars: List[Bar] = []

        for c in candles:
            raw_dt = c.get("datetime", 0)
            # Schwab datetime is in milliseconds; TradingView expects seconds
            time_sec = int(raw_dt / 1000.0) if raw_dt > 1e11 else int(raw_dt)
            bars.append(Bar(
                time=time_sec,
                open=round(float(c.get("open", 0.0)), 4),
                high=round(float(c.get("high", 0.0)), 4),
                low=round(float(c.get("low", 0.0)), 4),
                close=round(float(c.get("close", 0.0)), 4),
                volume=float(c.get("volume", 0.0))
            ))

        bars.sort(key=lambda b: b.time)
        return bars
