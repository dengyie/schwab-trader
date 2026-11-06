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
        self.last_broadcast_ts: Dict[str, float] = {}
        self.throttle_sec = throttle_ms / 1000.0

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        # Default subscribe to SPY
        self.active_connections[websocket] = {"SPY"}

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            del self.active_connections[websocket]

    def subscribe(self, websocket: WebSocket, symbol: str) -> None:
        if websocket in self.active_connections:
            self.active_connections[websocket].add(symbol.upper())

    def unsubscribe(self, websocket: WebSocket, symbol: str) -> None:
        if websocket in self.active_connections:
            self.active_connections[websocket].discard(symbol.upper())

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
        if symbol is None:
            return list(self.active_connections.keys())
        sym = symbol.upper()
        return [ws for ws, subs in list(self.active_connections.items()) if sym in subs]

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
