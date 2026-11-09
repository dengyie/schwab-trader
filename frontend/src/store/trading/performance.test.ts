import { describe, expect, it } from 'vitest';
import type { Fill, PaperOrder } from '../../types/trading';
import { computeTradePerformance } from './performance';

let sequence = 0;

function fill(overrides: Partial<Fill> = {}): Fill {
  sequence += 1;
  return {
    fillId: `fill_${sequence}`,
    price: '10',
    quantity: '1',
    fee: '0',
    feeAsset: 'USD',
    time: 1_700_000_000_000 + sequence,
    ...overrides,
  };
}

function order(side: 'buy' | 'sell', fills: Fill[], symbol = 'SPY'): PaperOrder {
  return {
    clientOrderId: `order_${sequence += 1}`,
    symbol,
    side,
    type: 'market',
    price: null,
    quantity: fills.reduce((sum, f) => sum + Number(f.quantity), 0).toString(),
    filledQty: fills.reduce((sum, f) => sum + Number(f.quantity), 0).toString(),
    avgPrice: fills[0]?.price ?? '0',
    status: 'filled',
    createdAt: fills[0]?.time ?? 1,
    updatedAt: fills[fills.length - 1]?.time ?? 1,
    fills,
  };
}

describe('computeTradePerformance', () => {
  it('closes lots FIFO and reports a single winning trade', () => {
    const performance = computeTradePerformance([
      order('buy', [fill({ price: '10', quantity: '2' })]),
      order('buy', [fill({ price: '20', quantity: '2' })]),
      order('sell', [fill({ price: '30', quantity: '3' })]),
    ]);

    expect(performance.totalTrades).toBe(3);
    expect(performance.closedTrades).toBe(1);
    expect(performance.winRate).toBe(1);
    expect(performance.profitFactor).toBe(Number.POSITIVE_INFINITY);
    expect(performance.totalRealizedPnl).toBe('50.00');
  });

  it('mixes winners and losers into a profit factor', () => {
    const performance = computeTradePerformance([
      order('buy', [fill({ price: '10', quantity: '2' })]),
      order('sell', [fill({ price: '15', quantity: '1' })]),
      order('sell', [fill({ price: '8', quantity: '1' })]),
    ]);

    expect(performance.closedTrades).toBe(2);
    expect(performance.winRate).toBe(0.5);
    expect(performance.profitFactor).toBeCloseTo(5 / 2);
    expect(performance.totalRealizedPnl).toBe('3.00');
  });

  it('nets fees against realized PnL', () => {
    const performance = computeTradePerformance([
      order('buy', [fill({ price: '10', quantity: '2', fee: '1' })]),
      order('sell', [fill({ price: '12', quantity: '2', fee: '1' })]),
    ]);

    // 2 shares gained $4, minus $2 of fees.
    expect(performance.totalRealizedPnl).toBe('2.00');
  });

  it('never treats an unmatched sell as a closed trade', () => {
    const performance = computeTradePerformance([
      order('sell', [fill({ price: '30', quantity: '1' })]),
    ]);

    expect(performance.totalTrades).toBe(1);
    expect(performance.closedTrades).toBe(0);
    expect(performance.winRate).toBe(0);
    expect(performance.profitFactor).toBe(0);
    expect(performance.totalRealizedPnl).toBe('0.00');
  });

  it('ignores nothing but reports empty stats for a book with no fills', () => {
    const performance = computeTradePerformance([
      order('buy', [fill({ quantity: '0' })]),
    ]);

    expect(performance).toEqual({
      totalTrades: 0,
      closedTrades: 0,
      winRate: 0,
      profitFactor: 0,
      totalRealizedPnl: '0.00',
    });
  });
});
