"""
Integration tests for FastAPI REST endpoints and WebSocket communication flow.
"""
import os
import pytest

# Enforce virtual matching engine and local simulated feed during integration unit testing
os.environ["BROKER_PROVIDER"] = "virtual"
os.environ["MARKET_DATA_PROVIDER"] = "simulation"

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from app.core.config import settings
from app import main
from app.main import app, ledger, virtual_engine

# Pin a deterministic token for the test session (settings are read from env at import).
settings.admin_token = "test-token"
AUTH = {"X-Auth-Token": "test-token"}


@pytest.fixture(autouse=True)
def reset_state():
    """Reset ledger and orders before each test."""
    ledger.reset(10000.0)
    virtual_engine.orders.clear()


def test_health_endpoint():
    with TestClient(app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


def test_unprefixed_health_probe_is_json_not_spa():
    """``/health`` must answer JSON.

    Regression guard: the SPA catch-all used to answer ``/health`` with
    ``index.html`` and a 200, so any external monitor reading that path saw
    "healthy" even while the API was broken. The unprefixed probe now mirrors
    ``/api/health``.
    """
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.headers.get("content-type", "").startswith("application/json")
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "schwab-trader-hub"
        assert "markets" in data


def test_protected_endpoints_require_token():
    with TestClient(app) as client:
        assert client.get("/api/portfolio").status_code == 401
        assert client.get("/api/portfolio", headers={"X-Auth-Token": "wrong"}).status_code == 401
        assert client.post("/api/portfolio/reset", json={}).status_code == 401
        assert client.get("/api/auth/verify").status_code == 401
        assert client.get("/api/auth/verify", headers={"X-Auth-Token": "wrong"}).status_code == 401
        assert client.get("/api/auth/verify", headers=AUTH).status_code == 200
        assert client.get("/api/portfolio", headers=AUTH).status_code == 200


def test_market_endpoints():
    with TestClient(app) as client:
        # 1. Historical bars
        resp_bars = client.get("/api/market/bars?symbol=SPY", headers=AUTH)
        assert resp_bars.status_code == 200
        bars = resp_bars.json()
        assert isinstance(bars, list)
        assert len(bars) > 0
        assert "close" in bars[0]
        assert "time" in bars[0]

        # Unsupported timeframe must be rejected, never silently substituted.
        assert client.get("/api/market/bars?symbol=SPY&timeframe=bogus", headers=AUTH).status_code == 400

        # 2. Latest quote
        resp_quote = client.get("/api/market/quote?symbol=SPY", headers=AUTH)
        assert resp_quote.status_code == 200
        quote = resp_quote.json()
        assert quote["symbol"] == "SPY"
        assert quote["bid"] > 0
        assert quote["ask"] >= quote["bid"]

        # 3. Model feature and Agent context
        resp_ctx = client.get("/api/market/model-context?symbol=SPY", headers=AUTH)
        assert resp_ctx.status_code == 200
        ctx = resp_ctx.json()
        assert ctx["symbol"] == "SPY"
        assert "technical_indicators" in ctx
        assert "real_time_quote" in ctx


def test_market_clock_endpoint():
    """Session clock endpoint: auth-gated; per-market clocks are computed
    locally, so every supported market answers instead of a 503."""
    with TestClient(app) as client:
        assert client.get("/api/market/clock").status_code == 401
        data = client.get("/api/market/clock", headers=AUTH).json()
        assert data["market"] == "US"
        assert isinstance(data["is_open"], bool)
        assert data["session"] in ("open", "break", "pre", "closed")
        hk = client.get("/api/market/clock?market=HK", headers=AUTH).json()
        assert hk["market"] == "HK"
        assert hk["currency"] == "HKD"
        # Unknown market is a 400, never a fabricated session state
        assert client.get("/api/market/clock?market=XX", headers=AUTH).status_code == 400
        # /api/markets catalog is present and self-consistent
        markets = client.get("/api/markets", headers=AUTH).json()
        codes = [m["code"] for m in markets["markets"]]
        assert "US" in codes and "HK" in codes and "JP" in codes and "CN" in codes


def test_order_submission_and_portfolio_rest():
    with TestClient(app) as client:
        # Initial portfolio check
        p_init = client.get("/api/portfolio", headers=AUTH).json()
        assert p_init["cash"] == 10000.0

        # Submit market buy for 5 shares of SPY
        order_payload = {
            "symbol": "SPY",
            "side": "BUY",
            "order_type": "MARKET",
            "quantity": 5
        }
        resp_order = client.post("/api/orders", json=order_payload, headers=AUTH)
        assert resp_order.status_code == 200
        order_data = resp_order.json()
        assert order_data["status"] == "FILLED"
        assert order_data["filled_quantity"] == 5

        # Check portfolio update
        p_updated = client.get("/api/portfolio", headers=AUTH).json()
        assert p_updated["cash"] < 10000.0
        assert "SPY" in p_updated["positions"]
        assert p_updated["positions"]["SPY"]["quantity"] == 5

        # Submit limit sell above market price
        limit_order_payload = {
            "symbol": "SPY",
            "side": "SELL",
            "order_type": "LIMIT",
            "quantity": 2,
            "price": 999.0
        }
        resp_limit = client.post("/api/orders", json=limit_order_payload, headers=AUTH)
        assert resp_limit.status_code == 200
        limit_data = resp_limit.json()
        assert limit_data["status"] == "PENDING"

        # Cancel limit order
        cancel_resp = client.delete(f"/api/orders/{limit_data['order_id']}", headers=AUTH)
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "CANCELLED"


def test_websocket_auth_frame_then_order_flow():
    """Current client path: no token in the URL, AUTH sent as the first frame."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"action": "AUTH", "token": "test-token"})

            init_msg = websocket.receive_json()
            assert init_msg["type"] == "PORTFOLIO_SNAPSHOT"
            assert "cash" in init_msg["data"]

            # Ping Pong test
            websocket.send_json({"action": "PING"})
            pong_msg = websocket.receive_json()
            assert pong_msg["type"] == "PONG"

            # Submit order via WebSocket
            order_msg = {
                "action": "SUBMIT_ORDER",
                "symbol": "AAPL",
                "side": "BUY",
                "order_type": "MARKET",
                "quantity": 2
            }
            websocket.send_json(order_msg)

            # Receive ORDER_UPDATE
            ord_res = websocket.receive_json()
            assert ord_res["type"] == "ORDER_UPDATE"
            assert ord_res["data"]["status"] == "FILLED"


def test_websocket_legacy_query_token_still_accepted():
    """A tab holding a pre-deploy bundle connects with ?token= and must not break."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws?token=test-token") as websocket:
            init_msg = websocket.receive_json()
            assert init_msg["type"] == "PORTFOLIO_SNAPSHOT"

            websocket.send_json({"action": "PING"})
            assert websocket.receive_json()["type"] == "PONG"


