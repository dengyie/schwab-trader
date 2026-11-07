"""Multi-market execution and multi-currency accounting.

These tests exercise the virtual engine end-to-end: board lots, tick sizes,
price bands, T+1, FX conversion into the USD account, and the broker router
that keeps US and non-US books in one view.
"""
from __future__ import annotations

import pytest

from app.core.markets import MARKETS
from app.engine.fx_service import FxService
from app.engine.market_router import MarketRouterBroker
from app.engine.matching_engine import VirtualMatchingEngine
from app.engine.portfolio_ledger import PortfolioLedger
from app.models.trading import (
    OrderSide,
    OrderStatus,
    OrderType,
    Quote,
    Trade,
)


class FixedFx:
    """Deterministic FX stub: 1 unit of currency = ``usd`` USD."""

    def __init__(self, rates=None):
        self.rates = {"USD": 1.0, "HKD": 0.128, "JPY": 0.0064, "CNY": 0.15}
        if rates:
            self.rates.update(rates)

    def usd_per_unit(self, currency: str) -> float:
        return self.rates.get((currency or "USD").upper(), 1.0)

    def all_rates(self) -> dict:
        return dict(self.rates)


@pytest.fixture
def fx():
    return FixedFx()


@pytest.fixture
def engine(fx):
    ledger = PortfolioLedger(initial_capital=100000.0, fx=fx)
    return VirtualMatchingEngine(
        ledger=ledger,
        default_slippage_bps=0.0,
        default_commission_per_order=0.0,
        fx=fx,
        enforce_sessions=False,
    )


def quote(symbol, last, prev_close=None, bid=None, ask=None):
    from app.core.markets import market_of

    market = market_of(symbol)
    return Quote(
        symbol=symbol,
        bid=bid if bid is not None else last,
        ask=ask if ask is not None else last,
        last=last,
        market=market.code,
        currency=market.currency,
        prev_close=prev_close,
    )


