import { create } from 'zustand';
import { subscribeWithSelector, persist } from 'zustand/middleware';
import { shallow } from 'zustand/shallow';
import { v4 as uuidv4 } from 'uuid';
import Decimal from 'decimal.js';
import type {
  Trigger,
  TriggerStatus,
  ExecutionLog,
  AutomationErrorCode,
} from '../types/automation';
import type { DataConfidenceLevel } from '../types/market';
import { useTradingStore } from './tradingStore';
import { useMarketStore } from './marketStore';
import { useWalletStore } from './walletStore';
import { useAuthStore } from './authStore';
import { useMarketsStore, marketCodeOfSymbol } from './marketsStore';
import { apiJson } from '../services/apiClient';

// ===== Constants =====
// Execution configuration (reserved for future use)
// const EXECUTION_CONFIG = {
//   maxConcurrent: 3,
//   maxQueueSize: 10,
//   executionTimeoutMs: 5000,
//   retryCount: 0,
// };

// ===== Store State =====
interface AutomationState {
  triggers: Trigger[];
  executionLogs: ExecutionLog[];
  isEngineRunning: boolean;
  
  // Actions - CRUD
  addTrigger: (params: Omit<Trigger, 'id' | 'status' | 'createdAt' | 'updatedAt' | 'triggerCount' | 'successCount' | 'failCount'> & { id?: string }) => Trigger;
  updateTriggerStatus: (id: string, status: TriggerStatus, reason?: string) => void;
  removeTrigger: (id: string) => void;
  pauseTrigger: (id: string) => void;
  resumeTrigger: (id: string) => void;
  
  // Actions - Engine
  startEngine: () => void;
  stopEngine: () => void;
  /** Logout / 401: stop the engine and wipe armed triggers + persisted storage. */
  resetForLogout: () => void;
  processTriggers: (symbol: string, priceState: { current: Decimal; previous: Decimal; source: 'last' | 'bid' | 'ask' | 'mid' }) => void;
  
  // Actions - Logs
  addExecutionLog: (log: Omit<ExecutionLog, 'id'>) => void;
  clearLogs: () => void;
  
  // Getters
  getTriggersBySymbol: (symbol: string) => Trigger[];
  getTrigger: (id: string) => Trigger | undefined;
}

// Custom storage to handle potential serialization issues if any
const customStorage = {
  getItem: (name: string) => {
    const str = localStorage.getItem(name);
    if (!str) return null;
    try {
      return JSON.parse(str);
    } catch (e) {
      console.error('[AutomationStore] Failed to parse storage:', e);
      return null;
    }
  },
  setItem: (name: string, value: any) => {
    try {
      localStorage.setItem(name, JSON.stringify(value));
    } catch (e) {
      console.error('[AutomationStore] Failed to save storage:', e);
    }
  },
  removeItem: (name: string) => localStorage.removeItem(name),
};

// Helper for crossing detection
function evaluateTrigger(trigger: Trigger, current: Decimal, previous: Decimal): boolean {
  const threshold = new Decimal(trigger.condition.threshold);
  const { operator, direction } = trigger.condition;

  if (direction === 'up' && operator === 'gte') {
    return previous.lt(threshold) && current.gte(threshold);
  }
  if (direction === 'down' && operator === 'lte') {
    return previous.gt(threshold) && current.lte(threshold);
  }
  return false;
}

// Track previous prices per source for each symbol to detect crossings
const lastPrices: Record<string, Record<string, Decimal>> = {};

function lastPricesClear(): void {
  for (const key of Object.keys(lastPrices)) delete lastPrices[key];
}

// Per-market session clocks for non-US triggers (2026-11-23 review P1): the
// global dataConfidence level keys off the US clock and the streamed feed,
// and the HK / JP / CN sessions barely overlap the US one — a foreign
// stop-loss was blocked with confidence 'closed' during its ENTIRE home
// session. The cache is refreshed lazily from /market/clock?market=….
const triggerMarketClocks = new Map<string, { isOpen: boolean; fetchedAt: number }>();
const TRIGGER_CLOCK_TTL_MS = 60_000;

