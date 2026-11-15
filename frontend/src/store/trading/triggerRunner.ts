/**
 * App-level runner for client-side risk automation.
 *
 * 2026-11-16 review, P1: conditional orders (stop-loss / TP / trailing) and
 * their trigger evaluation lived only inside the two Trade pages — navigate
 * to /wallet or hide the tab and every armed stop quietly stopped being
 * watched while OrdersPage still showed it as armed. The evaluation now runs
 * here, at application scope, whenever the app is authenticated:
 *
 * 1. A marketStore subscription evaluates conditional orders on every book
 *    update, on any page (the automation store runs its own subscription for
 *    its triggers — see automationStore.ts bottom).
 * 2. A heartbeat keeps the feed subscribed to the selected symbol while risk
 *    orders exist, even after the Trade page's own subscribe/unsubscribe has
 *    torn it down.
 * 3. `hasLiveRiskOrders()` gates the tab-hidden WS pause in AdaptiveLayout:
 *    an armed stop-loss is exactly the situation where "nobody is looking"
 *    must not mean "nobody is watching".
 *
 * Coverage note: evaluation follows the ONE selected symbol (as before —
 * this is not a regression). Multi-symbol trigger coverage needs a
 * multi-subscription feed and is deliberately out of scope here.
 */
import { useEffect } from 'react';
import { useMarketStore } from '../marketStore';
import { useTradingStore } from '../tradingStore';
import { useAutomationStore } from '../automationStore';
import { useWatchlistStore, selectSelectedSymbol } from '../watchlistStore';
import { hasToken } from '../../services/apiClient';

const HEARTBEAT_MS = 5_000;

/** True while anything on the client is waiting to fire on a price. */
export function hasLiveRiskOrders(): boolean {
  const trading = useTradingStore.getState();
  for (const order of trading.orders) {
    if (order.backendId) continue; // resting at the broker: the venue owns it
    const armedConditional =
      (order.type !== 'limit' && order.type !== 'market') || order.isOcoOrder;
    if (armedConditional && (order.status === 'open' || order.status === 'triggered')) return true;
    if (order.status === 'pending') return true; // submit in flight
  }
  for (const position of trading.positions.values()) {
    if (position.takeProfitPrice || position.stopLossPrice) return true;
  }
  const automation = useAutomationStore.getState();
  return automation.triggers.some(
    (t) => t.status === 'armed' || t.status === 'blocked'
  );
}

let orderBookUnsubscribe: (() => void) | null = null;
let heartbeat: ReturnType<typeof setInterval> | null = null;

/** Idempotent start. Safe to call from the app root on every mount. */
export function startTriggerRunner(): void {
  if (orderBookUnsubscribe) return;

  orderBookUnsubscribe = useMarketStore.subscribe(
    (state) => state.orderBook,
    (orderBook) => {
      // Authenticated sessions only: a logged-out tab rehydrating persisted
      // state must not evaluate triggers against a dead token.
      if (!hasToken() || !orderBook) return;
      useTradingStore.getState().evaluateOrderTriggers(orderBook);
    }
  );

  heartbeat = setInterval(() => {
    if (!hasToken() || !hasLiveRiskOrders()) return;
    const selected = selectSelectedSymbol(useWatchlistStore.getState());
    const market = useMarketStore.getState();
    if (selected && market.activeSymbol !== selected) {
      // Off /trade: the Trade page unsubscribed on unmount. Keep the feed
      // alive so trigger evaluation keeps receiving book updates.
      market.subscribe(selected);
    }
  }, HEARTBEAT_MS);
}

export function stopTriggerRunner(): void {
  orderBookUnsubscribe?.();
  orderBookUnsubscribe = null;
  if (heartbeat) clearInterval(heartbeat);
  heartbeat = null;
}

/** React hook: run the app-level trigger engine while the layout is mounted. */
export function useTriggerRunner(): void {
  useEffect(() => {
    startTriggerRunner();
    return () => stopTriggerRunner();
  }, []);
}
