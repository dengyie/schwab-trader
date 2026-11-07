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
