import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { useTradingStore } from './tradingStore';
import { useWalletStore } from './walletStore';
import { useMarketStore } from './marketStore';
import { useMarketsStore } from './marketsStore';

const originalFetch = globalThis.fetch;

let requested: Array<{ url: string; method: string; body: any }> = [];

function json(data: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => data } as any;
}

function book(bid: string, ask: string) {
  return {
    symbol: 'SPY',
    bids: [{ price: bid, quantity: '100' }],
    asks: [{ price: ask, quantity: '100' }],
  } as any;
}

function submission() {
  return requested.find((r) => r.url.endsWith('/orders') && r.method === 'POST');
}

beforeEach(() => {
  requested = [];
  useWalletStore.getState().resetWallet();
  useWalletStore.getState().syncFromBackendPortfolio({ cash: 100000, total_equity: 100000 });
  useTradingStore.setState({ orders: [], positions: new Map(), ocoOrders: [] });
  useMarketsStore.setState({ markets: [] as any, status: 'ready' });
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
    metrics: { mid: '100.00' } as any,
  });

  globalThis.fetch = async (url: any, init?: any) => {
    const target = String(url);
    const method = String(init?.method || 'GET').toUpperCase();
    requested.push({
      url: target,
      method,
      body: init?.body ? JSON.parse(init.body as string) : undefined,
    });

    if (target.endsWith('/orders') && method === 'POST') {
      return json({ order_id: 'ord_new', status: 'OPEN' });
    }
    if (target.endsWith('/orders') && method === 'GET') {
      return json([]);
    }
    if (target.endsWith('/portfolio/reset')) {
      return json({ status: 'reset' });
    }
    if (target.endsWith('/portfolio')) {
      return json({
        cash: 100000,
        total_equity: 100000,
        initial_capital: 200000,
        positions: {},
      });
    }
    return json({});
  };
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe('order submission', () => {
  it('sends US orders to the backend instead of running a local ledger', () => {
    const before = useWalletStore.getState().getBalance('USD')!;
    const order = useTradingStore.getState().createOrder({
      symbol: 'SPY',
      side: 'buy',
      type: 'market',
      quantity: '10',
    }, '500.00');

    expect(order).not.toBeNull();
    expect(order!.status).toBe('pending');

    const post = submission();
    expect(post).toBeDefined();
    expect(post!.body).toMatchObject({
      symbol: 'SPY',
      side: 'BUY',
      order_type: 'MARKET',
      quantity: 10,
      market: 'US',
    });
    expect(post!.body.price).toBeUndefined();

    const after = useWalletStore.getState().getBalance('USD')!;
    expect(after.frozen).toBe(before.frozen);
    expect(after.available).toBe(before.available);
  });

  it('rounds foreign orders to a whole board lot and tags the market', () => {
    useMarketsStore.setState({
      markets: [{
        code: 'HK',
        name: 'Hong Kong',
        currency: 'HKD',
        timezone: 'Asia/Hong_Kong',
        exchange: 'HKEX',
        sessions: [],
        lot_size: 100,
        settlement: 'T+0',
        suffix: '.HK',
        price_limit_pct: null,
        decimals: 2,
        catalog: ['0700.HK'],
      }] as any,
      status: 'ready',
    });

    const order = useTradingStore.getState().createOrder({
      symbol: '0700.HK',
      side: 'buy',
      type: 'limit',
      price: '419.80',
      quantity: '250',
    }, '419.80');

    expect(order).not.toBeNull();
    expect(submission()!.body).toMatchObject({
      symbol: '0700.HK',
      market: 'HK',
      quantity: 200,
      order_type: 'LIMIT',
      price: 419.8,
    });
  });

  it('rejects a quantity below one board lot without contacting the broker', () => {
    useMarketsStore.setState({
      markets: [{
        code: 'HK', name: 'Hong Kong', currency: 'HKD', timezone: 'Asia/Hong_Kong',
        exchange: 'HKEX', sessions: [], lot_size: 100, settlement: 'T+0', suffix: '.HK',
        price_limit_pct: null, decimals: 2, catalog: ['0700.HK'],
      }] as any,
      status: 'ready',
    });

    const order = useTradingStore.getState().createOrder({
      symbol: '0700.HK',
      side: 'buy',
      type: 'limit',
      price: '419.80',
      quantity: '50',
    }, '419.80');

    // 2026-11-16: refusals are typed blocks so the UI can name the real
    // cause (the old null was misreported as "insufficient balance").
    expect(order).toEqual({ blocked: 'lot_floor_zero' });
    expect(submission()).toBeUndefined();
  });
});

