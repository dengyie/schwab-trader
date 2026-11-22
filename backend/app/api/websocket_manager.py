"""
WebSocket Manager handling multi-client subscriptions, realtime quote broadcasts, and order dispatch.
Includes high-frequency quote conflation/throttling to keep browser frontends responsive at 25 FPS.
"""
from __future__ import annotations
import asyncio
import json
import time
import logging
from typing import Dict, Set, Any, List
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

    def symbols_without_watchers(self) -> Set[str]:
        """Symbols this manager still counts as watched (internal helper)."""
        return {sym for sym, refs in self._symbol_refs.items() if refs > 0}

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

        Returns the symbols whose LAST watcher this socket was — the caller
        must deactivate those at the feed level (skipping symbols that hold
        resting virtual orders, which the resting-order loop owns).
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
        return released

    def subscribe(self, websocket: WebSocket, symbol: str) -> None:
        if websocket in self.active_connections:
            sym = symbol.upper()
            symbols = self.active_connections[websocket]
            if sym not in symbols:
                symbols.add(sym)
                self._symbol_refs[sym] = self._symbol_refs.get(sym, 0) + 1

    def unsubscribe(self, websocket: WebSocket, symbol: str) -> bool:
        """Drop one watcher. Returns True when the last watcher left."""
        if websocket in self.active_connections:
            sym = symbol.upper()
            symbols = self.active_connections[websocket]
            if sym in symbols:
                symbols.discard(sym)
                refs = self._symbol_refs.get(sym, 0)
                if refs <= 1:
                    self._symbol_refs.pop(sym, None)
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
        await self._send_many(json.dumps(message), self._targets())

    async def broadcast_quote(self, quote_dict: Dict[str, Any], force: bool = False) -> None:
        """Broadcast quote with conflation (max 25 FPS per symbol for UI smoothness)."""
        symbol = quote_dict.get("symbol", "").upper()
        now = time.time()
        if not force and symbol in self.last_broadcast_ts:
            if (now - self.last_broadcast_ts[symbol]) < self.throttle_sec:
                return  # Conflate sub-40ms high-frequency ticks

        self.last_broadcast_ts[symbol] = now
        payload = json.dumps({"type": "QUOTE_UPDATE", "data": quote_dict})
        await self._send_many(payload, self._targets(symbol))

    async def broadcast_trade(self, trade_dict: Dict[str, Any]) -> None:
        """Broadcast a real executed trade (not a quote) to symbol subscribers."""
        symbol = trade_dict.get("symbol", "").upper()
        payload = json.dumps({"type": "TRADE_UPDATE", "data": trade_dict})
        await self._send_many(payload, self._targets(symbol))

    async def broadcast_bar(self, symbol: str, bar_dict: Dict[str, Any]) -> None:
        sym = symbol.upper()
        payload = json.dumps({"type": "BAR_UPDATE", "symbol": sym, "data": bar_dict})
        await self._send_many(payload, self._targets(sym))
