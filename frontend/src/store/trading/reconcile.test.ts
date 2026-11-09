import { describe, expect, it } from 'vitest';
import type { OCOOrder, PaperOrder, Position } from '../../types/trading';
import { isUnsentLocalOrder, reconcileOcoGroups, reconcileOrders, reconcilePositions } from './reconcile';

function position(symbol: string, overrides: Partial<Position> = {}): Position {
  return {
    symbol,
    side: 'long',
    quantity: '100',
    avgEntryPrice: '10',
    unrealizedPnl: '0',
    realizedPnl: '0',
    updatedAt: 1,
    ...overrides,
  };
}

function order(clientOrderId: string, overrides: Partial<PaperOrder> = {}): PaperOrder {
  return {
    clientOrderId,
    symbol: 'SPY',
    side: 'buy',
    type: 'limit',
    price: '100',
    quantity: '10',
    filledQty: '0',
    avgPrice: '0',
    status: 'open',
    createdAt: 1_700_000_000_000,
    updatedAt: 1_700_000_000_000,
    fills: [],
    ...overrides,
  };
}

describe('reconcilePositions', () => {
  it('removes a local position the broker no longer reports', () => {
    const previous = new Map([
      ['600519.SS', position('600519.SS', { quantity: '100' })],
      ['AAPL', position('AAPL', { quantity: '5' })],
    ]);

    const next = reconcilePositions(previous, {
      AAPL: { quantity: 5, avg_cost: 200, unrealized_pnl: 12, market: 'US', currency: 'USD' },
    });

    expect(next.has('600519.SS')).toBe(false);
    expect(next.get('AAPL')?.quantity).toBe('5');
    expect(next.get('AAPL')?.avgEntryPrice).toBe('200');
    expect(next.get('AAPL')?.market).toBe('US');
  });

  it('keeps client-side TP/SL targets on a surviving symbol', () => {
    const previous = new Map([
      ['AAPL', position('AAPL', { takeProfitPrice: '250', stopLossPrice: '180' })],
    ]);

    const next = reconcilePositions(previous, {
      AAPL: { quantity: 5, avg_cost: 200, market: 'US', currency: 'USD' },
    });

    expect(next.get('AAPL')?.takeProfitPrice).toBe('250');
    expect(next.get('AAPL')?.stopLossPrice).toBe('180');
  });

  it('ignores zero-quantity rows instead of showing a flat position', () => {
    const next = reconcilePositions(new Map(), {
      SPY: { quantity: 0, avg_cost: 100, market: 'US', currency: 'USD' },
    });
    expect(next.size).toBe(0);
  });
});

