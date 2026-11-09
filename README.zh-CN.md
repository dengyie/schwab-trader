<!-- markdownlint-disable-file MD033 MD045 -->
<div align="center">

# Schwab Trader Hub

**生产就绪级美股虚拟操盘中枢、实时行情推流与量化/AI 模型特征工程网关**

[![License](https://img.shields.io/badge/license-MIT-green.svg?style=flat-square)](LICENSE)
[![Python Version](https://img.shields.io/badge/Python-3.11+-3776AB.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Frontend](https://img.shields.io/badge/Frontend-React_18_%7C_TypeScript_%7C_Vite-61DAFB.svg?style=flat-square&logo=react&logoColor=white)](frontend/)
[![Charts](https://img.shields.io/badge/Charts-TradingView_Lightweight_Charts-blue.svg?style=flat-square)](https://tradingview.github.io/lightweight-charts/)
[![Architecture](https://img.shields.io/badge/Architecture-Full--Duplex_WebSocket_%7C_Virtual_Ledger-orange.svg?style=flat-square)](backend/)
[![Tests](https://img.shields.io/badge/tests-247%20passed-brightgreen.svg?style=flat-square)]()

[English](./README.md) · [简体中文](./README.zh-CN.md) · [核心特性](#核心特性) · [系统架构](#系统架构全景) · [快速上手](#快速上手) · [操盘与量化](#虚拟操盘与量化模型接入) · [测试套件](#质量保证与自动化测试)

<br>

<p align="center">
  <b>彻底告别实盘资金试错风险、券商接口频控封锁以及量化策略回测与前端展示割裂的痛点。</b><br>
  严格基于 FastAPI 异步网关、全双工 WebSocket 推流与 TradingView 毫秒级轻量级图表构建，直连嘉信证券与 Alpaca 协议标准。<br>
  提供真实滑点与加权持仓成本虚拟撮合、限价单与止损单状态机、Pandas/Polars 兼容时序特征矩阵导出，以及面向大语言模型 Agent 的结构化上下文接口。
</p>

</div>

---

## 痛点与架构对比

在研发与验证美股量化模型、AI 操盘 Agent 或日内交易策略时，量化开发者常面临无处安全演练、实盘接口权限门槛高、真实行情推流抖动以及特征格式无法直连 LLM 的阻碍：

| 评估维度 | Schwab Trader Hub 操盘中枢 | 简易脚本与静态回测框架 | 商业闭源模拟软件 |
| :--- | :--- | :--- | :--- |
| **交互与行情感知** | TradingView 原生毫米级 K 线流、动态成本线标注、实时浮动盈亏与盘口档位 | 仅能生成事后静态图表，无法体验动态盘口与实时点差变化 | 界面陈旧笨重，不提供现代 Web 界面与响应式支持 |
| **撮合与资产记账** | 本地虚拟撮合与资产记账状态机，真实模拟 Ask/Bid 滑点与购买力越界拦截 | 理想化成交假设，严重忽略真实买卖盘口深度与流动性冲击 | 记账规则黑盒，无法自定义调整初始本金与杠杆规则 |
| **推流与网络开销** | 全双工 WebSocket 双向通信，支持行情订阅降噪与动态重连 | 盲目轮询 REST 接口，极易触发券商 API 严格的频控封锁 | 强制依赖其专有客户端中继，存在网络延迟与隐私泄露 |
| **AI 与量化生态适配**| 原生输出 Pandas/Polars 特征矩阵，提供 LLM Tool-Calling 规范 JSON 上下文 | 特征转换逻辑零散，需自行拼装清洗时序指标数据 | 闭门造车，数据导出困难，无法打通现代 AI Agent 工具链 |
| **实盘无缝平滑迁移**| 交易接口严格抽象 `BaseBroker`，修改配置即可一键切换为嘉信实盘 | 虚拟盘代码与实盘 API 严重耦合，二次重构工作量巨大 | 强行绑定特定券商或收费托管通道 |

---

## 核心特性

| 模块 | 特性亮点 | 技术实现与工程优势 |
| :--- | :--- | :--- |
| **轻量级专业级图表**| TradingView 原生渲染 | 基于 TradingView Lightweight Charts 实时渲染分钟/日 K 线，支持持仓成本线动态投影与浮动盈亏联动。 |
| **全双工行情推流** | 毫秒级低延迟推流 | FastAPI 后端建立原生 WebSocket 通道，提供实时 Level 1 深度盘口、成交流与断线自愈重连。 |
| **真实撮合记账引擎**| 资产与购买力严密管控 | 内置本地虚拟撮合引擎，按当前 Ask 买入、Bid 卖出，支持市价单、限价单与止损单；严密拦截购买力不足等越界指令。 |
| **AI 模型特征上下文**| LLM Tool-Calling 就绪 | 专设 `/api/market/model-context` 端点，一键导出包含实时报价、技术指标（RSI/SMA）与持仓状态的标准 JSON 上下文。 |
| **时序矩阵导出适配**| 量化数据管道标准化 | 提供 `ModelFeatureExporter`，将行情流与 Bar 序列直接转换为标准的 Pandas / Polars DataFrame 特征矩阵。 |
| **多源市场数据适配**| 嘉信/Alpaca/Yahoo | 抽象解耦的底层适配层，支持接入 Charles Schwab 实时 Streamer、Alpaca 行情馈送与 Yahoo Finance 历史数据。 |
| **高可信实盘抽象层**| 策略无损切换实盘 | 严格抽象 `BaseBroker` 统一交易契约，策略在 `VirtualBroker` 验证成熟后，配置 API 密钥即可无缝切入实盘下单。 |
| **全栈双端离线自测**| 247 项严密回归测试 | 包含 169 项后端单元/集成测试与 78 项前端 Vitest 隔离测试，覆盖撮合算法、网络脱敏与状态机。 |

---

## 系统架构全景

```text
┌────────────────────────────────────────────────────────────────────────┐
│                        前端操盘看板 (React 18 + Vite)                  │
│  ├─ TradingView Lightweight Charts (K线 · 盘口 · 均线 · 动态成本线)    │
│  ├─ 实时浮动盈亏计算器 · 资产折线图 · 一键买卖快速下单面板            │
│  └─ WebSocket 客户端通道 (实时侦听行情与订单回报)                      │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ 本地全双工 WebSocket (ws://127.0.0.1:8000/ws)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│               后端调度网关 (FastAPI 异步微服务架构)                    │
│  ┌──────────────────────┐ ┌──────────────────────┐ ┌────────────────┐  │
│  │ 本地虚拟撮合与记账   │ │ 行情推流与订阅中枢   │ │ AI 模型特征引擎│  │
│  │ Virtual Matching     │ │ WebSocket Streamer   │ │ Model Context  │  │
│  │ • 模拟持仓/浮动盈亏  │ │ • 实时 Level 1 报价  │ │ • RSI/SMA 矩阵 │  │
│  │ • 限价单/止损单撮合  │ │ • 历史 1m/日 K 线    │ │ • LLM JSON 契约│  │
│  └──────────┬───────────┘ └──────────┬───────────┘ └────────┬───────┘  │
│             │                        │                      │          │
│             ▼                        ▼                      ▼          │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │           统一 Broker 与 Market Data 适配器抽象层                │  │
│  │  BaseBroker · AlpacaMarketFeedService · YahooFinanceAdapter      │  │
│  └──────────────────────────────────┬───────────────────────────────┘  │
└─────────────────────────────────────┼──────────────────────────────────┘
                                      │ 行情接收与实盘指令透传
        ┌─────────────────────────────┴─────────────────────────────┐
        ▼                                                           ▼
┌────────────────────────────────┐          ┌────────────────────────────────┐
│   嘉信理财 (Charles Schwab)    │          │   Alpaca / Yahoo Finance       │
│   OAuth 2.0 鉴权 · 真实实盘接口 │          │   多市场实时盘口与历史 K 线流   │
└────────────────────────────────┘          └────────────────────────────────┘
```

---

## 快速上手

### 1. 自动化一键启动

系统提供一键检查与依赖加载脚本：

```bash
# 克隆代码仓库
git clone https://github.com/dengyie/schwab-trader.git
cd schwab-trader

# 执行自动化一键启动脚本
./start.sh
```

### 2. 手动分步启动

#### 后端服务

系统要求：**Python 3.11+**：

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 启动 FastAPI 服务网关 (监听 127.0.0.1:8000)
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

#### 前端工作台

系统要求：**Node.js 20+**：

```bash
cd frontend
npm install

# 启动 Vite 开发服务器 (监听 http://127.0.0.1:5173)
npm run dev
```

启动完成后可直接访问：
- **Web 操盘工作台**：`http://127.0.0.1:5173`
- **交互式 API 文档 (Swagger)**：`http://127.0.0.1:8000/docs`
- **全双工 WebSocket 推流端点**：`ws://127.0.0.1:8000/ws`

---

## 虚拟操盘与量化模型接入

### 1. 虚拟操盘规则与操作

- **初始本金**：系统默认注入 **$10,000.00** 虚拟可用资金，支持在顶部操作栏一键重置。
- **市价委托**：买入按当前实时盘口 Ask（卖一价）即时成交，卖出按 Bid（买一价）成交，真实扣减滑点。
- **动态持仓均价线**：开仓后，TradingView 图表自动绘制一条蓝色加权持仓成本线，随浮动盈亏动态更新。
- **限价与止损挂单**：支持提交 Limit 与 Stop 挂单，撮合引擎侦听到行情穿越阈值时触发自动成交。

### 2. 接入 AI Agent 与量化模型

通过调用 `GET /api/market/model-context?symbol=SPY`，可直接获取标准的行情与技术面特征：

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

- **LLM Agent Tool-Calling**：上述 JSON 格式已适配标准工具调用返回值，Agent 可依据趋势与持仓做出交易决策。
- **时序深度学习模型 (LSTM / XGBoost)**：在 Python 代码中直接调用 `ModelFeatureExporter.to_records(bars)` 即可无缝导出为标准 DataFrame。

### 3. 平滑切换至嘉信实盘

当交易策略在虚拟盘中完成充分回测与验证后：
1. 在环境变量或 `backend/app/core/config.py` 中填入你的 `SCHWAB_APP_KEY` 与 `SCHWAB_APP_SECRET`；
2. 系统底层的交易接口抽象了 `BaseBroker`，修改配置将 `VirtualBroker` 替换为 `SchwabLiveBroker`，同样的指令即可直连嘉信官方实盘交易网关。

---

## 质量保证与自动化测试

项目全栈配备覆盖边界场景、高并发数据推送与资产撮合的自动化测试：

```bash
# 1. 运行后端测试套件 (169 项通过)
cd backend
source .venv/bin/activate
pytest -v

# 2. 运行前端 Vitest 测试套件 (78 项通过)
cd ../frontend
npm test
```

测试范围全面覆盖：
- 资产记账与加权持仓成本算法（买入、减仓、平仓、盈亏结算）；
- 限价单（Limit Order）与止损单（Stop Order）撮合逻辑；
- 购买力不足（Insufficient Buying Power）越界拒绝拦截；
- 嘉信 Streamer 报文解析与 REST K 线流式聚合；
- WebSocket 查询参数敏感凭证脱敏与连接自愈。

---

## 参与贡献与许可证

欢迎提交 Issue 与 Pull Request！在提交代码变更前，请确保全量测试套件执行通过：

```bash
cd backend && pytest && cd ../frontend && npm test
```

本项目采用 [MIT License](LICENSE) 开源协议。
