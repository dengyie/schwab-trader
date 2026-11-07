"""Pure unit tests for the market registry, symbol resolution, session clocks
and per-market order rules. No network, no app import."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.markets import (
    MARKETS,
    is_market_open,
    local_date,
    market_clock,
    price_limit_pct,
    resolve_symbol,
    round_to_tick,
    tick_decimals,
    tick_size_for,
    validate_order,
)
from app.models.trading import OrderSide, Position


def utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


class TestResolveSymbol:
    def test_us_bare(self):
        ref = resolve_symbol("SPY")
        assert ref.symbol == "SPY"
        assert ref.market.code == "US"

    def test_us_lowercase(self):
        assert resolve_symbol("spy").symbol == "SPY"

    def test_hk_suffix_and_padding(self):
        assert resolve_symbol("0700.HK").symbol == "0700.HK"
        assert resolve_symbol("700.HK").symbol == "0700.HK"

    def test_jp_suffix(self):
        assert resolve_symbol("7203.T").symbol == "7203.T"
        assert resolve_symbol("7203.T").market.code == "JP"

    def test_cn_suffix_folding(self):
        assert resolve_symbol("600519.SS").symbol == "600519.SS"
        assert resolve_symbol("600519.SH").symbol == "600519.SS"
        assert resolve_symbol("000001.SZ").symbol == "000001.SZ"

    def test_bare_numeric_with_hint(self):
        assert resolve_symbol("0700", "HK").symbol == "0700.HK"
        assert resolve_symbol("600519", "CN").symbol == "600519.SS"
        assert resolve_symbol("000001", "CN").symbol == "000001.SZ"
        assert resolve_symbol("7203", "JP").symbol == "7203.T"

    def test_bare_numeric_needs_hint(self):
        assert resolve_symbol("600519") is None

    def test_rejects_handles(self):
        for bad in ("", "..", ".", "A" * 20, ".HK", "12.34.56", "../../etc", "0700.XX"):
            assert resolve_symbol(bad) is None, bad

    def test_supported_symbol(self):
        from app.core.markets import is_supported_symbol

        assert is_supported_symbol("0700.HK")
        assert not is_supported_symbol("../../etc")


class TestMarketClock:
    def test_us_pre_open(self):
        clock = market_clock(MARKETS.require("US"), utc("2026-11-06T14:29:00"))
        assert clock["session"] == "pre"
        assert clock["is_open"] is False

    def test_us_open_close_is_next_day(self):
        clock = market_clock(MARKETS.require("US"), utc("2026-11-06T15:00:00"))
        assert clock["is_open"] is True
        assert clock["next_close"].startswith("2026-11-06")
        assert clock["next_open"].startswith("2026-11-09")

    def test_us_closed_friday_rolls_to_monday(self):
        clock = market_clock(MARKETS.require("US"), utc("2026-11-06T21:00:00"))
        assert clock["is_open"] is False
        assert clock["next_open"].startswith("2026-11-09T")
        assert clock["next_open"] < clock["next_close"]

    def test_hk_lunch_break(self):
        clock = market_clock(MARKETS.require("HK"), utc("2026-11-06T04:00:00"))  # 12:00 HKT
        assert clock["session"] == "break"
        assert clock["is_open"] is False
        assert clock["next_open"].startswith("2026-11-06T13:00")

    def test_hk_afternoon_open(self):
        clock = market_clock(MARKETS.require("HK"), utc("2026-11-06T06:00:00"))  # 14:00 HKT
        assert clock["is_open"] is True

    def test_jp_lunch_break(self):
        clock = market_clock(MARKETS.require("JP"), utc("2026-11-06T03:00:00"))  # 12:00 JST
        assert clock["session"] == "break"

    def test_cn_afternoon_open(self):
        clock = market_clock(MARKETS.require("CN"), utc("2026-11-06T05:00:00"))  # 13:00 CST
        assert clock["is_open"] is True

    def test_weekend_closed(self):
        clock = market_clock(MARKETS.require("JP"), utc("2026-11-07T02:00:00"))  # Sat 11:00 JST
        assert clock["is_open"] is False
        assert clock["next_open"].startswith("2026-11-09")

    def test_next_open_never_in_past(self):
        for code in ("US", "HK", "JP", "CN"):
            for iso in (
                "2026-11-06T00:00:00",
                "2026-11-06T14:30:00",
                "2026-11-06T23:00:00",
                "2026-11-07T12:00:00",
                "2026-11-08T12:00:00",
                "2026-11-09T12:00:00",
            ):
                clock = market_clock(MARKETS.require(code), utc(iso))
                if clock["next_open"]:
                    assert datetime.fromisoformat(clock["next_open"]) > utc(iso)
                if clock["next_close"]:
                    assert datetime.fromisoformat(clock["next_close"]) > utc(iso)


class TestTicks:
    def test_us_ticks(self):
        us = MARKETS.require("US")
        assert tick_size_for(us, 150) == 0.01
        assert tick_size_for(us, 0.5) == 0.0001

    def test_hk_tiers(self):
        hk = MARKETS.require("HK")
        assert tick_size_for(hk, 0.1) == 0.001
        assert tick_size_for(hk, 3) == 0.01
        assert tick_size_for(hk, 419.8) == 0.2
        assert tick_size_for(hk, 4000) == 2.0

    def test_jp_tiers(self):
        jp = MARKETS.require("JP")
        assert tick_size_for(jp, 2500) == 1.0
        assert tick_size_for(jp, 3338) == 5.0
        assert tick_size_for(jp, 20000) == 10.0

    def test_cn_tick_flat(self):
        cn = MARKETS.require("CN")
        assert tick_size_for(cn, 1453) == 0.01

    @pytest.mark.parametrize(
        "code,price",
        [("US", 150.0), ("US", 0.5), ("HK", 419.8), ("JP", 3338.0), ("CN", 1453.0)],
    )
    def test_round_to_tick_is_stable(self, code, price):
        market = MARKETS.require(code)
        tick = tick_size_for(market, price)
        rounded = round_to_tick(price, tick)
        assert abs(rounded / tick - round(rounded / tick)) < 1e-9
        assert abs(rounded - price) <= tick + 1e-9

    def test_round_to_tick_never_zeroes_positive_price(self):
        jp = MARKETS.require("JP")
        # tick 1.0 for a 0.5 "price" would round to 0; it must round up to tick.
        assert round_to_tick(0.5, tick_size_for(jp, 0.5)) > 0


class TestOrderRules:
    def test_us_any_quantity(self):
        validate_order(MARKETS.require("US"), "SPY", "BUY", "LIMIT", 1, price=100.0)

    def test_cn_board_lot(self):
        with pytest.raises(ValueError, match="multiple of 100"):
            validate_order(MARKETS.require("CN"), "600519.SS", "BUY", "LIMIT", 150, price=1450.0)

    def test_cn_tick_alignment(self):
        with pytest.raises(ValueError, match="increments of 0.01"):
            validate_order(MARKETS.require("CN"), "600519.SS", "BUY", "LIMIT", 100, price=1450.005)

    def test_cn_price_band(self):
        with pytest.raises(ValueError, match="price limit"):
            validate_order(
                MARKETS.require("CN"), "600519.SS", "BUY", "LIMIT", 100,
                price=1700.0, prev_close=1450.0,
            )

    def test_cn_star_wide_band(self):
        validate_order(
            MARKETS.require("CN"), "688981.SS", "BUY", "LIMIT", 100,
            price=1550.0, prev_close=1300.0,
        )

    def test_hk_valid_order(self):
        validate_order(MARKETS.require("HK"), "0700.HK", "BUY", "LIMIT", 100, price=419.8)

    def test_cn_t1_blocks_same_day_sell(self):
        pos = Position(symbol="600519.SS", quantity=100, bought_on="2026-11-06")
        with pytest.raises(ValueError, match="T\\+1"):
            validate_order(
                MARKETS.require("CN"), "600519.SS", "SELL", "LIMIT", 100,
                price=1450.0, position=pos, now=utc("2026-11-06T05:00:00"),
            )

    def test_cn_t1_allows_previous_day_sell(self):
        pos = Position(symbol="600519.SS", quantity=100, bought_on="2026-11-05")
        validate_order(
            MARKETS.require("CN"), "600519.SS", "SELL", "LIMIT", 100,
            price=1450.0, position=pos, now=utc("2026-11-06T05:00:00"),
        )

    def test_stop_uses_stop_price_not_price(self):
        validate_order(MARKETS.require("US"), "SPY", "BUY", "STOP", 10, stop_price=200)

    def test_market_ok_without_price(self):
        validate_order(MARKETS.require("US"), "SPY", "BUY", "MARKET", 10)


class TestAux:
    def test_price_limit_pct(self):
        assert price_limit_pct(MARKETS.require("US"), "SPY") is None
        assert price_limit_pct(MARKETS.require("CN"), "600519.SS") == 10.0
        assert price_limit_pct(MARKETS.require("CN"), "688981.SS") == 20.0
        assert price_limit_pct(MARKETS.require("CN"), "300750.SZ") == 20.0

    def test_tick_decimals(self):
        assert tick_decimals(0.01) == 2
        assert tick_decimals(0.0001) == 4
        assert tick_decimals(0.2) == 1
        assert tick_decimals(2.0) == 0

    def test_local_date_shanghai(self):
        d = local_date(MARKETS.require("CN"), utc("2026-11-06T05:00:00"))
        assert d == "2026-11-06"

    def test_is_open_helper(self):
        assert is_market_open(MARKETS.require("US"), utc("2026-11-06T15:00:00"))
        assert not is_market_open(MARKETS.require("US"), utc("2026-11-07T15:00:00"))