class TestMultiCurrency:
    def test_hk_buy_debits_usd_at_fx(self, engine, fx):
        engine.on_quote(quote("0700.HK", 400.0, prev_close=395.0))
        order = engine.submit_order(
            "0700.HK", OrderSide.BUY, OrderType.LIMIT, 100, price=420.0
        )
        assert order.status == OrderStatus.FILLED
        assert order.market == "HK"
        assert order.currency == "HKD"

        portfolio = engine.get_portfolio()
        # 100 * 400 HKD at 0.128 = 5120 USD
        assert portfolio.cash == pytest.approx(100000.0 - 5120.0, abs=1e-6)
        pos = portfolio.positions["0700.HK"]
        assert pos.currency == "HKD"
        assert pos.quantity == 100
        assert pos.avg_cost == pytest.approx(400.0)
        assert pos.cost_basis_usd == pytest.approx(5120.0)
        assert pos.market_value_usd(fx.usd_per_unit("HKD")) == pytest.approx(5120.0)

    def test_jp_round_lot(self, engine):
        engine.on_quote(quote("7203.T", 3000.0, prev_close=2990.0))
        order = engine.submit_order(
            "7203.T", OrderSide.BUY, OrderType.LIMIT, 100, price=3100.0
        )
        assert order.status == OrderStatus.FILLED
        assert order.quantity == 100

    def test_jp_fractional_lot_rejected(self, engine):
        engine.on_quote(quote("7203.T", 3000.0, prev_close=2990.0))
        with pytest.raises(ValueError, match="multiple of 100"):
            engine.submit_order("7203.T", OrderSide.BUY, OrderType.LIMIT, 50, price=3100.0)

    def test_cn_price_band_rejected(self, engine):
        engine.on_quote(quote("600519.SS", 1450.0, prev_close=1450.0))
        with pytest.raises(ValueError, match="price limit"):
            engine.submit_order("600519.SS", OrderSide.BUY, OrderType.LIMIT, 100, price=1700.0)

    def test_cn_t1_sell_blocked(self, engine):
        engine.on_quote(quote("600519.SS", 1450.0, prev_close=1450.0))
        engine.submit_order("600519.SS", OrderSide.BUY, OrderType.LIMIT, 100, price=1460.0)
        with pytest.raises(ValueError, match="T\\+1"):
            engine.submit_order("600519.SS", OrderSide.SELL, OrderType.LIMIT, 100, price=1500.0)

    def test_us_sale_proceeds_feed_foreign_purchase(self, engine):
        engine.on_quote(quote("SPY", 500.0, prev_close=500.0))
        engine.submit_order("SPY", OrderSide.BUY, OrderType.LIMIT, 10, price=500.0)
        engine.on_quote(quote("0700.HK", 400.0, prev_close=400.0))
        engine.submit_order("0700.HK", OrderSide.BUY, OrderType.LIMIT, 100, price=400.0)
        portfolio = engine.get_portfolio()
        assert set(portfolio.positions) == {"SPY", "0700.HK"}
        # Both positions are valued in USD for the account total.
        assert portfolio.market_value == pytest.approx(5000.0 + 5120.0, abs=1e-6)
        assert portfolio.total_equity == pytest.approx(
            portfolio.cash + 10120.0, abs=1e-6
        )

    def test_realized_pnl_is_usd(self, engine, fx):
        engine.on_quote(quote("0700.HK", 400.0, prev_close=400.0))
        engine.submit_order("0700.HK", OrderSide.BUY, OrderType.LIMIT, 100, price=400.0)
        engine.on_quote(quote("0700.HK", 420.0))
        engine.submit_order("0700.HK", OrderSide.SELL, OrderType.LIMIT, 100, price=420.0)
        portfolio = engine.get_portfolio()
        # 20 HKD/share * 100 = 2000 HKD * 0.128 = 256 USD
        assert portfolio.realized_pnl == pytest.approx(256.0, abs=1e-6)
        assert portfolio.positions["0700.HK"].quantity == 0
        assert portfolio.positions["0700.HK"].realized_pnl_usd == pytest.approx(256.0)

    def test_insufficient_buying_power_rejects(self, fx):
        ledger = PortfolioLedger(initial_capital=100.0, fx=fx)
        engine = VirtualMatchingEngine(ledger=ledger, fx=fx, enforce_sessions=False)
        engine.on_quote(quote("0700.HK", 400.0, prev_close=400.0))
        order = engine.submit_order("0700.HK", OrderSide.BUY, OrderType.LIMIT, 100, price=400.0)
        assert order.status == OrderStatus.REJECTED
        assert "buying power" in (order.reject_reason or "")


class TestSessionGuard:
    def test_non_us_market_order_blocked_when_closed(self, fx):
        ledger = PortfolioLedger(initial_capital=100000.0, fx=fx)
        engine = VirtualMatchingEngine(
            ledger=ledger, fx=fx, enforce_sessions_for=frozenset({"HK", "JP", "CN"})
        )
        engine.on_quote(quote("0700.HK", 400.0, prev_close=400.0))
        # A market order depends on a live session; a limit order does not.
        with pytest.raises(ValueError, match="market is closed"):
            # 2026-11-07 is a Saturday in every supported timezone
            from app.core.markets import is_market_open

            if is_market_open(MARKETS.require("HK")):
                pytest.skip("HK market is open right now")
            engine.submit_order("0700.HK", OrderSide.BUY, OrderType.MARKET, 100)

    def test_us_market_order_not_guarded(self, fx):
        ledger = PortfolioLedger(initial_capital=100000.0, fx=fx)
        engine = VirtualMatchingEngine(
            ledger=ledger, fx=fx, enforce_sessions_for=frozenset({"HK", "JP", "CN"})
        )
        engine.on_quote(quote("SPY", 500.0, prev_close=500.0))
        order = engine.submit_order("SPY", OrderSide.BUY, OrderType.MARKET, 1)
        assert order.status == OrderStatus.FILLED


