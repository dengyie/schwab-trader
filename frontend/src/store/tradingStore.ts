/**
 * Order and position store for the paper-trading terminal.
 *
 * Ledger ownership:
 * - The backend broker is the only ledger. Cash, positions and submitted
 *   orders come from `/api/portfolio` + `/api/orders` and the websocket
 *   snapshot; this store never invents fills or moves the local wallet.
 * - The client owns only *pre-submission* automation: stop / take-profit /
 *   trailing / OCO orders are watched locally and submitted to the backend
 *   once their trigger fires.
 *
 * That split is what makes refresh, another tab or a second browser safe: any
 * local server-backed row is replaced by broker truth, and positions are
 * rebuilt from the snapshot instead of merged into.
 */
import { create } from 'zustand';
import { subscribeWithSelector, persist } from 'zustand/middleware';
import { v4 as uuidv4 } from 'uuid';
import Decimal from 'decimal.js';
import type {
  CreateOCOParams,
  CreateTrailingStopParams,
  OCOOrder,
  OrderSide,
  OrderType,
  PaperOrder,
  Position,
  TrailingType,
  TriggerDirection,
} from '../types/trading';
import type { OrderBook } from '../types/market';
import { useWalletStore } from './walletStore';
import { useMarketStore } from './marketStore';
import { useMarketsStore, marketCodeOfSymbol } from './marketsStore';
import { notification } from './notificationStore';
import { apiFetch } from '../services/apiClient';
import {
  isUnsentLocalOrder,
  mapBackendStatus,
  reconcileOcoGroups,
  reconcileOrders,
  reconcilePositions,
  type BackendOrderRow,
  type BackendPosition,
} from './trading/reconcile';
import {
  evaluateTrailingStop,
  getDefaultTriggerDirection,
  isConditionalOrder,
  shouldTriggerConditional,
  shouldTriggerLimit,
} from './trading/triggers';

interface TradingState {
  orders: PaperOrder[];
  ocoOrders: OCOOrder[];
  positions: Map<string, Position>;
  focusMode: boolean;

  // Basic order operations
  createOrder: (params: CreateOrderParams, currentMarketPrice?: string) => CreateOrderResult;
  cancelOrder: (clientOrderId: string) => Promise<boolean>;

  // Advanced order operations
  createStopLimitOrder: (params: CreateStopLimitParams) => CreateOrderResult;
  createTakeProfitLimitOrder: (params: CreateTakeProfitLimitParams) => CreateOrderResult;
  createOCOOrder: (params: CreateOCOParams) => OCOOrder | OrderBlock | null;
  createTrailingStopOrder: (params: CreateTrailingStopParams) => CreateOrderResult;
  cancelOCOOrder: (ocoGroupId: string) => Promise<boolean>;

  // Client-side trigger evaluation
  evaluateOrderTriggers: (orderBook: OrderBook) => void;

  // Position management
  setFocusMode: (enabled: boolean) => void;
  resetAccount: () => Promise<void>;
  clearLocalState: () => void;
  syncPositionsFromBackend: (backendPositions: Record<string, BackendPosition>) => void;
  fetchOrders: () => Promise<void>;

  // Getters
  getOpenOrders: () => PaperOrder[];
}

/**
 * Why a store-level order creation refused to build an order. The UI maps
 * each reason to a specific toast — the old single "insufficient balance"
 * answer sent users chasing the wrong cause during a routine feed resync
 * (2026-11-16 review).
 */
export type OrderBlockReason =
  | 'data_confidence'
  | 'invalid_quantity'
  | 'invalid_params'
  | 'missing_price'
  | 'missing_trigger'
  | 'lot_floor_zero'
  | 'markets_loading';

export interface OrderBlock {
  blocked: OrderBlockReason;
}

/** An order, a typed refusal, or null (legacy callers / no attempt made). */
export type CreateOrderResult = PaperOrder | OrderBlock | null;

interface CreateOrderParams {
  symbol: string;
  side: OrderSide;
  type: OrderType;
  price?: string;
  quantity: string;
  takeProfitPrice?: string;
  stopLossPrice?: string;
  triggerPrice?: string;
  triggerDirection?: TriggerDirection;
}

interface CreateStopLimitParams {
  symbol: string;
  side: OrderSide;
  quantity: string;
  triggerPrice: string;
  limitPrice: string;
}

interface CreateTakeProfitLimitParams {
  symbol: string;
  side: OrderSide;
  quantity: string;
  triggerPrice: string;
  limitPrice: string;
}

const CANCELLABLE_STATUSES = ['pending', 'open', 'partial', 'triggered'];

