"""
Unified Model Feature Exporter.
Transforms market bars and quotes into standard formats ready for:
1. Pandas / NumPy / Polars DataFrames (Time-series ML)
2. PyTorch / Tensor matrices (Deep Learning models)
3. Structured JSON / Text prompt contexts (LLM / AI Agent Tool-Calling)
"""
from __future__ import annotations
from typing import List, Dict, Any, Optional
import math
from app.models.trading import Bar, Quote, Position


class ModelFeatureExporter:
    @staticmethod
    def to_records(bars: List[Bar]) -> List[Dict[str, Any]]:
        """Directly loads into pd.DataFrame(records)."""
        return [
            {
                "time": b.time,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume
            }
            for b in bars
        ]

    @staticmethod
    def extract_features(bars: List[Bar]) -> Dict[str, List[float]]:
        """
        Calculates core technical factors without heavyweight external C-dependencies:
        - closes (raw)
        - sma_20 (simple mean of the trailing 20 closes)
        - rsi_14 (Cutler variant: simple means, NOT Wilder-smoothed — consumers
          comparing against Wilder RSI should expect small deviations)
        - returns (log returns)
        """
        if not bars:
            return {}

        closes = [b.close for b in bars]
        n = len(closes)

        # 1. Simple Moving Average 20
        sma_20: List[float] = []
        for i in range(n):
            if i < 19:
                sma_20.append(round(sum(closes[:i+1]) / (i+1), 4))
            else:
                sma_20.append(round(sum(closes[i-19:i+1]) / 20.0, 4))

        # 2. RSI 14
        rsi_14: List[float] = []
        gains = [0.0]
        losses = [0.0]
        for i in range(1, n):
            delta = closes[i] - closes[i-1]
            gains.append(max(0.0, delta))
            losses.append(max(0.0, -delta))

        for i in range(n):
            if i < 14:
                rsi_14.append(50.0)
            else:
                avg_gain = sum(gains[i-13:i+1]) / 14.0
                avg_loss = sum(losses[i-13:i+1]) / 14.0
                if avg_loss == 0:
                    rsi_14.append(100.0)
                else:
                    rs = avg_gain / avg_loss
                    rsi = 100.0 - (100.0 / (1.0 + rs))
                    rsi_14.append(round(rsi, 2))

        # 3. Log returns
        returns: List[float] = [0.0]
        for i in range(1, n):
            if closes[i-1] > 0:
                ret = math.log(closes[i] / closes[i-1])
                returns.append(round(ret, 6))
            else:
                returns.append(0.0)

        return {
            "closes": closes,
            "sma_20": sma_20,
            "rsi_14": rsi_14,
            "returns": returns
        }

    @staticmethod
    def format_agent_market_context(
        symbol: str,
        quote: Optional[Quote],
        recent_bars: List[Bar],
        position: Optional[Position] = None
    ) -> Dict[str, Any]:
        """
        Generates a clean, rich structured observation dictionary for LLM Agent prompt input.
        """
        features = ModelFeatureExporter.extract_features(recent_bars[-30:]) if recent_bars else {}
        # last == 0 is the adapter's "unknown" sentinel (no IEX print yet):
        # publishing 0.0 as a price made the Agent read "SPY trades at $0"
        # (2026-11-23 review). Fall through to the bar close, else None.
        latest_close = (
            recent_bars[-1].close if recent_bars
            else (quote.last if quote and quote.last > 0 else None)
        )
        latest_rsi = features.get("rsi_14", [-1])[-1] if features.get("rsi_14") else None
        latest_sma = features.get("sma_20", [-1])[-1] if features.get("sma_20") else None

        return {
            "symbol": symbol.upper(),
            "real_time_quote": {
                "bid": quote.bid if quote else None,
                "ask": quote.ask if quote else None,
                "last": quote.last if quote and quote.last > 0 else latest_close,
                # No two-sided book means no observable spread. Reporting
                # 0 - 0 = 0 would read as a perfectly tight market.
                "spread": (
                    round(quote.ask - quote.bid, 4)
                    if quote and quote.bid > 0 and quote.ask > 0
                    else None
                ),
                "book_available": bool(quote.book_available) if quote else False,
                "timestamp": quote.timestamp.isoformat() if quote else None
            },
            "technical_indicators": {
                "latest_close": latest_close,
                "rsi_14": latest_rsi,
                "sma_20": latest_sma,
                "trend": (
                    "BULLISH"
                    if (latest_sma and latest_close and latest_close > latest_sma)
                    else "BEARISH" if (latest_sma and latest_close and latest_close < latest_sma) else "NEUTRAL"
                )
            },
            "current_position": {
                "shares": position.quantity if position else 0,
                "avg_cost": position.avg_cost if position else 0.0,
                "unrealized_pnl": position.unrealized_pnl if position else 0.0,
                "unrealized_pnl_pct": position.unrealized_pnl_pct if position else 0.0
            } if position and position.quantity > 0 else None
        }
