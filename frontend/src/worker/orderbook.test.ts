import { describe, it, expect, beforeEach } from 'vitest';
import { OrderBookManager } from './orderbook';

describe('OrderBookManager', () => {
  let manager: OrderBookManager;

  beforeEach(() => {
    manager = new OrderBookManager('BTCUSDT');
  });

  describe('snapshot application', () => {
    it('should apply snapshot correctly', () => {
      const snapshot = {
        lastUpdateId: 100,
        bids: [
          ['50000.00', '1.5'],
          ['49999.00', '2.0'],
          ['49998.00', '0.5'],
        ] as [string, string][],
        asks: [
          ['50001.00', '1.0'],
          ['50002.00', '1.5'],
          ['50003.00', '2.0'],
        ] as [string, string][],
      };

      manager.applySnapshot(snapshot);

      expect(manager.isInitialized).toBe(true);
      expect(manager.lastUpdateId).toBe(100);

      const orderBook = manager.getOrderBook();
      expect(orderBook.bids).toHaveLength(3);
      expect(orderBook.asks).toHaveLength(3);
      expect(orderBook.bids[0]?.price).toBe('50000.00');
      expect(orderBook.asks[0]?.price).toBe('50001.00');
    });

    it('should ignore zero quantity levels', () => {
      const snapshot = {
        lastUpdateId: 100,
        bids: [
          ['50000.00', '1.5'],
          ['49999.00', '0'], // Should be ignored
        ] as [string, string][],
        asks: [
          ['50001.00', '1.0'],
        ] as [string, string][],
      };

      manager.applySnapshot(snapshot);

      const orderBook = manager.getOrderBook();
      expect(orderBook.bids).toHaveLength(1);
    });
  });

  describe('delta processing', () => {
    beforeEach(() => {
      manager.applySnapshot({
        lastUpdateId: 100,
        bids: [['50000.00', '1.0']] as [string, string][],
        asks: [['50001.00', '1.0']] as [string, string][],
      });
    });

    it('should apply valid delta update', () => {
      const update = {
        e: 'depthUpdate',
        E: Date.now(),
        s: 'BTCUSDT',
        U: 101,
        u: 101,
        b: [['50000.00', '2.0']] as [string, string][],
        a: [['50001.00', '1.5']] as [string, string][],
      };

      const result = manager.processUpdate(update as never);

      expect(result.success).toBe(true);
      expect(result.needsResync).toBe(false);
      expect(manager.lastUpdateId).toBe(101);

      const orderBook = manager.getOrderBook();
      expect(orderBook.bids[0]?.quantity).toBe('2.0');
      expect(orderBook.asks[0]?.quantity).toBe('1.5');
    });

    it('should detect gap and trigger resync', () => {
      const update = {
        e: 'depthUpdate',
        E: Date.now(),
        s: 'BTCUSDT',
        U: 105, // Gap: expecting 101, got 105
        u: 105,
        b: [] as [string, string][],
        a: [] as [string, string][],
      };

      // Need multiple failures to trigger resync
      manager.processUpdate(update as never);
      manager.processUpdate(update as never);
      const result = manager.processUpdate(update as never);

      expect(result.success).toBe(false);
      expect(result.needsResync).toBe(true);
    });

    it('should remove level when quantity is zero', () => {
      const update = {
        e: 'depthUpdate',
        E: Date.now(),
        s: 'BTCUSDT',
        U: 101,
        u: 101,
        b: [['50000.00', '0']] as [string, string][], // Remove bid level
        a: [] as [string, string][],
      };

      manager.processUpdate(update as never);

      const orderBook = manager.getOrderBook();
      expect(orderBook.bids).toHaveLength(0);
    });
  });

  describe('derived metrics', () => {
    beforeEach(() => {
      manager.applySnapshot({
        lastUpdateId: 100,
        bids: [
          ['50000.00', '1.0'],
          ['49999.00', '2.0'],
          ['49998.00', '3.0'],
        ] as [string, string][],
        asks: [
          ['50001.00', '1.5'],
          ['50002.00', '2.5'],
          ['50003.00', '3.5'],
        ] as [string, string][],
      });
    });

    it('should calculate mid price', () => {
      const metrics = manager.getMetrics();
      const mid = parseFloat(metrics.mid);
      expect(mid).toBeCloseTo(50000.5, 2);
    });

    it('should calculate spread', () => {
      const metrics = manager.getMetrics();
      const spread = parseFloat(metrics.spread);
      expect(spread).toBeCloseTo(1.0, 2);
    });

    it('should calculate spread in basis points', () => {
      const metrics = manager.getMetrics();
      // Spread = 1, Mid = 50000.5
      // spreadBps = (1 / 50000.5) * 10000 ≈ 0.2
      expect(metrics.spreadBps).toBeGreaterThan(0);
      expect(metrics.spreadBps).toBeLessThan(1);
    });

    it('should calculate bid/ask imbalance', () => {
      const metrics = manager.getMetrics();
      // Bids: 1 + 2 + 3 = 6 (top 5, but only 3)
      // Asks: 1.5 + 2.5 + 3.5 = 7.5
      // Imbalance = (6 - 7.5) / (6 + 7.5) = -1.5 / 13.5 ≈ -0.11
      expect(metrics.bidAskImbalance).toBeLessThan(0);
    });

    it('should calculate liquidity score', () => {
      const metrics = manager.getMetrics();
      expect(metrics.liquidityScore).toBeGreaterThan(0);
      expect(metrics.liquidityScore).toBeLessThanOrEqual(100);
    });
  });

  describe('trade-only venues', () => {
    // HK / JP / CN publish a last price and no book. The book must stay empty
    // and the real print must still drive the price display -- the defect this
    // guards against was a mid price fabricated from a one-sided/absent book.
    it('shows the real print and no levels when the quote has no book', () => {
      manager.applyQuote({ symbol: '0700.HK', bid: 0, ask: 0, last: 402.5 });

      const orderBook = manager.getOrderBook();
      expect(orderBook.bids).toHaveLength(0);
      expect(orderBook.asks).toHaveLength(0);

      const metrics = manager.getMetrics();
      expect(parseFloat(metrics.mid)).toBeCloseTo(402.5, 6);
      expect(metrics.spread).toBe('0');
      expect(metrics.spreadBps).toBe(0);
    });

    it('ignores a quote with neither a book nor a print', () => {
      manager.applyQuote({ symbol: '0700.HK', bid: 0, ask: 0, last: 0 });
      expect(manager.getOrderBook().bids).toHaveLength(0);
      expect(manager.getOrderBook().asks).toHaveLength(0);
      expect(manager.getMetrics().mid).toBe('0');
    });
  });

  describe('live quote honesty', () => {
    it('applies only the real NBBO levels (no synthetic ladder)', () => {
      manager.applyQuote({ symbol: 'SPY', bid: 500.0, ask: 500.05, bid_size: 300, ask_size: 200 });

      const orderBook = manager.getOrderBook();
      expect(orderBook.bids).toHaveLength(1);
      expect(orderBook.asks).toHaveLength(1);
      expect(orderBook.bids[0]?.price).toBe('500.00');
      expect(orderBook.asks[0]?.price).toBe('500.05');
      expect(orderBook.bids[0]?.quantity).toBe('300');
      expect(orderBook.asks[0]?.quantity).toBe('200');
    });

    it('does not fabricate a trade from a quote', () => {
      manager.applyQuote({ symbol: 'SPY', bid: 500.0, ask: 500.05, last: 500.02 });
      const metrics = manager.getMetrics();
      expect(metrics.tradeIntensity).toBe(0);
      expect(metrics.vwap60s).toBe('0');
    });

    it('does not invent session high/low when the feed omits them', () => {
      manager.applyQuote({ symbol: 'SPY', bid: 500.0, ask: 500.05 });
      const metrics = manager.getMetrics();
      // Empty, not masked with the last price: the UI renders "—".
      expect(metrics.high24h).toBe('');
      expect(metrics.low24h).toBe('');
      expect(parseFloat(metrics.vol24h)).toBe(0);
    });

    it('maps the backend day range and prev close onto the session stats', () => {
      manager.applyQuote({
        symbol: 'SPY',
        bid: 500.0,
        ask: 500.05,
        last: 505.0,
        day_high: 507.0,
        day_low: 498.0,
        prev_close: 500.0,
        volume: 61_234_567,
      });
      const metrics = manager.getMetrics();
      expect(parseFloat(metrics.high24h)).toBeCloseTo(507.0, 2);
      expect(parseFloat(metrics.low24h)).toBeCloseTo(498.0, 2);
      expect(parseFloat(metrics.vol24h)).toBeCloseTo(61_234_567, 0);
      expect(parseFloat(metrics.priceChange24h)).toBeCloseTo(5.0, 2);
      expect(parseFloat(metrics.priceChangePercent24h)).toBeCloseTo(1.0, 2);
    });

    it('ingests real trades into VWAP and intensity', () => {
      manager.applyQuote({ symbol: 'SPY', bid: 500.0, ask: 500.05 });
      manager.addTrade(500.0, 10, Date.now());
      manager.addTrade(500.05, 30, Date.now());
      const metrics = manager.getMetrics();
      expect(metrics.tradeIntensity).toBe(2);
      expect(parseFloat(metrics.vwap60s)).toBeCloseTo((500.0 * 10 + 500.05 * 30) / 40, 4);
    });
  });

  describe('update timestamp semantics', () => {
    it('publishes a wall-clock epoch, never a monotonic counter', () => {
      manager.applySnapshot({
        lastUpdateId: 100,
        bids: [['50000.00', '1.0']] as [string, string][],
        asks: [['50001.00', '1.0']] as [string, string][],
      });

      const orderBook = manager.getOrderBook();
      const now = Date.now();

      // performance.now() is milliseconds since page load (~1e5 on a long-lived
      // tab). Treated as an epoch it renders 1970, which is what the UI showed.
      expect(orderBook.localUpdateTime).toBeGreaterThan(1e12);
      expect(orderBook.localUpdateTime).toBeLessThanOrEqual(now);
      expect(now - orderBook.localUpdateTime).toBeLessThan(5_000);
      expect(new Date(orderBook.localUpdateTime).getFullYear()).toBeGreaterThan(2020);
    });

    it('keeps staleness detection monotonic across repeated snapshots', () => {
      manager.applySnapshot({
        lastUpdateId: 100,
        bids: [['50000.00', '1.0']] as [string, string][],
        asks: [['50001.00', '1.0']] as [string, string][],
      });

      // Converting at the snapshot boundary must not corrupt internal state:
      // checkStale still compares against performance.now().
      expect(manager.checkStale()).toBe(false);
      manager.processUpdate({
        e: 'depthUpdate',
        E: Date.now(),
        s: 'BTCUSDT',
        U: 101,
        u: 101,
        b: [['50000.00', '2.0']] as [string, string][],
        a: [] as [string, string][],
      } as never);
      expect(manager.checkStale()).toBe(false);
      expect(manager.getOrderBook().lastUpdateId).toBe(101);
    });

    it('reports 0 before the first update instead of a bogus epoch', () => {
      const orderBook = manager.getOrderBook();
      expect(orderBook.localUpdateTime).toBe(0);
    });

    it('keeps the venue quote time after the local clock moves on', () => {
      manager.applyQuote({
        symbol: 'SPY',
        bid: 500,
        ask: 500.05,
        timestamp: '2026-11-06T21:00:00Z',
      });

      const orderBook = manager.getOrderBook();
      expect(orderBook.quoteTime).toBe(Date.parse('2026-11-06T21:00:00Z'));
      // The receive time stays a separate wall-clock value.
      expect(orderBook.localUpdateTime).toBeGreaterThan(1e12);
    });

    it('leaves quote time unset when the feed omits a timestamp', () => {
      manager.applyQuote({ symbol: 'SPY', bid: 500, ask: 500.05, last: 500.02 });
      expect(manager.getOrderBook().quoteTime).toBe(0);
    });
  });

  describe('stale detection', () => {
    it('should mark as stale when not initialized', () => {
      expect(manager.checkStale()).toBe(true);
    });

    it('should not be stale immediately after snapshot', () => {
      manager.applySnapshot({
        lastUpdateId: 100,
        bids: [['50000.00', '1.0']] as [string, string][],
        asks: [['50001.00', '1.0']] as [string, string][],
      });

      expect(manager.checkStale()).toBe(false);
    });
  });
});





