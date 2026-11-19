"""
Alpaca Data and Trading Protocol Adapter.
Translates Alpaca REST and WebSocket streaming messages into internal domain models.
"""
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
from app.models.trading import (
    Quote, Bar, Trade, Order, OrderSide, OrderType, OrderStatus, TimeInForce,
    Portfolio, Position, utc_now
)


def _parse_alpaca_timestamp(ts_val: Any) -> datetime:
    """Parse ISO8601 string or numeric timestamp to UTC datetime."""
    if not ts_val:
        return utc_now()
    if isinstance(ts_val, datetime):
        return ts_val.astimezone(timezone.utc)
    if isinstance(ts_val, str):
        # Fast path for the canonical Alpaca tick shape "2026-11-02T19:48:19
        # [.fraction]Z" (every stream timestamp ends in Z). On py3.10 the old
        # replace/split munging plus astimezone(utc) measured ~13us per call
        # on the VPS (astimezone alone ~4.6us vs fromisoformat ~0.3us); the
        # slice form parses in well under half that and never rebases because
        # the string is already UTC. Anything else falls through to the
        # general munging below, unchanged.
        if ts_val.endswith("Z"):
            body = ts_val[:19]
            frac = (ts_val[20:-1] + "000000")[:6]
            try:
                # frac is always 6 digits here ('000000' for whole seconds)
                return datetime.fromisoformat(f"{body}.{frac}+00:00")
            except ValueError:
                pass  # unusual shape (no seconds, offset inside) -> slow path
        # Alpaca uses RFC3339 / ISO8601: "2026-11-02T19:48:19.218325244Z"
        # Handle nanoseconds by truncating to microseconds (6 digits)
        clean_str = ts_val.replace("Z", "+00:00")
        if "." in clean_str:
            parts = clean_str.split(".")
            sec = parts[0]
            frac_and_tz = parts[1]
            if "+" in frac_and_tz:
                frac, tz = frac_and_tz.split("+")
                frac = frac[:6].ljust(6, "0")
                clean_str = f"{sec}.{frac}+{tz}"
            else:
                frac = frac_and_tz[:6].ljust(6, "0")
                clean_str = f"{sec}.{frac}"
        try:
            return datetime.fromisoformat(clean_str).astimezone(timezone.utc)
        except Exception:
            return utc_now()
    return utc_now()


def _parse_optional_timestamp(ts_val: Any) -> Optional[datetime]:
    """Like ``_parse_alpaca_timestamp`` but preserves "absent" as ``None``.

    Order history needs to distinguish a venue timestamp the broker actually
    sent from one we made up at fetch time; inventing ``utc_now()`` here is what
    made every historical fill look freshly executed.
    """
    if not ts_val:
        return None
    return _parse_alpaca_timestamp(ts_val)