async function refreshTriggerMarketClocks(markets: string[]): Promise<void> {
  const now = Date.now();
  await Promise.all(markets.map(async (market) => {
    const cached = triggerMarketClocks.get(market);
    if (cached && now - cached.fetchedAt < TRIGGER_CLOCK_TTL_MS) return;
    try {
      const data = await apiJson<{ is_open?: boolean | null }>(`/market/clock?market=${encodeURIComponent(market)}`);
      if (data && typeof data.is_open === 'boolean') {
        triggerMarketClocks.set(market, { isOpen: data.is_open, fetchedAt: Date.now() });
      }
    } catch {
      // Keep the last known answer; an unreachable clock must not widen the
      // gate on its own.
    }
  }));
}

export const useAutomationStore = create<AutomationState>()(
  subscribeWithSelector(
    persist(
      (set, get) => ({
        triggers: [],
        executionLogs: [],
        isEngineRunning: false,

        addTrigger: (params) => {
          const now = Date.now();
          const { id, ...rest } = params;
          const trigger: Trigger = {
            ...rest,
            id: id || uuidv4(),
            status: 'armed',
            createdAt: now,
            updatedAt: now,
            triggerCount: 0,
            successCount: 0,
            failCount: 0,
          };

          set((state) => ({
            triggers: [trigger, ...state.triggers],
          }));

          return trigger;
        },

        updateTriggerStatus: (id, status, reason) => {
          set((state) => ({
            triggers: state.triggers.map((t) =>
              t.id === id ? { ...t, status, statusReason: reason, updatedAt: Date.now() } : t
            ),
          }));
        },

        removeTrigger: (id) => {
          set((state) => ({
            triggers: state.triggers.filter((t) => t.id !== id),
          }));
        },

        pauseTrigger: (id) => {
          get().updateTriggerStatus(id, 'paused');
        },

        resumeTrigger: (id) => {
          get().updateTriggerStatus(id, 'armed');
        },

        startEngine: () => {
          if (get().isEngineRunning) return;
          set({ isEngineRunning: true });
          console.log('[AutomationStore] Engine started');
        },

        stopEngine: () => {
          set({ isEngineRunning: false });
          console.log('[AutomationStore] Engine stopped');
        },

        /** Logout / 401: armed triggers must not survive into the next
         * session on this browser — the persisted storage is wiped too, so
         * a different person logging in cannot inherit someone else's
         * stop-losses (2026-11-16 review). */
        resetForLogout: () => {
          get().stopEngine();
          set({ triggers: [], executionLogs: [] });
          lastPricesClear();
        },

        processTriggers: (symbol, priceState) => {
          const { current, previous, source } = priceState;
          const state = get();
          if (!state.isEngineRunning) return;

          // Keep foreign-market clocks warm for the gate below (fire and
          // forget — evaluation never waits on the network).
          const foreignMarkets = new Set(
            state.triggers
              .filter((t) => t.status === 'armed' || t.status === 'blocked')
              .map((t) => marketCodeOfSymbol(t.symbol, useMarketsStore.getState().markets))
              .filter((m) => m !== 'US')
          );
          if (foreignMarkets.size > 0) {
            void refreshTriggerMarketClocks([...foreignMarkets]);
          }

          const armedTriggers = state.triggers.filter(
            (t) =>
              t.symbol === symbol &&
              t.condition.priceSource === source &&
              (t.status === 'armed' || t.status === 'blocked')
          );

          const now = Date.now();
          const dataConfidence = useMarketStore.getState().dataConfidence;

          for (const trigger of armedTriggers) {
            // Check crossing
            const isSatisfied = evaluateTrigger(trigger, current, previous);
            if (!isSatisfied) continue;

            // Check debounce
            if (trigger.lastTriggeredAt && now - trigger.lastTriggeredAt < trigger.condition.debounceMs) {
              continue;
            }

            // Check cooldown for repeat triggers
            if (trigger.repeat && trigger.lastTriggeredAt && now - trigger.lastTriggeredAt < trigger.condition.cooldownMs) {
              continue;
            }

            // Handle Data Confidence. For a non-US trigger the global level
            // is the WRONG gate during its own session (see
            // refreshTriggerMarketClocks) — the trigger's home clock decides
            // when the global level is 'closed' purely because of the US
            // session (2026-11-23 review P1).
            const isLive = dataConfidence.level === 'live';
            const isDegradedAllowed = dataConfidence.level === 'degraded' && trigger.allowDegraded;
            let canExecute = isLive || isDegradedAllowed;
            if (!canExecute && dataConfidence.level === 'closed') {
              const triggerMarket = marketCodeOfSymbol(trigger.symbol, useMarketsStore.getState().markets);
              if (triggerMarket !== 'US') {
                const clock = triggerMarketClocks.get(triggerMarket);
                if (clock?.isOpen) canExecute = true;
              }
            }
            
            if (!canExecute) {
              if (trigger.status !== 'blocked') {
                state.updateTriggerStatus(trigger.id, 'blocked', `Data confidence: ${dataConfidence.level}`);
              }
              
              state.addExecutionLog({
                triggerId: trigger.id,
                firedAt: now,
                observedPrice: current.toFixed(8),
                confidenceLevel: dataConfidence.level,
                confidenceReason: dataConfidence.reason,
                result: 'blocked',
                errorCode: 'DATA_NOT_RELIABLE',
                errorMessage: 'Execution blocked due to low data confidence',
                executionLatencyMs: Date.now() - now,
              });
              continue;
            }

            // If degraded but allowed, apply 2s delay as per plan
            const executionDelay = isDegradedAllowed ? 2000 : 0;

            // Reserve BEFORE the async hop: the 'triggered' status excludes
            // the trigger from every later evaluation pass. Without it, a
            // price oscillating around the threshold queued a second
            // execution while the first was still inside its (degraded)
            // delay window — repeat:false triggers placed multiple real
            // orders and stopEngine/pauseTrigger were ineffective inside
            // the window (2026-11-23 review P1-1).
            fireTrigger(trigger, current, dataConfidence, now, executionDelay);
          }
        },

        addExecutionLog: (log) => {
          set((state) => ({
            executionLogs: [{ ...log, id: uuidv4() }, ...state.executionLogs].slice(0, 1000),
          }));
        },

        clearLogs: () => {
          set({ executionLogs: [] });
        },

        getTriggersBySymbol: (symbol) => {
          return get().triggers.filter((t) => t.symbol === symbol);
        },

        getTrigger: (id) => {
          return get().triggers.find((t) => t.id === id);
        },
      }),
      {
        name: 'paper-automation-storage',
        storage: customStorage as any,
        partialize: (state) => ({
          triggers: state.triggers,
          executionLogs: state.executionLogs,
        }),
        onRehydrateStorage: () => (state) => {
          if (state) {
            // Rehydrated armed triggers only run inside an authenticated
            // session: after a logout (or a 401 mid-session) an auto-started
            // engine would resume firing someone else's stop-losses into
            // whatever session logs in next (2026-11-16 review).
            if (useAuthStore.getState().isAuthenticated) {
              state.startEngine();
            }
          }
        },
      }
    )
  )
);

