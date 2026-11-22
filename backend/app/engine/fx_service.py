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
# These are 2026-11 levels — the previous CNY value (6.667) was a 2021-era
# rate and mispriced every CNY position by ~7% whenever FX refresh was
# disabled or failed (2026-11-23 review). If you touch these, keep them
# within a sane band of the live pegs: CNY managed float ~7.1-7.3,
# HKD peg 7.75-7.85, JPY recent range ~145-165.
_FALLBACK_PER_USD = {
    "USD": 1.0,
    "HKD": 7.830,
    "JPY": 158.4,
    "CNY": 7.120,
}
# Rough sanity bands (units per USD) used to log a loud warning if a live
# refresh ever returns a value wildly outside the expected regime.
_SANITY_BAND_PER_USD = {
    "HKD": (7.60, 8.00),
    "JPY": (120.0, 180.0),
    "CNY": (6.60, 7.80),
}

SUPPORTED_CURRENCIES = tuple(_FALLBACK_PER_USD)
_UNWARNED_CURRENCIES: set[str] = set()


class FxService:
    def __init__(
        self,
        ttl_seconds: float = 120.0,
        base_currency: str = "USD",
        enabled: bool = True,
    ):
        self._base = base_currency
        if self._base != "USD":
            # _fetch builds "<CCY><BASE>=X" (units of CCY per BASE) but
            # usd_per_unit inverts toward USD regardless — a non-USD base
            # would misprice everything by a factor of the base/USD rate.
            # Nothing in the deployment configures this today; make the
            # latent trap explicit (2026-11-23 review P3).
            logger.warning(
                "FxService base_currency=%r is not USD: USD-per-unit outputs "
                "are NOT actually USD-per-unit", self._base,
            )
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
                    band = _SANITY_BAND_PER_USD.get(currency)
                    if band and not (band[0] <= per_usd <= band[1]):
                        # A live rate outside the regime band is more likely a
                        # parser/upstream artefact than a real move; refuse it
                        # rather than misprice every converted position.
                        logger.error(
                            "FX rate for %s outside sanity band %s: %s — keeping previous",
                            currency, band, per_usd,
                        )
                        continue
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
        """Convert 1 unit of ``currency`` into USD (non-blocking).

        An unknown currency falls back to parity 1.0 — a wrong rate is worse
        than a missing one, so at least make the fallback loud (throttled to
        one warning per currency per process, 2026-11-23 review P3).
        """
        currency = (currency or self._base).upper()
        if currency == self._base:
            return 1.0
        self._maybe_refresh()
        with self._lock:
            per_usd = self._rates.get(currency)
        if per_usd and per_usd > 0:
            return 1.0 / per_usd
        if currency not in _UNWARNED_CURRENCIES:
            _UNWARNED_CURRENCIES.add(currency)
            logger.warning("FX parity fallback (1.0) for unknown currency %s", currency)
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
