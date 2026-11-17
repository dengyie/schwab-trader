"""
Multi-market domain rules: the single source of truth for which markets the
terminal supports, how their symbols are written, when they are open, and how
an order must be shaped before it is allowed to reach an execution engine.

Everything here is pure and deterministic (no I/O, no clock injection beyond an
optional ``now``), so the rules are unit-testable and cannot drift between the
REST API, the websocket handler and the matching engine.
"""
from __future__ import annotations

import functools
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Dict, Optional, Iterable, Tuple
from zoneinfo import ZoneInfo

# Weekday numbering follows datetime.weekday(): Monday == 0.
WEEKDAYS_MON_FRI = (0, 1, 2, 3, 4)

# A last price may sit a hair outside the session range, but a bid-only or very
# stale quote can land far outside it. Anything beyond this tolerance is not a
# tradeable price for the session, so it must never become a mark or a row price.
DAY_RANGE_TOLERANCE = 1.002


def within_day_range(price: Optional[float], low: Optional[float], high: Optional[float]) -> bool:
    """True when ``price`` is plausibly inside today's low/high range.

    Missing prices or missing range return True: this guard only rejects data
    that positively contradicts itself, it never invents a rejection.
    """
    if not price or price <= 0 or not low or not high or low <= 0 or high <= 0:
        return True
    lo = min(low, high) / DAY_RANGE_TOLERANCE
    hi = max(low, high) * DAY_RANGE_TOLERANCE
    return lo <= price <= hi


# A print older than this while its venue is open means no fresh print is
# arriving; the UI must show that instead of painting the old price as live.
STALE_AFTER_SECONDS = 120.0


def freshness_updates(
    timestamp: datetime,
    market: "MarketDef",
    now: Optional[datetime] = None,
) -> dict:
    """``age_seconds`` / ``is_stale`` for a quote, computed in one place.

    Staleness is only meaningful while the venue is open: a closed market's
    last print is supposed to be old, and flagging it would make a correct
    close look like a broken feed.
    """
    moment = now or datetime.now(timezone.utc)
    age = max((moment - timestamp).total_seconds(), 0.0)
    return {
        "age_seconds": round(age, 1),
        "is_stale": bool(age > STALE_AFTER_SECONDS and is_market_open(market, moment)),
    }



@dataclass(frozen=True)
class Session:
    """A contiguous trading window in the exchange's local time."""

    start: dt_time
    end: dt_time


@dataclass(frozen=True)
class MarketDef:
    code: str                 # canonical market code: US / HK / JP / CN
    name: str                 # English display name
    currency: str             # ISO 4217 settlement currency
    timezone: str             # IANA exchange timezone
    sessions: tuple[Session, ...]
    lot_size: int             # board lot / minimum tradable increment
    settlement: str           # "T+0", "T+1", ...
    suffix: str               # Yahoo-style symbol suffix ("" for US)
    exchange: str             # primary listing venue
    price_limit_pct: Optional[float] = None
    catalog: tuple[str, ...] = ()
    # Decimal places used for money display of this currency.
    decimals: int = 2


class MarketRegistry:
    """Ordered registry of the supported markets."""

    def __init__(self, markets: Iterable[MarketDef]):
        self._markets: dict[str, MarketDef] = {m.code: m for m in markets}

    def codes(self) -> list[str]:
        return list(self._markets)

    def all(self) -> list[MarketDef]:
        return list(self._markets.values())

    def get(self, code: Optional[str]) -> Optional[MarketDef]:
        if not code:
            return None
        return self._markets.get(code.strip().upper())

    def require(self, code: Optional[str]) -> MarketDef:
        market = self.get(code)
        if market is None:
            raise ValueError(f"Unknown market: {code!r}")
        return market


