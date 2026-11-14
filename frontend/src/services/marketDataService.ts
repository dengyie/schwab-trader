/**
 * Market Data Service
 * Professional Terminal Edition - US Equities & FastAPI Backend Integration
 */

import { handleApiError, logError } from '../utils/errorHandler';
import { apiFetch } from './apiClient';

export interface MarketTicker {
  symbol: string;
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
  /** 所属市场（US/HK/JP/CN），由后端按符号标注。 */
  market?: string;
  /** 该标的的计价币种。 */
  currency?: string;
}

export interface MarketSparkline {
  symbol: string;
  prices: number[];
}

export interface MarketIndicators {
  symbol: string;
  rsi14: number | null;
  momentum: 'bullish' | 'bearish' | 'neutral';
  volatility: number;
}

export const POPULAR_SYMBOLS = [
  'SPY', 'QQQ', 'AAPL', 'MSFT', 'NVDA', 'TSLA',
  'AMD', 'AMZN', 'GOOGL', 'META', 'IWM', 'COIN',
  'PLTR', 'NFLX',
];

const CATEGORY_MAP: Record<string, string[]> = {
  'Indices': ['SPY', 'QQQ', 'IWM'],
  'Mag 7': ['AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMZN', 'GOOGL', 'META'],
  'Semis': ['NVDA', 'AMD'],
  'Growth': ['PLTR', 'COIN', 'NFLX'],
};

export const GET_SYMBOLS_BY_CATEGORY = (cat: string) => CATEGORY_MAP[cat] || POPULAR_SYMBOLS;

interface TickerCacheEntry {
  fetchedAt: number;
  rows: Map<string, MarketTicker>;
  etag?: string; // 后端按渲染体哈希的弱校验器，命中即 304
}

// 每个市场单独缓存：切换市场时不能拿到上一个市场的行情。
const tickerCaches: Map<string, TickerCacheEntry> = new Map();
const CACHE_TTL = 2000;

/** 拉取某个市场的行情列表。market 为空时回退到后端默认（美股）。 */
export async function fetchAllTickers(market?: string | null): Promise<MarketTicker[]> {
  const key = (market || 'US').toUpperCase();
  const cached = tickerCaches.get(key);
  const now = Date.now();
  if (cached && now - cached.fetchedAt < CACHE_TTL && cached.rows.size > 0) {
    return Array.from(cached.rows.values());
  }

  try {
    const path = `/market/tickers?market=${encodeURIComponent(key)}`;
    const condHeaders: Record<string, string> = {};
    if (cached?.etag) condHeaders['If-None-Match'] = cached.etag;
    const response = await apiFetch(path, { headers: condHeaders });
    if (response.status === 304 && cached) {
      // 行情未变：只续鲜客户端缓存，不重解析整段列表。
      tickerCaches.set(key, { ...cached, fetchedAt: now });
      return Array.from(cached.rows.values());
    }
    if (!response.ok) throw response;
    const data: MarketTicker[] = await response.json();

    const rows = new Map<string, MarketTicker>();
    data.forEach((t) => rows.set(t.symbol, t));
    const etag = response.headers.get('etag') || undefined;
    tickerCaches.set(key, { fetchedAt: now, rows, etag });

    return data.sort((a, b) => b.volume24h - a.volume24h);
  } catch (error) {
    logError(handleApiError(error));
    return cached ? Array.from(cached.rows.values()) : [];
  }
}

/** 全部市场并发聚合：行情页/移动端行情页的全市场视图数据源。
 *  每市场仍走各自的条件请求缓存；单市场失败不拖垮整页（回退该市场缓存，
 *  再不行就空数组——别的市场照常渲染）。 */
export async function fetchAllMarketsTickers(
  markets: readonly string[] = ['US', 'HK', 'JP', 'CN'],
): Promise<MarketTicker[]> {
  const results = await Promise.all(
    markets.map(async (m) => {
      try {
        return await fetchAllTickers(m);
      } catch {
        return [];
      }
    }),
  );
  return results.flat().sort((a, b) => b.quoteVolume24h - a.quoteVolume24h);
}

