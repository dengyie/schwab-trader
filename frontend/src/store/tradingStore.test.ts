import { describe, it, expect, beforeEach } from 'vitest';
import { useTradingStore } from './tradingStore';
import { useWalletStore } from './walletStore';
import { useMarketStore } from './marketStore';
import { useMarketsStore } from './marketsStore';

describe('TradingStore - Order Freeze & Cancellation', () => {
  beforeEach(() => {
    useWalletStore.getState().resetWallet();
    // Fund the account the way production does: a broker portfolio snapshot.
    // (The old createEmptyBalances placeholder grant was removed.)
    useWalletStore.getState().syncFromBackendPortfolio({ cash: 100000, total_equity: 100000 });
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

describe('TradingStore - non-US orders are delegated to the backend', () => {
  let requested: Array<{ url: string; body: any }> = [];

  beforeEach(() => {
    requested = [];
    useWalletStore.getState().resetWallet();
    useWalletStore.getState().syncFromBackendPortfolio({ cash: 100000, total_equity: 100000 });
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
      metrics: { mid: '419.80' } as any,
    });
    // 市场注册表：港股每手 100 股、币种 HKD。
    useMarketsStore.setState({
      markets: [
        {
          code: 'HK', name: 'Hong Kong', currency: 'HKD', timezone: 'Asia/Hong_Kong',
          exchange: 'HKEX', sessions: [], lot_size: 100, settlement: 'T+0', suffix: '.HK',
          price_limit_pct: null, decimals: 2, catalog: ['0700.HK'],
        },
      ],
      status: 'ready',
    });

    globalThis.fetch = async (url: any, init?: any) => {
      requested.push({ url: String(url), body: init?.body ? JSON.parse(init.body) : undefined });
      return { ok: true, json: async () => ({ order_id: 'hk_1', status: 'OPEN' }) } as any;
    };
  });

  it('sends the market hint and lot-rounded quantity instead of a USD order', () => {
    const order = useTradingStore.getState().createOrder({
      symbol: '0700.HK',
      side: 'buy',
      type: 'limit',
      price: '419.80',
      quantity: '250',
    }, '419.80');

    expect(order).toBeDefined();
    const submit = requested.find((r) => r.url.includes('/orders'));
    expect(submit).toBeDefined();
    expect(submit!.body.market).toBe('HK');
    expect(submit!.body.symbol).toBe('0700.HK');
    // 250 股向上/向下都必须是 100 的整数倍 -> 200
    expect(submit!.body.quantity).toBe(200);
  });

  it('does not freeze the USD wallet for a foreign-currency order', () => {
    const before = useWalletStore.getState().getBalance('USD');
    useTradingStore.getState().createOrder({
      symbol: '0700.HK', side: 'buy', type: 'limit', price: '419.80', quantity: '100',
    }, '419.80');

    const after = useWalletStore.getState().getBalance('USD');
    // 港币订单由后端虚拟撮合引擎结算，本地 USD 钱包不能被占用。
    expect(parseFloat(after!.frozen)).toBe(parseFloat(before!.frozen));
    expect(parseFloat(after!.available)).toBe(parseFloat(before!.available));
  });

  it('rejects a quantity below one board lot', () => {
    const order = useTradingStore.getState().createOrder({
      symbol: '0700.HK', side: 'buy', type: 'limit', price: '419.80', quantity: '50',
    }, '419.80');

    expect(order).toBeNull();
    expect(requested.find((r) => r.url.includes('/orders'))).toBeUndefined();
  });
});
