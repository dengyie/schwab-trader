// ===== 多市场支持：市场定义 / 交易时段 / 行情快照 =====
//
// 后端 /api/markets 是唯一事实来源。符号采用 Yahoo 规范形式
// （SPY、0700.HK、7203.T、600519.SS），市场由后缀唯一确定，
// 因此前端无需把「市场」再塞进每一个 symbol 字符串里。

export type MarketCode = 'US' | 'HK' | 'JP' | 'CN' | string;

/** 一个连续交易时段（本地时间，HH:MM）。 */
export interface MarketSession {
  start: string;
  end: string;
}

/** 会话状态：开盘 / 午休 / 盘前盘后 / 已收盘。 */
export type SessionState = 'open' | 'break' | 'pre' | 'closed';

export interface MarketClockInfo {
  market: MarketCode;
  is_open: boolean;
  session: SessionState;
  timezone: string;
  currency: string;
  now_local: string | null;
  next_open: string | null;
  next_close: string | null;
}

export interface MarketDef {
  code: MarketCode;
  name: string;
  currency: string;
  timezone: string;
  exchange: string;
  sessions: MarketSession[];
  /** 每手股数，下单数量必须是它的整数倍。 */
  lot_size: number;
  settlement: string;
  suffix: string;
  /** 涨跌停百分比（A 股 10/20），无限制市场为 null。 */
  price_limit_pct: number | null;
  decimals: number;
  catalog: string[];
  clock?: MarketClockInfo;
}

export interface MarketsPayload {
  base_currency: string;
  /** 1 单位外币兑换成基准货币的汇率。 */
  fx_rates: Record<string, number>;
  markets: MarketDef[];
}

/** 行情列表行（/api/market/tickers?market=）。 */
export interface MarketTickerRow {
  symbol: string;
  market: MarketCode;
  currency: string;
  price: number;
  priceChange: number;
  priceChangePercent: number;
  high24h: number;
  low24h: number;
  volume24h: number;
  quoteVolume24h: number;
  openPrice: number;
  trades24h: number;
  lastUpdated: number;
}
