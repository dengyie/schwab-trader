"""
Foreign-exchange rates used to consolidate a multi-currency portfolio into a
single base currency (USD).

Rates come from Yahoo Finance's chart API (``HKD=X``, ``JPY=X``, ``CNY=X``),
which returns units of currency per USD. The service never blocks a request on
the network: it serves its cached (or fallback) rate immediately and refreshes
in a background thread at most once per ``ttl_seconds``. A failed refresh keeps
the last known-good rate, so a transient outage cannot zero out a converted
position.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from app.adapters.yahoo_adapter import YahooFinanceAdapter

logger = logging.getLogger(__name__)

# Units of currency per USD. Only used until the first successful refresh.
_FALLBACK_PER_USD = {
    "USD": 1.0,
    "HKD": 7.830,
    "JPY": 158.4,
    "CNY": 6.667,
}

SUPPORTED_CURRENCIES = tuple(_FALLBACK_PER_USD)


class FxService:
    def __init__(
        self,
        ttl_seconds: float = 120.0,
        base_currency: str = "USD",
        enabled: bool = True,
    ):
        self._base = base_currency
        self._ttl = ttl_seconds
        self._enabled = enabled
        # currency -> units per USD
        self._rates: dict[str, float] = dict(_FALLBACK_PER_USD)
        self._updated_at = 0.0
        self._lock = threading.Lock()
        self._refreshing = False
        self._live = False

    # -- internals ---------------------------------------------------------
    def _fetch(self, currency: str) -> Optional[float]:
        """Units of ``currency`` per USD via Yahoo, or None on any failure.

        Yahoo writes USD-based FX pairs as ``<CCY>=X`` (e.g. ``HKD=X`` is the
        USD/HKD rate), not ``<CCY>=USD``.
        """
        pair = f"{currency}=X" if self._base == "USD" else f"{currency}{self._base}=X"
        try:
            meta = YahooFinanceAdapter.fetch_meta(pair)
            if not meta:
                return None
            for key in ("regularMarketPrice", "previousClose", "chartPreviousClose"):
                value = meta.get(key)
                if isinstance(value, (int, float)) and value > 0:
                    return float(value)
        except Exception as exc:  # pragma: no cover - network layer
            logger.warning("FX fetch failed for %s: %s", pair, exc)
        return None

    def _refresh_worker(self) -> None:
        try:
            fresh: dict[str, float] = {self._base: 1.0}
            ok = 0
            for currency in SUPPORTED_CURRENCIES:
                if currency == self._base:
                    continue
                per_usd = self._fetch(currency)
                if per_usd and per_usd > 0:
                    fresh[currency] = per_usd
                    ok += 1
            if ok:
                with self._lock:
                    # Keep the previous value for a pair that failed this round.
                    for currency, per_usd in fresh.items():
                        self._rates[currency] = per_usd
                    self._updated_at = time.time()
                    self._live = True
                logger.info("FX rates refreshed (%d pairs): %s", ok, fresh)
        finally:
            with self._lock:
                self._refreshing = False

    def _maybe_refresh(self) -> None:
        if not self._enabled:
            return
        with self._lock:
            if self._refreshing:
                return
            if self._updated_at and time.time() - self._updated_at < self._ttl:
                return
            self._refreshing = True
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    # -- public API --------------------------------------------------------
    def usd_per_unit(self, currency: str) -> float:
        """Convert 1 unit of ``currency`` into USD (non-blocking)."""
        currency = (currency or self._base).upper()
        if currency == self._base:
            return 1.0
        self._maybe_refresh()
        with self._lock:
            per_usd = self._rates.get(currency)
        if per_usd and per_usd > 0:
            return 1.0 / per_usd
        return 1.0

    def convert(self, amount: float, currency: str) -> float:
        return amount * self.usd_per_unit(currency)

    def all_rates(self) -> dict[str, float]:
        """USD per unit for every supported currency (non-blocking)."""
        self._maybe_refresh()
        with self._lock:
            snapshot = dict(self._rates)
        return {
            currency: (1.0 / per_usd if per_usd else 1.0)
            for currency, per_usd in snapshot.items()
        }

    def is_live(self) -> bool:
        """True once at least one real rate has been fetched."""
        with self._lock:
            return self._live
