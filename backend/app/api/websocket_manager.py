"""
WebSocket Manager handling multi-client subscriptions, realtime quote broadcasts, and order dispatch.
Includes high-frequency quote conflation/throttling to keep browser frontends responsive at 25 FPS.
"""
from __future__ import annotations
import asyncio
import json
import time
import logging
from typing import Dict, Set, Any, List, Optional
from fastapi import WebSocket

logger = logging.getLogger("websocket_manager")


class WebSocketManager:
    # A single slow/stuck client must never block the broadcast for everyone else.
    SEND_TIMEOUT_SEC = 2.0

    def __init__(self, throttle_ms: float = 40.0):
        # Store connected websockets and their subscribed ticker sets
        self.active_connections: Dict[WebSocket, Set[str]] = {}
        # Accepted is not the same as trusted: the endpoint accepts the socket
        # before it has the token, and only this set may ever receive a message.
        self._authenticated: Set[WebSocket] = set()
        self.last_broadcast_ts: Dict[str, float] = {}
        self.throttle_sec = throttle_ms / 1000.0
        # Live symbol -> number of sockets watching it. The feed-level
        # subscription must be released when the LAST watcher leaves, not
        # when any socket unsubscribes: previously a tab close (the normal
        # path — no UNSUBSCRIBE frame is sent) leaked the symbol into the
        # feed forever, permanently exhausting the 50-symbol dynamic cap
        # (2026-11-23 review P1-4). Runs only on the event loop thread; no
        # lock needed.
        self._symbol_refs: Dict[str, int] = {}
        # Feed-level release hook, wired by main.py (_release_feed_symbols).
        # The broadcast path (_send_many) ALSO drops sockets, so the release
        # must live here rather than only in the endpoint's exception
        # handlers — a socket dropped mid-broadcast is already popped, and
        # the endpoint's later disconnect() would return an empty set,
        # leaking its symbols into the feed again (2026-11-23 second review
        # P1-2). main.py assigns a callable at import time; a bare manager
        # (tests) simply skips the feed release.
        self.on_last_watcher_left = None

    def _notify_released(self, released: Set[str]) -> None:
        if released and self.on_last_watcher_left:
            try:
                self.on_last_watcher_left(released)
            except Exception as exc:
                logger.warning("Feed release hook failed for %s: %s", released, exc)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        # No subscriptions and no trusted status until the caller authenticates.
        self.active_connections[websocket] = set()

    def mark_authenticated(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self._authenticated.add(websocket)

    def is_authenticated(self, websocket: WebSocket) -> bool:
        return websocket in self._authenticated

    def has_subscribers(self) -> bool:
        """True while some trusted client is streaming at least one symbol.

        Background work (chart prewarming, broadcast serialisation) is only
        worth doing for a client that is actually watching something.
        """
        return any(
            self.active_connections.get(ws) for ws in self._authenticated
        )

    async def close(self, websocket: WebSocket, code: int = 1000, reason: str = "") -> None:
        """Close and forget a connection that must be refused (e.g. bad auth)."""
        self.disconnect(websocket)
        try:
            await websocket.close(code=code, reason=reason)
        except Exception:
            # The peer may already be gone; nothing left to release.
            pass

    def disconnect(self, websocket: WebSocket) -> Set[str]:
        """Drop a socket and release its symbol references.

        Returns the symbols whose LAST watcher this socket was — also passed
        to the on_last_watcher_left hook so every leave path (endpoint
        handlers AND the broadcast drop path in _send_many) reaches the
        feed-level release. Callers may use the return value directly; they
        must NOT re-run the feed release themselves or the hook would
        double-fire.
        """
        symbols = self.active_connections.pop(websocket, set())
        self._authenticated.discard(websocket)
        released: Set[str] = set()
        for sym in symbols:
            refs = self._symbol_refs.get(sym, 0)
            if refs <= 1:
                self._symbol_refs.pop(sym, None)
                released.add(sym)
            else:
                self._symbol_refs[sym] = refs - 1
        # A symbol nobody watches can never pass the conflation gate again, so
        # its throttle stamp is dead weight — drop it with the last watcher
        # instead of accumulating one float per ever-quoted symbol.
        for sym in released:
            self.last_broadcast_ts.pop(sym, None)
        self._notify_released(released)
        return released

    def subscribe(self, websocket: WebSocket, symbol: str) -> None:
        if websocket in self.active_connections:
            sym = symbol.upper()
            symbols = self.active_connections[websocket]
            if sym not in symbols:
                symbols.add(sym)
                self._symbol_refs[sym] = self._symbol_refs.get(sym, 0) + 1

    def unsubscribe(self, websocket: WebSocket, symbol: str) -> bool:
        """Drop one watcher. Returns True when the last watcher left.

        The feed release for a true return goes through the
        on_last_watcher_left hook (fired here), not the caller.
        """
        if websocket in self.active_connections:
            sym = symbol.upper()
            symbols = self.active_connections[websocket]
            if sym in symbols:
                symbols.discard(sym)
                refs = self._symbol_refs.get(sym, 0)
                if refs <= 1:
                    self._symbol_refs.pop(sym, None)
                    self.last_broadcast_ts.pop(sym, None)
                    self._notify_released({sym})
                    return True
                self._symbol_refs[sym] = refs - 1
        return False

    async def _send_many(self, payload: str, targets: List[WebSocket]) -> None:
        """Send concurrently with a per-client timeout; drop dead/slow clients."""
        if not targets:
            return
        results = await asyncio.gather(
            *(self._send_one(ws, payload) for ws in targets),
            return_exceptions=True,
        )
        for ws, ok in zip(targets, results):
            if ok is not True:
                self.disconnect(ws)

    async def _send_one(self, websocket: WebSocket, payload: str) -> bool:
        try:
            await asyncio.wait_for(
                websocket.send_text(payload), timeout=self.SEND_TIMEOUT_SEC
            )
            return True
        except Exception:
            return False

    def _targets(self, symbol: str | None = None) -> List[WebSocket]:
        trusted = [
            ws for ws in self.active_connections if ws in self._authenticated
        ]
        if symbol is None:
            return trusted
        sym = symbol.upper()
        return [
            ws for ws in trusted if sym in self.active_connections[ws]
        ]

    async def broadcast(self, message: Dict[str, Any]) -> None:
        """Broadcast message to all connected clients immediately."""
        payload = self._encode(message)
        if payload is None:
            return
        await self._send_many(payload, self._targets())

    def _symbol_of(self, message: Any) -> str:
        """Symbol of a Pydantic model or a legacy plain dict, uppercased."""
        if isinstance(message, dict):
            sym = message.get("symbol", "")
        else:
            sym = getattr(message, "symbol", "")
        return (sym or "").upper()

    def _encode(self, data: Any) -> Optional[str]:
        """One JSON encode of a Pydantic model or a plain dict, never raising.

        Models serialize with ``model_dump_json`` — the single-pass encoder.
        A failure (an exotic value the model cannot render) must not take the
        tick path down, so it degrades to a dropped broadcast with a log line.
        """
        try:
            if isinstance(data, dict):
                return json.dumps(data)
            return data.model_dump_json()
        except Exception as exc:
            logger.warning("Broadcast encode failed for %s: %s", type(data).__name__, exc)
            return None

    def _envelope(self, msg_type: str, data: Any) -> Optional[str]:
        """Encode the {type, data} envelope exactly once.

        The body serializes directly (``model_dump_json`` for models — no
        intermediate dict — or ``json.dumps`` for dicts) and the envelope is
        concatenated around the pre-encoded text, so the payload never goes
        through a second conversion pass. Both parts are valid JSON texts and
        ``msg_type`` is a caller-owned literal, so the result parses exactly
        like the wire schema clients already consume.
        """
        body = self._encode(data)
        if body is None:
            return None
        return '{"type": "%s", "data": %s}' % (msg_type, body)

    async def broadcast_quote(self, quote: Any, force: bool = False) -> None:
        """Broadcast quote with conflation (max 25 FPS per symbol for UI smoothness).

        Accepts a Pydantic Quote or a legacy plain dict. Target selection and
        the conflation gate both run BEFORE the encode: a tick nobody can
        receive must not pay for ``model_dump_json``, and it must not leave a
        throttle stamp behind for a symbol nobody watches (that stamp would
        otherwise linger forever — one dict entry per ever-quoted symbol).
        """
        symbol = self._symbol_of(quote)
        targets = self._targets(symbol)
        if not targets:
            return
        now = time.time()
        if not force and symbol in self.last_broadcast_ts:
            if (now - self.last_broadcast_ts[symbol]) < self.throttle_sec:
                return  # Conflate sub-40ms high-frequency ticks

        self.last_broadcast_ts[symbol] = now
        payload = self._envelope("QUOTE_UPDATE", quote)
        if payload is None:
            return
        await self._send_many(payload, targets)

    async def broadcast_trade(self, trade: Any) -> None:
        """Broadcast a real executed trade (not a quote) to symbol subscribers."""
        symbol = self._symbol_of(trade)
        targets = self._targets(symbol)
        if not targets:
            return
        payload = self._envelope("TRADE_UPDATE", trade)
        if payload is None:
            return
        await self._send_many(payload, targets)

    async def broadcast_bar(self, symbol: str, bar: Any) -> None:
        sym = symbol.upper()
        targets = self._targets(sym)
        if not targets:
            return
        body = self._encode(bar)
        if body is None:
            return
        payload = '{"type": "BAR_UPDATE", "symbol": %s, "data": %s}' % (
            json.dumps(sym), body,
        )
        await self._send_many(payload, targets)
