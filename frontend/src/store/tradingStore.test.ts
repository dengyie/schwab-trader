import { describe, it, expect, beforeEach } from 'vitest';
import { useTradingStore } from './tradingStore';
import { useWalletStore } from './walletStore';
import { useMarketStore } from './marketStore';

describe('TradingStore - Order Freeze & Cancellation', () => {
  beforeEach(() => {
    useWalletStore.getState().resetWallet();
    useWalletStore.getState().grantInitialFunds();
    useTradingStore.setState({ orders: [], positions: new Map(), ocoOrders: [] });
    useMarketStore.setState({
      dataConfidence: {
        level: 'live',
        canTrade: true,
        canTrustMetrics: true,
        lastLiveTime: Date.now(),
        degradedSince: 0,
        details: {
          wsConnected: true,
          sequenceContinuous: true,
          latencyOk: true,
          updateFrequencyOk: true,
          queueHealthy: true,
        },
      },
      metrics: {
        mid: '500.00',
        spread: '0.05',
        spreadBps: 1,
        spreadStatus: 'normal',
        depthImbalance: 0,
        microprice: '500.00',
        vwap5m: '500.00',
        vwap15m: '500.00',
        realizedVol15m: 0.01,
        realizedVol1h: 0.01,
        high24h: '510.00',
        low24h: '490.00',
        volume24h: '100000',
      },
    });
  });

  it('correctly freezes balance on trailing stop and unfreezes on cancellation without locking funds', async () => {
    const wallet = useWalletStore.getState();
    const usdBefore = wallet.getBalance('USD');
    expect(usdBefore).toBeDefined();
    expect(parseFloat(usdBefore!.available)).toBeGreaterThan(0);
    expect(parseFloat(usdBefore!.frozen)).toBe(0);

    const trading = useTradingStore.getState();
    // Create buy trailing stop: 10 SPY @ $500 mid * 1.1 = $5500 freeze
    const order = trading.createTrailingStopOrder({
      symbol: 'SPY',
      side: 'buy',
      quantity: '10',
      trailingType: 'amount',
      trailingValue: '5.00',
    });

    expect(order).toBeDefined();
    expect(order!.clientOrderId).toBeDefined();

    // Verify wallet frozen balance is > 0
    const usdDuring = useWalletStore.getState().getBalance('USD');
    expect(parseFloat(usdDuring!.frozen)).toBeGreaterThan(0);

    // Cancel order
    const cancelled = await useTradingStore.getState().cancelOrder(order!.clientOrderId);
    expect(cancelled).toBe(true);

    const updatedOrder = useTradingStore.getState().orders.find(o => o.clientOrderId === order!.clientOrderId);
    expect(updatedOrder?.status).toBe('cancelled');

    // Verify wallet frozen balance is cleanly reset to 0
    const usdAfter = useWalletStore.getState().getBalance('USD');
    expect(parseFloat(usdAfter!.frozen)).toBe(0);
    expect(parseFloat(usdAfter!.available)).toBe(parseFloat(usdBefore!.available));
  });

  it('correctly calculates proportional unfreezing for partially filled orders without magic numbers', async () => {
    const wallet = useWalletStore.getState();
    const usdBefore = wallet.getBalance('USD');
    const orderId = 'test_partially_filled_order';
    const freezeAmount = '5000.00000000';

    // Freeze $5000
    wallet.freezeBalance('USD', freezeAmount, orderId, 'order');
    expect(parseFloat(wallet.getBalance('USD')!.frozen)).toBe(5000);

    // Order was 10 units, 4 already filled, 6 remaining
    useTradingStore.setState({
      orders: [
        {
          clientOrderId: orderId,
          symbol: 'SPY',
          side: 'buy',
          type: 'limit',
          price: '500.00',
          quantity: '10',
          filledQty: '4',
          avgPrice: '500.00',
          status: 'open',
          createdAt: Date.now(),
          updatedAt: Date.now(),
          fills: [],
          frozenAmount: freezeAmount,
          frozenAsset: 'USD',
        },
      ],
    });

    // Cancel remaining order: should unfreeze exactly 6/10 * 5000 = $3000
    const cancelled = await useTradingStore.getState().cancelOrder(orderId);
    expect(cancelled).toBe(true);

    const usdAfter = useWalletStore.getState().getBalance('USD');
    // Frozen should now be 5000 - 3000 = 2000
    expect(parseFloat(usdAfter!.frozen)).toBe(2000);
  });

  it('hydrates and updates backend orders correctly via fetchOrders', async () => {
    const mockBackendOrders = [
      {
        order_id: 'backend_ord_1',
        symbol: 'NVDA',
        side: 'BUY',
        order_type: 'LIMIT',
        quantity: 5,
        price: 200.0,
        status: 'OPEN',
        filled_quantity: 0,
        filled_avg_price: 0,
        created_at: new Date(1700000000000).toISOString(),
        updated_at: new Date(1700000000000).toISOString(),
      },
      {
        order_id: 'backend_ord_2',
        symbol: 'AAPL',
        side: 'SELL',
        order_type: 'MARKET',
        quantity: 10,
        price: null,
        status: 'FILLED',
        filled_quantity: 10,
        filled_avg_price: 230.5,
        created_at: new Date(1700000001000).toISOString(),
        updated_at: new Date(1700000001000).toISOString(),
      },
    ];

    const originalFetch = globalThis.fetch;
    globalThis.fetch = async () => ({
      ok: true,
      json: async () => mockBackendOrders,
    } as any);

    try {
      useTradingStore.setState({ orders: [] });
      await useTradingStore.getState().fetchOrders();

      const orders = useTradingStore.getState().orders;
      expect(orders).toHaveLength(2);

      const ord1 = orders.find(o => o.backendId === 'backend_ord_1');
      expect(ord1).toBeDefined();
      expect(ord1?.symbol).toBe('NVDA');
      expect(ord1?.side).toBe('buy');
      expect(ord1?.type).toBe('limit');
      expect(ord1?.price).toBe('200');
      expect(ord1?.quantity).toBe('5');
      expect(ord1?.status).toBe('open');

      const ord2 = orders.find(o => o.backendId === 'backend_ord_2');
      expect(ord2).toBeDefined();
      expect(ord2?.symbol).toBe('AAPL');
      expect(ord2?.side).toBe('sell');
      expect(ord2?.status).toBe('filled');
      expect(ord2?.filledQty).toBe('10');
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});
