from app.adapters.yahoo_adapter import YahooFinanceAdapter
from app.models.trading import Bar


def bar(t, o, h, l, c, v=0):
    return Bar(time=t, open=o, high=h, low=l, close=c, volume=v)


def test_drop_forming_tail():
    bars = [
        bar(1793997900, 735.19, 736.10, 734.90, 735.80, 10),
        bar(1794013192, 736.53, 736.53, 736.53, 736.53),
    ]
    out = YahooFinanceAdapter._drop_forming_tail(bars, "15m")
    assert [b.time for b in out] == [1793997900]


def test_keep_flat_on_grid():
    bars = [
        bar(1793997900, 735.19, 736.10, 734.90, 735.80, 10),
        bar(1793998800, 735.00, 735.00, 735.00, 735.00),
    ]
    out = YahooFinanceAdapter._drop_forming_tail(bars, "15m")
    assert [b.time for b in out] == [1793997900, 1793998800]


def test_keep_traded_off_grid():
    bars = [
        bar(1793997900, 735.19, 736.10, 734.90, 735.80, 10),
        bar(1794013192, 735.50, 736.60, 735.20, 736.10, 5),
    ]
    out = YahooFinanceAdapter._drop_forming_tail(bars, "15m")
    assert len(out) == 2


def test_single_bar_untouched():
    bars = [bar(1794013192, 736.53, 736.53, 736.53, 736.53)]
    assert YahooFinanceAdapter._drop_forming_tail(bars, "15m") == bars


def test_keep_suspended_flat_daily_bar():
    # Daily bars are anchored to the session open (mod 86400 != 0), and a
    # suspended stock prints a flat day. One session step from the previous
    # bar means it is a real candle, not a forming tail.
    bars = [
        bar(1793889000, 765.47, 765.97, 732.71, 733.00, 5_000_000),
        bar(1793975400, 733.00, 733.00, 733.00, 733.00),
    ]
    out = YahooFinanceAdapter._drop_forming_tail(bars, "1d")
    assert [b.time for b in out] == [1793889000, 1793975400]


def test_drop_weekend_forming_daily_bar():
    # Fetched on a weekend: the forming daily bar carries the wall clock, two
    # days plus a remainder after Friday's session-open anchor.
    bars = [
        bar(1793889000, 765.47, 765.97, 732.71, 733.00, 5_000_000),
        bar(1793889000 + 2 * 86400 + 43_000, 733.00, 733.00, 733.00, 733.00),
    ]
    out = YahooFinanceAdapter._drop_forming_tail(bars, "1d")
    assert [b.time for b in out] == [1793889000]


def test_keep_flat_final_hour_bar():
    # US hourly buckets start at :30, so a genuine bar sits off the 3600 grid.
    # Its spacing from the previous hour is still exactly one step.
    bars = [
        bar(1793993400, 735.85, 736.27, 735.31, 735.45, 400_000),
        bar(1793997000, 736.00, 736.00, 736.00, 736.00),
    ]
    out = YahooFinanceAdapter._drop_forming_tail(bars, "1h")
    assert [b.time for b in out] == [1793993400, 1793997000]
