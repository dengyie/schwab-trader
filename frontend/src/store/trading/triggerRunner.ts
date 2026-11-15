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
import { apiJson } from '../../services/apiClient';
import { useAuthStore } from '../authStore';

const HEARTBEAT_MS = 5_000;
// Non-active-symbol evaluation cadence. These symbols have no live book
// (no WS subscription), so a REST quote poll is the only data source; 30s
// means a crossing can be acted on up to one poll late — the honest cost
// of covering every symbol without a multi-subscription feed.
const MULTI_SYMBOL_POLL_MS = 30_000;

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

/**
 * Symbols whose risk state is evaluated ONLY on this client (position
 * TP/SL, non-mirrored conditional orders). Mirrored orders are absent —
 * the venue owns them. These need synthetic book evaluation when they are
 * not the active feed symbol.
 */
function collectLocalRiskSymbols(): Set<string> {
  const trading = useTradingStore.getState();
  const symbols = new Set<string>();
  for (const order of trading.orders) {
    if (order.backendId) continue;
    if (order.status !== 'open' || order.isTriggered) continue;
    if (isConditionalLike(order.type)) symbols.add(order.symbol);
    else if (order.isOcoOrder && order.type === 'limit' && order.price) symbols.add(order.symbol);
  }
  for (const [symbol, position] of trading.positions) {
    if (position.takeProfitPrice || position.stopLossPrice) symbols.add(symbol);
  }
  return symbols;
}

function isConditionalLike(type: string): boolean {
  return ['stop_limit', 'take_profit_limit', 'stop_market', 'take_profit_market', 'trailing_stop']
    .includes(type);
}

let multiSymbolTimer: ReturnType<typeof setInterval> | null = null;
let multiSymbolInFlight = false;

/** One pass: evaluate every non-active local-risk symbol against a polled quote. */
async function evaluateOffSymbols(): Promise<void> {
  if (multiSymbolInFlight || !useAuthStore.getState().isAuthenticated) return;
  const market = useMarketStore.getState();
  const active = market.activeSymbol;
  const symbols = [...collectLocalRiskSymbols()].filter((s) => s !== active);
  if (symbols.length === 0) return;
  multiSymbolInFlight = true;
  try {
    for (const symbol of symbols) {
      // A quote arrives per symbol; build the minimal book shape the
      // trigger machinery consumes (bid/ask mid, like the real orderbook).
      try {
        const quote = await apiJson<any>(
          `/market/quote?symbol=${encodeURIComponent(symbol)}`,
        );
        if (!quote) continue;
        const last = Number(quote.last) || 0;
        const bid = Number(quote.bid) > 0 ? Number(quote.bid) : last;
        const ask = Number(quote.ask) > 0 ? Number(quote.ask) : last;
        if (!(last > 0)) continue;
        const stillActive = useMarketStore.getState().activeSymbol;
        if (stillActive === symbol) continue; // live book took over mid-poll
        useTradingStore.getState().evaluateOrderTriggers({
          symbol,
          bids: [{ price: String(bid), quantity: '0' }],
          asks: [{ price: String(ask), quantity: '0' }],
        } as any);
      } catch {
        // 404 (no quote for the symbol) or a network blip: skip this round.
      }
    }
  } finally {
    multiSymbolInFlight = false;
  }
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
      if (!useAuthStore.getState().isAuthenticated || !orderBook) return;
      useTradingStore.getState().evaluateOrderTriggers(orderBook);
    }
  );

  heartbeat = setInterval(() => {
    if (!useAuthStore.getState().isAuthenticated || !hasLiveRiskOrders()) return;
    const selected = selectSelectedSymbol(useWatchlistStore.getState());
    const market = useMarketStore.getState();
    if (selected && market.activeSymbol !== selected) {
      // Off /trade: the Trade page unsubscribed on unmount. Keep the feed
      // alive so trigger evaluation keeps receiving book updates.
      market.subscribe(selected);
    }
  }, HEARTBEAT_MS);

  // 2026-11-16 (multi-symbol coverage): the book subscription only ever
  // sees ONE symbol — a stop on NVDA while watching TSLA starved. Poll
  // quotes for every other locally-evaluated risk symbol.
  multiSymbolTimer = setInterval(() => {
    void evaluateOffSymbols();
  }, MULTI_SYMBOL_POLL_MS);
}

export function stopTriggerRunner(): void {
  orderBookUnsubscribe?.();
  orderBookUnsubscribe = null;
  if (heartbeat) clearInterval(heartbeat);
  heartbeat = null;
  if (multiSymbolTimer) clearInterval(multiSymbolTimer);
  multiSymbolTimer = null;
}

/** React hook: run the app-level trigger engine while the layout is mounted. */
export function useTriggerRunner(): void {
  useEffect(() => {
    startTriggerRunner();
    return () => stopTriggerRunner();
  }, []);
}