MARKETS = MarketRegistry([
    MarketDef(
        code="US",
        name="United States",
        currency="USD",
        timezone="America/New_York",
        sessions=(Session(dt_time(9, 30), dt_time(16, 0)),),
        lot_size=1,
        settlement="T+0",
        suffix="",
        exchange="NYSE / NASDAQ",
        catalog=(
            "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA",
            "AMD", "AMZN", "GOOGL", "META", "IWM", "COIN", "PLTR", "NFLX",
        ),
    ),
    MarketDef(
        code="HK",
        name="Hong Kong",
        currency="HKD",
        timezone="Asia/Hong_Kong",
        sessions=(
            Session(dt_time(9, 30), dt_time(12, 0)),
            Session(dt_time(13, 0), dt_time(16, 0)),
        ),
        lot_size=100,
        settlement="T+0",
        suffix=".HK",
        exchange="HKEX",
        catalog=(
            "0700.HK", "9988.HK", "3690.HK", "1810.HK", "0941.HK", "0005.HK",
            "0388.HK", "1299.HK", "2318.HK", "9618.HK", "2020.HK", "0883.HK",
        ),
    ),
    MarketDef(
        code="JP",
        name="Japan",
        currency="JPY",
        timezone="Asia/Tokyo",
        sessions=(
            Session(dt_time(9, 0), dt_time(11, 30)),
            Session(dt_time(12, 30), dt_time(15, 30)),
        ),
        lot_size=100,
        settlement="T+0",
        suffix=".T",
        exchange="TSE",
        catalog=(
            "7203.T", "6758.T", "9984.T", "8306.T", "6861.T", "9432.T",
            "8035.T", "6098.T", "4063.T", "7974.T", "6501.T", "9433.T",
        ),
        decimals=0,
    ),
    MarketDef(
        code="CN",
        name="China A-shares",
        currency="CNY",
        timezone="Asia/Shanghai",
        sessions=(
            Session(dt_time(9, 30), dt_time(11, 30)),
            Session(dt_time(13, 0), dt_time(15, 0)),
        ),
        lot_size=100,
        settlement="T+1",
        suffix=".SS",
        exchange="SSE / SZSE",
        price_limit_pct=10.0,
        catalog=(
            "600519.SS", "601318.SS", "600036.SS", "601899.SS", "600900.SS",
            "000001.SZ", "000858.SZ", "300750.SZ", "002594.SZ", "000333.SZ",
            "601127.SS", "688981.SS",
        ),
    ),
])

DEFAULT_MARKET = "US"

# Symbols ending in these suffixes belong to the mapped market.
_SUFFIX_MARKET = {
    "HK": "HK",
    "T": "JP",
    "SS": "CN",
    "SH": "CN",
    "SZ": "CN",
}

# A-share boards with a wider daily price band.
_CN_WIDE_LIMIT_PREFIXES = ("688", "300", "301", "689")

# US sub-dollar names have a finer tick.
_US_SUBPENNY_TICK = 0.0001
_US_TICK = 0.01


@dataclass(frozen=True)
class SymbolRef:
    """A resolved tradable symbol plus the market that governs it."""

    symbol: str
    market: MarketDef


def _split_suffix(raw: str) -> tuple[str, Optional[str]]:
    if "." not in raw:
        return raw, None
    base, _, suffix = raw.rpartition(".")
    return base, suffix.upper() or None


def _canonical_hk(base: str) -> str:
    return f"{base.zfill(4)}.HK"


def _canonical_jp(base: str) -> str:
    return f"{base.zfill(4)}.T"


def _canonical_cn(base: str, suffix: Optional[str]) -> str:
    # Yahoo uses .SS for Shanghai and .SZ for Shenzhen; .SH is the SSE's own
    # suffix and must be folded onto Yahoo's spelling.
    if suffix in ("SZ", "SH"):
        tail = "SZ" if suffix == "SZ" else "SS"
    elif base.startswith(("6", "9", "5")):
        tail = "SS"
    else:
        tail = "SZ"
    return f"{base.zfill(6)}.{tail}"


