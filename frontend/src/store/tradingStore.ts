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
  evaluatePositionTpsl,
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
  createOCOOrder: (params: CreateOCOParams) => OCOOrder | null;
  createTrailingStopOrder: (params: CreateTrailingStopParams) => CreateOrderResult;
  cancelOCOOrder: (ocoGroupId: string) => Promise<boolean>;

  // Client-side trigger evaluation
  evaluateOrderTriggers: (orderBook: OrderBook) => void;

  // Position management
  setFocusMode: (enabled: boolean) => void;
  updatePositionTPSL: (symbol: string, takeProfitPrice?: string, stopLossPrice?: string) => void;
  resetAccount: () => Promise<void>;
  clearLocalState: () => void;
  syncPositionsFromBackend: (backendPositions: Record<string, BackendPosition>) => void;
  fetchOrders: () => Promise<void>;

  // Getters
  getOrder: (clientOrderId: string) => PaperOrder | undefined;
  getOpenOrders: () => PaperOrder[];
  getConditionalOrders: () => PaperOrder[];
  getOrderHistory: () => PaperOrder[];
  getPosition: (symbol: string) => Position | undefined;
  getOCOOrders: () => OCOOrder[];
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
  | 'missing_price'
  | 'missing_trigger'
  | 'lot_floor_zero';

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

        const checkPositionTpsl = (symbol: string, midPrice: string) => {
          const position = get().positions.get(symbol);
          if (!position) return;
          const trigger = evaluatePositionTpsl(position, midPrice);
          if (!trigger) return;

          // Clear the local targets first so a rapid tick cannot fire twice.
          get().updatePositionTPSL(symbol, undefined, undefined);
          notification.info(
            `order:tpsl:${symbol}`,
            `${trigger === 'take_profit' ? 'Take profit' : 'Stop loss'} triggered: ${symbol}`
          );
          get().createOrder({ symbol, side: 'sell', type: 'market', quantity: position.quantity }, midPrice);
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

            if (!quantity || parseFloat(quantity) <= 0) return { blocked: 'invalid_quantity' };

            const needsLimitPrice = ['limit', 'stop_limit', 'take_profit_limit'].includes(type);
            const needsTriggerPrice = isConditionalOrder(type);
            if (needsLimitPrice && (!price || parseFloat(price) <= 0)) return { blocked: 'missing_price' };
            if (needsTriggerPrice && (!triggerPrice || parseFloat(triggerPrice) <= 0)) return { blocked: 'missing_trigger' };

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
              return null;
            }

            if (!(parseFloat(limitPrice) > 0) || !(parseFloat(stopPrice) > 0) || !(parseFloat(stopLimitPrice) > 0)) {
              return null;
            }

            const lot = Math.max(1, Math.floor(useMarketsStore.getState().lotSizeFor(symbol)));
            const execQuantity = Math.floor(parseFloat(quantity) / lot) * lot;
            if (!(execQuantity > 0)) return null;

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
            checkPositionTpsl(orderBook.symbol, midPrice);
          },

          setFocusMode: (enabled) => set({ focusMode: enabled }),

          updatePositionTPSL: (symbol, takeProfitPrice, stopLossPrice) => {
            set((state) => {
              const newPositions = new Map(state.positions);
              const position = newPositions.get(symbol);
              if (position) {
                newPositions.set(symbol, {
                  ...position,
                  takeProfitPrice,
                  stopLossPrice,
                  updatedAt: Date.now(),
                });
              }
              return { positions: newPositions };
            });
          },

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
                if (!['filled', 'partial'].includes(order.status)) continue;
                const sibling = get().orders.find((candidate) => (
                  candidate.clientOrderId === order.ocoLinkedOrderId &&
                  CANCELLABLE_STATUSES.includes(candidate.status)
                ));
                if (sibling) void cancelRecord(sibling, false);
              }
              // Trade statistics are a pure view over these reconciled fills.
              useWalletStore.getState().syncPerformanceFromOrders(get().orders);

            } catch {
              // Offline or broker hiccup: keep the last known view.
            }
          },

          getOrder: (id) => get().orders.find((order) => order.clientOrderId === id),
          getOpenOrders: () => get().orders.filter((order) => (
            ['open', 'partial', 'pending', 'submitted'].includes(order.status) && !isConditionalOrder(order.type)
          )),
          getConditionalOrders: () => get().orders.filter((order) => (
            order.status === 'open' && isConditionalOrder(order.type) && !order.isTriggered
          )),
          getOrderHistory: () => get().orders.filter((order) => (
            ['filled', 'cancelled', 'rejected', 'expired'].includes(order.status)
          )),
          getPosition: (symbol) => get().positions.get(symbol),
          getOCOOrders: () => get().ocoOrders.filter((order) => order.status === 'active'),
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
export const selectConditionalOrders = (state: TradingState) => state.getConditionalOrders();
export const selectOCOOrders = (state: TradingState) => state.getOCOOrders();
export const selectFocusMode = (state: TradingState) => state.focusMode;
export const selectPositions = (state: TradingState) => state.positions;
