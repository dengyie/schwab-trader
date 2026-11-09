/**
 * Account-level trade statistics derived from executed fills.
 *
 * Orders carry aggregate fills, so this module is the only place that turns
 * them into win rate, profit factor and realized PnL. Matching is FIFO per
 * symbol: a sell closes the oldest buys first, which is the convention every
 * broker statement uses and keeps partial fills honest. Everything is
 * long-only because the paper account cannot open short positions.
 */
import Decimal from 'decimal.js';
import type { PaperOrder } from '../../types/trading';

export interface TradePerformance {
  /** Executed fills, matching the orders page's trade count. */
  totalTrades: number;
  /** Sells that closed at least one long lot. */
  closedTrades: number;
  /** Wins / closed trades, in the 0-1 range. */
  winRate: number;
  /** Gross profit / gross loss; Infinity when every closed trade won. */
  profitFactor: number;
  totalRealizedPnl: string;
}

interface FillEvent {
  symbol: string;
  side: 'buy' | 'sell';
  quantity: Decimal;
  price: Decimal;
  fee: Decimal;
  time: number;
  sequence: number;
}

interface Lot {
  quantity: Decimal;
  /** Price plus the buy fee allocated per share. */
  unitCost: Decimal;
}

const EMPTY_PERFORMANCE: TradePerformance = {
  totalTrades: 0,
  closedTrades: 0,
  winRate: 0,
  profitFactor: 0,
  totalRealizedPnl: '0.00',
};

function collectFills(orders: PaperOrder[]): FillEvent[] {
  const events: FillEvent[] = [];

  orders.forEach((order, orderIndex) => {
    order.fills.forEach((fill, fillIndex) => {
      const quantity = new Decimal(fill.quantity);
      const price = new Decimal(fill.price);
      if (!quantity.isFinite() || quantity.lte(0) || !price.isFinite() || price.lte(0)) return;

      const rawFee = new Decimal(fill.fee || '0');
      events.push({
        symbol: order.symbol,
        side: order.side,
        quantity,
        price,
        fee: rawFee.isFinite() && rawFee.gte(0) ? rawFee : new Decimal(0),
        time: Number.isFinite(fill.time) ? fill.time : 0,
        sequence: orderIndex * 100_000 + fillIndex,
      });
    });
  });

  return events.sort((a, b) => a.time - b.time || a.sequence - b.sequence);
}

export function computeTradePerformance(orders: PaperOrder[]): TradePerformance {
  const events = collectFills(orders);
  if (events.length === 0) return { ...EMPTY_PERFORMANCE };

  const lots = new Map<string, Lot[]>();
  let closedTrades = 0;
  let wins = 0;
  let grossProfit = new Decimal(0);
  let grossLoss = new Decimal(0);
  let realized = new Decimal(0);

  for (const event of events) {
    if (event.side === 'buy') {
      const queue = lots.get(event.symbol) ?? [];
      queue.push({
        quantity: event.quantity,
        unitCost: event.price.plus(event.fee.div(event.quantity)),
      });
      lots.set(event.symbol, queue);
      continue;
    }

    const queue = lots.get(event.symbol);
    if (!queue || queue.length === 0) continue;

    let remaining = event.quantity;
    let matched = new Decimal(0);
    let tradePnl = new Decimal(0);

    while (remaining.gt(0) && queue.length > 0) {
      const lot = queue[0];
      const take = Decimal.min(remaining, lot.quantity);
      tradePnl = tradePnl.plus(event.price.minus(lot.unitCost).times(take));
      lot.quantity = lot.quantity.minus(take);
      remaining = remaining.minus(take);
      matched = matched.plus(take);
      if (lot.quantity.lte(0)) queue.shift();
    }

    if (matched.lte(0)) continue;

    // Allocate the sell fee to the matched portion only.
    tradePnl = tradePnl.minus(event.fee.times(matched.div(event.quantity)));

    closedTrades += 1;
    realized = realized.plus(tradePnl);
    if (tradePnl.gt(0)) {
      wins += 1;
      grossProfit = grossProfit.plus(tradePnl);
    } else if (tradePnl.lt(0)) {
      grossLoss = grossLoss.plus(tradePnl.abs());
    }
  }

  const profitFactor = closedTrades === 0
    ? 0
    : grossLoss.gt(0)
      ? grossProfit.div(grossLoss).toNumber()
      : grossProfit.gt(0)
        ? Number.POSITIVE_INFINITY
        : 0;

  return {
    totalTrades: events.length,
    closedTrades,
    winRate: closedTrades > 0 ? wins / closedTrades : 0,
    profitFactor,
    totalRealizedPnl: realized.toFixed(2),
  };
}
