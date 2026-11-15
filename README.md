<!-- markdownlint-disable-file MD033 MD045 -->
<div align="center">

# Schwab Trader Hub

**Production-Ready US Equities Paper Trading, Real-Time Market Telemetry & Quantitative Model Feature Gateway**

[![License](https://img.shields.io/badge/license-MIT-green.svg?style=flat-square)](LICENSE)
[![Python Version](https://img.shields.io/badge/Python-3.11+-3776AB.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Frontend](https://img.shields.io/badge/Frontend-React_18_%7C_TypeScript_%7C_Vite-61DAFB.svg?style=flat-square&logo=react&logoColor=white)](frontend/)
[![Charts](https://img.shields.io/badge/Charts-TradingView_Lightweight_Charts-blue.svg?style=flat-square)](https://tradingview.github.io/lightweight-charts/)
[![Architecture](https://img.shields.io/badge/Architecture-Full--Duplex_WebSocket_%7C_Virtual_Ledger-orange.svg?style=flat-square)](backend/)
[![Tests](https://img.shields.io/badge/tests-477%20passed-brightgreen.svg?style=flat-square)]()

[English](./README.md) · [简体中文](./README.zh-CN.md) · [Core Features](#core-features) · [Architecture](#system-architecture) · [Quick Start](#quick-start) · [Trading & Quant](#paper-trading--quant-model-integration) · [Testing](#testing--quality-assurance)

<br>

<p align="center">
  <b>Terminates real capital risk, broker rate-limit penalties, and fragile data pipelines when validating equity trading strategies.</b><br>
  Engineered with high-throughput FastAPI async WebSocket streamer backends, TradingView Lightweight Charts rendering sub-millisecond price action, and a deterministic virtual matching ledger.<br>
  Delivers realistic slippage and position cost tracking, limit and stop-loss order matching, unified multi-market Alpaca and Yahoo Finance data adapters, Pandas/Polars feature matrix exports, and structured LLM tool-calling contexts.
</p>

</div>

---

## Motivation & Architecture Comparison

When developing algorithmic models, AI trading agents, or intraday execution strategies, quants and developers often struggle with expensive live-trading mistakes, strict broker rate-limit bans, erratic tick streams, and data formats incompatible with modern LLMs:

| Evaluation Dimension | Schwab Trader Hub Platform | Static Backtesting Scripts | Closed-Source Paper Platforms |
| :--- | :--- | :--- | :--- |
| **Market Perception** | Native TradingView charts, dynamic position cost lines, real-time PnL, and live order books | Post-hoc static plot generation; cannot experience dynamic spreads or book depth | Outdated desktop UIs lacking responsive modern web interfaces |
| **Matching & Ledger** | Local virtual matching state machine with realistic Ask/Bid slippage and buying-power validation | Unrealistic execution assumptions ignoring order-book liquidity dynamics | Opaque proprietary ledgers preventing custom initial capital or rules |
| **Data Stream Hygiene**| Full-duplex WebSocket channel with de-duplicated subscriptions and auto-reconnect | Aggressive REST polling that quickly triggers strict broker IP bans | Requires external proprietary proxy relays with privacy risks |
| **AI & Quant Readiness**| Native Pandas/Polars matrix exports and standardized JSON contexts for LLM Tool-Calling | Fragmented indicator calculations requiring custom data munging pipelines | Closed ecosystems with difficult data extraction pipelines |
| **Live Broker Migration**| Clean `BaseBroker` abstraction enabling seamless transition to Charles Schwab live API | Paper trading logic coupled tightly to mock fixtures, requiring complete rewrites | Vendor locked into specific paid brokerage partners |

---

## Core Features

| Module | Feature Highlight | Technical Implementation & Advantage |
| :--- | :--- | :--- |
| **Sub-Millisecond Charts** | TradingView Lightweight Charts| Renders real-time minute and daily bars with dynamic position cost line overlays and floating PnL synchronization. |
| **Full-Duplex Streamer** | High-throughput WebSocket | FastAPI backend provides native WebSockets pushing Level 1 quotes, tick streams, and automated self-healing reconnections. |
| **Virtual Matching Engine**| Realistic ledger accounting | Executes market orders against real-time Ask/Bid quotes, supports Limit and Stop orders, and enforces strict buying-power guards. |
| **AI Model Context** | LLM Tool-Calling interface | Dedicated `/api/market/model-context` endpoint outputs real-time quotes, technical indicators (RSI/SMA), and positions in clean JSON. |
| **Time-Series Matrix** | Quant pipeline exports | `ModelFeatureExporter` transforms raw bars and order-book snapshots directly into Pandas or Polars DataFrame matrices. |
| **Multi-Market Adapters** | Schwab, Alpaca & Yahoo | Decoupled adapter architecture supporting Charles Schwab live streamers, Alpaca market feeds, and Yahoo Finance historical bars. |
| **Broker Abstraction Layer**| Zero-friction live trading | Implements `BaseBroker` contract; transition from `VirtualBroker` to `SchwabLiveBroker` by updating credentials in config. |
| **Full-Stack Test Coverage**| 247 automated regression tests | 169 backend unit/integration tests and 78 frontend Vitest specs covering ledger math, order books, and WebSocket scrubbing. |

---

## System Architecture

```text
┌────────────────────────────────────────────────────────────────────────┐
│                     Trading Dashboard (React 18 + Vite)                │
│  ├─ TradingView Lightweight Charts (Bars · Depth · Dynamic Cost Line)  │
│  ├─ Floating PnL Calculator · Equity Curve · Instant Order Entry Panel │
│  └─ WebSocket Client (Real-time telemetry and order execution reports) │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Local Full-Duplex WebSocket (ws://127.0.0.1:8000/ws)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│               Backend Gateway (FastAPI Async Microservice)             │
│  ┌──────────────────────┐ ┌──────────────────────┐ ┌────────────────┐  │
│  │ Virtual Matcher      │ │ Market Data Streamer │ │ Model Context  │  │
│  │ • Position & PnL     │ │ WebSocket Streamer   │ │ • RSI/SMA Hub  │  │
│  │ • Limit/Stop Orders  │ │ • Level 1 Quotes     │ │ • LLM JSON API │  │
│  │ • Slippage Guard     │ │ • 1m / Daily Bars    │ │ • Polars Matrix│  │
│  └──────────┬───────────┘ └──────────┬───────────┘ └────────┬───────┘  │
│             │                        │                      │          │
│             ▼                        ▼                      ▼          │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │           Unified Broker & Market Data Adapter Architecture      │  │
│  │  BaseBroker · AlpacaMarketFeedService · YahooFinanceAdapter      │  │
│  └──────────────────────────────────┬───────────────────────────────┘  │
└─────────────────────────────────────┼──────────────────────────────────┘
                                      │ Inbound Market Data & Outbound Live Orders
        ┌─────────────────────────────┴─────────────────────────────┐
        ▼                                                           ▼
┌────────────────────────────────┐          ┌────────────────────────────────┐
│   Charles Schwab Brokerage     │          │   Alpaca / Yahoo Finance       │
│   OAuth 2.0 · Live Trading API │          │   Multi-Market Data Streaming  │
└────────────────────────────────┘          └────────────────────────────────┘
```

---

## Quick Start

### 1. Automated Launch

The repository includes an all-in-one startup script that checks environments and initializes services:

```bash
# Clone the repository
git clone https://github.com/dengyie/schwab-trader.git
cd schwab-trader

# Run the automated launch script — sim mode by default (synthetic quotes,
# in-process virtual broker, no Alpaca session, login token: local-dev-token)
./start.sh

# Real Alpaca IEX stream + paper-trading account
# (credentials from ALPACA_API_KEY/ALPACA_API_SECRET/ADMIN_TOKEN, or
#  ~/.config/schwab-trader/prod.env). Stop the production service first:
# two processes sharing one key evict each other's stream every few seconds.
./start.sh --real
```

### 2. Manual Launch

#### Backend Service

Requirements: **Python 3.11+**:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Sim mode needs no credentials; without these the app refuses to boot in
# alpaca mode (it would have nothing to authenticate with).
export MARKET_DATA_PROVIDER=simulation BROKER_PROVIDER=virtual ADMIN_TOKEN=local-dev-token

# Start FastAPI server on 127.0.0.1:8000
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

#### Frontend Dashboard

Requirements: **Node.js 20+**:

```bash
cd frontend
npm install

# Start Vite development server on http://127.0.0.1:5173
npm run dev
```

Once running:
- **Trading Dashboard**: `http://127.0.0.1:5173`
- **Interactive API Documentation (Swagger)**: `http://127.0.0.1:8000/docs`
- **WebSocket Streaming Channel**: `ws://127.0.0.1:8000/ws`

---

## Paper Trading & Quant Model Integration

### 1. Virtual Trading Operations

- **Initial Capital**: Initialized with **$10,000.00** in virtual buying power, resettable via the top navigation bar.
- **Market Orders**: Fills immediately at the real-time Ask for buys and Bid for sells, accounting for live slippage.
- **Dynamic Cost Line**: Opening a position projects a blue weighted average cost line across TradingView charts, updating alongside floating PnL.
- **Limit & Stop Orders**: Supports Limit and Stop orders triggered automatically when live market prices breach defined thresholds.

### 2. AI Agent & Quantitative Modeling

Query `GET /api/market/model-context?symbol=SPY` to retrieve structured market context:

```json
{
  "symbol": "SPY",
  "real_time_quote": {
    "bid": 585.45,
    "ask": 585.55,
    "last": 585.50,
    "spread": 0.10
  },
  "technical_indicators": {
    "latest_close": 585.50,
    "rsi_14": 56.42,
    "sma_20": 584.20,
    "trend": "BULLISH"
  },
  "current_position": {
    "shares": 10,
    "avg_cost": 580.00,
    "unrealized_pnl": 55.00,
    "unrealized_pnl_pct": 0.95
  }
}
```

- **LLM Agent Tool-Calling**: Formatted as a standard tool return, enabling coding or autonomous agents to evaluate trends and execute orders.
- **Time-Series Deep Learning (LSTM / XGBoost)**: Call `ModelFeatureExporter.to_records(bars)` in Python to export structured feature matrices into Pandas or Polars DataFrames.

### 3. Transition to Charles Schwab Live Trading

Once trading strategies have been validated:
1. Provide your `SCHWAB_APP_KEY` and `SCHWAB_APP_SECRET` in environment variables or `backend/app/core/config.py`;
2. The core system abstracts trading operations under `BaseBroker`. Replace `VirtualBroker` with `SchwabLiveBroker` to direct buy and sell orders straight to Charles Schwab endpoints.

---

## Testing & Quality Assurance

The repository includes comprehensive automated test suites covering matching algorithms, WebSocket scrubbing, and edge cases:

```bash
# 1. Run backend unit and integration test suite (169 passing)
cd backend
source .venv/bin/activate
pytest -v

# 2. Run frontend Vitest test suite (78 passing)
cd ../frontend
npm test
```

Test coverage includes:
- Virtual ledger calculations and weighted average cost tracking (Buy, Partial Sell, Close, PnL);
- Limit order and stop-loss order matching state machines;
- Buying power verification and overdraft rejection;
- Charles Schwab streamer frame decoding and REST historical bar aggregation;
- Sensitive credential scrubbing in WebSocket connection URLs.

---

## Contributing & License

Contributions are welcome. Please ensure all tests pass prior to submitting changes:

```bash
cd backend && pytest && cd ../frontend && npm test
```

Licensed under the [MIT License](LICENSE).
