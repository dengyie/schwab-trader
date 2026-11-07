import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { apiFetch } from '../services/apiClient';
import type { MarketCode, MarketDef, MarketTickerRow } from '../types/markets';

/**
 * 市场注册表与行情路由。
 *
 * 前端只认识 Yahoo 规范符号（0700.HK、7203.T、600519.SS），后缀唯一决定
 * 市场，因此本 store 负责「符号 -> 市场 -> 币种/每手/最小跳动/涨跌停」。
 * tick 规则与后端 app/core/markets.py 保持一致。
 */

const SUFFIX_MARKET: Record<string, MarketCode> = {
  '.HK': 'HK',
  '.T': 'JP',
  '.SS': 'CN',
  '.SH': 'CN',
  '.SZ': 'CN',
};

export const MARKET_CODES: MarketCode[] = ['US', 'HK', 'JP', 'CN'];

interface TickTier {
  /** 上界（不含）：price < max 时取该档 tick。 */
  max: number;
  tick: number;
}

// 与后端 app/core/markets.py 的分档完全一致（上界不含）。
// 美股：< $1 用 0.0001，其余 0.01。
const US_TICK_TIERS: TickTier[] = [
  { max: 1, tick: 0.0001 },
  { max: Infinity, tick: 0.01 },
];
// 港交所最小跳动单位分档（HKD）。
const HK_TICK_TIERS: TickTier[] = [
  { max: 0.25, tick: 0.001 },
  { max: 0.5, tick: 0.005 },
  { max: 10.0, tick: 0.01 },
  { max: 20.0, tick: 0.02 },
  { max: 100.0, tick: 0.05 },
  { max: 200.0, tick: 0.1 },
  { max: 500.0, tick: 0.2 },
  { max: 1000.0, tick: 0.5 },
  { max: 2000.0, tick: 1.0 },
  { max: 5000.0, tick: 2.0 },
  { max: Infinity, tick: 5.0 },
];
// 东证最小跳动单位分档（JPY）。
const JP_TICK_TIERS: TickTier[] = [
  { max: 3000.0, tick: 1.0 },
  { max: 5000.0, tick: 5.0 },
  { max: 30000.0, tick: 10.0 },
  { max: 100000.0, tick: 50.0 },
  { max: 3000000.0, tick: 100.0 },
  { max: Infinity, tick: 1000.0 },
];

const DEFAULT_CURRENCY: Record<MarketCode, string> = {
  US: 'USD',
  HK: 'HKD',
  JP: 'JPY',
  CN: 'CNY',
};

function tierTick(tiers: TickTier[], price: number): number {
  if (!(price > 0)) return tiers[0]!.tick;
  // 上界不含：price < max 命中该档，与后端 tick_size_for 一致。
  return tiers.find((t) => price < t.max)?.tick ?? tiers[tiers.length - 1]!.tick;
}

/** 从符号解析市场代码；无法识别时回退到 US。 */
export function marketCodeOfSymbol(symbol: string, markets: MarketDef[]): MarketCode {
  const upper = (symbol || '').toUpperCase();
  for (const code of MARKET_CODES) {
    const def = markets.find((m) => m.code === code);
    if (def?.suffix && upper.endsWith(def.suffix)) return code;
  }
  for (const suffix of Object.keys(SUFFIX_MARKET)) {
    if (upper.endsWith(suffix)) return SUFFIX_MARKET[suffix]!;
  }
  return 'US';
}

/** 参考价下的最小跳动（纯函数，供表单步进/校验复用）。 */
export function tickSizeForPrice(market: MarketCode, price: number): number {
  if (market === 'HK') return tierTick(HK_TICK_TIERS, price);
  if (market === 'JP') return tierTick(JP_TICK_TIERS, price);
  if (market === 'CN') return 0.01;
  return tierTick(US_TICK_TIERS, price);
}