// ===== Execution Logic =====

async function executeAction(
  trigger: Trigger,
  observedPrice: Decimal
): Promise<{ success: boolean; orderId?: string; errorCode?: string; errorMessage?: string }> {
  // Fresh-state recheck at execution time: the crossing that queued this
  // call may be stale by the time the (degraded-mode) delay elapses — the
  // engine may have been stopped, or the trigger paused/cancelled/completed
  // by an earlier queued run (2026-11-23 review P1-1).
  const live = useAutomationStore.getState();
  if (!live.isEngineRunning) {
    return { success: false, errorCode: 'UNKNOWN_ERROR', errorMessage: 'Engine stopped before execution' };
  }
  const pending = live.triggers.find((t) => t.id === trigger.id);
  if (!pending || pending.status !== 'triggered') {
    return { success: false, errorCode: 'UNKNOWN_ERROR', errorMessage: 'Trigger no longer in-flight (stopped, paused or already fired)' };
  }
  const { action, symbol } = trigger;
  const tradingStore = useTradingStore.getState();
  const walletStore = useWalletStore.getState();

  if (action.type === 'alert') {
    // For MVP, alerts are just execution logs with success
    return { success: true };
  }

  if (action.type === 'order') {
    // 1. Calculate quantity
    let quantityValue = new Decimal(action.quantityValue);
    let finalQuantity: string;

    if (action.quantityMode === 'percent') {
      if (action.side === 'sell') {
        // Sell protection closes the POSITION it guards, not a wallet row:
        // foreign-currency positions (0700.HK / 7203.T / 600519.SS) never had
        // a wallet balance entry, so every HK/JP/CN stop-loss died with
        // INSUFFICIENT_BALANCE before this (2026-11-23 review P1). The
        // trading store's broker-synced position is the source of truth;
        // the wallet row is only a fallback for legacy crypto-style assets.
        const position = useTradingStore.getState().positions.get(symbol);
        const held = position && new Decimal(position.quantity).gt(0)
          ? new Decimal(position.quantity)
          : (() => {
              const balance = walletStore.getBalance(symbol.replace('USDT', ''));
              return balance ? new Decimal(balance.available) : null;
            })();
        if (!held) {
          return { success: false, errorCode: 'INSUFFICIENT_BALANCE', errorMessage: 'No position to close for ' + symbol };
        }
        finalQuantity = held.times(quantityValue).div(100).toFixed(8);
      } else {
        const assetToCheck = 'USDT';
        const balance = walletStore.getBalance(assetToCheck);
        if (!balance) return { success: false, errorCode: 'INSUFFICIENT_BALANCE', errorMessage: 'Asset not found in wallet' };

        const available = new Decimal(balance.available);
        // Buy using percentage of USDT available
        const priceToUse = action.orderType === 'limit' ? new Decimal(action.limitPrice!) : observedPrice;
        // Add 5% buffer for market buy to ensure execution
        const costPerUnit = priceToUse.times(action.orderType === 'market' ? 1.05 : 1);
        finalQuantity = available.times(quantityValue).div(100).div(costPerUnit).toFixed(8);
      }
    } else {
      finalQuantity = quantityValue.toFixed(8);
    }

    // 2. Double check balance before placing order
    const canTrade = await validateBalanceForAction(trigger, finalQuantity, observedPrice);
    if (!canTrade) {
      return { success: false, errorCode: 'INSUFFICIENT_BALANCE', errorMessage: 'Insufficient balance at trigger time' };
    }

    // 3. Place order
    const order = tradingStore.createOrder({
      symbol,
      side: action.side!,
      type: action.orderType!,
      price: action.limitPrice,
      quantity: finalQuantity,
    }, observedPrice.toFixed(8));

    if (order && !('blocked' in order)) {
      return { success: true, orderId: order.clientOrderId };
    } else if (order && 'blocked' in order) {
      return {
        success: false,
        errorCode: 'ORDER_BLOCKED',
        errorMessage: `Order blocked: ${order.blocked}`,
      };
    } else {
      return { success: false, errorCode: 'ORDER_REJECTED', errorMessage: 'Trading engine rejected order' };
    }
  }

  return { success: false, errorCode: 'UNKNOWN_ERROR', errorMessage: 'Invalid action type' };
}

