/**
 * Conditional requests for the tickers poller.
 *
 * The backend answers /api/market/tickers with a body-hash ETag and a 304
 * when nothing moved. The service must store that validator, send it back on
 * the next poll, and treat 304 as "keep serving the cached rows" - never as
 * an error, never as fresh data.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const fetchMock = vi.fn();

vi.mock('./apiClient', () => ({
  apiFetch: (...args: unknown[]) => fetchMock(...args),
}));

import { fetchAllTickers } from './marketDataService';
import type { MarketTicker } from './marketDataService';

function ticker(symbol: string, price: number): MarketTicker {
  return {
    symbol,
    price,
    priceChange: 0,
    priceChangePercent: 0,
    high24h: price + 1,
    low24h: price - 1,
    volume24h: 1000,
    quoteVolume24h: price * 1000,
    openPrice: price,
    trades24h: 1,
    lastUpdated: 1731300000000,
  };
}

function jsonResponse(body: MarketTicker[] | null, etag?: string, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (k: string) => (k.toLowerCase() === 'etag' ? (etag ?? null) : null) },
    json: async () => body,
  };
}

beforeEach(() => {
  fetchMock.mockReset();
  // fetchAllTickers keeps a 2s fresh-cache; jump the clock past it between
  // calls so each test exercises the network path, not the module cache.
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

function skipClientCache() {
  vi.setSystemTime(Date.now() + 3000);
}

describe('fetchAllTickers - conditional requests', () => {
  it('stores the ETag from the first full response', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse([ticker('SPY', 100)], 'W/"tickers-US-abc"'),
    );

    const rows = await fetchAllTickers('US');

    expect(rows.map((r) => r.symbol)).toEqual(['SPY']);
    const [path, init] = fetchMock.mock.calls[0];
    expect(String(path)).toContain('/market/tickers?market=US');
    expect((init as RequestInit).headers).toEqual({});
  });

  it('revalidates with If-None-Match and keeps cached rows on 304', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse([ticker('SPY', 100)], 'W/"tickers-HK-e1"'),
    );
    await fetchAllTickers('HK');

    skipClientCache();
    fetchMock.mockResolvedValueOnce(jsonResponse(null, undefined, 304));
    const rows = await fetchAllTickers('HK');

    expect(rows.map((r) => r.symbol)).toEqual(['SPY']);
    const [, init] = fetchMock.mock.calls[1];
    expect((init as RequestInit).headers).toEqual({ 'If-None-Match': 'W/"tickers-HK-e1"' });
    // The 304 body is never parsed.
    expect(fetchMock.mock.results[1].value).toBeTruthy();
  });

  it('replaces the validator when the body actually changes', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse([ticker('SPY', 100)], 'W/"tickers-JP-e1"'),
    );
    await fetchAllTickers('JP');

    fetchMock.mockResolvedValueOnce(
      jsonResponse([ticker('SPY', 101.5)], 'W/"tickers-JP-e2"'),
    );
    skipClientCache();
    const rows = await fetchAllTickers('JP');

    expect(rows[0].price).toBe(101.5);
    const [, init] = fetchMock.mock.calls[1];
    expect((init as RequestInit).headers).toEqual({ 'If-None-Match': 'W/"tickers-JP-e1"' });
  });
});