class TestRouter:
    def _build(self, fx):
        us_ledger = PortfolioLedger(initial_capital=50000.0, fx=fx)
        us = VirtualMatchingEngine(ledger=us_ledger, fx=fx, enforce_sessions=False)
        intl = VirtualMatchingEngine(
            ledger=PortfolioLedger(initial_capital=50000.0, fx=fx),
            fx=fx,
            enforce_sessions=False,
        )
        return MarketRouterBroker(us, intl), us, intl

    def test_routes_us_to_us_book(self, fx):
        router, us, intl = self._build(fx)
        router.on_quote(quote("SPY", 500.0))
        order = router.submit_order("SPY", OrderSide.BUY, OrderType.LIMIT, 1, price=500.0)
        assert order.status == OrderStatus.FILLED
        assert len(us.get_orders()) == 1
        assert len(intl.get_orders()) == 0

    def test_routes_hk_to_intl_book(self, fx):
        router, us, intl = self._build(fx)
        router.on_quote(quote("0700.HK", 400.0, prev_close=400.0))
        order = router.submit_order("0700.HK", OrderSide.BUY, OrderType.LIMIT, 100, price=400.0)
        assert order.status == OrderStatus.FILLED
        assert len(intl.get_orders()) == 1
        assert len(us.get_orders()) == 0

    def test_portfolio_merges_both_books(self, fx):
        router, us, intl = self._build(fx)
        router.on_quote(quote("SPY", 500.0))
        router.submit_order("SPY", OrderSide.BUY, OrderType.LIMIT, 2, price=500.0)
        router.on_quote(quote("0700.HK", 400.0, prev_close=400.0))
        router.submit_order("0700.HK", OrderSide.BUY, OrderType.LIMIT, 100, price=400.0)

        merged = router.get_portfolio()
        assert set(merged.positions) == {"SPY", "0700.HK"}
        assert merged.cash == pytest.approx(100000.0 - 1000.0 - 5120.0, abs=1e-6)

    def test_cancel_searches_both_books(self, fx):
        router, us, intl = self._build(fx)
        router.on_quote(quote("0700.HK", 400.0, prev_close=400.0))
        order = router.submit_order(
            "0700.HK", OrderSide.BUY, OrderType.LIMIT, 100, price=300.0
        )
        assert order.status == OrderStatus.PENDING
        cancelled = router.cancel_order(order.order_id)
        assert cancelled is not None
        assert cancelled.status == OrderStatus.CANCELLED

    def test_clock_per_market(self, fx):
        router, _, _ = self._build(fx)
        hk = router.get_market_clock("HK")
        assert hk["market"] == "HK"
        assert hk["currency"] == "HKD"
        cn = router.get_market_clock("CN")
        assert cn["market"] == "CN"
        assert router.get_market_clock("XX") is None


class TestFxService:
    def test_fallback_rates_without_network(self):
        service = FxService(enabled=False)
        assert service.usd_per_unit("USD") == 1.0
        # HKD fallback 7.83 per USD
        assert service.usd_per_unit("HKD") == pytest.approx(1 / 7.83, rel=1e-6)
        rates = service.all_rates()
        assert set(rates) == {"USD", "HKD", "JPY", "CNY"}
        for value in rates.values():
            assert 0 < value <= 1.0

    def test_unknown_currency_is_identity(self):
        service = FxService(enabled=False)
        assert service.usd_per_unit("XXX") == 1.0


class TestSnapshotParser:
    def test_parse_snapshots_response(self):
        from app.adapters.alpaca_adapter import AlpacaDataAdapter

        payload = {
            "SPY": {
                "prevDailyBar": {"c": 733.43},
                "dailyBar": {"o": 734.0, "h": 735.5, "l": 733.0, "c": 735.4, "v": 50000000},
                "latestTrade": {"p": 735.4},
            },
            "UNKNOWN_SYM": {"error": "invalid symbol"},
        }
        stats = AlpacaDataAdapter.parse_snapshots_response(payload)
        assert stats["SPY"]["prev_close"] == 733.43
        assert stats["SPY"]["day_open"] == 734.0
        assert stats["SPY"]["day_high"] == 735.5
        assert stats["SPY"]["day_low"] == 733.0
        assert stats["SPY"]["volume"] == 50000000
        assert "UNKNOWN_SYM" not in stats

    def test_parse_snapshots_response_skips_garbage(self):
        from app.adapters.alpaca_adapter import AlpacaDataAdapter

        assert AlpacaDataAdapter.parse_snapshots_response(None) == {}
        assert AlpacaDataAdapter.parse_snapshots_response({"X": None}) == {}
        assert AlpacaDataAdapter.parse_snapshots_response("not a dict") == {}