describe('client-side conditional orders', () => {
  it('keeps a stop local until it crosses, then submits a market order', () => {
    const order = useTradingStore.getState().createOrder({
      symbol: 'SPY',
      side: 'sell',
      type: 'stop_market',
      quantity: '10',
      triggerPrice: '90',
    }, '100.00');

    expect(order).not.toBeNull();
    expect(order!.status).toBe('open');
    expect(submission()).toBeUndefined();

    // Establish the reference price, then cross the trigger.
    useTradingStore.getState().evaluateOrderTriggers(book('95.00', '95.10'));
    expect(submission()).toBeUndefined();

    useTradingStore.getState().evaluateOrderTriggers(book('89.00', '89.10'));
    const post = submission();
    expect(post).toBeDefined();
    expect(post!.body).toMatchObject({
      symbol: 'SPY',
      side: 'SELL',
      order_type: 'MARKET',
      quantity: 10,
    });

    const triggered = useTradingStore.getState().orders.find((o) => o.clientOrderId === order!.clientOrderId);
    expect(triggered?.isTriggered).toBe(true);
  });
});

describe('OCO orders', () => {
  it('mirrors both legs to the venue at creation (limit leg + native stop_limit)', () => {
    globalThis.fetch = async (url: any, init?: any) => {
      const method = String(init?.method || 'GET').toUpperCase();
      requested.push({ url: String(url), method, body: init?.body ? JSON.parse(init.body as string) : undefined });
      if (String(url).endsWith('/orders') && method === 'POST') {
        return json({ order_id: `ord_${requested.filter((r) => r.method === 'POST').length}`, status: 'OPEN' });
      }
      if (String(url).endsWith('/orders') && method === 'GET') return json([]);
      if (String(url).endsWith('/portfolio')) {
        return json({ cash: 100000, total_equity: 100000, initial_capital: 200000, positions: {} });
      }
      return json({});
    };

    const oco = useTradingStore.getState().createOCOOrder({
      symbol: 'SPY',
      side: 'sell',
      quantity: '10',
      limitPrice: '110',
      stopPrice: '90',
      stopLimitPrice: '89',
    });

    expect(oco).not.toBeNull();
    // 2026-11-16: conditional orders rest at the venue — both OCO legs are
    // submitted immediately, the stop leg as a native stop_limit carrying
    // trigger + limit. The local trigger evaluation skips mirrored legs,
    // so no second order can fire locally.
    const posts = requested.filter((r) => r.url.endsWith('/orders') && r.method === 'POST');
    expect(posts).toHaveLength(2);
    const types = posts.map((p) => p.body.order_type).sort();
    expect(types).toEqual(['LIMIT', 'STOP']);
    const stopPost = posts.find((p) => p.body.order_type === 'STOP')!;
    expect(stopPost.body).toMatchObject({ stop_price: 90, price: 89 });
    const limitPost = posts.find((p) => p.body.order_type === 'LIMIT')!;
    expect(limitPost.body).toMatchObject({ price: 110, client_order_id: expect.any(String) });

    // A local trigger must NOT fire on top of the venue-mirrored legs.
    useTradingStore.getState().evaluateOrderTriggers(book('111.00', '111.10'));
    const postsAfterTrigger = requested.filter((r) => r.url.endsWith('/orders') && r.method === 'POST');
    expect(postsAfterTrigger).toHaveLength(2);
  });

  it('cascades a venue-filled OCO leg into a sibling cancel', async () => {
    // 2026-11-16 review P2 pin: a PARTIALLY filled leg must NOT cancel the
    // sibling — only a full fill does. Both scenarios share the setup.
    const makeRows = (tpStatus: string) => [
      { order_id: 'ord_tp', client_order_id: 'leg-tp', symbol: 'SPY', side: 'sell',
        order_type: 'limit', quantity: 10, price: 110, status: tpStatus },
      { order_id: 'ord_sl', client_order_id: 'leg-sl', symbol: 'SPY', side: 'sell',
        order_type: 'stop_limit', quantity: 10, stop_price: 90, status: 'OPEN' },
    ];
    const cascadeScenario = { rows: makeRows('FILLED') };
    globalThis.fetch = async (url: any, init?: any) => {
      const method = String(init?.method || 'GET').toUpperCase();
      requested.push({ url: String(url), method, body: init?.body ? JSON.parse(init.body as string) : undefined });
      if (String(url).endsWith('/orders') && method === 'GET') {
        return json(cascadeScenario.rows);
      }
      if (method === 'DELETE') return json({ status: 'CANCELED' });
      return json({});
    };

    useTradingStore.setState({
      orders: [
        { clientOrderId: 'leg-tp', backendId: 'ord_tp', symbol: 'SPY', side: 'sell',
          type: 'limit', price: '110', quantity: '10', filledQty: '0', avgPrice: '0',
          status: 'open', createdAt: Date.now(), updatedAt: Date.now(), fills: [],
          ocoGroupId: 'oco_x', ocoLinkedOrderId: 'leg-sl', isOcoOrder: true },
        { clientOrderId: 'leg-sl', backendId: 'ord_sl', symbol: 'SPY', side: 'sell',
          type: 'stop_limit', price: '89', quantity: '10', filledQty: '0', avgPrice: '0',
          status: 'open', createdAt: Date.now(), updatedAt: Date.now(), fills: [],
          triggerPrice: '90', ocoGroupId: 'oco_x', ocoLinkedOrderId: 'leg-tp', isOcoOrder: true },
      ],
      ocoOrders: [{
        ocoGroupId: 'oco_x', symbol: 'SPY', side: 'sell', quantity: '10',
        limitPrice: '110', stopPrice: '90', stopLimitPrice: '89', status: 'active',
        limitOrderId: 'leg-tp', stopOrderId: 'leg-sl', createdAt: Date.now(), updatedAt: Date.now(),
      }],
    });

    await useTradingStore.getState().fetchOrders();
    // The sibling cancel is fire-and-forget async: settle its DELETE round
    // trip before asserting.
    await new Promise((resolve) => setTimeout(resolve, 20));

    const del = requested.find((r) => r.method === 'DELETE');
    expect(del).toBeDefined();
    expect(del!.url).toContain('ord_sl');
    const sibling = useTradingStore.getState().orders.find((o) => o.backendId === 'ord_sl');
    expect(sibling?.status).toBe('cancelled');
    // The group derivation marks a group with a filled leg 'filled'
    // (fill takes priority over the triggered marker).
    expect(useTradingStore.getState().ocoOrders[0].status).toBe('filled');

    // Partial fill: the sibling must stay open (no cancel request at all).
    requested = [];
    cascadeScenario.rows = makeRows('PARTIALLY_FILLED');
    useTradingStore.setState({
      orders: [
        { clientOrderId: 'leg-tp', backendId: 'ord_tp', symbol: 'SPY', side: 'sell',
          type: 'limit', price: '110', quantity: '10', filledQty: '3', avgPrice: '0',
          status: 'open', createdAt: Date.now(), updatedAt: Date.now(), fills: [],
          ocoGroupId: 'oco_x', ocoLinkedOrderId: 'leg-sl', isOcoOrder: true },
        { clientOrderId: 'leg-sl', backendId: 'ord_sl', symbol: 'SPY', side: 'sell',
          type: 'stop_limit', price: '89', quantity: '10', filledQty: '0', avgPrice: '0',
          status: 'open', createdAt: Date.now(), updatedAt: Date.now(), fills: [],
          triggerPrice: '90', ocoGroupId: 'oco_x', ocoLinkedOrderId: 'leg-tp', isOcoOrder: true },
      ],
      ocoOrders: [{
        ocoGroupId: 'oco_x', symbol: 'SPY', side: 'sell', quantity: '10',
        limitPrice: '110', stopPrice: '90', stopLimitPrice: '89', status: 'active',
        limitOrderId: 'leg-tp', stopOrderId: 'leg-sl', createdAt: Date.now(), updatedAt: Date.now(),
      }],
    });
    await useTradingStore.getState().fetchOrders();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(requested.find((r) => r.method === 'DELETE')).toBeUndefined();
    expect(useTradingStore.getState().orders.find((o) => o.backendId === 'ord_sl')?.status).toBe('open');
  });
});