async function validateBalanceForAction(trigger: Trigger, quantity: string, observedPrice: Decimal): Promise<boolean> {
  const { action, symbol } = trigger;
  const walletStore = useWalletStore.getState();
  const baseAsset = symbol.replace('USDT', '');
  const quoteAsset = 'USDT';

  if (action.side === 'buy') {
    const price = action.orderType === 'limit' ? new Decimal(action.limitPrice!) : observedPrice;
    const requiredQuote = price.times(quantity).times(action.orderType === 'market' ? 1.05 : 1);
    const balance = walletStore.getBalance(quoteAsset);
    return !!balance && new Decimal(balance.available).gte(requiredQuote);
  } else {
    // Position-based validation for sells (2026-11-23 review P1): foreign
    // positions have no wallet row; the broker-synced position is authoritative.
    const position = useTradingStore.getState().positions.get(symbol);
    if (position && new Decimal(position.quantity).gt(0)) {
      return new Decimal(position.quantity).gte(quantity);
    }
    const balance = walletStore.getBalance(baseAsset);
    return !!balance && new Decimal(balance.available).gte(quantity);
  }
}

function fireTrigger(
  trigger: Trigger,
  observedPrice: Decimal,
  dataConfidence: { level: DataConfidenceLevel; reason?: string },
  now: number,
  executionDelay: number,
): void {
  const state = useAutomationStore.getState();
  state.updateTriggerStatus(trigger.id, 'triggered');

  const executionStartTime = Date.now();
  setTimeout(() => {
    executeAction(trigger, observedPrice)
      .then((result) => {
        const latency = Date.now() - executionStartTime;
        const live = useAutomationStore.getState();

        if (result.success) {
          live.addExecutionLog({
            triggerId: trigger.id,
            firedAt: now,
            observedPrice: observedPrice.toFixed(8),
            confidenceLevel: dataConfidence.level,
            confidenceReason: dataConfidence.reason ?? '',
            result: 'success',
            orderId: result.orderId,
            executionLatencyMs: latency,
          });

          useAutomationStore.setState((st) => {
            const newTriggers = st.triggers.map((t) =>
              t.id === trigger.id
                ? {
                    ...t,
                    status: t.repeat ? ('armed' as TriggerStatus) : ('completed' as TriggerStatus),
                    triggerCount: t.triggerCount + 1,
                    successCount: t.successCount + 1,
                    lastTriggeredAt: now,
                    updatedAt: Date.now(),
                  }
                : t
            );

            // OCO Cancellation logic
            if (trigger.linkedTriggerId) {
              return {
                triggers: newTriggers.map((t) =>
                  t.id === trigger.linkedTriggerId
                    ? { ...t, status: 'cancelled' as TriggerStatus, statusReason: 'OCO cancelled by linked trigger', updatedAt: Date.now() }
                    : t
                ),
              };
            }

            return { triggers: newTriggers };
          });
        } else {
          live.addExecutionLog({
            triggerId: trigger.id,
            firedAt: now,
            observedPrice: observedPrice.toFixed(8),
            confidenceLevel: dataConfidence.level,
            confidenceReason: dataConfidence.reason ?? '',
            result: 'failed',
            errorCode: result.errorCode as AutomationErrorCode,
            errorMessage: result.errorMessage,
            executionLatencyMs: latency,
          });

          useAutomationStore.setState((st) => ({
            triggers: st.triggers.map((t) =>
              t.id === trigger.id
                ? {
                    ...t,
                    status: t.repeat ? ('armed' as TriggerStatus) : ('failed' as TriggerStatus),
                    triggerCount: t.triggerCount + 1,
                    failCount: t.failCount + 1,
                    lastTriggeredAt: now,
                    updatedAt: Date.now(),
                  }
                : t
            ),
          }));
        }
      })
      .catch((err) => {
        console.error('[AutomationStore] Execution error:', err);
        const live = useAutomationStore.getState();
        live.addExecutionLog({
          triggerId: trigger.id,
          firedAt: now,
          observedPrice: observedPrice.toFixed(8),
          confidenceLevel: dataConfidence.level,
          confidenceReason: dataConfidence.reason ?? '',
          result: 'failed',
          errorCode: 'UNKNOWN_ERROR',
          errorMessage: err.message,
          executionLatencyMs: Date.now() - executionStartTime,
        });
        // Release the reservation on a thrown error too: the old code left
        // the trigger stuck in the in-flight status forever (2026-11-23
        // review P1-1).
        useAutomationStore.setState((st) => ({
          triggers: st.triggers.map((t) =>
            t.id === trigger.id
              ? {
                  ...t,
                  status: t.repeat ? ('armed' as TriggerStatus) : ('failed' as TriggerStatus),
                  failCount: t.failCount + 1,
                  lastTriggeredAt: now,
                  updatedAt: Date.now(),
                }
              : t
          ),
        }));
      });
  }, executionDelay);
}

