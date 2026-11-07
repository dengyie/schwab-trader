import { describe, it, expect } from 'vitest';
import {
  marketCodeOfSymbol,
  normalizeTicker,
  priceLimitPctForSymbol,
  tickSizeForPrice,
} from '../store/marketsStore';
import { formatMoney, currencySymbol, roundToTick } from '../utils/money';
import type { MarketDef } from '../types/markets';

const MARKETS: MarketDef[] = [
  {
    code: 'US', name: 'United States', currency: 'USD', timezone: 'America/New_York',
    exchange: 'NYSE', sessions: [{ start: '09:30', end: '16:00' }], lot_size: 1,
    settlement: 'T+0', suffix: '', price_limit_pct: null, decimals: 2, catalog: ['SPY'],
  },
  {
    code: 'HK', name: 'Hong Kong', currency: 'HKD', timezone: 'Asia/Hong_Kong',
    exchange: 'HKEX', sessions: [{ start: '09:30', end: '12:00' }], lot_size: 100,
    settlement: 'T+0', suffix: '.HK', price_limit_pct: null, decimals: 2, catalog: ['0700.HK'],
  },
  {
    code: 'JP', name: 'Japan', currency: 'JPY', timezone: 'Asia/Tokyo',
    exchange: 'TSE', sessions: [{ start: '09:00', end: '11:30' }], lot_size: 100,
    settlement: 'T+0', suffix: '.T', price_limit_pct: null, decimals: 0, catalog: ['7203.T'],
  },
  {
    code: 'CN', name: 'China', currency: 'CNY', timezone: 'Asia/Shanghai',
    exchange: 'SSE', sessions: [{ start: '09:30', end: '11:30' }], lot_size: 100,
    settlement: 'T+1', suffix: '.SS', price_limit_pct: 10, decimals: 2, catalog: ['600519.SS'],
  },
];

describe('marketCodeOfSymbol', () => {
  it('identifies every supported venue by suffix', () => {
    expect(marketCodeOfSymbol('SPY', MARKETS)).toBe('US');
    expect(marketCodeOfSymbol('0700.HK', MARKETS)).toBe('HK');
    expect(marketCodeOfSymbol('7203.T', MARKETS)).toBe('JP');
    expect(marketCodeOfSymbol('600519.SS', MARKETS)).toBe('CN');
    expect(marketCodeOfSymbol('000001.SZ', MARKETS)).toBe('CN');
  });

  it('folds .SH onto the China market', () => {
    expect(marketCodeOfSymbol('600519.SH', MARKETS)).toBe('CN');
  });

  it('is case insensitive and falls back to US', () => {
    expect(marketCodeOfSymbol('0700.hk', MARKETS)).toBe('HK');
    expect(marketCodeOfSymbol('MSFT', MARKETS)).toBe('US');
  });

  it('works before the market list has loaded', () => {
    expect(marketCodeOfSymbol('0700.HK', [])).toBe('HK');
    expect(marketCodeOfSymbol('7203.T', [])).toBe('JP');
  });
});

describe('tickSizeForPrice', () => {
  it('uses US cent ticks and sub-penny under a dollar', () => {
    expect(tickSizeForPrice('US', 150)).toBe(0.01);
    expect(tickSizeForPrice('US', 0.5)).toBe(0.0001);
  });

  it('follows the HKEX tier table', () => {
    expect(tickSizeForPrice('HK', 0.1)).toBe(0.001);
    expect(tickSizeForPrice('HK', 3)).toBe(0.01);
    expect(tickSizeForPrice('HK', 419.8)).toBe(0.2);
    expect(tickSizeForPrice('HK', 4000)).toBe(2.0);
  });

  it('follows the TSE tier table', () => {
    // 与后端一致：上界不含，2500 < 3000 -> 1.0
    expect(tickSizeForPrice('JP', 2500)).toBe(1.0);
    expect(tickSizeForPrice('JP', 3338)).toBe(5.0);
    expect(tickSizeForPrice('JP', 20000)).toBe(10.0);
  });

  it('uses one cent for A shares', () => {
    expect(tickSizeForPrice('CN', 1453)).toBe(0.01);
  });
});

describe('priceLimitPctForSymbol', () => {
  it('is unbounded outside China', () => {
    expect(priceLimitPctForSymbol('SPY', MARKETS)).toBeNull();
    expect(priceLimitPctForSymbol('0700.HK', MARKETS)).toBeNull();
  });

  it('applies 10% on the main board and 20% on growth boards', () => {
    expect(priceLimitPctForSymbol('600519.SS', MARKETS)).toBe(10);
    expect(priceLimitPctForSymbol('688981.SS', MARKETS)).toBe(20);
    expect(priceLimitPctForSymbol('300750.SZ', MARKETS)).toBe(20);
  });
});

describe('normalizeTicker', () => {
  it('fills market and currency from the registry', () => {
    const row = normalizeTicker({ symbol: '0700.HK', price: 419.8 }, MARKETS);
    expect(row.market).toBe('HK');
    expect(row.currency).toBe('HKD');
    expect(row.price).toBe(419.8);
    expect(row.priceChangePercent).toBe(0);
  });

  it('keeps an explicit currency from the server', () => {
    const row = normalizeTicker({ symbol: 'SPY', price: 1, currency: 'USD' }, MARKETS);
    expect(row.currency).toBe('USD');
  });
});

describe('money formatting', () => {
  it('labels each currency instead of printing a bare dollar sign', () => {
    expect(formatMoney(419.8, 'HKD')).toBe('HK$419.80');
    expect(formatMoney(1453, 'CNY')).toBe('CN¥1,453.00');
    expect(formatMoney(738.435, 'USD')).toBe('$738.44');
  });

  it('drops decimals for yen', () => {
    expect(formatMoney(3338, 'JPY')).toBe('¥3,338');
  });

  it('signs gains and losses on request', () => {
    expect(formatMoney(5.005, 'USD', { signed: true })).toBe('+$5.01');
    expect(formatMoney(-2.19, 'USD', { signed: true })).toBe('-$2.19');
  });

  it('renders a placeholder for missing values', () => {
    expect(formatMoney(null, 'USD')).toBe('--');
    expect(formatMoney(undefined, 'HKD')).toBe('--');
  });

  it('falls back to the currency code for unknown currencies', () => {
    expect(currencySymbol('GBP')).toBe('GBP ');
  });
});

describe('roundToTick', () => {
  it('snaps a price to the venue tick without float noise', () => {
    expect(roundToTick(419.83, 0.2)).toBe(419.8);
    expect(roundToTick(419.9, 0.2)).toBe(420);
    expect(roundToTick(3337.4, 5)).toBe(3335);
    expect(roundToTick(1453.004, 0.01)).toBe(1453);
  });
});
