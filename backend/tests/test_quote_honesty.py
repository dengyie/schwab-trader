"""
Quote honesty: a published price must be a real print, never a book estimate.

Regression cover for two defects that put prices on screen which never traded:

* the Alpaca IEX quote's bid/ask midpoint was published as ``last`` (SPY showed
  738.435 while the consolidated close was 735.29);
* the Yahoo feed synthesized a bid/ask around its last price, so a trade-only
  venue looked like it had a book.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.adapters.alpaca_adapter import AlpacaDataAdapter
from app.adapters.schwab_adapter import SchwabDataAdapter


def test_stream_quote_does_not_invent_a_price():
    msg = {
        "T": "q",
        "S": "SPY",
        "bp": 753.47,
        "bs": 40,
        "ap": 753.51,
        "as": 80,
        "t": "2026-11-02T19:48:19.218325Z",
    }
    quote = AlpacaDataAdapter.parse_stream_quote(msg)
    assert quote is not None
    # A quote is an intent to trade, not a trade.
    assert quote.last == 0.0
    assert quote.bid == 753.47
    assert quote.ask == 753.51


def test_stream_quote_keeps_the_known_print():
    msg = {"T": "q", "S": "SPY", "bp": 753.47, "ap": 753.51}
    quote = AlpacaDataAdapter.parse_stream_quote(msg, last_price=753.50)
    assert quote is not None
    assert quote.last == 753.50


def test_rest_quote_does_not_invent_a_price():
    payload = {"quote": {"bp": 753.47, "ap": 753.51, "t": "2026-11-02T19:48:19Z"}}
    quote = AlpacaDataAdapter.parse_rest_quote_response("SPY", payload)
    assert quote is not None
    assert quote.last == 0.0


def test_schwab_quote_does_not_invent_a_book():
    quote = SchwabDataAdapter.parse_streamer_level_one_equity(
        {"key": "SPY", "1": 753.47, "2": 753.51}
    )
    assert quote is not None
    assert quote.last == 0.0
    assert quote.bid == 753.47
    assert quote.ask == 753.51


def test_schwab_quote_without_any_price_is_dropped():
    assert SchwabDataAdapter.parse_streamer_level_one_equity({"key": "SPY"}) is None


class TestTradeableQuoteGate:
    def test_two_sided_tight_book_is_tradeable(self):
        assert AlpacaDataAdapter.is_tradeable_quote(753.47, 753.51) is True

    def test_one_sided_book_is_not_tradeable(self):
        assert AlpacaDataAdapter.is_tradeable_quote(753.47, 0.0) is False
        assert AlpacaDataAdapter.is_tradeable_quote(0.0, 753.51) is False

    def test_crossed_book_is_not_tradeable(self):
        assert AlpacaDataAdapter.is_tradeable_quote(753.51, 753.47) is False

    def test_absurdly_wide_book_is_not_tradeable(self):
        # IEX after hours: ~7% wide. A midpoint there is not a market.
        assert AlpacaDataAdapter.is_tradeable_quote(700.0, 750.0) is False


class TestYahooLastOnlyQuote:
    """A trade-only venue carries a print and no book."""

    def _meta(self, price=402.5, trade_time=None):
        return {
            "regularMarketPrice": price,
            "chartPreviousClose": 400.0,
            "regularMarketDayHigh": 405.0,
            "regularMarketDayLow": 399.0,
            "regularMarketVolume": 1000,
            "regularMarketTime": trade_time,
        }

    def test_quote_is_last_only_and_stamped(self, monkeypatch):
        from app.engine.yahoo_feed import YahooQuoteFeed
        from app.adapters.yahoo_adapter import YahooFinanceAdapter

        trade_time = int((datetime.now(timezone.utc) - timedelta(seconds=5)).timestamp())
        monkeypatch.setattr(
            YahooFinanceAdapter, "fetch_meta", classmethod(lambda cls, sym: self._meta(402.5, trade_time))
        )

        feed = YahooQuoteFeed(symbols=["0700.HK"], enabled=True)
        quote = feed.get_latest_quote("0700.HK")

        assert quote is not None
        assert quote.last == 402.5
        # No book was invented around the print.
        assert quote.bid == 0.0
        assert quote.ask == 0.0
        # Freshness is measured from the print's own time.
        assert quote.age_seconds is not None
        assert quote.age_seconds >= 5.0
        assert quote.source == "yahoo"

    def test_mid_price_falls_back_to_the_print(self, monkeypatch):
        from app.engine.yahoo_feed import YahooQuoteFeed
        from app.adapters.yahoo_adapter import YahooFinanceAdapter

        monkeypatch.setattr(
            YahooFinanceAdapter, "fetch_meta", classmethod(lambda cls, sym: self._meta(402.5))
        )
        feed = YahooQuoteFeed(symbols=["0700.HK"], enabled=True)
        quote = feed.get_latest_quote("0700.HK")
        assert quote is not None
        assert quote.mid_price == 402.5