/** Best-effort current price for a trigger's symbol+source, for state-based
 * catch-up evaluation (2026-11-23 review P2: a crossing that happened while
 * confidence was degraded never fires after recovery — the stop-loss stays
 * armed and silently dead). */
function currentPriceForTrigger(trigger: Trigger): Decimal | null {
  const market = useMarketStore.getState();
  if (market.orderBook && market.orderBook.symbol === trigger.symbol) {
    if (trigger.condition.priceSource === 'bid' && market.orderBook.bids?.[0]) {
      return new Decimal(market.orderBook.bids[0].price);
    }
    if (trigger.condition.priceSource === 'ask' && market.orderBook.asks?.[0]) {
      return new Decimal(market.orderBook.asks[0].price);
    }
    if (trigger.condition.priceSource === 'last' && market.recentTrades?.[0]) {
      return new Decimal(market.recentTrades[0].price);
    }
    if (trigger.condition.priceSource === 'mid' && market.metrics?.mid) {
      return new Decimal(market.metrics.mid);
    }
  }
  // The symbol is not the actively streamed one; the trigger engine only
  // sees prices for the streamed symbol anyway.
  return null;
}

// ===== Market Data Subscription =====

// Subscription to marketStore to drive the engine
// (2026-11-23 review P3) selector 每次返回新对象，旧版在任意 store 变更
// （每 250ms 指标批、日志、连接状态…）上都触发 listener。zustand 的
// useShallow 是 React hook，不能用于模块级 vanilla subscribe——正确的
// 工具是 subscribeWithSelector 的 equalityFn 选项 + shallow 比较。
useMarketStore.subscribe(
  (state) => ({
    metrics: state.metrics,
    orderBook: state.orderBook,
    recentTrades: state.recentTrades,
  }),
  (data) => {
    const automationStore = useAutomationStore.getState();
    if (!automationStore.isEngineRunning) return;

    const symbol = useMarketStore.getState().orderBook?.symbol;
    if (!symbol) return;

    // Process Mid Price
    if (data.metrics?.mid) {
      const current = new Decimal(data.metrics.mid);
      updateAndProcess(symbol, 'mid', current);
    }

    // Process Bid/Ask
    if (data.orderBook?.bids?.[0]) {
      const current = new Decimal(data.orderBook.bids[0].price);
      updateAndProcess(symbol, 'bid', current);
    }
    if (data.orderBook?.asks?.[0]) {
      const current = new Decimal(data.orderBook.asks[0].price);
      updateAndProcess(symbol, 'ask', current);
    }

    // Process Last Price
    if (data.recentTrades?.[0]) {
      const current = new Decimal(data.recentTrades[0].price);
      updateAndProcess(symbol, 'last', current);
    }
  },
  { equalityFn: shallow }
);