def _rejected_code(client, path: str, frames: list[dict] | None = None) -> int:
    """Wait for the server to hang up and report the close code it chose."""
    with client.websocket_connect(path) as websocket:
        for frame in frames or []:
            websocket.send_json(frame)
        with pytest.raises(WebSocketDisconnect) as excinfo:
            websocket.receive_json()
    return excinfo.value.code


def test_websocket_rejects_wrong_auth_frame():
    with TestClient(app) as client:
        assert _rejected_code(client, "/ws", [{"action": "AUTH", "token": "wrong"}]) == 4401


def test_websocket_rejects_wrong_query_token():
    with TestClient(app) as client:
        assert _rejected_code(client, "/ws?token=wrong") == 4401


def test_websocket_rejects_unauthenticated_handshake(monkeypatch):
    """No query token and no AUTH frame: the server must time out and close 4401."""
    monkeypatch.setattr(main, "WS_AUTH_TIMEOUT_SEC", 0.2)
    with TestClient(app) as client:
        assert _rejected_code(client, "/ws") == 4401


def test_websocket_survives_malformed_frames():
    """Garbage in the stream must not kill a session that already authenticated."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws?token=test-token") as websocket:
            assert websocket.receive_json()["type"] == "PORTFOLIO_SNAPSHOT"

            websocket.send_text("not json at all")
            websocket.send_json(["a", "list"])          # JSON, but not an object
            websocket.send_json({"action": "SUBSCRIBE", "symbol": "../../etc"})
            assert websocket.receive_json()["type"] == "SUBSCRIBE_ERROR"

            websocket.send_json({"action": "PING"})
            pong_msg = websocket.receive_json()
            assert pong_msg["type"] == "PONG"


def test_multi_market_catalog_and_rules():
    """Multi-market endpoints: catalog, per-market tickers, symbol routing and
    exchange rule enforcement on the order API (no network in offline mode)."""
    with TestClient(app) as client:
        # Catalog is self-consistent for all four markets.
        markets = client.get("/api/markets", headers=AUTH).json()
        by_code = {m["code"]: m for m in markets["markets"]}
        assert set(by_code) == {"US", "HK", "JP", "CN"}
        assert by_code["HK"]["currency"] == "HKD"
        assert by_code["HK"]["lot_size"] == 100
        assert by_code["CN"]["settlement"] == "T+1"
        assert by_code["JP"]["suffix"] == ".T"
        assert "base_currency" in markets
        assert markets["fx_rates"]["USD"] == 1.0

        # International tickers are served (empty offline, never a crash).
        hk_tickers = client.get("/api/market/tickers?market=HK", headers=AUTH)
        assert hk_tickers.status_code == 200
        assert isinstance(hk_tickers.json(), list)
        assert client.get("/api/market/tickers?market=XX", headers=AUTH).status_code == 400

        # Bare numeric symbols resolve with the market hint.
        order = client.post("/api/orders", json={
            "symbol": "0700",
            "market": "HK",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 100,
            "price": 400.0,
        }, headers=AUTH)
        assert order.status_code == 200
        body = order.json()
        assert body["symbol"] == "0700.HK"
        assert body["market"] == "HK"
        assert body["currency"] == "HKD"

        # A-share board lot is enforced server-side.
        bad_lot = client.post("/api/orders", json={
            "symbol": "600519.SS",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 150,
            "price": 1450.0,
        }, headers=AUTH)
        assert bad_lot.status_code == 400
        assert "multiple of 100" in bad_lot.json()["detail"]

        # Non-numeric tickers and nonsense are rejected, not sent upstream.
        assert client.post("/api/orders", json={
            "symbol": "../../etc", "side": "BUY", "order_type": "MARKET", "quantity": 1
        }, headers=AUTH).status_code == 400

        # Portfolio reports the base currency and FX rates.
        portfolio = client.get("/api/portfolio", headers=AUTH).json()
        assert portfolio["base_currency"] == "USD"
        assert "fx_rates" in portfolio
        assert "USD" in portfolio["fx_rates"]