/** A 股涨跌停：创业板/科创板 20%，其余 10%。 */
export function priceLimitPctForSymbol(symbol: string, markets: MarketDef[]): number | null {
  const market = marketCodeOfSymbol(symbol, markets);
  const def = markets.find((m) => m.code === market);
  if (!def?.price_limit_pct) return null;
  const code = (symbol || '').toUpperCase().split('.')[0] ?? '';
  if (market === 'CN' && ['688', '689', '300', '301'].some((p) => code.startsWith(p))) {
    return 20;
  }
  return def.price_limit_pct;
}

/** 从 /api/market/tickers 规范化行情行。 */
export function normalizeTicker(row: Partial<MarketTickerRow> & { symbol: string }, markets: MarketDef[]): MarketTickerRow {
  const market = marketCodeOfSymbol(row.symbol, markets);
  const def = markets.find((m) => m.code === market);
  return {
    price: 0,
    priceChange: 0,
    priceChangePercent: 0,
    high24h: 0,
    low24h: 0,
    volume24h: 0,
    quoteVolume24h: 0,
    openPrice: 0,
    trades24h: 0,
    lastUpdated: 0,
    isStale: false,
    ...row,
    market,
      currency: row.currency || def?.currency || DEFAULT_CURRENCY[market] || 'USD',
  } as MarketTickerRow;
}

interface MarketsState {
  markets: MarketDef[];
  fxRates: Record<string, number>;
  /** 自选列表当前展示的市场。 */
  activeMarket: MarketCode;
  status: 'idle' | 'loading' | 'ready' | 'error';

  loadMarkets: () => Promise<void>;
  setActiveMarket: (code: MarketCode) => void;

  marketOf: (symbol: string) => MarketDef | undefined;
  currencyOf: (symbol: string) => string;
  lotSizeFor: (symbol: string) => number;
  tickSizeFor: (symbol: string, price: number) => number;
  priceLimitPctFor: (symbol: string) => number | null;
  usdPerUnit: (currency: string) => number;
}

function fallbackMarket(code: MarketCode): MarketDef {
  return {
    code,
    name: code,
    currency: DEFAULT_CURRENCY[code] ?? 'USD',
    timezone: '',
    exchange: '',
    sessions: [],
    lot_size: code === 'US' ? 1 : 100,
    settlement: 'T+0',
    suffix: '',
    price_limit_pct: null,
    decimals: 2,
    catalog: [],
  };
}

export const useMarketsStore = create<MarketsState>()(
  persist(
    (set, get) => ({
      markets: [],
      fxRates: { USD: 1.0 },
      activeMarket: 'US',
      status: 'idle',

      loadMarkets: async () => {
        if (get().status === 'loading') return;
        set({ status: 'loading' });
        try {
          const response = await apiFetch('/markets');
          if (!response.ok) throw new Error(`markets ${response.status}`);
          const payload = await response.json();
          set({
            markets: payload.markets ?? [],
            fxRates: payload.fx_rates ?? { USD: 1.0 },
            status: 'ready',
          });
        } catch (err) {
          console.error('Failed to load markets:', err);
          set({ status: 'error' });
        }
      },

      setActiveMarket: (code) => set({ activeMarket: code }),

      marketOf: (symbol) => {
        const { markets } = get();
        if (markets.length === 0) return undefined;
        const code = marketCodeOfSymbol(symbol, markets);
        return markets.find((m) => m.code === code) ?? fallbackMarket(code);
      },

      currencyOf: (symbol) => get().marketOf(symbol)?.currency ?? 'USD',

      lotSizeFor: (symbol) => get().marketOf(symbol)?.lot_size ?? 1,

      tickSizeFor: (symbol, price) =>
        tickSizeForPrice(marketCodeOfSymbol(symbol, get().markets), price),

      priceLimitPctFor: (symbol) => priceLimitPctForSymbol(symbol, get().markets),

      usdPerUnit: (currency) => get().fxRates[(currency || 'USD').toUpperCase()] ?? 1,
    }),
    {
      name: 'markets_state',
      version: 1,
      partialize: (state) => ({ activeMarket: state.activeMarket }),
    },
  ),
);