const customStorage = {
  getItem: (name: string) => {
    const str = localStorage.getItem(name);
    if (!str) return null;
    try {
      const parsed = JSON.parse(str);
      if (parsed.state?.positions && Array.isArray(parsed.state.positions)) {
        parsed.state.positions = new Map(parsed.state.positions);
      }
      return parsed;
    } catch {
      return null;
    }
  },
  setItem: (name: string, value: { state: TradingState; version?: number }) => {
    try {
      const toStore = {
        ...value,
        state: {
          ...value.state,
          positions: value.state.positions instanceof Map
            ? Array.from(value.state.positions.entries())
            : value.state.positions,
        },
      };
      localStorage.setItem(name, JSON.stringify(toStore));
    } catch {
      // Quota or private-mode failures must not break trading.
    }
  },
  removeItem: (name: string) => localStorage.removeItem(name),
};

// Last mid seen per symbol, used to detect a crossing for conditional orders.
const previousPrices = new Map<string, string>();

function normalizeSymbol(symbol: string): string {
  return symbol.replace('/USD', '').replace('USDT', '');
}

/**
 * Map a local order type to the backend's coarse order-type enum.
 *
 * Backend enum: MARKET | LIMIT | STOP | TRAILING_STOP. A stop_limit rides
 * as STOP carrying BOTH stop_price (trigger) and price (limit) — the broker
 * layer turns that pair into the venue's native stop_limit. A
 * take_profit_limit rides as LIMIT at its limit price: for a long it rests
 * as a sell limit above the market (fills when the target is reached), for
 * a short as a buy limit below — venue semantics identical to the local
 * trigger's intent.
 */
function backendType(type: OrderType): 'MARKET' | 'LIMIT' | 'STOP' | 'TRAILING_STOP' {
  if (type === 'limit' || type === 'take_profit_limit') return 'LIMIT';
  if (type === 'trailing_stop') return 'TRAILING_STOP';
  if (type === 'stop_limit' || type === 'stop_market') return 'STOP';
  return 'MARKET';
}

/**
 * True when this order should rest at the venue instead of waiting for the
 * local trigger. One-shot conditionals (stop_limit / take_profit_limit) are
 * mirrored for BOTH markets: US maps to Alpaca's native stop_limit, HK/JP/CN
 * rest in the server-side virtual engine, which matches them on quotes
 * independently of any browser. Trailing stops mirror only for US (Alpaca
 * has a native trailing order) and only without an activation price (the
 * venue cannot defer activation). Mirrored orders are skipped by the local
 * trigger evaluation — the venue owns their execution; a local trigger
 * firing on top would place a duplicate order.
 */
function mirrorToVenue(order: PaperOrder): boolean {
  if (isConditionalOrder(order.type)) {
    if (order.type === 'stop_limit' || order.type === 'take_profit_limit') return true;
    if (order.type === 'trailing_stop') {
      return marketOf(order.symbol) === 'US' && !order.trailingActivationPrice;
    }
    return false;
  }
  return true;
}

async function rejectionReason(res: Response): Promise<string> {
  try {
    const body = await res.json();
    const detail = body?.detail;
    if (typeof detail === 'string') return detail;
    if (detail !== undefined) return JSON.stringify(detail);
    return `HTTP ${res.status}`;
  } catch {
    return `HTTP ${res.status}`;
  }
}

function marketOf(symbol: string): string {
  return marketCodeOfSymbol(symbol, useMarketsStore.getState().markets);
}

/**
 * Order-level TP/SL collected by the order entry forms, keyed by the
 * submitting order's client id. The backend /orders contract has no TP/SL
 * fields (2026-11-23 review P1: the values used to be stored on the local
 * row and never read again — a silent fake protection), so on the order's
 * FILL we convert them into automation triggers, the same mechanism the
 * position panel's TPSLForm uses. Cleared on terminal non-fill states.
 */
const pendingTpslByOrderId = new Map<string, { takeProfitPrice?: string; stopLossPrice?: string }>();

function createTpslTriggersFromFill(order: PaperOrder): void {
  const entry = pendingTpslByOrderId.get(order.clientOrderId);
  if (!entry) return;
  pendingTpslByOrderId.delete(order.clientOrderId);

  // Dynamic import: automationStore has module-level store subscriptions and
  // imports tradingStore itself — a static import here would make the two
  // modules evaluate each other mid-initialization (vitest caught it as a
  // ReferenceError at tradingStore.ts:8).
  void import('./automationStore').then(({ useAutomationStore }) => {
  const automation = useAutomationStore.getState();
  const tpId = uuidv4();
  const slId = uuidv4();

  if (entry.takeProfitPrice) {
    automation.addTrigger({
      id: tpId,
      symbol: order.symbol,
      type: 'takeProfit',
      enabled: true,
      condition: {
        priceSource: 'last',
        operator: 'gte',
        threshold: entry.takeProfitPrice,
        direction: 'up',
        debounceMs: 1000,
        cooldownMs: 60000,
      },
      action: {
        type: 'order',
        side: 'sell',
        orderType: 'market',
        quantityMode: 'percent',
        quantityValue: '100',
      },
      allowDegraded: false,
      repeat: false,
      linkedTriggerId: entry.stopLossPrice ? slId : undefined,
    });
  }

  if (entry.stopLossPrice) {
    automation.addTrigger({
      id: slId,
      symbol: order.symbol,
      type: 'stopLoss',
      enabled: true,
      condition: {
        priceSource: 'last',
        operator: 'lte',
        threshold: entry.stopLossPrice,
        direction: 'down',
        debounceMs: 1000,
        cooldownMs: 60000,
      },
      action: {
        type: 'order',
        side: 'sell',
        orderType: 'market',
        quantityMode: 'percent',
        quantityValue: '100',
      },
      allowDegraded: false,
      repeat: false,
      linkedTriggerId: entry.takeProfitPrice ? tpId : undefined,
    });
  }

  notification.info(
    `order:tpsl-armed:${order.clientOrderId}`,
    `TP/SL armed on fill: ${order.symbol} (protection triggers created)`,
  );
  });
}

