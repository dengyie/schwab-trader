# Schwab Trader Hub (美股虚拟操盘与盯盘中枢)

一套生产就绪、开箱即用的美股本地虚拟操盘、数据盯盘与量化/AI 模型特征系统。

* **前端**：依托 **TradingView Lightweight Charts** 渲染毫秒级实时 K 线，支持持仓均价线动态标注与一键买卖操作。
* **后端**：采用 **Python FastAPI 异步网关 + WebSocket Streamer**，直连嘉信证券（Charles Schwab）数据协议。
* **零风险虚拟撮合**：内置本地虚拟撮合与资产记账引擎（Virtual Matching & Ledger Engine），支持市价单、限价单、止损单撮合，先不投真钱、完全验证交易策略。
* **AI/量化模型天然适配**：提供标准的 DataFrame 特征导出与大语言模型 Agent Tool-Calling 上下文接口。

---

## 一、系统架构

```text
┌────────────────────────────────────────────────────────┐
│               前端看板 (TradingView Web)                │
│  • TradingView Lightweight Charts (K线、均线、盘口)     │
│  • 持仓成本线动态标注、实时浮动盈亏计算、一键买卖面板     │
└───────────────────────────▲────────────────────────────┘
                            │ 本地全双工 WebSocket (127.0.0.1:8000/ws)
┌───────────────────────────▼────────────────────────────┐
│            后端网关 (FastAPI 异步微服务)               │
│                                                        │
│  ┌──────────────────────┐    ┌──────────────────────┐  │
│  │ 本地虚拟撮合与记账   │    │ 嘉信/行情推流适配器  │  │
│  │ (Virtual Matcher)    │    │ (Schwab Streamer)    │  │
│  │ • 模拟持仓、盈亏计算 │    │ • 实时 Level 1 报价  │  │
│  │ • 限价单/止损单撮合  │    │ • 历史 1m/日 K 线    │  │
│  └──────────────────────┘    └──────────────────────┘  │
│                            │                           │
│  ┌─────────────────────────▼────────────────────────┐  │
│  │ AI & 模型特征适配器 (/api/market/model-context)   │  │
│  │ • 一键导出 Pandas/Polars 兼容特征矩阵 (RSI/SMA)  │  │
│  │ • 适配 LLM Agent Tool Use 标准 JSON 上下文       │  │
│  └──────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────┘
```

---

## 二、快速启动（一键运行）

系统支持全自动环境检测与依赖加载，只需执行：

```bash
cd schwab-trader
./start.sh
```

启动完成后：
* 📈 **Web 操盘工作台**：打开浏览器访问 `http://127.0.0.1:5173`
* 🔌 **交互式 API 文档 (Swagger)**：`http://127.0.0.1:8000/docs`
* ⚡ **WebSocket 推流通道**：`ws://127.0.0.1:8000/ws`

---

## 三、核心功能与使用指南

### 1. 虚拟操盘（Paper Trading）
* **初始资金**：默认给予 **$10,000.00** 虚拟本金（可通过顶部「Reset Capital」随时重置）。
* **市价买入/卖出**：按当前实时盘口 Ask（卖一价）买入、Bid（买一价）卖出，真实计算滑点与持仓均价。
* **图表持仓线**：买入后，TradingView K 线图上会自动绘制一条蓝色的**持仓均价线（Position Cost Line）**，持仓盈亏随着行情变动实时更新。
* **限价挂单与止损**：支持提交 Limit 单和 Stop 单，当行情价格穿过设定阈值时自动触发撮合。

### 2. 接入 AI 与量化模型
调用 `GET /api/market/model-context?symbol=SPY` 可直接获取规范的模型特征输入：
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
* **传统时序模型（LSTM / XGBoost）**：可调用 `ModelFeatureExporter.to_records(bars)` 转换为 Pandas DataFrame。
* **大语言模型（LLM Agent）**：上述输出格式可直接作为 Agent 的 Tool Return，用于判断买入、卖出或观望。

### 3. 如何切换为嘉信实盘交易
当你在虚拟操盘中验证策略成熟后，切换到嘉信真实下单非常简单：
1. 在 `schwab-trader/backend/app/core/config.py` 或环境变量中填入你的 `SCHWAB_APP_KEY` 和 `SCHWAB_APP_SECRET`；
2. 系统底层的交易接口抽象了 `BaseBroker`，修改配置将 `VirtualBroker` 替换为 `SchwabLiveBroker`，同样的买卖指令即会向嘉信官方实盘接口提交挂单。

---

## 四、自动化测试

项目内置完整的单元测试与端到端集成测试套件：

```bash
cd schwab-trader/backend
source .venv/bin/activate
PYTHONPATH=. pytest -v
```

测试覆盖：
* ✅ 资产记账与加权持仓成本算法（买入、减仓、平仓、盈亏计算）
* ✅ 限价单（Limit Order）与止损单（Stop Order）撮合逻辑
* ✅ 购买力不足（Insufficient Buying Power）越界拒绝拦截
* ✅ 嘉信 Streamer 原始报文与 REST K 线解析
* ✅ REST API 端点与全双工 WebSocket 收发集成测试
