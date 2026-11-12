"""Yahoo intraday volume reconciliation against the daily tape.

Regression tests for the measured 2026-11-12 defect: QQQ's 15m/1h endpoints
returned ~18x the real session volume (374M summed vs a 21M consolidated
total), contaminating the chart's 日量 header through 15m/1h and the 4h view
aggregated from 1h. The 1d endpoint and daily_stats (regularMarketVolume)
carry the real figure.

Day keys are computed from ``now`` because the service-side authority maps
today's consolidated total onto the current venue-local date; the helper
itself is date-agnostic.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.engine.alpaca_market_feed import (
    INTRADAY_TIMEFRAMES,
    reconcile_intraday_volume,
)
from app.models.trading import Bar

ET = ZoneInfo("America/New_York")

# Measured prod evidence, 2026-11-12: QQQ traded ~21M shares consolidated.
QQQ_DAILY = 21_000_000.0

TODAY = datetime.now(ET).date()
DAY0 = str(TODAY - timedelta(days=1))  # the honest session before today's
DAY1 = str(TODAY)                      # the inflated session


def et_ts(hour, minute, offset_days=0):
    """Venue-local wall clock → epoch, on the day offset_days back."""
    dt = datetime.now(ET).replace(hour=hour, minute=minute, second=0,
                                  microsecond=0) - timedelta(days=offset_days)
    return int(dt.timestamp())


def bar(t, o, h, l, c, v):
    return Bar(time=t, open=o, high=h, low=l, close=c, volume=v)


def make_inflated_day():
    """QQQ's 15m bars, volumes rounded from prod evidence.

    Real shape: a morning burst then heavy late prints (the late prints are
    what sum to ~18x the consolidated total).
    """
    bars = [
        bar(et_ts(9, 30, 0), 680.0, 681.0, 679.9, 681.0, 5_947_000),
        bar(et_ts(9, 45, 0), 681.0, 681.9, 680.5, 681.5, 1_970_000),
        bar(et_ts(10, 0, 0), 681.5, 682.4, 681.0, 682.0, 1_830_000),
        bar(et_ts(10, 15, 0), 682.0, 683.9, 681.9, 683.5, 11_920_000),
        bar(et_ts(10, 30, 0), 683.5, 684.4, 683.0, 684.0, 52_530_000),
        bar(et_ts(10, 45, 0), 684.0, 684.9, 683.9, 684.5, 14_660_000),
        bar(et_ts(11, 0, 0), 684.5, 685.4, 684.0, 685.0, 65_140_000),
        bar(et_ts(11, 15, 0), 685.0, 686.1, 684.9, 685.5, 67_810_000),
        bar(et_ts(11, 30, 0), 685.5, 686.0, 685.2, 686.0, 17_940_000),
    ]
    # A prior, honest day keeps the multi-day window honest.
    prior = [bar(et_ts(h, 30, 1), 685.0, 686.0, 684.0, 685.5, 2_000_000)
             for h in (9, 10, 11)]
    return prior + bars


def test_inflated_day_is_scaled_proportionally():
    bars = make_inflated_day()
    out = reconcile_intraday_volume(
        bars, {DAY1: QQQ_DAILY, DAY0: 8_000_000}, "America/New_York"
    )

    # The honest prior day (6M < 2x 8M) is untouched.
    assert sum(b.volume for b in out[:3]) == 6_000_000
    # The inflated day sums to the authoritative total again (per-bar
    # rounding drift is bounded by half a unit per bar).
    day_sum = sum(b.volume for b in out[3:])
    assert abs(day_sum - QQQ_DAILY) <= len(out[3:]) / 2
    # The intraday shape survives: the 11:15 print stays the biggest bar.
    assert out[3 + 7].volume == max(b.volume for b in out[3:])


def test_mild_elevated_day_is_untouched():
    # SPY measured 1.3x: real tape drift is not rewritten on a guess.
    bars = make_inflated_day()
    honest = [Bar(time=b.time, open=b.open, high=b.high, low=b.low,
                  close=b.close, volume=b.volume / 18.0)
              for b in bars[3:]]
    out = reconcile_intraday_volume(
        honest, {DAY1: QQQ_DAILY}, "America/New_York"
    )
    assert [b.volume for b in out] == [b.volume for b in honest]


def test_under_reporting_day_is_untouched():
    bars = make_inflated_day()[3:6]
    out = reconcile_intraday_volume(
        bars, {DAY1: QQQ_DAILY}, "America/New_York"
    )
    assert out == bars


def test_day_without_authority_is_passed_through():
    bars = make_inflated_day()[3:6]
    out = reconcile_intraday_volume(bars, {}, "America/New_York")
    assert out == bars


def test_empty_bars_is_identity():
    assert reconcile_intraday_volume([], {DAY1: QQQ_DAILY},
                                     "America/New_York") == []


def test_aggregated_4h_day_is_scaled():
    # The 4h view is built from 1h bars (measured: one session 4h bar carrying
    # 352.4M), so the day's inflated sum lands in a single fat bar.
    fourh = [bar(et_ts(9, 30, 0), 680.0, 686.0, 679.9, 686.0, 352_400_000)]
    out = reconcile_intraday_volume(
        fourh, {DAY1: QQQ_DAILY}, "America/New_York"
    )
    assert out[0].volume == QQQ_DAILY


def test_bad_timezone_is_identity():
    bars = make_inflated_day()
    out = reconcile_intraday_volume(
        bars, {DAY1: QQQ_DAILY}, "Not/A_Zone"
    )
    assert out == bars


def test_intraday_timeframe_set():
    assert INTRADAY_TIMEFRAMES == {"1m", "5m", "15m", "1h", "4h"}