export const useTradingStore = create<TradingState>()(
  subscribeWithSelector(
    persist(
      (set, get) => {
        const updateOrder = (clientOrderId: string, patch: Partial<PaperOrder>) => {
          set((state) => ({
            orders: state.orders.map((order) => (
              order.clientOrderId === clientOrderId
                ? { ...order, ...patch, updatedAt: Date.now() }
                : order
            )),
          }));
        };

        const markRejected = (clientOrderId: string, reason: string) => {
          updateOrder(clientOrderId, { status: 'rejected', rejectReason: reason });
          // OCO sibling cascade: one leg rejected at the venue must not
          // leave the other leg running alone with the group still showing
          // active (2026-11-23 review P2). cancelRecord's own cascade is
          // suppressed — the initiating leg is already terminal.
          const rejected = get().orders.find((o) => o.clientOrderId === clientOrderId);
          if (rejected?.isOcoOrder && rejected.ocoLinkedOrderId) {
            const sibling = get().orders.find((candidate) => (
              candidate.clientOrderId === rejected.ocoLinkedOrderId &&
              CANCELLABLE_STATUSES.includes(candidate.status)
            ));
            if (sibling) void cancelRecord(sibling, false);
          }
        };

        const syncOcoGroups = () => {
          set((state) => ({
            ocoOrders: reconcileOcoGroups(state.ocoOrders, state.orders),
          }));
        };

        /** Submit one order to the backend and fold the answer back in. */
        const submitToBackend = async (order: PaperOrder): Promise<void> => {
          const market = marketOf(order.symbol);
          try {
            const res = await apiFetch('/orders', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                symbol: normalizeSymbol(order.symbol),
                side: order.side.toUpperCase(),
                order_type: backendType(order.type),
                quantity: Math.trunc(parseFloat(order.quantity)),
                price: (order.type === 'limit' || order.type === 'take_profit_limit' || order.type === 'stop_limit') && order.price
                  ? parseFloat(order.price)
                  : undefined,
                stop_price: (order.type === 'stop_limit' || order.type === 'stop_market') && order.triggerPrice
                  ? parseFloat(order.triggerPrice)
                  : undefined,
                trail_value: order.type === 'trailing_stop' && order.trailingValue
                  ? parseFloat(order.trailingValue)
                  : undefined,
                trail_type: order.type === 'trailing_stop' ? order.trailingType : undefined,
                // 2026-11-16 review P1: a venue-resting stop under the
                // backend's 'day' default expired at the session close —
                // the position went unprotected overnight while the local
                // record just moved to history. Protective orders ride GTC.
                time_in_force:
                  isConditionalOrder(order.type) || order.isOcoOrder ? 'gtc' : undefined,
                market,
                // Idempotency key: a retry after a lost response replays the
                // original order server-side instead of placing a duplicate.
                client_order_id: order.clientOrderId,
              }),
            });

            if (!res.ok) {
              const reason = await rejectionReason(res);
              notification.error(`order:reject:${order.clientOrderId}`, `ORDER_REJECTED: ${reason}`);
              markRejected(order.clientOrderId, reason);
              return;
            }

            const data = await res.json().catch(() => null);
            const backendId: string | undefined = data?.order_id || data?.id;
            const status = data?.status ? mapBackendStatus(data.status) : undefined;

            updateOrder(order.clientOrderId, {
              backendId,
              status: status && status !== 'pending' ? status : 'open',
            });

            // Arm the fill-linked TP/SL watch (2026-11-23 review P1): only
            // once the venue accepted the order — a rejected submission
            // must not leave protection triggers behind.
            if (order.takeProfitPrice || order.stopLossPrice) {
              pendingTpslByOrderId.set(order.clientOrderId, {
                takeProfitPrice: order.takeProfitPrice,
                stopLossPrice: order.stopLossPrice,
              });
            }

            // The broker moved cash or positions; pull the authoritative view.
            void useWalletStore.getState().fetchPortfolio();
          } catch {
            // Network error or 10s timeout: whether the backend received the
            // order is UNKNOWN — the old code marked it rejected, which sent
            // the user off to resubmit and place a duplicate live order
            // (2026-11-16 review). Keep it pending, reconcile against the
            // broker's own ledger: reconcileOrders matches the backend row
            // by client_order_id (attaching its backendId) or drops the
            // local ghost once the submit grace expires.
            notification.warning(
              `order:unconfirmed:${order.clientOrderId}`,
              'ORDER_UNCONFIRMED: connection dropped mid-submit — reconciling with broker',
            );
            await get().fetchOrders();
          }
        };

        /**
         * Cancel one local order, confirming with the broker first.
         * `cascade` mirrors a user cancel; a trigger cancels only the losing
         * OCO leg, never the leg that just fired.
         */
        const cancelRecord = async (order: PaperOrder, cascade = true): Promise<boolean> => {
          if (order.backendId) {
            try {
              const res = await apiFetch(`/orders/${encodeURIComponent(order.backendId)}`, {
                method: 'DELETE',
              });
              if (!res.ok && res.status !== 404) {
                notification.error('error:orderCancelFailed', `Cancel failed (HTTP ${res.status})`);
                await get().fetchOrders();
                return false;
              }
            } catch {
              notification.error('error:orderCancelFailed', 'Cancel request failed');
              await get().fetchOrders();
              return false;
            }
          }

          updateOrder(order.clientOrderId, { status: 'cancelled' });

          if (cascade && order.isOcoOrder && order.ocoLinkedOrderId) {
            const sibling = get().orders.find((candidate) => (
              candidate.clientOrderId === order.ocoLinkedOrderId &&
              CANCELLABLE_STATUSES.includes(candidate.status)
            ));
            if (sibling) await cancelRecord(sibling);
          }

          syncOcoGroups();
          return true;
        };

        const triggerOrder = (order: PaperOrder, label: string, price: string): void => {
          notification.info(`order:triggered:${order.clientOrderId}`, `${label} triggered: ${order.symbol} ${order.side.toUpperCase()} @ ${price}`);

          // A limit-shaped trigger keeps resting at its limit price; every
          // other conditional (stop market, trailing) becomes a market order.
          const nextType: OrderType = ['limit', 'stop_limit', 'take_profit_limit'].includes(order.type)
            ? 'limit'
            : 'market';

          updateOrder(order.clientOrderId, {
            isTriggered: true,
            triggeredAt: Date.now(),
            status: 'pending',
            type: nextType,
          });

          if (order.isOcoOrder && order.ocoLinkedOrderId) {
            const sibling = get().orders.find((candidate) => (
              candidate.clientOrderId === order.ocoLinkedOrderId &&
              CANCELLABLE_STATUSES.includes(candidate.status)
            ));
            if (sibling) void cancelRecord(sibling, false);
          }
          syncOcoGroups();

          const updated = get().orders.find((candidate) => candidate.clientOrderId === order.clientOrderId);
          if (updated) void submitToBackend(updated);
        };

        const checkConditionalOrders = (symbol: string, currentPrice: string, previousPrice: string) => {
          const candidates = get().orders.filter((order) => {
            if (order.symbol !== symbol || order.status !== 'open' || order.isTriggered) return false;
            // A venue-mirrored order is executed by the broker — the local
            // trigger firing on top would place a duplicate order. Its fill
            // arrives through reconcile, which cascades the OCO sibling.
            if (order.backendId) return false;
            if (isConditionalOrder(order.type)) return Boolean(order.triggerPrice);
            // An OCO's limit leg is a resting limit watched locally (only
            // when it was NOT mirrored — mirrored legs have a backendId).
            return Boolean(order.isOcoOrder && order.type === 'limit' && order.price);
          });

          for (const order of candidates) {
            // A sibling may have cancelled this leg earlier in the same tick.
            const live = get().orders.find((candidate) => candidate.clientOrderId === order.clientOrderId);
            if (!live || live.status !== 'open') continue;

            const fires = order.type === 'limit'
              ? shouldTriggerLimit(order, currentPrice)
              : shouldTriggerConditional(order, currentPrice, previousPrice);
            if (!fires) continue;

            const label = order.type === 'limit' ? 'OCO LIMIT' : order.type.toUpperCase().replace(/_/g, ' ');
            triggerOrder(order, label, currentPrice);
          }
        };

        const updateTrailingStops = (symbol: string, currentPrice: string) => {
          const candidates = get().orders.filter((order) => (
            order.symbol === symbol &&
            order.status === 'open' &&
            order.type === 'trailing_stop' &&
            !order.isTriggered &&
            // Venue-mirrored trailing (Alpaca native) trails server-side.
            !order.backendId
          ));

          for (const order of candidates) {
            const live = get().orders.find((candidate) => candidate.clientOrderId === order.clientOrderId);
            if (!live || live.status !== 'open') continue;

            const { patch, triggered } = evaluateTrailingStop(order, currentPrice);
            if (Object.keys(patch).length > 0) {
              updateOrder(order.clientOrderId, patch);
            }
            if (triggered) {
              triggerOrder(
                { ...order, ...patch },
                'Trailing Stop',
                patch.trailingStopPrice ?? order.trailingStopPrice ?? currentPrice
              );
            }
          }
        };

        return {
          orders: [],
          ocoOrders: [],
          positions: new Map(),
          focusMode: false,

          createOrder: (params, _currentMarketPrice) => {
            const {
              symbol, side, type, price, quantity,
              takeProfitPrice, stopLossPrice, triggerPrice, triggerDirection,
            } = params;
            const marketStore = useMarketStore.getState();

            const confidence = marketStore.dataConfidence;
            if (confidence.level === 'stale' || confidence.level === 'resyncing') {
              notification.error(`order:block:${Date.now()}`, `EXECUTION_BLOCKED: Data Integrity Critical (${confidence.level})`);
              return { blocked: 'data_confidence' };
            }

            // Number.isFinite guards: parseFloat("1e999") is Infinity and
            // parseFloat("abc") is NaN — both used to pass every `<= 0`
            // check, serialize to JSON null and die as a backend 422 AFTER
            // the UI had toasted "submitted" (2026-11-23 review P2).
            const qtyNum = parseFloat(quantity);
            if (!quantity || !Number.isFinite(qtyNum) || qtyNum <= 0 || qtyNum > 1e9) {
              return { blocked: 'invalid_quantity' };
            }

            const needsLimitPrice = ['limit', 'stop_limit', 'take_profit_limit'].includes(type);
            const needsTriggerPrice = isConditionalOrder(type);
            const priceNum = price ? parseFloat(price) : NaN;
            if (needsLimitPrice && (!price || !Number.isFinite(priceNum) || priceNum <= 0 || priceNum > 1e9)) {
              return { blocked: 'missing_price' };
            }
            const triggerNum = triggerPrice ? parseFloat(triggerPrice) : NaN;
            if (needsTriggerPrice && (!triggerPrice || !Number.isFinite(triggerNum) || triggerNum <= 0 || triggerNum > 1e9)) {
              return { blocked: 'missing_trigger' };
            }

            // (2026-11-23 review P3) 市场注册表未就绪时，港/日/A 股的每手大小
            // 与币种会按美股兜底（lot=1 / USD），整手取整和后续定价全错。
            // 非美股形态的符号（带 .HK/.T/.SS/.SH/.SZ 后缀）直接拦截，等
            // /api/markets 加载完成再下单；美股符号不受影响。
            const marketsNow = useMarketsStore.getState();
            if (marketsNow.markets.length === 0 && marketCodeOfSymbol(symbol, marketsNow.markets) !== 'US') {
              return { blocked: 'markets_loading' };
            }

            // Every venue trades whole shares; foreign markets add a board
            // lot on top of that. The backend re-validates, this only keeps
            // obviously-invalid input from leaving the form.
            const lot = Math.max(1, Math.floor(useMarketsStore.getState().lotSizeFor(symbol)));
            const execQuantity = Math.floor(parseFloat(quantity) / lot) * lot;
            if (!(execQuantity > 0)) return { blocked: 'lot_floor_zero' };

            const now = Date.now();
            const isConditional = isConditionalOrder(type);
            // Mirror decision made up front so the initial status is right:
            // venue-bound orders start 'pending' (the local trigger only
            // acts on 'open'), local-only conditionals start 'open'.
            const willMirror = !isConditional || type === 'stop_limit' || type === 'take_profit_limit';

            const order: PaperOrder = {
              clientOrderId: uuidv4(),
              symbol,
              side,
              type,
              price: needsLimitPrice ? price! : null,
              quantity: String(execQuantity),
              filledQty: '0',
              avgPrice: '0',
              status: willMirror ? 'pending' : 'open',
              createdAt: now,
              updatedAt: now,
              fills: [],
              takeProfitPrice,
              stopLossPrice,
              triggerPrice: needsTriggerPrice ? triggerPrice : undefined,
              triggerDirection: needsTriggerPrice
                ? (triggerDirection || getDefaultTriggerDirection(type, side))
                : undefined,
              isTriggered: false,
            };

            set((state) => ({ orders: [order, ...state.orders] }));

            // Conditional orders REST AT THE VENUE now: stop_limit /
            // take_profit_limit mirror for both markets (Alpaca native
            // stop_limit; the server-side virtual engine matches intl ones
            // on quotes), US trailing mirrors to Alpaca's trailing order.
            // The venue owns their execution; the local trigger evaluation
            // skips anything with a backendId so a price crossing cannot
            // place a duplicate. Non-mirrored conditionals (intl trailing,
            // trailing with an activation price) keep the old local path.
            if (mirrorToVenue(order)) void submitToBackend(order);
            return order;
          },

          createStopLimitOrder: ({ symbol, side, quantity, triggerPrice, limitPrice }) => (
            get().createOrder({
              symbol,
              side,
              type: 'stop_limit',
              quantity,
              triggerPrice,
              price: limitPrice,
              triggerDirection: getDefaultTriggerDirection('stop_limit', side),
            })
          ),

          createTakeProfitLimitOrder: ({ symbol, side, quantity, triggerPrice, limitPrice }) => (
            get().createOrder({
              symbol,
              side,
              type: 'take_profit_limit',
              quantity,
              triggerPrice,
              price: limitPrice,
              triggerDirection: getDefaultTriggerDirection('take_profit_limit', side),
            })
          ),

          createOCOOrder: ({ symbol, side, quantity, limitPrice, stopPrice, stopLimitPrice }) => {
            const confidence = useMarketStore.getState().dataConfidence;
            if (confidence.level === 'stale' || confidence.level === 'resyncing') {
              notification.error(`order:block:${Date.now()}`, `EXECUTION_BLOCKED: Data Integrity Critical (${confidence.level})`);
              // (2026-11-23 review P3) 返回结构化拒绝原因而不是 null：旧的
              // null 让调用方落进"余额不足"分支，把用户引向完全错误的排查方向。
              return { blocked: 'data_confidence' };
            }

            // Number.isFinite + range required: parseFloat("1e999") = Infinity
            // passes the old `> 0` check and serialises to JSON null → backend
            // 422 AFTER the UI's success toast (2026-11-23 second review P2-B).
            const finitePositive = (value: string) => {
              const n = parseFloat(value);
              return Number.isFinite(n) && n > 0 && n <= 1e9;
            };
            if (!finitePositive(limitPrice) || !finitePositive(stopPrice) || !finitePositive(stopLimitPrice)) {
              return { blocked: 'invalid_params' };
            }

            const lot = Math.max(1, Math.floor(useMarketsStore.getState().lotSizeFor(symbol)));
            const qtyNum = parseFloat(quantity);
            if (!Number.isFinite(qtyNum) || qtyNum <= 0 || qtyNum > 1e9) {
              return { blocked: 'invalid_params' };
            }
            const execQuantity = Math.floor(qtyNum / lot) * lot;
            if (!(execQuantity > 0)) return { blocked: 'invalid_params' };

            // Prices and sizing are validated by the broker when the winning
            // leg triggers; the OCO itself is only a client-side watch.
            const now = Date.now();
            const ocoGroupId = `oco_${uuidv4().slice(0, 8)}`;
            const limitOrderId = uuidv4();
            const stopOrderId = uuidv4();

            const limitOrder: PaperOrder = {
              clientOrderId: limitOrderId,
              symbol,
              side,
              type: 'limit',
              price: limitPrice,
              quantity: String(execQuantity),
              filledQty: '0',
              avgPrice: '0',
              status: 'pending',
              createdAt: now,
              updatedAt: now,
              fills: [],
              ocoGroupId,
              ocoLinkedOrderId: stopOrderId,
              isOcoOrder: true,
            };

            const stopOrder: PaperOrder = {
              clientOrderId: stopOrderId,
              symbol,
              side,
              type: 'stop_limit',
              price: stopLimitPrice,
              quantity: String(execQuantity),
              filledQty: '0',
              avgPrice: '0',
              status: 'pending',
              createdAt: now,
              updatedAt: now,
              fills: [],
              triggerPrice: stopPrice,
              triggerDirection: getDefaultTriggerDirection('stop_limit', side),
              isTriggered: false,
              ocoGroupId,
              ocoLinkedOrderId: limitOrderId,
              isOcoOrder: true,
            };

            const ocoOrder: OCOOrder = {
              ocoGroupId,
              symbol,
              side,
              quantity: String(execQuantity),
              limitPrice,
              stopPrice,
              stopLimitPrice,
              status: 'active',
              limitOrderId,
              stopOrderId,
              createdAt: now,
              updatedAt: now,
            };

            set((state) => ({
              orders: [limitOrder, stopOrder, ...state.orders],
              ocoOrders: [ocoOrder, ...state.ocoOrders],
            }));

            // Both legs mirror to the venue: the limit leg rests as a plain
            // limit, the stop leg as a native stop_limit. Whichever fills
            // first cascades a cancel of the sibling (see fetchOrders'
            // venue-fill cascade) — the OCO pairing lives in the client
            // metadata, the executions live at the venue.
            void submitToBackend(limitOrder);
            void submitToBackend(stopOrder);

            notification.success(`oco:created:${now}`, `OCO Order created: ${symbol} ${side.toUpperCase()}`);
            return ocoOrder;
          },

          createTrailingStopOrder: ({ symbol, side, quantity, trailingType, trailingValue, activationPrice }) => {
            const confidence = useMarketStore.getState().dataConfidence;
            if (confidence.level === 'stale' || confidence.level === 'resyncing') {
              notification.error(`order:block:${Date.now()}`, `EXECUTION_BLOCKED: Data Integrity Critical (${confidence.level})`);
              return { blocked: 'data_confidence' };
            }

            const currentPrice = useMarketStore.getState().metrics?.mid;
            if (!currentPrice) {
              notification.error(`order:price:${Date.now()}`, 'No market price available');
              return { blocked: 'data_confidence' };
            }

            const lot = Math.max(1, Math.floor(useMarketsStore.getState().lotSizeFor(symbol)));
            const execQuantity = Math.floor(parseFloat(quantity) / lot) * lot;
            if (!(execQuantity > 0)) return { blocked: 'lot_floor_zero' };

            const parsedValue = parseFloat(trailingValue);
            if (!Number.isFinite(parsedValue) || parsedValue <= 0) return { blocked: 'missing_price' };

            const current = new Decimal(currentPrice);
            const isSell = side === 'sell';
            const distance = trailingType === 'percent'
              ? current.times(parsedValue).div(100)
              : new Decimal(parsedValue);
            const stopPrice = isSell ? current.minus(distance) : current.plus(distance);

            const now = Date.now();
            const order: PaperOrder = {
              clientOrderId: uuidv4(),
              symbol,
              side,
              type: 'trailing_stop',
              price: null,
              quantity: String(execQuantity),
              filledQty: '0',
              avgPrice: '0',
              // Venue-mirrored trailing starts 'pending' until the POST
              // confirms; local-only trailing (intl, activation-gated)
              // starts 'open' and is watched by the local trigger.
              status: 'pending',
              createdAt: now,
              updatedAt: now,
              fills: [],
              trailingType: trailingType as TrailingType,
              trailingValue,
              trailingActivationPrice: activationPrice,
              trailingHighestPrice: isSell ? currentPrice : undefined,
              trailingLowestPrice: isSell ? undefined : currentPrice,
              trailingStopPrice: stopPrice.toFixed(8),
              isTriggered: false,
            };

            set((state) => ({ orders: [order, ...state.orders] }));
            // US trailing without an activation price mirrors to the venue's
            // native trailing order (server-side monitoring); everything
            // else stays on the local trigger path — flip those back to
            // 'open' so the local trigger watches them.
            if (mirrorToVenue(order)) {
              void submitToBackend(order);
            } else {
              updateOrder(order.clientOrderId, { status: 'open' });
            }
            notification.success(`trailing:created:${now}`, `Trailing Stop created: ${symbol} ${side.toUpperCase()}`);
            return order;
          },

          cancelOCOOrder: async (ocoGroupId) => {
            const group = get().ocoOrders.find((candidate) => candidate.ocoGroupId === ocoGroupId);
            if (!group || group.status === 'cancelled') return false;

            const legs = get().orders.filter((order) => (
              order.ocoGroupId === ocoGroupId && CANCELLABLE_STATUSES.includes(order.status)
            ));
            if (legs.length === 0) {
              set((state) => ({
                ocoOrders: state.ocoOrders.map((candidate) => (
                  candidate.ocoGroupId === ocoGroupId
                    ? { ...candidate, status: 'cancelled', updatedAt: Date.now() }
                    : candidate
                )),
              }));
              return true;
            }

            // Cancelling one leg cascades to the other.
            return cancelRecord(legs[0]);
          },

          cancelOrder: async (clientOrderId) => {
            const order = get().orders.find((candidate) => candidate.clientOrderId === clientOrderId);
            if (!order || !CANCELLABLE_STATUSES.includes(order.status)) return false;
            return cancelRecord(order);
          },

          evaluateOrderTriggers: (orderBook) => {
            const bestBid = orderBook.bids[0];
            const bestAsk = orderBook.asks[0];
            if (!bestBid || !bestAsk) return;

            const midPrice = new Decimal(bestBid.price).plus(bestAsk.price).div(2).toFixed(8);
            const previousPrice = previousPrices.get(orderBook.symbol) ?? midPrice;
            previousPrices.set(orderBook.symbol, midPrice);
            // Bound the map: one entry per symbol ever subscribed grows on
            // symbol-lookup trading (2026-11-16 review).
            if (previousPrices.size > 200) {
              const oldestKey = previousPrices.keys().next().value;
              if (oldestKey) previousPrices.delete(oldestKey);
            }

            checkConditionalOrders(orderBook.symbol, midPrice, previousPrice);
            updateTrailingStops(orderBook.symbol, midPrice);
            // (2026-11-23 review P3) checkPositionTpsl 已随 position 级 TP/SL
            // 机制一并删除：position.takeProfitPrice/stopLossPrice 从未由任何
            // 存活代码路径写入，保护全部走 automationStore triggers。
          },

          setFocusMode: (enabled) => set({ focusMode: enabled }),

          /** Reset the broker account, then drop every local copy of it. */
          resetAccount: async () => {
            let initialCapital = 100000;
            try {
              const portfolio = await apiFetch('/portfolio');
              if (portfolio.ok) {
                const data = await portfolio.json();
                const reported = Number(data?.initial_capital);
                if (Number.isFinite(reported) && reported > 0) initialCapital = reported;
              }

              const reset = await apiFetch('/portfolio/reset', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ initial_capital: initialCapital }),
              });
              if (!reset.ok) {
                notification.error('error:resetFailed', `RESET_REJECTED: ${await rejectionReason(reset)}`);
              }
            } catch {
              notification.error('error:resetFailed', 'RESET_REJECTED: broker unreachable');
            }

            get().clearLocalState();
            await useWalletStore.getState().fetchPortfolio();
          },

          clearLocalState: () => {
            set({ orders: [], ocoOrders: [], positions: new Map() });
            previousPrices.clear();
          },

          syncPositionsFromBackend: (backendPositions) => {
            set({ positions: reconcilePositions(get().positions, backendPositions) });
          },

          fetchOrders: async () => {
            try {
              const res = await apiFetch('/orders');
              if (!res.ok) return;
              const rows: BackendOrderRow[] = await res.json();
              if (!Array.isArray(rows)) return;

              const before = get().orders;
              set((state) => {
                const orders = reconcileOrders(state.orders, rows);
                return { orders, ocoOrders: reconcileOcoGroups(state.ocoOrders, orders) };
              });
              // Venue-fill OCO cascade: a mirrored leg that the venue just
              // filled must cancel its sibling there too — the OCO pairing
              // is client metadata, so the cascade lives on the client.
              const filledNow = new Set(
                before
                  .filter((o) => o.ocoGroupId && o.backendId && !['filled', 'partial'].includes(o.status))
                  .map((o) => o.clientOrderId)
              );
              for (const order of get().orders) {
                if (!filledNow.has(order.clientOrderId)) continue;
                // Venue OCO semantics: only a FULLY filled leg cancels the
                // sibling. A partial fill must keep both legs working —
                // cancelling on 'partial' left the remaining quantity
                // without a stop (2026-11-16 review P2).
                if (order.status !== 'filled') continue;
                const sibling = get().orders.find((candidate) => (
                  candidate.clientOrderId === order.ocoLinkedOrderId &&
                  CANCELLABLE_STATUSES.includes(candidate.status)
                ));
                if (sibling) void cancelRecord(sibling, false);
              }
              // Trade statistics are a pure view over these reconciled fills.
              useWalletStore.getState().syncPerformanceFromOrders(get().orders);

              // Fill-linked TP/SL (2026-11-23 review P1): a just-filled BUY
              // whose entry carried TP/SL values now creates the protection
              // triggers. Entries on terminally-failed orders are dropped.
              const beforeFilled = new Set(
                before.filter((o) => o.status === 'filled').map((o) => o.clientOrderId),
              );
              for (const order of get().orders) {
                if (['rejected', 'cancelled', 'expired'].includes(order.status)) {
                  pendingTpslByOrderId.delete(order.clientOrderId);
                  continue;
                }
                if (
                  order.status === 'filled' &&
                  !beforeFilled.has(order.clientOrderId) &&
                  order.side === 'buy'
                ) {
                  createTpslTriggersFromFill(order);
                }
              }

            } catch (err) {
              // Offline or broker hiccup: keep the last known view — but the
              // failure used to vanish entirely, leaving the terminal on a
              // stale ledger with no signal (2026-11-23 review P2).
              console.error('[TradingStore] Order reconcile failed:', err);
            }
          },

          getOpenOrders: () => get().orders.filter((order) => (
            ['open', 'partial', 'pending', 'submitted'].includes(order.status) && !isConditionalOrder(order.type)
          )),
        };
      },
      {
        name: 'paper-trading-storage',
        version: 5,
        storage: customStorage as any,
        // Server-backed orders and positions are never persisted: they are
        // rebuilt from the broker on every load, so a stale local copy cannot
        // resurrect a closed position or a phantom order.
        partialize: (s) => ({
          orders: s.orders.filter((order) => isUnsentLocalOrder(order)),
          ocoOrders: s.ocoOrders.filter((order) => order.status === 'active' || order.status === 'triggered'),
          positions: new Map<string, Position>(),
          focusMode: false,
        }),
        migrate: (persisted: any) => {
          const state = persisted?.state ?? {};
          const orders: PaperOrder[] = Array.isArray(state.orders) ? state.orders : [];
          const surviving = orders.filter((order) => isUnsentLocalOrder(order));
          const survivingIds = new Set(surviving.map((order) => order.clientOrderId));
          return {
            ...state,
            orders: surviving,
            positions: new Map<string, Position>(),
            // Drop groups whose legs did not survive the migration.
            ocoOrders: Array.isArray(state.ocoOrders)
              ? state.ocoOrders.filter((group: OCOOrder) => (
                  survivingIds.has(group.limitOrderId) || survivingIds.has(group.stopOrderId)
                ))
              : [],
          };
        },
      }
    )
  )
);

// ===== Selectors =====
export const selectOrders = (state: TradingState) => state.orders;
export const selectOpenOrders = (state: TradingState) => state.getOpenOrders();
export const selectFocusMode = (state: TradingState) => state.focusMode;
export const selectPositions = (state: TradingState) => state.positions;
