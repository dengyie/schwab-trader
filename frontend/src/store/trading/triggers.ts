/**
 * Pure trigger evaluation for client-side conditional orders.
 *
 * Stops, take profits and trailing stops are watched locally against the
 * live order book. Trigger evaluation is pure so the order-flow store stays
 * a thin coordinator and the crossing rules are unit-testable.
 */
import Decimal from 'decimal.js';
import type {
  OrderSide,
  OrderType,
  PaperOrder,
  TriggerDirection,
} from '../../types/trading';

const CONDITIONAL_TYPES: OrderType[] = [
  'stop_limit',
  'take_profit_limit',
  'stop_market',
  'take_profit_market',
  'trailing_stop',
];

export function isConditionalOrder(type: OrderType): boolean {
  return CONDITIONAL_TYPES.includes(type);
}

export function getDefaultTriggerDirection(type: OrderType, side: OrderSide): TriggerDirection {
  // A stop triggers when price moves against the position; a take profit
  // triggers when it moves in favour.
  if (type === 'stop_limit' || type === 'stop_market') {
    return side === 'buy' ? 'up' : 'down';
  }
  if (type === 'take_profit_limit' || type === 'take_profit_market') {
    return side === 'buy' ? 'down' : 'up';
  }
  return 'down';
}

/**
 * A resting limit leg (an OCO's profit-taking side) fires as soon as price
 * reaches it; there is no crossing direction to wait for.
 */
export function shouldTriggerLimit(order: PaperOrder, currentPrice: string): boolean {
  if (!order.price) return false;
  const limit = new Decimal(order.price);
  const current = new Decimal(currentPrice);
  return order.side === 'buy' ? current.lte(limit) : current.gte(limit);
}

export function shouldTriggerConditional(
  order: PaperOrder,
  currentPrice: string,
  previousPrice: string
): boolean {
  if (!order.triggerPrice) return false;
  const trigger = new Decimal(order.triggerPrice);
  const current = new Decimal(currentPrice);
  const previous = new Decimal(previousPrice);

  if (order.triggerDirection === 'up') {
    return previous.lt(trigger) && current.gte(trigger);
  }
  return previous.gt(trigger) && current.lte(trigger);
}

export interface TrailingDecision {
  /** Fields to persist on the order (tracked extreme / moved stop). */
  patch: Partial<PaperOrder>;
  triggered: boolean;
}

/**
 * Move a trailing stop with the market and report whether it fired.
 *
 * Sell stops track the highest price and sit below it; buy stops track the
 * lowest price and sit above it. The activation gate, when present, keeps the
 * stop dormant until price reaches it.
 */
export function evaluateTrailingStop(
  order: PaperOrder,
  currentPrice: string
): TrailingDecision {
  if (!order.trailingValue) return { patch: {}, triggered: false };

  const current = new Decimal(currentPrice);
  const patch: Partial<PaperOrder> = {};

  if (order.trailingActivationPrice) {
    const activation = new Decimal(order.trailingActivationPrice);
    if (order.side === 'sell' && current.lt(activation)) return { patch, triggered: false };
    if (order.side === 'buy' && current.gt(activation)) return { patch, triggered: false };
  }

  const isSell = order.side === 'sell';
  const extreme = new Decimal(
    (isSell ? order.trailingHighestPrice : order.trailingLowestPrice) || currentPrice
  );

  const movedFavourably = isSell ? current.gt(extreme) : current.lt(extreme);
  if (movedFavourably) {
    const stopPrice = order.trailingType === 'percent'
      ? (isSell
          ? current.times(1 - parseFloat(order.trailingValue) / 100)
          : current.times(1 + parseFloat(order.trailingValue) / 100))
      : (isSell
          ? current.minus(order.trailingValue)
          : current.plus(order.trailingValue));

    patch.trailingHighestPrice = isSell ? current.toFixed(8) : order.trailingHighestPrice;
    patch.trailingLowestPrice = isSell ? order.trailingLowestPrice : current.toFixed(8);
    patch.trailingStopPrice = stopPrice.toFixed(8);
  }

  const stopPrice = new Decimal(patch.trailingStopPrice ?? order.trailingStopPrice ?? '0');
  const triggered = stopPrice.gt(0) && (isSell ? current.lte(stopPrice) : current.gte(stopPrice));

  return { patch, triggered };
}