describe('broker reconciliation', () => {
  it('removes a phantom order the backend does not know', async () => {
    useTradingStore.setState({
      orders: [{
        clientOrderId: 'local_ghost',
        backendId: 'ord_ghost',
        symbol: '600519.SS',
        side: 'buy',
        type: 'limit',
        price: '1453',
        quantity: '100',
        filledQty: '100',
        avgPrice: '1453',
        status: 'filled',
        createdAt: 1,
        updatedAt: 1,
        fills: [],
      }],
    });

    await useTradingStore.getState().fetchOrders();
    expect(useTradingStore.getState().orders).toHaveLength(0);
  });

  it('cancels a broker-backed order only after the DELETE succeeds', async () => {
    useTradingStore.setState({
      orders: [{
        clientOrderId: 'c1', backendId: 'ord_x', symbol: 'SPY', side: 'buy', type: 'limit',
        price: '100', quantity: '10', filledQty: '0', avgPrice: '0', status: 'open',
        createdAt: 1, updatedAt: 1, fills: [],
      }],
    });

    const ok = await useTradingStore.getState().cancelOrder('c1');
    expect(ok).toBe(true);
    expect(requested.some((r) => r.url.includes('/orders/ord_x') && r.method === 'DELETE')).toBe(true);
    expect(useTradingStore.getState().orders.find((o) => o.clientOrderId === 'c1')?.status).toBe('cancelled');
  });

  it('does not report success when the broker rejects the cancellation', async () => {
    globalThis.fetch = async (url: any, init?: any) => {
      const target = String(url);
      if (String(init?.method || 'GET').toUpperCase() === 'DELETE') {
        return json({ detail: 'already filled' }, false, 409);
      }
      return json([]);
    };
    useTradingStore.setState({
      orders: [{
        clientOrderId: 'c2', backendId: 'ord_y', symbol: 'SPY', side: 'buy', type: 'limit',
        price: '100', quantity: '10', filledQty: '0', avgPrice: '0', status: 'open',
        createdAt: 1, updatedAt: 1, fills: [],
      }],
    });

    const ok = await useTradingStore.getState().cancelOrder('c2');
    expect(ok).toBe(false);
  });
});