class TestDailyStatsWarmup:
    """Regression: a cold watchlist used to populate only the first row.

    The per-symbol path passed a single-symbol list through a TTL gate, so the
    first cold symbol refreshed and every later one bailed out on the fresh
    timestamp -- production showed SPY with a real change and the other 13 rows
    at 0.0%.
    """

    def test_one_batched_call_covers_the_whole_catalog(self, monkeypatch):
        from app.engine.alpaca_market_feed import AlpacaMarketFeedService

        feed = AlpacaMarketFeedService.__new__(AlpacaMarketFeedService)
        feed.daily_stats = {}
        feed._snapshot_ts = 0.0
        feed._snapshot_lock = __import__("threading").Lock()
        feed.symbols = {"SPY", "QQQ", "AAPL"}
        calls = []

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, params=None):
                calls.append(params["symbols"])
                body = {
                    sym: {"prevDailyBar": {"c": 100.0}, "dailyBar": {"o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 1000}}
                    for sym in params["symbols"].split(",")
                }

                class R:
                    status_code = 200

                    @staticmethod
                    def json():
                        return body

                return R()

        monkeypatch.setattr("app.engine.alpaca_market_feed.httpx.Client", FakeClient)
        feed.headers = {}
        feed.data_base_url = "https://example.invalid"
        feed.feed = "iex"

        feed.ensure_daily_stats(["SPY", "QQQ", "AAPL"])
        assert len(calls) == 1
        assert set(feed.daily_stats) == {"SPY", "QQQ", "AAPL"}
        for sym in ("SPY", "QQQ", "AAPL"):
            assert feed.get_daily_stats(sym)["prev_close"] == 100.0

        # Inside the TTL the warm-up is free.
        feed.ensure_daily_stats(["SPY", "QQQ", "AAPL"])
        assert len(calls) == 1

    def test_get_daily_stats_never_hits_the_network(self, monkeypatch):
        from app.engine.alpaca_market_feed import AlpacaMarketFeedService

        feed = AlpacaMarketFeedService.__new__(AlpacaMarketFeedService)
        feed.daily_stats = {}
        feed._snapshot_ts = 0.0
        feed._snapshot_lock = __import__("threading").Lock()

        def boom(*a, **k):
            raise AssertionError("get_daily_stats must not fetch")

        monkeypatch.setattr("app.engine.alpaca_market_feed.httpx.Client", boom)
        assert feed.get_daily_stats("SPY") == {}


class TestDayPriceGuard:
    """Regression: a stale bid below the session low drove both the price and
    the day change. Production showed AAPL 328.06 with a 338.59-345.765 range."""

    def _quote(self, last):
        from app.models.trading import Quote

        return Quote(symbol="AAPL", bid=last, ask=0.0, last=last)

    def test_stale_price_outside_range_is_rejected(self):
        from app.api.routes import _pick_day_price

        daily = {"prev_close": 344.6, "last": 342.41, "day_high": 345.765, "day_low": 338.59}
        price = _pick_day_price(self._quote(328.06), daily, [], 338.59, 345.765)
        assert price == 342.41

    def test_in_range_quote_is_preferred_over_snapshot(self):
        from app.api.routes import _pick_day_price

        daily = {"prev_close": 344.6, "last": 342.41, "day_high": 345.765, "day_low": 338.59}
        assert _pick_day_price(self._quote(343.9), daily, [], 338.59, 345.765) == 343.9

    def test_without_range_the_quote_is_used(self):
        from app.api.routes import _pick_day_price

        assert _pick_day_price(self._quote(100.0), {}, [], None, None) == 100.0

    def test_every_price_outside_range_yields_none(self):
        from app.api.routes import _pick_day_price

        daily = {"last": 1.0, "day_high": 345.765, "day_low": 338.59}
        assert _pick_day_price(self._quote(328.06), daily, [], 338.59, 345.765) is None


class TestPositionMarkGuard:
    """Regression: the tickers endpoint rejected the stale 328.06 print but the
    position mark did not, so the portfolio still showed AAPL 6 @ 328.06 while
    the watchlist showed 342.41 for the same symbol."""

    def _ledger_with_long(self):
        from app.engine.portfolio_ledger import PortfolioLedger
        from app.models.trading import Position

        ledger = PortfolioLedger(initial_capital=100_000.0)
        ledger.portfolio.positions["AAPL"] = Position(
            symbol="AAPL", quantity=6, avg_cost=337.46, current_price=342.41
        )
        return ledger

    def _quote(self, last, low=338.59, high=345.765):
        from app.models.trading import Quote

        return Quote(symbol="AAPL", bid=last, ask=0.0, last=last, day_low=low, day_high=high)

    def test_out_of_range_quote_does_not_move_the_mark(self):
        ledger = self._ledger_with_long()
        ledger.update_quote(self._quote(328.06))
        assert ledger.portfolio.positions["AAPL"].current_price == 342.41

    def test_in_range_quote_moves_the_mark(self):
        ledger = self._ledger_with_long()
        ledger.update_quote(self._quote(343.90))
        assert ledger.portfolio.positions["AAPL"].current_price == 343.90

    def test_quote_without_range_still_marks(self):
        ledger = self._ledger_with_long()
        ledger.update_quote(self._quote(328.06, low=None, high=None))
        assert ledger.portfolio.positions["AAPL"].current_price == 328.06

    def test_feed_stamps_the_cached_daily_range_onto_quotes(self):
        """The guard is only useful if the quote actually carries the range."""
        from app.engine.alpaca_market_feed import AlpacaMarketFeedService
        from app.models.trading import Quote

        feed = AlpacaMarketFeedService.__new__(AlpacaMarketFeedService)
        feed.daily_stats = {
            "AAPL": {"prev_close": 344.6, "day_high": 345.765, "day_low": 338.59}
        }
        raw = Quote(symbol="AAPL", bid=328.06, ask=0.0, last=328.06)

        decorated = feed._decorate_daily(raw)

        assert decorated.day_low == 338.59
        assert decorated.day_high == 345.765
        assert decorated.prev_close == 344.6
        # The raw object is not mutated: callers keep owning their cache entry.
        assert raw.day_low is None

    def test_feed_heals_an_out_of_range_last_from_the_snapshot_trade(self):
        """An out-of-range last must be replaced by the snapshot's latest trade
        so portfolio marks converge with the watchlist instead of freezing."""
        from app.engine.alpaca_market_feed import AlpacaMarketFeedService
        from app.models.trading import Quote

        feed = AlpacaMarketFeedService.__new__(AlpacaMarketFeedService)
        feed.daily_stats = {
            "AAPL": {
                "last": 342.41,
                "day_high": 345.765,
                "day_low": 338.59,
            }
        }
        raw = Quote(symbol="AAPL", bid=328.06, ask=0.0, last=328.06)

        decorated = feed._decorate_daily(raw)

        assert decorated.last == 342.41

    def test_feed_keeps_an_in_range_last_untouched(self):
        from app.engine.alpaca_market_feed import AlpacaMarketFeedService
        from app.models.trading import Quote

        feed = AlpacaMarketFeedService.__new__(AlpacaMarketFeedService)
        feed.daily_stats = {
            "AAPL": {
                "last": 300.0,
                "day_high": 345.765,
                "day_low": 338.59,
            }
        }
        raw = Quote(symbol="AAPL", bid=343.90, ask=0.0, last=343.90)

        decorated = feed._decorate_daily(raw)

        assert decorated.last == 343.90

    def test_feed_does_not_heal_from_a_snapshot_trade_outside_the_range(self):
        """If even the snapshot's last contradicts the range, leave the quote
        alone — the downstream guard will reject it rather than invent a price."""
        from app.engine.alpaca_market_feed import AlpacaMarketFeedService
        from app.models.trading import Quote

        feed = AlpacaMarketFeedService.__new__(AlpacaMarketFeedService)
        feed.daily_stats = {
            "AAPL": {
                "last": 328.06,
                "day_high": 345.765,
                "day_low": 338.59,
            }
        }
        raw = Quote(symbol="AAPL", bid=328.06, ask=0.0, last=328.06)

        decorated = feed._decorate_daily(raw)

        assert decorated.last == 328.06
        assert decorated.day_high == 345.765