class AlpacaDataAdapter:
    """
    Normalizes Alpaca Market Data and Paper Trading API payloads.
    """

    # Widest two-sided spread, in basis points, that still counts as a usable
    # market. Single-venue (IEX) books go one-sided or absurdly wide around the
    # close and outside the session; a midpoint taken there is not a price any
    # trade ever happened at, so it must never become a mark or a "last".
    MAX_QUOTE_SPREAD_BPS = 50.0

    @classmethod
    def is_tradeable_quote(cls, bid: float, ask: float) -> bool:
        """True only for a two-sided, sane (not crossed, not wide) quote."""
        if bid <= 0 or ask <= 0 or bid > ask:
            return False
        mid = (ask + bid) / 2.0
        if mid <= 0:
            return False
        spread_bps = (ask - bid) / mid * 10000.0
        return spread_bps <= cls.MAX_QUOTE_SPREAD_BPS

    @staticmethod
    def parse_stream_quote(item: Dict[str, Any], last_price: Optional[float] = None) -> Optional[Quote]:
        """
        Parses Alpaca IEX/SIP stream quote message (T='q').
        Example:
        {"T": "q", "S": "SPY", "bp": 753.47, "bs": 40, "ap": 753.51, "as": 80, "t": "2026-11-02T19:48:19Z"}

        ``last`` is the last *trade* price and nothing else. When no trade price
        is known it is left at 0 (unknown) so a downstream provider can fill it
        from a real consolidated print. A midpoint of a one-sided or wide
        after-hours IEX book is not a trade and used to be shown to the user as
        one -- see the SPY 738.435 / close 735.29 incident.
        """
        try:
            symbol = item.get("S", "").upper()
            if not symbol:
                return None

            bid = float(item.get("bp") or 0.0)
            ask = float(item.get("ap") or 0.0)
            volume = int(item.get("as", 0) + item.get("bs", 0))
            ts = _parse_alpaca_timestamp(item.get("t"))

            # Only a real print may set last. If this tick carries no price we
            # keep the one the caller already knows and pass 0 otherwise.
            last = float(last_price) if (last_price and last_price > 0) else 0.0

            return Quote(
                symbol=symbol,
                bid=bid,
                ask=ask,
                last=last,
                volume=volume,
                timestamp=ts
            )
        except Exception:
            return None

    @staticmethod
    def parse_stream_trade(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Parses Alpaca IEX/SIP stream trade message (T='t').
        Example:
        {"T": "t", "S": "SPY", "p": 753.49, "s": 100, "t": "2026-11-02T19:48:19Z"}
        """
        try:
            return {
                "symbol": item.get("S", "").upper(),
                "price": float(item.get("p", 0.0)),
                "size": int(item.get("s", 0)),
                "timestamp": _parse_alpaca_timestamp(item.get("t"))
            }
        except Exception:
            return None

    @staticmethod
    def parse_stream_bar(item: Dict[str, Any]) -> Optional[Bar]:
        """
        Parses Alpaca IEX/SIP stream minute bar (T='b').
        Example:
        {"T": "b", "S": "SPY", "o": 758.7, "h": 759.1, "l": 758.5, "c": 759.0, "v": 300, "t": "2026-11-02T13:36:00Z"}
        """
        try:
            ts = _parse_alpaca_timestamp(item.get("t"))
            unix_sec = int(ts.timestamp())
            return Bar(
                time=unix_sec,
                open=float(item.get("o", 0.0)),
                high=float(item.get("h", 0.0)),
                low=float(item.get("l", 0.0)),
                close=float(item.get("c", 0.0)),
                volume=float(item.get("v", 0.0))
            )
        except Exception:
            return None

    @staticmethod
    def parse_stream_trade(item: Dict[str, Any]) -> Optional[Trade]:
        """
        Parses an Alpaca trade tick (T='t').

        Example: {"T":"t","S":"SPY","i":123,"p":753.49,"s":100,"t":"2026-11-02T19:48:19Z"}
        Alpaca does not expose the aggressor side on this feed, so ``side`` is a
        placeholder; consumers must derive buy/sell pressure from price vs. quote.
        """
        try:
            symbol = (item.get("S") or "").upper()
            price = float(item.get("p") or 0.0)
            quantity = int(float(item.get("s") or 0))
            if not symbol or price <= 0 or quantity <= 0:
                return None
            ts = _parse_alpaca_timestamp(item.get("t"))
            return Trade(
                trade_id=str(item.get("i") or f"{symbol}-{int(ts.timestamp() * 1000)}"),
                order_id="",
                symbol=symbol,
                side=OrderSide.BUY,
                price=price,
                quantity=quantity,
                timestamp=ts,
            )
        except Exception:
            return None

    @staticmethod
    def parse_rest_quote_response(
        symbol: str, res_json: Dict[str, Any], last_price: Optional[float] = None
    ) -> Optional[Quote]:
        """
        Parses Alpaca REST /v2/stocks/{symbol}/quotes/latest response.
        Example:
        {"symbol": "SPY", "quote": {"ap": 753.51, "bp": 753.47, "t": "2026-11-02T19:48:19Z"}}

        The REST quote carries no trade, so ``last`` is only whatever real print
        the caller already knows. It is never derived from the book.
        """
        try:
            q_data = res_json.get("quote", {})
            bid = float(q_data.get("bp") or 0.0)
            ask = float(q_data.get("ap") or 0.0)
            volume = int(q_data.get("as", 0) + q_data.get("bs", 0))
            ts = _parse_alpaca_timestamp(q_data.get("t"))
            last = float(last_price) if (last_price and last_price > 0) else 0.0

            return Quote(
                symbol=symbol.upper(),
                bid=bid,
                ask=ask,
                last=last,
                volume=volume,
                timestamp=ts
            )
        except Exception:
            return None

    @staticmethod
    def parse_snapshots_response(res_json: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        """Parses ``/v2/stocks/snapshots`` into per-symbol daily context.

        The multi-symbol snapshot endpoint returns, for every requested symbol:
        ``{"prevDailyBar": {...}, "dailyBar": {...}, "latestTrade": {...}}``.
        A single request therefore yields the previous close plus today's
        open/high/low/volume and last trade for the whole catalog, instead of
        one historical call per symbol.
        """
        stats: Dict[str, Dict[str, float]] = {}
        if not isinstance(res_json, dict):
            return stats

        def _num(obj: Any, key: str) -> Optional[float]:
            if not isinstance(obj, dict):
                return None
            value = obj.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
            return None

        for symbol, snap in res_json.items():
            if not isinstance(snap, dict):
                continue
            bar = snap.get("dailyBar")
            entry: Dict[str, float] = {}
            for key, value in (
                ("prev_close", _num(snap.get("prevDailyBar"), "c")),
                ("last", _num(snap.get("latestTrade"), "p") or _num(bar, "c")),
                ("day_open", _num(bar, "o")),
                ("day_high", _num(bar, "h")),
                ("day_low", _num(bar, "l")),
                ("volume", _num(bar, "v")),
            ):
                if value is not None:
                    entry[key] = value
            if entry:
                stats[str(symbol).upper()] = entry
        return stats

    @staticmethod
    def parse_rest_bars_response(res_json: Dict[str, Any], symbol: Optional[str] = None) -> List[Bar]:
        """
        Parses Alpaca REST /v2/stocks/bars or /v2/stocks/{symbol}/bars response.
        Handles both:
        {"bars": [{"o": ..., "h": ..., "l": ..., "c": ..., "v": ..., "t": ...}], "symbol": "SPY"}
        and
        {"bars": {"SPY": [{"o": ..., "h": ..., "l": ..., "c": ..., "v": ..., "t": ...}]}}
        """
        bars_out: List[Bar] = []
        raw_bars = res_json.get("bars", [])

        if isinstance(raw_bars, dict):
            if symbol and symbol.upper() in raw_bars:
                candle_list = raw_bars[symbol.upper()]
            else:
                first_key = next(iter(raw_bars)) if raw_bars else None
                candle_list = raw_bars.get(first_key, []) if first_key else []
        elif isinstance(raw_bars, list):
            candle_list = raw_bars
        else:
            candle_list = []

        for c in candle_list:
            try:
                ts = _parse_alpaca_timestamp(c.get("t"))
                bars_out.append(
                    Bar(
                        time=int(ts.timestamp()),
                        open=float(c.get("o", 0.0)),
                        high=float(c.get("h", 0.0)),
                        low=float(c.get("l", 0.0)),
                        close=float(c.get("c", 0.0)),
                        volume=float(c.get("v", 0.0))
                    )
                )
            except Exception:
                continue

        bars_out.sort(key=lambda b: b.time)
        return bars_out

    @staticmethod
    def parse_account_to_portfolio(account_json: Dict[str, Any], positions_json: List[Dict[str, Any]]) -> Portfolio:
        """
        Maps Alpaca /v2/account and /v2/positions to unified domain Portfolio model.
        """
        cash = float(account_json.get("cash", 100000.0))
        portfolio = Portfolio(
            cash=round(cash, 2),
            initial_capital=100000.0,
            positions={},
            realized_pnl=0.0
        )

        for p in positions_json:
            try:
                sym = p.get("symbol", "").upper()
                qty = int(float(p.get("qty", 0)))
                avg_cost = float(p.get("avg_entry_price", 0.0))
                current_price = float(p.get("current_price", avg_cost))
                realized_pnl = 0.0

                portfolio.positions[sym] = Position(
                    symbol=sym,
                    quantity=qty,
                    avg_cost=avg_cost,
                    current_price=current_price,
                    realized_pnl=realized_pnl,
                    # Alpaca reports US positions in USD only. Without the USD
                    # basis the field reads 0, and any USD total derived from
                    # it silently overstates value.
                    market="US",
                    currency="USD",
                    cost_basis_usd=round(qty * avg_cost, 4),
                )
            except Exception:
                continue

        return portfolio

    @staticmethod
    def parse_order_to_domain(order_json: Dict[str, Any]) -> Order:
        """
        Maps Alpaca /v2/orders JSON object to internal Order domain model.
        """
        side_str = (order_json.get("side") or "buy").upper()
        side = OrderSide.BUY if side_str == "BUY" else OrderSide.SELL

        type_str = (order_json.get("type") or "market").upper()
        if type_str == "LIMIT":
            order_type = OrderType.LIMIT
        elif type_str in ("STOP", "STOP_LIMIT"):
            # The domain enum collapses stop_limit into STOP carrying a
            # limit price — the same shape the submit side sends, so a
            # venue round-trip keeps its stop_limit identity instead of
            # degrading to MARKET (2026-11-16 live test caught this).
            order_type = OrderType.STOP
        elif type_str == "TRAILING_STOP":
            order_type = OrderType.TRAILING_STOP
        else:
            order_type = OrderType.MARKET

        raw_status = (order_json.get("status") or "").lower()
        if raw_status in ("filled", "partially_filled"):
            status = OrderStatus.FILLED if raw_status == "filled" else OrderStatus.PARTIALLY_FILLED
        elif raw_status in ("canceled", "cancelled", "expired"):
            status = OrderStatus.CANCELLED
        elif raw_status in ("rejected", "suspended"):
            status = OrderStatus.REJECTED
        else:
            status = OrderStatus.PENDING

        qty = int(float(order_json.get("qty") or 0))
        filled_qty = int(float(order_json.get("filled_qty") or 0))
        filled_avg_price = float(order_json.get("filled_avg_price") or 0.0) if order_json.get("filled_avg_price") else 0.0
        price = float(order_json.get("limit_price")) if order_json.get("limit_price") else None
        stop_price = float(order_json.get("stop_price")) if order_json.get("stop_price") else None
        created_at = _parse_alpaca_timestamp(order_json.get("created_at"))
        # Prefer the venue's own timestamps. A lookup must never rewrite when an
        # order last changed; falling back to created_at keeps history stable
        # instead of claiming the row was touched on every poll.
        updated_at = _parse_optional_timestamp(order_json.get("updated_at")) or created_at
        filled_at = _parse_optional_timestamp(order_json.get("filled_at"))

        # Venue TIF round-trips (a GTC protective stop must not present
        # itself as DAY); trail distance lands on the domain model so the
        # UI can show what the venue is actually trailing by.
        tif_str = (order_json.get("time_in_force") or "day").upper()
        tif = TimeInForce.GTC if tif_str == "GTC" else (
            TimeInForce.EXT if tif_str == "EXT" else TimeInForce.DAY
        )
        trail_value: Optional[float] = None
        trail_type: Optional[str] = None
        if order_json.get("trail_percent"):
            trail_value = float(order_json["trail_percent"])
            trail_type = "percent"
        elif order_json.get("trail_price"):
            trail_value = float(order_json["trail_price"])
            trail_type = "absolute"

        return Order(
            order_id=order_json.get("id") or order_json.get("client_order_id") or "ord_unknown",
            symbol=order_json.get("symbol", "").upper(),
            side=side,
            order_type=order_type,
            quantity=qty,
            price=price,
            stop_price=stop_price,
            time_in_force=tif,
            trail_value=trail_value,
            trail_type=trail_type,
            status=status,
            filled_quantity=filled_qty,
            filled_avg_price=filled_avg_price,
            created_at=created_at,
            updated_at=updated_at,
            filled_at=filled_at,
        )