def resolve_symbol(
    raw: object, market: Optional[str] = None, *, allow_bare_numeric: bool = True
) -> Optional[SymbolRef]:
    """Resolve a user-supplied ticker into a canonical symbol + market.

    Accepts ``SPY``, ``0700.HK``, ``700.hk``, ``7203.T``, ``600519.SS``,
    ``600519.SH`` and, when the market is known, a bare numeric code such as
    ``0700`` or ``600519``. Returns ``None`` for anything that is not a real
    ticker shape so callers can reject it before it reaches a provider.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip().upper()
    if not text or len(text) > 16:
        return None

    base, suffix = _split_suffix(text)

    if not base:
        return None

    if suffix is not None:
        market_code = _SUFFIX_MARKET.get(suffix)
        if market_code is None:
            return None
        if not base.isalnum():
            return None
        if market_code == "HK":
            if not base.isdigit():
                return None
            return SymbolRef(_canonical_hk(base), MARKETS.require("HK"))
        if market_code == "JP":
            if not base.isdigit():
                return None
            return SymbolRef(_canonical_jp(base), MARKETS.require("JP"))
        if not base.isdigit():
            return None
        return SymbolRef(_canonical_cn(base, suffix), MARKETS.require("CN"))

    # No suffix: the market hint decides how a numeric code is interpreted.
    hinted = MARKETS.get(market)
    if hinted is not None and hinted.code != "US":
        if not base.isdigit():
            return None
        if hinted.code == "HK":
            return SymbolRef(_canonical_hk(base), hinted)
        if hinted.code == "JP":
            return SymbolRef(_canonical_jp(base), hinted)
        return SymbolRef(_canonical_cn(base, None), hinted)

    if not allow_bare_numeric and base.isdigit():
        return None
    # A bare US ticker: letters plus the usual class-share dots/dashes.
    if len(base) > 10 or not base[0].isalpha():
        return None
    if not all(ch.isalnum() or ch in ".-" for ch in base):
        return None
    return SymbolRef(base, MARKETS.require("US"))


def market_of(symbol: str) -> MarketDef:
    """Market for an already-canonical symbol (falls back to US)."""
    ref = resolve_symbol(symbol)
    return ref.market if ref else MARKETS.require(DEFAULT_MARKET)


def is_supported_symbol(symbol: object) -> bool:
    return resolve_symbol(symbol) is not None


# ---------------------------------------------------------------------------
# Tick sizes
# ---------------------------------------------------------------------------

# HKEX tick table: (upper price bound exclusive, tick).
_HK_TICKS: tuple[tuple[float, float], ...] = (
    (0.25, 0.001),
    (0.5, 0.005),
    (10.0, 0.01),
    (20.0, 0.02),
    (100.0, 0.05),
    (200.0, 0.1),
    (500.0, 0.2),
    (1000.0, 0.5),
    (2000.0, 1.0),
    (5000.0, 2.0),
    (float("inf"), 5.0),
)

# TSE tick table for domestic common stocks: (upper bound exclusive, tick).
_JP_TICKS: tuple[tuple[float, float], ...] = (
    (3000.0, 1.0),
    (5000.0, 5.0),
    (30000.0, 10.0),
    (100000.0, 50.0),
    (3000000.0, 100.0),
    (float("inf"), 1000.0),
)


def tick_size_for(market: MarketDef, price: float) -> float:
    """Minimum price increment for ``price`` in ``market``."""
    if price <= 0:
        return _US_TICK
    if market.code == "US":
        return _US_SUBPENNY_TICK if price < 1 else _US_TICK
    if market.code == "HK":
        return next(tick for bound, tick in _HK_TICKS if price < bound)
    if market.code == "JP":
        return next(tick for bound, tick in _JP_TICKS if price < bound)
    return 0.01


def tick_decimals(tick: float) -> int:
    text = f"{tick:.8f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


def round_to_tick(price: float, tick: float) -> float:
    if price <= 0 or tick <= 0:
        return price
    steps = round(price / tick)
    rounded = round(steps * tick, max(4, tick_decimals(tick) + 2))
    # Never round a positive price down to zero.
    if rounded <= 0:
        rounded = tick
    return rounded


def price_limit_pct(market: MarketDef, symbol: str) -> Optional[float]:
    """Daily price band for a symbol, or None when the market has no limit."""
    if market.code != "CN":
        return market.price_limit_pct
    base = symbol.split(".", 1)[0]
    if base.startswith(_CN_WIDE_LIMIT_PREFIXES):
        return 20.0
    return market.price_limit_pct


# ---------------------------------------------------------------------------
# Trading sessions
# ---------------------------------------------------------------------------

def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _session_start(day: date, session: Session, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, session.start, tzinfo=tz)


def _session_end(day: date, session: Session, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, session.end, tzinfo=tz)


def _upcoming_sessions(
    today: date, market: MarketDef, tz: ZoneInfo, days: int = 14
) -> list[tuple[datetime, datetime]]:
    """Every weekday session from ``today`` forward, in chronological order."""
    windows: list[tuple[datetime, datetime]] = []
    for offset in range(days):
        day = today + timedelta(days=offset)
        if day.weekday() not in WEEKDAYS_MON_FRI:
            continue
        for session in market.sessions:
            windows.append(
                (_session_start(day, session, tz), _session_end(day, session, tz))
            )
    return windows


def market_clock(market: MarketDef, now: Optional[datetime] = None) -> dict:
    """Session state for one market.

    ``session`` is one of ``open``, ``break`` (lunch), ``pre`` or ``closed``
    (after the close / weekend). Exchange holidays are intentionally not
    modelled: a holiday behaves like a quiet session (no quotes, no fills)
    rather than risking a hard-coded calendar that wrongly reports the market
    shut on a real trading day.

    The wall-clock form is memoised per 5s bucket: every symbol in the catalog
    lives in the same two sessions, and without it each incoming tick rebuilds
    fourteen days of windows plus their ISO strings. An explicit ``now`` is
    never cached. Callers get their own copy of the dict.
    """
    if now is None:
        return dict(_market_clock_cached(market, int(time.time() // 5)))
    moment = now
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return _render_clock(market, moment)


@functools.lru_cache(maxsize=32)
def _market_clock_cached(market: MarketDef, _bucket: int) -> dict:
    return _render_clock(market, datetime.now(timezone.utc))


def _render_clock(market: MarketDef, moment: datetime) -> dict:
    tz = ZoneInfo(market.timezone)
    local = moment.astimezone(tz)
    today = local.date()
    windows = _upcoming_sessions(today, market, tz)

    state = "closed"
    next_open: Optional[datetime] = None
    next_close: Optional[datetime] = None

    for index, (start, end) in enumerate(windows):
        if start <= local < end:
            state = "open"
            next_close = end
            if index + 1 < len(windows):
                next_open = windows[index + 1][0]
            break
        if local < start:
            next_open, next_close = start, end
            if start.date() == today:
                # An earlier session already closed today => lunch break.
                closed_today = any(
                    other_end <= local
                    for other_start, other_end in windows[:index]
                    if other_start.date() == today
                )
                state = "break" if closed_today else "pre"
            else:
                state = "closed"
            break

    return {
        "market": market.code,
        "is_open": state == "open",
        "session": state,
        "timezone": market.timezone,
        "currency": market.currency,
        "now_local": _iso(local),
        "next_open": _iso(next_open) if next_open else None,
        "next_close": _iso(next_close) if next_close else None,
    }


def _is_open_between(market: MarketDef, local: datetime, day: date) -> bool:
    """True while ``local`` falls inside one of ``day``'s sessions.

    Same weekday rule as ``_upcoming_sessions`` (the one ``market_clock``
    uses), so the two can never disagree about whether a day trades.
    """
    if day.weekday() not in WEEKDAYS_MON_FRI:
        return False
    tz = ZoneInfo(market.timezone)
    for session in market.sessions:
        if _session_start(day, session, tz) <= local < _session_end(day, session, tz):
            return True
    return False


# The open/close answer for a market is asked on every accepted tick (quote
# decoration, matching-engine session guard), but the answer only changes at
# session boundaries -- minutes apart. A short TTL turns two full
# astimezone + session-composition round trips per tick into a dict lookup.
# Callers that pass an explicit ``now`` (tests, clock rendering) bypass it so
# determinism is preserved where it is depended on.
_MARKET_OPEN_TTL = 5.0
_market_open_cache: Dict[str, Tuple[float, bool]] = {}


def is_market_open(market: MarketDef, now: Optional[datetime] = None) -> bool:
    if now is None:
        cached = _market_open_cache.get(market.code)
        if cached and time.monotonic() - cached[0] < _MARKET_OPEN_TTL:
            return cached[1]
        open_now = is_market_open(market, datetime.now(timezone.utc))
        _market_open_cache[market.code] = (time.monotonic(), open_now)
        return open_now

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    # Only the local date is needed: build it directly instead of rendering
    # fourteen days of sessions into ISO strings (``market_clock``) for every
    # incoming tick.
    local = now.astimezone(ZoneInfo(market.timezone))
    return _is_open_between(market, local, local.date())


def local_date(market: MarketDef, now: Optional[datetime] = None) -> str:
    """Exchange-local calendar date (ISO) used for T+1 settlement checks."""
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(ZoneInfo(market.timezone)).date().isoformat()


# ---------------------------------------------------------------------------
# Order validation
# ---------------------------------------------------------------------------

def validate_order(
    market: MarketDef,
    symbol: str,
    side: str,
    order_type: str,
    quantity: int,
    price: Optional[float] = None,
    stop_price: Optional[float] = None,
    prev_close: Optional[float] = None,
    position=None,
    now: Optional[datetime] = None,
) -> None:
    """Reject an order that the exchange would refuse.

    Raises ``ValueError`` with a message safe to show to the user. Checks run
    in the order a trader would experience them: size, then price shape, then
    the daily band, then position restrictions.
    """
    side_u = str(getattr(side, "value", side)).upper()
    type_u = str(getattr(order_type, "value", order_type)).upper()

    if not isinstance(quantity, int) or isinstance(quantity, bool):
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            raise ValueError("Order quantity must be a whole number of shares")
    if quantity <= 0:
        raise ValueError("Order quantity must be positive")

    lot = market.lot_size
    if lot > 1 and quantity % lot != 0:
        raise ValueError(
            f"{market.code} orders must be a multiple of {lot} shares "
            f"(board lot); got {quantity}"
        )

    if type_u == "LIMIT" and (price is None or price <= 0):
        raise ValueError("LIMIT orders require a positive price")
    if type_u == "STOP" and (stop_price is None or stop_price <= 0):
        raise ValueError("STOP orders require a positive stop price")

    if price is not None and price > 0:
        tick = tick_size_for(market, price)
        steps = price / tick
        if abs(steps - round(steps)) > 1e-6:
            decimals = tick_decimals(tick)
            raise ValueError(
                f"Price must be in increments of {tick:g} "
                f"({decimals} decimal places) for {market.code} at this level"
            )

    limit_pct = price_limit_pct(market, symbol)
    if limit_pct and price and prev_close and prev_close > 0:
        upper = prev_close * (1 + limit_pct / 100.0)
        lower = prev_close * (1 - limit_pct / 100.0)
        # Allow one tick of rounding at the band edge.
        slack = tick_size_for(market, prev_close)
        if price > upper + slack or price < lower - slack:
            raise ValueError(
                f"{symbol} daily price limit is +/-{limit_pct:g}% "
                f"({lower:.2f}-{upper:.2f}); {price:g} is outside the band"
            )

    if market.settlement == "T+1" and side_u == "SELL" and position is not None:
        bought_on = getattr(position, "bought_on", None)
        if bought_on == local_date(market, now):
            raise ValueError(
                f"{market.code} settles T+1: shares bought today cannot be "
                "sold until the next trading day"
            )


def market_payload(market: MarketDef) -> dict:
    """JSON-friendly description of a market for the client."""
    return {
        "code": market.code,
        "name": market.name,
        "currency": market.currency,
        "timezone": market.timezone,
        "exchange": market.exchange,
        "sessions": [
            {"start": s.start.strftime("%H:%M"), "end": s.end.strftime("%H:%M")}
            for s in market.sessions
        ],
        "lot_size": market.lot_size,
        "settlement": market.settlement,
        "suffix": market.suffix,
        "price_limit_pct": market.price_limit_pct,
        "decimals": market.decimals,
        "catalog": list(market.catalog),
    }
