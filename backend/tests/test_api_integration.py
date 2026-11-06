"""
Integration tests for FastAPI REST endpoints and WebSocket communication flow.
"""
import os
import pytest

# Enforce virtual matching engine and local simulated feed during integration unit testing
os.environ["BROKER_PROVIDER"] = "virtual"
os.environ["MARKET_DATA_PROVIDER"] = "simulation"

from fastapi.testclient import TestClient
from app.core.config import settings
from app.main import app, ledger, engine

# Pin a deterministic token for the test session (settings are read from env at import).
settings.admin_token = "test-token"
AUTH = {"X-Auth-Token": "test-token"}


@pytest.fixture(autouse=True)
def reset_state():
    """Reset ledger and orders before each test."""
    ledger.reset(10000.0)
    engine.orders.clear()


def test_health_endpoint():
    with TestClient(app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


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


def test_websocket_flow():
    with TestClient(app) as client:
        # Unauthenticated handshake must be rejected.
        with pytest.raises(Exception):
            with client.websocket_connect("/ws") as websocket:
                websocket.receive_json()

        with client.websocket_connect("/ws?token=test-token") as websocket:
            # First message should be PORTFOLIO_SNAPSHOT
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
