import { describe, expect, it } from 'vitest';
import type { PaperOrder, Position } from '../../types/trading';
import {
  evaluatePositionTpsl,
  evaluateTrailingStop,
  shouldTriggerConditional,
  shouldTriggerLimit,
} from './triggers';

function order(overrides: Partial<PaperOrder> = {}): PaperOrder {
  return {
    clientOrderId: 'o1',
    symbol: 'SPY',
    side: 'sell',
    type: 'stop_market',
    price: null,
    quantity: '10',
    filledQty: '0',
    avgPrice: '0',
    status: 'open',
    createdAt: 1,
    updatedAt: 1,
    fills: [],
    ...overrides,
  };
}

describe('conditional crossing', () => {
  it('fires a stop only when price crosses the trigger', () => {
    const up = order({ triggerPrice: '100', triggerDirection: 'up' });
    expect(shouldTriggerConditional(up, '101', '99')).toBe(true);
    expect(shouldTriggerConditional(up, '101', '101')).toBe(false);

    const down = order({ triggerPrice: '100', triggerDirection: 'down' });
    expect(shouldTriggerConditional(down, '99', '101')).toBe(true);
    expect(shouldTriggerConditional(down, '99', '99')).toBe(false);
  });

  it('fires a resting limit as soon as price reaches it', () => {
    const buy = order({ side: 'buy', type: 'limit', price: '100' });
    expect(shouldTriggerLimit(buy, '99')).toBe(true);
    expect(shouldTriggerLimit(buy, '101')).toBe(false);

    const sell = order({ side: 'sell', type: 'limit', price: '100' });
    expect(shouldTriggerLimit(sell, '101')).toBe(true);
    expect(shouldTriggerLimit(sell, '99')).toBe(false);
  });
});

describe('trailing stops', () => {
  it('ratchets a sell stop upward and fires on the retrace', () => {
    const stop = order({
      type: 'trailing_stop',
      side: 'sell',
      trailingType: 'absolute',
      trailingValue: '5',
      trailingHighestPrice: '100',
      trailingStopPrice: '95',
    });

    const raised = evaluateTrailingStop(stop, '110');
    expect(raised.triggered).toBe(false);
    expect(raised.patch.trailingHighestPrice).toBe('110.00000000');
    expect(raised.patch.trailingStopPrice).toBe('105.00000000');

    const fired = evaluateTrailingStop({ ...stop, ...raised.patch }, '104');
    expect(fired.triggered).toBe(true);
  });

  it('supports percent-based distance', () => {
    const stop = order({
      type: 'trailing_stop',
      side: 'sell',
      trailingType: 'percent',
      trailingValue: '10',
      trailingHighestPrice: '100',
      trailingStopPrice: '90',
    });
    expect(evaluateTrailingStop(stop, '120').patch.trailingStopPrice).toBe('108.00000000');
  });

  it('stays dormant until the activation price is reached', () => {
    const stop = order({
      type: 'trailing_stop',
      side: 'sell',
      trailingType: 'absolute',
      trailingValue: '5',
      trailingActivationPrice: '105',
      trailingHighestPrice: '100',
      trailingStopPrice: '95',
    });
    expect(evaluateTrailingStop(stop, '100')).toEqual({ patch: {}, triggered: false });
  });

  it('tracks the lowest price for a buy stop', () => {
    const stop = order({
      type: 'trailing_stop',
      side: 'buy',
      trailingType: 'absolute',
      trailingValue: '5',
      trailingLowestPrice: '100',
      trailingStopPrice: '105',
    });
    const raised = evaluateTrailingStop(stop, '90');
    expect(raised.patch.trailingLowestPrice).toBe('90.00000000');
    expect(raised.patch.trailingStopPrice).toBe('95.00000000');
    expect(evaluateTrailingStop({ ...stop, ...raised.patch }, '96').triggered).toBe(true);
  });
});

describe('position TP/SL', () => {
  const base: Position = {
    symbol: 'SPY',
    side: 'long',
    quantity: '10',
    avgEntryPrice: '100',
    unrealizedPnl: '0',
    realizedPnl: '0',
    updatedAt: 1,
  };

  it('detects take profit and stop loss', () => {
    const position = { ...base, takeProfitPrice: '120', stopLossPrice: '90' };
    expect(evaluatePositionTpsl(position, '121')).toBe('take_profit');
    expect(evaluatePositionTpsl(position, '89')).toBe('stop_loss');
    expect(evaluatePositionTpsl(position, '100')).toBeNull();
  });

  it('does nothing for a flat or unset position', () => {
    expect(evaluatePositionTpsl(base, '80')).toBeNull();
  });
});