describe('account reset', () => {
  it('resets the broker account using its reported initial capital', async () => {
    useTradingStore.setState({
      positions: new Map([['600519.SS', {
        symbol: '600519.SS',
        side: 'long',
        quantity: '100',
        avgEntryPrice: '1453',
        unrealizedPnl: '0',
        realizedPnl: '0',
        updatedAt: 1,
      }]]),
    });

    await useTradingStore.getState().resetAccount();

    const reset = requested.find((r) => r.url.endsWith('/portfolio/reset'));
    expect(reset?.method).toBe('POST');
    expect(reset?.body).toEqual({ initial_capital: 200000 });
    expect(useTradingStore.getState().positions.size).toBe(0);
  });
});

describe('unconfirmed order reconciliation', () => {
  it('sends client_order_id as an idempotency key', async () => {
    useTradingStore.getState().createOrder({
      symbol: 'SPY', side: 'buy', type: 'market', quantity: '5',
    }, '100.00');

    const post = submission();
    expect(post).toBeDefined();
    expect(post!.body.client_order_id).toEqual(expect.any(String));
  });

  it('reconciles against broker truth on a network error instead of marking rejected', async () => {
    // The submit POST dies mid-flight; the GET /orders afterwards reports the
    // order the backend actually received (echoing client_order_id).
    globalThis.fetch = async (url: any, init?: any) => {
      const target = String(url);
      const method = String(init?.method || 'GET').toUpperCase();
      if (target.endsWith('/orders') && method === 'POST') {
        throw new TypeError('connection dropped');
      }
      if (target.endsWith('/orders') && method === 'GET') {
        return json([
          {
            order_id: 'ord_ghost',
            client_order_id: 'client-uuid-777',
            symbol: 'SPY', side: 'buy', order_type: 'market',
            quantity: 5, status: 'OPEN',
          },
        ]);
      }
      if (target.endsWith('/portfolio')) {
        return json({ cash: 100000, total_equity: 100000, initial_capital: 200000, positions: {} });
      }
      return json({});
    };

    useTradingStore.setState({
      orders: [{
        clientOrderId: 'client-uuid-777', symbol: 'SPY', side: 'buy',
        type: 'market', price: null, quantity: '5', filledQty: '0',
        avgPrice: '0', status: 'pending', createdAt: Date.now(),
        updatedAt: Date.now(), fills: [],
      }],
    });

    await useTradingStore.getState().fetchOrders();
    const orders = useTradingStore.getState().orders;
    expect(orders).toHaveLength(1);
    expect(orders[0].backendId).toBe('ord_ghost');
    expect(orders[0].clientOrderId).toBe('client-uuid-777');
    expect(orders[0].status).not.toBe('rejected');
  });
});