function updateAndProcess(symbol: string, source: 'last' | 'bid' | 'ask' | 'mid', current: Decimal) {
  if (!lastPrices[symbol]) {
    // Bound the map: one entry per symbol ever seen (2026-11-16 review).
    if (Object.keys(lastPrices).length > 200) {
      delete lastPrices[Object.keys(lastPrices)[0]];
    }
    lastPrices[symbol] = {};
  }
  
  const previous = lastPrices[symbol][source];
  if (previous && !previous.equals(current)) {
    useAutomationStore.getState().processTriggers(symbol, {
      current,
      previous,
      source,
    });
  }
  
  lastPrices[symbol][source] = current;
}


// Subscription to dataConfidence to update trigger statuses (armed vs blocked)
useMarketStore.subscribe(
  (state) => state.dataConfidence,
  (confidence) => {
    const automationStore = useAutomationStore.getState();
    const { triggers, updateTriggerStatus } = automationStore;
    
    triggers.forEach(trigger => {
      if (trigger.status === 'armed' && confidence.level !== 'live' && !(confidence.level === 'degraded' && trigger.allowDegraded)) {
        updateTriggerStatus(trigger.id, 'blocked', `Data confidence: ${confidence.level}`);
      } else if (trigger.status === 'blocked' && (confidence.level === 'live' || (confidence.level === 'degraded' && trigger.allowDegraded))) {
        updateTriggerStatus(trigger.id, 'armed');
        // State-based catch-up: a crossing is evaluated against `previous`,
        // so a threshold crossing that happened INSIDE the degraded window
        // never satisfied any later pass and the stop-loss silently stayed
        // armed-but-dead after recovery (2026-11-23 review P2). If the
        // level is already beyond the threshold right now, fire.
        const price = currentPriceForTrigger(trigger);
        if (price !== null) {
          const threshold = new Decimal(trigger.condition.threshold);
          const satisfied =
            trigger.condition.operator === 'gte'
              ? price.gte(threshold)
              : price.lte(threshold);
          if (satisfied) {
            fireTrigger(trigger, price, confidence, Date.now(), 0);
          }
        }
      }
    });
  }
);

// Subscription to positions to cancel TP/SL when position is closed
useTradingStore.subscribe(
  (state) => state.positions,
  (positions) => {
    const automationStore = useAutomationStore.getState();
    const { triggers, updateTriggerStatus } = automationStore;
    
    triggers.forEach(trigger => {
      if ((trigger.type === 'takeProfit' || trigger.type === 'stopLoss') && trigger.status === 'armed') {
        let position = undefined;
        if (positions instanceof Map) {
          position = positions.get(trigger.symbol);
        } else if (typeof positions === 'object' && positions !== null) {
          position = (positions as any)[trigger.symbol];
        }

        if (!position || new Decimal(position.quantity).lte(0)) {
          updateTriggerStatus(trigger.id, 'cancelled', 'Position closed');
        }
      }
    });
  }
);