describe('reconcileOrders', () => {
  it('drops a local order whose backend id the broker does not return', () => {
    const ghost = order('local_ghost', { backendId: 'ord_ghost', status: 'filled' });

    expect(isUnsentLocalOrder(ghost)).toBe(false);
    expect(reconcileOrders([ghost], [])).toHaveLength(0);
  });

  it('replaces a server-backed order with broker truth but keeps its client id', () => {
    const local = order('client_uuid', { backendId: 'ord_1', status: 'open', filledQty: '0' });

    const merged = reconcileOrders([local], [{
      order_id: 'ord_1',
      symbol: 'SPY',
      side: 'BUY',
      order_type: 'LIMIT',
      quantity: 10,
      price: 100,
      status: 'FILLED',
      filled_quantity: 10,
      filled_avg_price: 99.5,
      created_at: '2023-11-14T22:13:20.000Z',
      updated_at: '2023-11-14T22:14:20.000Z',
    }]);

    expect(merged).toHaveLength(1);
    expect(merged[0].clientOrderId).toBe('client_uuid');
    expect(merged[0].status).toBe('filled');
    expect(merged[0].filledQty).toBe('10');
    expect(merged[0].avgPrice).toBe('99.5');
  });

  it('rebuilds the executed fill from a filled broker row', () => {
    const [filled] = reconcileOrders([], [{
      order_id: 'ord_3',
      symbol: 'NVDA',
      side: 'BUY',
      order_type: 'MARKET',
      quantity: 3,
      status: 'FILLED',
      filled_quantity: 3,
      filled_avg_price: 207.47,
      currency: 'USD',
      created_at: '2023-11-14T22:13:20.000Z',
      updated_at: '2023-11-14T22:14:20.000Z',
      filled_at: '2023-11-14T22:13:55.000Z',
    }]);

    expect(filled.status).toBe('filled');
    expect(filled.fills).toHaveLength(1);
    expect(filled.fills[0]).toMatchObject({
      fillId: 'ord_3:fill',
      quantity: '3',
      price: '207.47',
      fee: '0',
      feeAsset: 'USD',
    });
    // The fill is dated by execution, not by the row's last status update.
    expect(filled.fills[0].time).toBe(Date.parse('2023-11-14T22:13:55.000Z'));
  });

  it('falls back to the status update when the broker sent no fill time', () => {
    const [filled] = reconcileOrders([], [{
      order_id: 'ord_5',
      symbol: 'SPY',
      side: 'SELL',
      order_type: 'MARKET',
      quantity: 1,
      status: 'FILLED',
      filled_quantity: 1,
      filled_avg_price: 500,
      created_at: '2023-11-14T22:13:20.000Z',
      updated_at: '2023-11-14T22:14:20.000Z',
    }]);

    expect(filled.fills[0].time).toBe(Date.parse('2023-11-14T22:14:20.000Z'));
  });

  it('keeps no fills for an order the broker never executed', () => {
    const [open] = reconcileOrders([], [{
      order_id: 'ord_4',
      symbol: 'SPY',
      side: 'BUY',
      order_type: 'LIMIT',
      quantity: 2,
      price: 700,
    }]);

    expect(open.fills).toEqual([]);
  });

  it('imports a broker order that was placed in another session', () => {    const merged = reconcileOrders([], [{
      order_id: 'ord_2',
      symbol: '0700.HK',
      side: 'SELL',
      order_type: 'MARKET',
      quantity: 100,
      status: 'OPEN',
    }]);

    expect(merged).toHaveLength(1);
    expect(merged[0].backendId).toBe('ord_2');
    expect(merged[0].clientOrderId).toBe('ord_2');
    expect(merged[0].side).toBe('sell');
    expect(merged[0].type).toBe('market');
  });

  it('keeps unsent conditional and OCO-limit orders across a refresh', () => {
    const conditional = order('auto_1', { type: 'stop_market', status: 'open', triggerPrice: '90' });
    const ocoLimit = order('oco_1', { type: 'limit', status: 'open', isOcoOrder: true });
    const freshPending = order('inflight', {
      type: 'market',
      price: null,
      status: 'pending',
      createdAt: Date.now(),
    });
    const stalePending = order('stale', { type: 'market', price: null, status: 'pending', createdAt: 0 });

    const merged = reconcileOrders([conditional, ocoLimit, freshPending, stalePending], []);

    expect(merged.map((o) => o.clientOrderId).sort()).toEqual(
      ['auto_1', 'inflight', 'oco_1'].sort()
    );
  });
});

describe('reconcileOcoGroups', () => {
  const group: OCOOrder = {
    ocoGroupId: 'oco_1',
    symbol: 'SPY',
    side: 'sell',
    quantity: '10',
    limitPrice: '110',
    stopPrice: '90',
    stopLimitPrice: '89',
    status: 'active',
    limitOrderId: 'leg_limit',
    stopOrderId: 'leg_stop',
    createdAt: 1,
    updatedAt: 1,
  };

  it('marks the group filled when a leg fills', () => {
    const legs = [
      order('leg_limit', { isOcoOrder: true, status: 'filled' }),
      order('leg_stop', { isOcoOrder: true, status: 'cancelled' }),
    ];
    expect(reconcileOcoGroups([group], legs)[0].status).toBe('filled');
  });

  it('marks the group cancelled when both legs are cancelled', () => {
    const legs = [
      order('leg_limit', { status: 'cancelled' }),
      order('leg_stop', { status: 'cancelled' }),
    ];
    expect(reconcileOcoGroups([group], legs)[0].status).toBe('cancelled');
  });

  it('marks the group triggered when one leg fires and the other is cancelled', () => {
    const legs = [
      order('leg_limit', { status: 'pending' }),
      order('leg_stop', { status: 'cancelled' }),
    ];
    expect(reconcileOcoGroups([group], legs)[0].status).toBe('triggered');
  });
});