export async function fetchSparkline(symbol: string): Promise<MarketSparkline | null> {
  try {
    const response = await apiFetch(`/market/bars?symbol=${encodeURIComponent(symbol)}`);
    if (!response.ok) return null;
    const data = await response.json();
    return { symbol, prices: data.slice(-24).map((k: any) => parseFloat(k.close)) };
  } catch {
    return null;
  }
}

export async function calculateIndicators(symbol: string): Promise<MarketIndicators | null> {
  try {
    const response = await apiFetch(`/market/model-context?symbol=${encodeURIComponent(symbol)}`);
    if (response.ok) {
      const data = await response.json();
      const tech = data.technical_indicators || {};
      const aiScore = data.ai_quant_score || {};
      return {
        symbol,
        rsi14: tech.rsi_14 ?? 50,
        momentum: aiScore.signal === 'BULLISH' ? 'bullish' : aiScore.signal === 'BEARISH' ? 'bearish' : 'neutral',
        volatility: tech.volatility ?? 1.5,
      };
    }

    // Fallback: calculate from bars
    const barsResp = await apiFetch(`/market/bars?symbol=${encodeURIComponent(symbol)}`);
    if (!barsResp.ok) return null;
    const bars = await barsResp.json();
    const closes = bars.map((k: any) => parseFloat(k.close));
    const rsi = calculateRSI(closes, 14);
    let momentum: 'bullish' | 'bearish' | 'neutral' = 'neutral';
    if (rsi) {
      if (rsi > 60) momentum = 'bullish';
      else if (rsi < 40) momentum = 'bearish';
    }

    return {
      symbol,
      rsi14: rsi,
      momentum,
      volatility: calculateVolatility(closes),
    };
  } catch {
    return null;
  }
}

/** 从一份收盘价序列客户端推导指标（与 calculateIndicators 的 bars 回退
 *  路径同一算法）。行情页用它把每标的 2 个请求（bars + model-context）
 *  砍成 1 个：model-context 在后端还要打 quote + broker.get_position。 */
export function deriveIndicators(symbol: string, closes: number[]): MarketIndicators | null {
  const rsi = calculateRSI(closes, 14);
  if (rsi === null) return null;
  let momentum: 'bullish' | 'bearish' | 'neutral' = 'neutral';
  if (rsi > 60) momentum = 'bullish';
  else if (rsi < 40) momentum = 'bearish';
  return { symbol, rsi14: rsi, momentum, volatility: calculateVolatility(closes) };
}

function calculateRSI(closes: number[], period: number): number | null {
  if (closes.length < period + 1) return null;
  let gains = 0, losses = 0;
  for (let i = 1; i <= period; i++) {
    const current = closes[i];
    const prev = closes[i - 1];
    if (current !== undefined && prev !== undefined) {
      const diff = current - prev;
      if (diff > 0) gains += diff; else losses -= diff;
    }
  }
  let avgGain = gains / period, avgLoss = losses / period;
  for (let i = period + 1; i < closes.length; i++) {
    const current = closes[i];
    const prev = closes[i - 1];
    if (current !== undefined && prev !== undefined) {
      const diff = current - prev;
      avgGain = (avgGain * (period - 1) + (diff > 0 ? diff : 0)) / period;
      avgLoss = (avgLoss * (period - 1) + (diff < 0 ? -diff : 0)) / period;
    }
  }
  return avgLoss === 0 ? 100 : 100 - (100 / (1 + avgGain / avgLoss));
}

function calculateVolatility(closes: number[]): number {
  const returns = [];
  for (let i = 1; i < closes.length; i++) {
    const current = closes[i];
    const prev = closes[i - 1];
    if (current !== undefined && prev !== undefined && prev !== 0) {
      returns.push((current - prev) / prev);
    }
  }
  if (returns.length === 0) return 0;
  const mean = returns.reduce((a, b) => a + b, 0) / returns.length;
  const variance = returns.reduce((a, b) => Math.pow(b - mean, 2), 0) / returns.length;
  return Math.sqrt(variance) * 100;
}

export function formatVolume(v: number): string {
  if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
  if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if (v >= 1e3) return (v / 1e3).toFixed(2) + 'K';
  return v.toFixed(2);
}

export function formatPrice(p: number): string {
  if (p >= 1000) return p.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (p >= 1) return p.toFixed(2);
  return p.toFixed(4);
}

export function parseSymbol(s: string) {
  return { base: s, quote: 'USD' };
}
