"""
Tests for the agent-facing model context exporter.
"""
from app.adapters.model_exporter import ModelFeatureExporter
from app.models.trading import Bar, Quote


def _bars(closes):
    return [
        Bar(time=1_700_000_000 + i * 60, open=c, high=c, low=c, close=c, volume=100)
        for i, c in enumerate(closes)
    ]


def test_trend_is_neutral_on_empty_history():
    """No bars at all means no SMA: the trend must not claim BEARISH."""
    ctx = ModelFeatureExporter.format_agent_market_context("SPY", None, [])

    indicators = ctx["technical_indicators"]
    assert indicators["sma_20"] is None
    assert indicators["trend"] == "NEUTRAL"


def test_trend_reflects_close_against_sma():
    rising = _bars([100 + i for i in range(30)])
    bullish = ModelFeatureExporter.format_agent_market_context("SPY", None, rising)
    assert bullish["technical_indicators"]["trend"] == "BULLISH"

    falling = _bars([100 - i for i in range(30)])
    bearish = ModelFeatureExporter.format_agent_market_context("SPY", None, falling)
    assert bearish["technical_indicators"]["trend"] == "BEARISH"


def test_quote_block_is_reported_verbatim():
    quote = Quote(symbol="SPY", bid=500.10, ask=500.14, last=500.12, volume=1000)
    ctx = ModelFeatureExporter.format_agent_market_context("SPY", quote, _bars([500] * 30))

    rt = ctx["real_time_quote"]
    assert rt["bid"] == 500.10
    assert rt["ask"] == 500.14
    assert rt["last"] == 500.12
    assert rt["spread"] == round(500.14 - 500.10, 4)
