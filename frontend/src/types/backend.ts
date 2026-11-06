export interface Bar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface Quote {
  symbol: string;
  bid: number;
  ask: number;
  last: number;
  volume: number;
  timestamp: string;
  bid_size?: number;
  ask_size?: number;
}

export interface BackendPosition {
  symbol: string;
  quantity: number;
  avg_cost: number;
  current_price: number;
  market_value: number;
  unrealized_pnl: number;
  unrealized_pnl_pct: number;
}

export interface Portfolio {
  cash: number;
  initial_capital: number;
  market_value: number;
  total_equity: number;
  realized_pnl: number;
  unrealized_pnl: number;
  total_pnl: number;
  total_pnl_pct: number;
  positions: Record<string, BackendPosition>;
}

export interface BackendOrder {
  order_id: string;
  symbol: string;
  side: 'BUY' | 'SELL';
  order_type: 'MARKET' | 'LIMIT' | 'STOP';
  quantity: number;
  price?: number;
  stop_price?: number;
  status: 'PENDING' | 'FILLED' | 'CANCELLED' | 'REJECTED';
  filled_quantity: number;
  filled_avg_price: number;
  created_at: string;
}

export interface MarketTrade {
  id: string;
  symbol: string;
  price: number;
  size: number;
  side: 'BUY' | 'SELL';
  timestamp: string;
}

export interface BackendOrderBookLevel {
  price: number;
  size: number;
  depthPct: number;
}

export interface OrderBookData {
  bids: BackendOrderBookLevel[];
  asks: BackendOrderBookLevel[];
  spread: number;
  spreadPct: number;
}

export interface TechnicalIndicators {
  sma_20?: number;
  ema_50?: number;
  rsi_14?: number;
  trend?: 'BULLISH' | 'BEARISH' | 'NEUTRAL';
  volatility?: number;
}

export interface ModelContext {
  symbol: string;
  /** Broker's live quote block (bid/ask/last/spread/timestamp). */
  real_time_quote?: {
    bid?: number | null;
    ask?: number | null;
    last?: number;
    spread?: number | null;
    timestamp?: string | null;
  };
  /** @deprecated legacy alias; prefer real_time_quote */
  latest_quote?: Quote;
  technical_indicators?: TechnicalIndicators;
  current_position?: {
    shares: number;
    avg_cost: number;
    unrealized_pnl: number;
    unrealized_pnl_pct: number;
  } | null;
  historical_bars_count?: number;
  timestamp?: string;
}

export interface SystemStatus {
  status?: string;
  market_data_provider?: string;
  broker_provider?: string;
  alpaca_feed?: string;
  latency_ms?: number;
}
