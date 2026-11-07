"""
The served quote must not expose a book nobody could trade against.

Alpaca's free feed is IEX only. Outside the primary session that venue's book
goes one-sided or absurdly wide (measured: SPY bid 712.63 / ask 764.24, a 7%
spread; NVDA and AAPL one-sided with a bid 4.8% under the last trade). Those
levels used to reach the client as if they were the market.
"""
from app.engine.alpaca_market_feed import AlpacaMarketFeedService
from app.models.trading import Quote


def _feed(stats=None):
    feed = AlpacaMarketFeedService.__new__(AlpacaMarketFeedService)
    feed.daily_stats = stats or {}
    return feed


def test_wide_book_is_withheld():
    feed = _feed({"SPY": {"last": 735.29, "day_high": 742.21, "day_low": 734.58}})
    raw = Quote(symbol="SPY", bid=712.63, ask=764.24, last=735.29)

    quote = feed._decorate_daily(raw)

    assert quote.book_available is False
    assert quote.bid == 0.0
    assert quote.ask == 0.0
    # The real print is untouched: it is the only trustworthy price here.
    assert quote.last == 735.29


def test_one_sided_book_is_withheld():
    feed = _feed({"NVDA": {"last": 186.64, "day_high": 188.0, "day_low": 181.0}})
    raw = Quote(symbol="NVDA", bid=177.61, ask=0.0, last=186.64)

    quote = feed._decorate_daily(raw)

    assert quote.book_available is False
    assert quote.bid == 0.0
    assert quote.ask == 0.0


def test_tight_two_sided_book_survives():
    feed = _feed({"SPY": {"last": 735.29, "day_high": 736.1, "day_low": 734.5}})
    raw = Quote(symbol="SPY", bid=735.27, ask=735.31, last=735.29)

    quote = feed._decorate_daily(raw)

    assert quote.book_available is True
    assert quote.bid == 735.27
    assert quote.ask == 735.31


def test_venue_volume_is_never_passed_off_as_session_volume():
    """IEX carried 80 shares in the quote; the consolidated volume is millions."""
    feed = _feed({"SPY": {"last": 735.29, "volume": 61_234_567}})
    raw = Quote(symbol="SPY", bid=0.0, ask=0.0, last=735.29, volume=80)

    quote = feed._decorate_daily(raw)

    assert quote.volume == 61_234_567


def test_venue_volume_is_dropped_when_no_consolidated_figure_exists():
    feed = _feed({"SPY": {"last": 735.29}})
    raw = Quote(symbol="SPY", bid=0.0, ask=0.0, last=735.29, volume=80)

    quote = feed._decorate_daily(raw)

    assert quote.volume == 0


def test_consolidated_print_carries_its_own_timestamp():
    """When `last` is swapped for the consolidated close, the age must describe
    that close -- not the venue quote it replaced."""
    from datetime import datetime, timezone

    close_ts = int(datetime(2026, 11, 6, 21, 0, tzinfo=timezone.utc).timestamp())
    feed = _feed({"SPY": {"last": 735.29, "last_ts": close_ts,
                          "day_high": 742.21, "day_low": 734.58}})
    raw = Quote(symbol="SPY", bid=0.0, ask=0.0, last=730.0,
                timestamp=datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc))

    quote = feed._decorate_daily(raw)

    assert quote.last == 735.29
    assert int(quote.timestamp.timestamp()) == close_ts


def test_meta_keeps_the_trade_time():
    from app.engine.alpaca_market_feed import AlpacaMarketFeedService

    entry = AlpacaMarketFeedService._daily_from_meta({
        "chartPreviousClose": 733.0,
        "regularMarketPrice": 735.29,
        "regularMarketTime": 1793998800,
    })
    assert entry["last_ts"] == 1793998800.0
