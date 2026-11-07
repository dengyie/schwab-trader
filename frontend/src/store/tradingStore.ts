import { create } from 'zustand';
import { subscribeWithSelector, persist } from 'zustand/middleware';
import { v4 as uuidv4 } from 'uuid';
import Decimal from 'decimal.js';
import type {
  PaperOrder,
  Fill,
  Position,
  OrderStatus,
  OrderSide,
  OrderType,
  OCOOrder,
  TriggerDirection,
  TrailingType,
  CreateOCOParams,
  CreateTrailingStopParams,
} from '../types/trading';
import type { OrderBook } from '../types/market';
import { useWalletStore } from './walletStore';
import { useMarketStore } from './marketStore';
import { useMarketsStore, marketCodeOfSymbol } from './marketsStore';
import { notification } from './notificationStore';
import { apiFetch } from '../services/apiClient';

// ===== Constants =====
const FEE_RATE = 0.001; // 0.1% fee
const SIMULATED_DELAY_MIN_MS = 50;
const SIMULATED_DELAY_MAX_MS = 200;

// ===== Helper: Check if order type is conditional =====
const isConditionalOrder = (type: OrderType): boolean => {
  return ['stop_limit', 'take_profit_limit', 'stop_market', 'take_profit_market', 'trailing_stop'].includes(type);
};

// ===== Helper: Get trigger direction based on order type and side =====
const getDefaultTriggerDirection = (type: OrderType, side: OrderSide): TriggerDirection => {
  // Stop orders trigger when price moves against position
  // Take profit orders trigger when price moves in favor
  if (type === 'stop_limit' || type === 'stop_market') {
    return side === 'buy' ? 'up' : 'down'; // Buy stop triggers on price going up, sell stop on price going down
  }
  if (type === 'take_profit_limit' || type === 'take_profit_market') {
    return side === 'buy' ? 'down' : 'up'; // Buy TP triggers on price going down, sell TP on price going up
  }
  return 'down';
};

interface TradingState {
  orders: PaperOrder[];
  ocoOrders: OCOOrder[];
  positions: Map<string, Position>;
  focusMode: boolean;
  
  // Basic order operations
  createOrder: (params: CreateOrderParams, currentMarketPrice?: string) => PaperOrder | null;
        cancelOrder: (clientOrderId: string) => Promise<boolean>;
  
  // Advanced order operations
  createStopLimitOrder: (params: CreateStopLimitParams) => PaperOrder | null;
  createTakeProfitLimitOrder: (params: CreateTakeProfitLimitParams) => PaperOrder | null;
  createOCOOrder: (params: CreateOCOParams) => OCOOrder | null;
  createTrailingStopOrder: (params: CreateTrailingStopParams) => PaperOrder | null;
  cancelOCOOrder: (ocoGroupId: string) => boolean;
  
  // Order book matching
  updateOrderBookForMatching: (orderBook: OrderBook) => void;
  checkConditionalOrders: (symbol: string, currentPrice: string, previousPrice: string) => void;
  updateTrailingStops: (symbol: string, currentPrice: string) => void;
  
  // Position management
  checkTPSL: (symbol: string, midPrice: string) => void;
  setFocusMode: (enabled: boolean) => void;
  updatePositionTPSL: (symbol: string, takeProfitPrice?: string, stopLossPrice?: string) => void;
  resetAccount: () => void;
  syncPositionsFromBackend: (backendPositions: Record<string, any>) => void;
  fetchOrders: () => Promise<void>;
  
  // Getters
  getOrder: (clientOrderId: string) => PaperOrder | undefined;
  getOpenOrders: () => PaperOrder[];
  getConditionalOrders: () => PaperOrder[];
  getOrderHistory: () => PaperOrder[];
  getPosition: (symbol: string) => Position | undefined;
  getOCOOrders: () => OCOOrder[];
}

interface CreateOrderParams {
  symbol: string;
  side: OrderSide;
  type: OrderType;
  price?: string;
  quantity: string;
  takeProfitPrice?: string;
  stopLossPrice?: string;
  // Conditional order params
  triggerPrice?: string;
  triggerDirection?: TriggerDirection;
}

interface CreateStopLimitParams {
  symbol: string;
  side: OrderSide;
  quantity: string;
  triggerPrice: string;    // Price at which order is triggered
  limitPrice: string;      // Limit price after trigger
}

interface CreateTakeProfitLimitParams {
  symbol: string;
  side: OrderSide;
  quantity: string;
  triggerPrice: string;    // Price at which order is triggered
  limitPrice: string;      // Limit price after trigger
}

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
    } catch (e) { return null; }
  },
  setItem: (name: string, value: { state: TradingState; version?: number }) => {
    try {
      const toStore = { ...value, state: { ...value.state, positions: value.state.positions instanceof Map ? Array.from(value.state.positions.entries()) : value.state.positions } };
      localStorage.setItem(name, JSON.stringify(toStore));
    } catch (e) {}
  },
  removeItem: (name: string) => localStorage.removeItem(name),
};

// Track previous prices for conditional order triggering
let previousPrices: Map<string, string> = new Map();

export const useTradingStore = create<TradingState>()(
  subscribeWithSelector(
    persist(
      (set, get) => ({
        orders: [],
        ocoOrders: [],
        positions: new Map(),
        focusMode: false,

        createOrder: (params, currentMarketPrice) => {
          const { symbol, side, type, price, quantity, takeProfitPrice, stopLossPrice, triggerPrice, triggerDirection } = params;
          const walletStore = useWalletStore.getState();
          const marketStore = useMarketStore.getState();

          // Data Confidence Check
          const confidence = marketStore.dataConfidence;
          if (confidence.level === 'stale' || confidence.level === 'resyncing') {
            notification.error(`order:block:${Date.now()}`, `EXECUTION_BLOCKED: Data Integrity Critical (${confidence.level})`);
            return null;
          }

          if (!quantity || parseFloat(quantity) <= 0) return null;
          
          // Validation based on order type
          const needsLimitPrice = ['limit', 'stop_limit', 'take_profit_limit'].includes(type);
          const needsTriggerPrice = isConditionalOrder(type);
          
          if (needsLimitPrice && (!price || parseFloat(price) <= 0)) return null;
          if (needsTriggerPrice && (!triggerPrice || parseFloat(triggerPrice) <= 0)) return null;

          // 美股由本地钱包 + 撮合镜像管账；港股/日股/A 股的下单簿在后端
          // 虚拟撮合引擎里，本地 USD 钱包无法结算外币，因此直接交给后端。
          const marketsState = useMarketsStore.getState();
          const market = marketCodeOfSymbol(symbol, marketsState.markets);
          const isDelegated = market !== 'US';

          // 非美股必须整手：100 股的倍数（美股 lot=1 时保持原行为）。
          const lot = isDelegated ? Math.max(1, marketsState.lotSizeFor(symbol)) : 1;
          const rawQuantity = parseFloat(quantity);
          const execQuantity = lot > 1 ? Math.floor(rawQuantity / lot) * lot : Math.round(rawQuantity);
          if (!(execQuantity > 0)) return null;
          const quantityText = String(execQuantity);

          const baseAsset = symbol.replace('USDT', '');
          const quoteAsset = isDelegated ? marketsState.currencyOf(symbol) : 'USD';
          let requiredAmount: string;
          let assetToCheck: string;

          // Calculate required amount based on order type
          if (side === 'buy') {
            assetToCheck = quoteAsset;
            if (needsLimitPrice) {
              requiredAmount = new Decimal(price!).times(quantityText).toFixed(8);
            } else if (needsTriggerPrice && triggerPrice) {
              // For stop/TP market orders, use trigger price as estimate
              requiredAmount = new Decimal(triggerPrice).times(quantityText).times(1.05).toFixed(8);
            } else {
              const mPrice = currentMarketPrice || useMarketStore.getState().metrics?.mid || '100.0';
              requiredAmount = new Decimal(mPrice).times(quantityText).times(1.05).toFixed(8);
            }
          } else {
            assetToCheck = baseAsset;
            requiredAmount = quantityText;
          }

          // 只有美股在这里做本地余额校验，外币账户由后端统一记账。
          if (!isDelegated) {
            const balance = walletStore.getBalance(assetToCheck);
            if (!balance || new Decimal(balance.available).lt(requiredAmount)) {
              notification.error(`order:balance:${Date.now()}`, 'INSUFFICIENT_MARGIN: Order rejected by engine');
              return null;
            }
          }

          const now = Date.now();
          const isConditional = isConditionalOrder(type);
          
          const order: PaperOrder = {
            clientOrderId: uuidv4(),
            symbol, side, type,
            price: needsLimitPrice ? price! : null,
            quantity: quantityText, filledQty: '0', avgPrice: '0',
            status: isConditional ? 'open' : 'pending', 
            createdAt: now, updatedAt: now,
            fills: [], takeProfitPrice, stopLossPrice,
            // Conditional order fields
            triggerPrice: needsTriggerPrice ? triggerPrice : undefined,
            triggerDirection: needsTriggerPrice ? (triggerDirection || getDefaultTriggerDirection(type, side)) : undefined,
            isTriggered: false,
          };

          set((state) => ({ orders: [order, ...state.orders] }));

          // For conditional orders, freeze balance immediately but don't try to match
          if (isConditional && !isDelegated) {
            setTimeout(() => {
              const walletState = useWalletStore.getState();
              let freezeAmount: string;
              let freezeAsset: string;

              if (side === 'buy') {
                freezeAsset = quoteAsset;
                if (needsLimitPrice) {
                  freezeAmount = new Decimal(price!).times(quantity).toFixed(8);
                } else {
                  freezeAmount = new Decimal(triggerPrice!).times(quantity).times(1.05).toFixed(8);
                }
              } else {
                freezeAsset = baseAsset;
                freezeAmount = quantity;
              }

              const frozen = walletState.freezeBalance(freezeAsset, freezeAmount, order.clientOrderId, 'order');
              if (!frozen) {
                set((s) => ({ orders: s.orders.map(o => o.clientOrderId === order.clientOrderId ? { ...o, status: 'rejected', rejectReason: 'Insufficient balance', updatedAt: Date.now() } : o) }));
              } else {
                set((s) => ({ orders: s.orders.map(o => o.clientOrderId === order.clientOrderId ? { ...o, frozenAmount: freezeAmount, frozenAsset: freezeAsset } : o) }));
              }
            }, SIMULATED_DELAY_MIN_MS);
            
            return order;
          }

          // Regular order processing
          if (!isDelegated) {
          setTimeout(() => {
            const currentState = get();
            const walletState = useWalletStore.getState();
            const currentOrder = currentState.orders.find(o => o.clientOrderId === order.clientOrderId);
            
            if (!currentOrder || currentOrder.status !== 'pending') return;

            let freezeAmount: string;
            let freezeAsset: string;

            if (currentOrder.side === 'buy') {
              freezeAsset = quoteAsset;
              if (currentOrder.type === 'limit') freezeAmount = new Decimal(currentOrder.price!).times(currentOrder.quantity).toFixed(8);
              else freezeAmount = new Decimal(currentMarketPrice || '0').times(currentOrder.quantity).times(1.05).toFixed(8);
            } else {
              freezeAsset = baseAsset;
              freezeAmount = currentOrder.quantity;
            }

            const frozen = walletState.freezeBalance(freezeAsset, freezeAmount, order.clientOrderId, 'order');

            if (!frozen) {
              set((s) => ({ orders: s.orders.map(o => o.clientOrderId === order.clientOrderId ? { ...o, status: 'rejected', rejectReason: 'Insufficient balance', updatedAt: Date.now() } : o) }));
              return;
            }

            set((s) => ({ orders: s.orders.map(o => o.clientOrderId === order.clientOrderId ? { ...o, status: 'open', frozenAmount: freezeAmount, frozenAsset: freezeAsset, updatedAt: Date.now() } : o) }));
            
            const ms = useMarketStore.getState();
            if (ms.orderBook && ms.orderBook.symbol === order.symbol) get().updateOrderBookForMatching(ms.orderBook);
          }, SIMULATED_DELAY_MIN_MS + Math.random() * (SIMULATED_DELAY_MAX_MS - SIMULATED_DELAY_MIN_MS));
          }

          // Submit order to FastAPI matching engine / Alpaca broker
          try {
            apiFetch('/orders', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                symbol: symbol.replace('/USD', '').replace('USDT', ''),
                side: side.toUpperCase(),
                order_type: type === 'limit' ? 'LIMIT' : (type.includes('stop') ? 'STOP' : 'MARKET'),
                quantity: execQuantity,
                price: price ? parseFloat(price) : undefined,
                stop_price: triggerPrice ? parseFloat(triggerPrice) : undefined,
                // 裸数字代码（如 "0700"）需要市场提示；带后缀的规范符号后端可自行解析。
                market,
              }),
            })
              .then(async (res) => {
                if (res.ok) {
                  const data = await res.json();
                  const backendId = data?.order_id || data?.id;
                  if (backendId) {
                    set((s) => ({
                      orders: s.orders.map(o => o.clientOrderId === order.clientOrderId ? { ...o, backendId } : o)
                    }));
                  }
                  // 外币订单的持仓/现金由后端变更，立即拉一次快照回填。
                  if (isDelegated) {
                    void get().fetchOrders();
                    void useWalletStore.getState().fetchPortfolio();
                  }
                } else if (isDelegated) {
                  // 后端拒绝（整手/涨跌停/休市）必须让用户看到，而不是静默成功。
                  let reason = '';
                  try {
                    const err = await res.json();
                    reason = typeof err?.detail === 'string' ? err.detail : JSON.stringify(err?.detail ?? err);
                  } catch {
                    reason = `HTTP ${res.status}`;
                  }
                  notification.error(`order:reject:${order.clientOrderId}`, `ORDER_REJECTED: ${reason}`);
                  set((s) => ({
                    orders: s.orders.map(o => o.clientOrderId === order.clientOrderId
                      ? { ...o, status: 'rejected', rejectReason: reason, updatedAt: Date.now() }
                      : o),
                  }));
                }
              })
              .catch(() => {});
          } catch {}

          return order;
        },

        // ===== Stop-Limit Order =====
        createStopLimitOrder: (params) => {
          const { symbol, side, quantity, triggerPrice, limitPrice } = params;
          return get().createOrder({
            symbol,
            side,
            type: 'stop_limit',
            quantity,
            triggerPrice,
            price: limitPrice,
            triggerDirection: getDefaultTriggerDirection('stop_limit', side),
          });
        },

        // ===== Take-Profit-Limit Order =====
        createTakeProfitLimitOrder: (params) => {
          const { symbol, side, quantity, triggerPrice, limitPrice } = params;
          return get().createOrder({
            symbol,
            side,
            type: 'take_profit_limit',
            quantity,
            triggerPrice,
            price: limitPrice,
            triggerDirection: getDefaultTriggerDirection('take_profit_limit', side),
          });
        },

        // ===== OCO Order (One-Cancels-Other) =====
        createOCOOrder: (params) => {
          const { symbol, side, quantity, limitPrice, stopPrice, stopLimitPrice } = params;
          const walletStore = useWalletStore.getState();
          const marketStore = useMarketStore.getState();

          // Data Confidence Check
          const confidence = marketStore.dataConfidence;
          if (confidence.level === 'stale' || confidence.level === 'resyncing') {
            notification.error(`order:block:${Date.now()}`, `EXECUTION_BLOCKED: Data Integrity Critical (${confidence.level})`);
            return null;
          }

          const baseAsset = symbol.replace('USDT', '');
          const quoteAsset = 'USD';
          
          // Calculate required amount (only need to freeze once since OCO is either/or)
          let requiredAmount: string;
          let assetToCheck: string;

          if (side === 'buy') {
            assetToCheck = quoteAsset;
            // Use the higher price for buy orders
            const maxPrice = Decimal.max(limitPrice, stopLimitPrice);
            requiredAmount = maxPrice.times(quantity).toFixed(8);
          } else {
            assetToCheck = baseAsset;
            requiredAmount = quantity;
          }

          const balance = walletStore.getBalance(assetToCheck);
          if (!balance || new Decimal(balance.available).lt(requiredAmount)) {
            notification.error(`order:balance:${Date.now()}`, 'INSUFFICIENT_MARGIN: OCO Order rejected');
            return null;
          }

          const now = Date.now();
          const ocoGroupId = `oco_${uuidv4().slice(0, 8)}`;
          const limitOrderId = uuidv4();
          const stopOrderId = uuidv4();

          // Create limit order
          const limitOrder: PaperOrder = {
            clientOrderId: limitOrderId,
            symbol, side, type: 'limit',
            price: limitPrice,
            quantity, filledQty: '0', avgPrice: '0',
            status: 'open', createdAt: now, updatedAt: now,
            fills: [],
            ocoGroupId,
            ocoLinkedOrderId: stopOrderId,
            isOcoOrder: true,
          };

          // Create stop-limit order
          const stopOrder: PaperOrder = {
            clientOrderId: stopOrderId,
            symbol, side, type: 'stop_limit',
            price: stopLimitPrice,
            quantity, filledQty: '0', avgPrice: '0',
            status: 'open', createdAt: now, updatedAt: now,
            fills: [],
            triggerPrice: stopPrice,
            triggerDirection: getDefaultTriggerDirection('stop_limit', side),
            isTriggered: false,
            ocoGroupId,
            ocoLinkedOrderId: limitOrderId,
            isOcoOrder: true,
          };

          // Create OCO record
          const ocoOrder: OCOOrder = {
            ocoGroupId,
            symbol, side, quantity,
            limitPrice, stopPrice, stopLimitPrice,
            status: 'active',
            limitOrderId, stopOrderId,
            createdAt: now, updatedAt: now,
          };

          // Freeze balance
          const freezeAsset = side === 'buy' ? quoteAsset : baseAsset;
          const freezeAmount = side === 'buy' 
            ? new Decimal(limitPrice).times(quantity).toFixed(8)
            : quantity;
          
          const frozen = walletStore.freezeBalance(freezeAsset, freezeAmount, ocoGroupId, 'order');
          if (!frozen) {
            notification.error(`order:balance:${Date.now()}`, 'INSUFFICIENT_MARGIN: Failed to freeze balance');
            return null;
          }

          set((state) => ({
            orders: [limitOrder, stopOrder, ...state.orders],
            ocoOrders: [ocoOrder, ...state.ocoOrders],
          }));

          notification.success(`oco:created:${now}`, `OCO Order created: ${symbol} ${side.toUpperCase()}`);
          return ocoOrder;
        },

        // ===== Trailing Stop Order =====
        createTrailingStopOrder: (params) => {
          const { symbol, side, quantity, trailingType, trailingValue, activationPrice } = params;
          const walletStore = useWalletStore.getState();
          const marketStore = useMarketStore.getState();

          const confidence = marketStore.dataConfidence;
          if (confidence.level === 'stale' || confidence.level === 'resyncing') {
            notification.error(`order:block:${Date.now()}`, `EXECUTION_BLOCKED: Data Integrity Critical (${confidence.level})`);
            return null;
          }

          const baseAsset = symbol.replace('USDT', '');
          const quoteAsset = 'USD';
          
          // Get current price for initial calculations
          const currentPrice = marketStore.metrics?.mid;
          if (!currentPrice) {
            notification.error(`order:price:${Date.now()}`, 'No market price available');
            return null;
          }

          let requiredAmount: string;
          let assetToCheck: string;

          if (side === 'buy') {
            assetToCheck = quoteAsset;
            requiredAmount = new Decimal(currentPrice).times(quantity).times(1.1).toFixed(8);
          } else {
            assetToCheck = baseAsset;
            requiredAmount = quantity;
          }

          const balance = walletStore.getBalance(assetToCheck);
          if (!balance || new Decimal(balance.available).lt(requiredAmount)) {
            notification.error(`order:balance:${Date.now()}`, 'INSUFFICIENT_MARGIN: Order rejected');
            return null;
          }

          const now = Date.now();
          const currentPriceDec = new Decimal(currentPrice);
          
          // Calculate initial trailing stop price
          let trailingStopPrice: string;
          if (side === 'sell') {
            // For sell trailing stop, stop price is below current price
            if (trailingType === 'percent') {
              trailingStopPrice = currentPriceDec.times(1 - parseFloat(trailingValue) / 100).toFixed(8);
            } else {
              trailingStopPrice = currentPriceDec.minus(trailingValue).toFixed(8);
            }
          } else {
            // For buy trailing stop, stop price is above current price
            if (trailingType === 'percent') {
              trailingStopPrice = currentPriceDec.times(1 + parseFloat(trailingValue) / 100).toFixed(8);
            } else {
              trailingStopPrice = currentPriceDec.plus(trailingValue).toFixed(8);
            }
          }

          const order: PaperOrder = {
            clientOrderId: uuidv4(),
            symbol, side, type: 'trailing_stop',
            price: null,
            quantity, filledQty: '0', avgPrice: '0',
            status: 'open', createdAt: now, updatedAt: now,
            fills: [],
            trailingType,
            trailingValue,
            trailingActivationPrice: activationPrice,
            trailingHighestPrice: side === 'sell' ? currentPrice : undefined,
            trailingLowestPrice: side === 'buy' ? currentPrice : undefined,
            trailingStopPrice,
            isTriggered: false,
          };

          // Freeze balance
          const freezeAsset = side === 'buy' ? quoteAsset : baseAsset;
          const freezeAmount = side === 'buy' 
            ? new Decimal(currentPrice).times(quantity).times(1.1).toFixed(8)
            : quantity;
          
          const frozen = walletStore.freezeBalance(freezeAsset, freezeAmount, order.clientOrderId, 'order');
          if (!frozen) {
            notification.error(`order:balance:${Date.now()}`, 'INSUFFICIENT_MARGIN: Failed to freeze balance');
            return null;
          }

          order.frozenAmount = freezeAmount;
          order.frozenAsset = freezeAsset;

          set((state) => ({ orders: [order, ...state.orders] }));
          notification.success(`trailing:created:${now}`, `Trailing Stop created: ${symbol} ${side.toUpperCase()}`);
          return order;
        },

        cancelOCOOrder: (ocoGroupId) => {
          const state = get();
          const ocoOrder = state.ocoOrders.find(o => o.ocoGroupId === ocoGroupId);
          if (!ocoOrder || ocoOrder.status !== 'active') return false;

          const walletStore = useWalletStore.getState();
          const baseAsset = ocoOrder.symbol.replace('USDT', '');
          const quoteAsset = 'USD';

          // Unfreeze balance
          const unfreezeAsset = ocoOrder.side === 'buy' ? quoteAsset : baseAsset;
          const unfreezeAmount = ocoOrder.side === 'buy'
            ? new Decimal(ocoOrder.limitPrice).times(ocoOrder.quantity).toFixed(8)
            : ocoOrder.quantity;
          
          walletStore.unfreezeBalance(unfreezeAsset, unfreezeAmount, ocoGroupId, 'order');

          // Cancel both orders
          set((s) => ({
            orders: s.orders.map(o => 
              o.ocoGroupId === ocoGroupId ? { ...o, status: 'cancelled', updatedAt: Date.now() } : o
            ),
            ocoOrders: s.ocoOrders.map(o =>
              o.ocoGroupId === ocoGroupId ? { ...o, status: 'cancelled', updatedAt: Date.now() } : o
            ),
          }));

          return true;
        },

        cancelOrder: async (clientOrderId) => {
          const state = get();
          const walletStore = useWalletStore.getState();
          const order = state.orders.find(o => o.clientOrderId === clientOrderId);
          if (!order || !['pending', 'open', 'partial'].includes(order.status)) return false;

          // If this is an OCO order, cancel the linked order and unfreeze via OCO group
          if (order.isOcoOrder && order.ocoGroupId) {
            return get().cancelOCOOrder(order.ocoGroupId);
          }

          // Confirm the cancellation with the backend BEFORE mutating local state.
          // A fire-and-forget DELETE would show "cancelled" even when the order
          // was already filled or the request failed.
          if (order.backendId) {
            try {
              const res = await apiFetch(`/orders/${encodeURIComponent(order.backendId)}`, {
                method: 'DELETE',
              });
              if (!res.ok) {
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

          const baseAsset = order.symbol.replace('USDT', '');
          const quoteAsset = 'USD';
          const remainingQty = new Decimal(order.quantity).minus(order.filledQty);

          if (remainingQty.gt(0)) {
            let unfreezeAmount: string;
            let unfreezeAsset: string;

            if (order.frozenAmount && order.frozenAsset) {
              unfreezeAsset = order.frozenAsset;
              const totalQty = new Decimal(order.quantity);
              if (totalQty.gt(0) && remainingQty.lt(totalQty)) {
                unfreezeAmount = new Decimal(order.frozenAmount).times(remainingQty).div(totalQty).toFixed(8);
              } else {
                unfreezeAmount = order.frozenAmount;
              }
            } else {
              if (order.side === 'buy') {
                unfreezeAsset = quoteAsset;
                if (order.price) {
                  unfreezeAmount = new Decimal(order.price).times(remainingQty).toFixed(8);
                } else if (order.triggerPrice) {
                  unfreezeAmount = new Decimal(order.triggerPrice).times(remainingQty).times(1.05).toFixed(8);
                } else {
                  const ms = useMarketStore.getState();
                  const currentMarketPrice = ms.recentTrades[0]?.price || ms.orderBook?.asks[0]?.price || ms.orderBook?.bids[0]?.price || '100';
                  unfreezeAmount = new Decimal(currentMarketPrice).times(remainingQty).times(1.05).toFixed(8);
                }
              } else {
                unfreezeAsset = baseAsset;
                unfreezeAmount = remainingQty.toFixed(8);
              }
            }
            walletStore.unfreezeBalance(unfreezeAsset, unfreezeAmount, clientOrderId, 'order');
          }

          set((s) => ({ orders: s.orders.map(o => o.clientOrderId === clientOrderId ? { ...o, status: 'cancelled', updatedAt: Date.now() } : o) }));
          return true;
        },

        // ===== Check and trigger conditional orders =====
        checkConditionalOrders: (symbol, currentPrice, previousPrice) => {
          const state = get();
          const currentPriceDec = new Decimal(currentPrice);
          const previousPriceDec = new Decimal(previousPrice);
          
          const conditionalOrders = state.orders.filter(o => 
            o.symbol === symbol && 
            o.status === 'open' && 
            isConditionalOrder(o.type) && 
            !o.isTriggered &&
            o.triggerPrice
          );

          for (const order of conditionalOrders) {
            const triggerPriceDec = new Decimal(order.triggerPrice!);
            let shouldTrigger = false;

            if (order.triggerDirection === 'up') {
              // Trigger when price crosses above trigger price
              shouldTrigger = previousPriceDec.lt(triggerPriceDec) && currentPriceDec.gte(triggerPriceDec);
            } else {
              // Trigger when price crosses below trigger price
              shouldTrigger = previousPriceDec.gt(triggerPriceDec) && currentPriceDec.lte(triggerPriceDec);
            }

            if (shouldTrigger) {
              // Mark as triggered
              set((s) => ({
                orders: s.orders.map(o => 
                  o.clientOrderId === order.clientOrderId 
                    ? { ...o, isTriggered: true, triggeredAt: Date.now(), status: 'triggered', updatedAt: Date.now() }
                    : o
                )
              }));

              notification.info(`order:triggered:${Date.now()}`, `${order.type.toUpperCase()} triggered: ${symbol} ${order.side.toUpperCase()}`);

              // Convert to regular order and execute
              const marketStore = useMarketStore.getState();
              if (order.type === 'stop_market' || order.type === 'take_profit_market') {
                // Execute as market order
                setTimeout(() => {
                  const updatedOrder = get().orders.find(o => o.clientOrderId === order.clientOrderId);
                  if (updatedOrder && updatedOrder.status === 'triggered') {
                    set((s) => ({
                      orders: s.orders.map(o => 
                        o.clientOrderId === order.clientOrderId 
                          ? { ...o, type: 'market', status: 'open', updatedAt: Date.now() }
                          : o
                      )
                    }));
                    if (marketStore.orderBook) {
                      get().updateOrderBookForMatching(marketStore.orderBook);
                    }
                  }
                }, SIMULATED_DELAY_MIN_MS);
              } else {
                // Execute as limit order
                setTimeout(() => {
                  const updatedOrder = get().orders.find(o => o.clientOrderId === order.clientOrderId);
                  if (updatedOrder && updatedOrder.status === 'triggered') {
                    set((s) => ({
                      orders: s.orders.map(o => 
                        o.clientOrderId === order.clientOrderId 
                          ? { ...o, type: 'limit', status: 'open', updatedAt: Date.now() }
                          : o
                      )
                    }));
                    if (marketStore.orderBook) {
                      get().updateOrderBookForMatching(marketStore.orderBook);
                    }
                  }
                }, SIMULATED_DELAY_MIN_MS);
              }

              // If OCO order, cancel the linked order
              if (order.isOcoOrder && order.ocoLinkedOrderId) {
                const linkedOrder = state.orders.find(o => o.clientOrderId === order.ocoLinkedOrderId);
                if (linkedOrder && linkedOrder.status === 'open') {
                  get().cancelOrder(linkedOrder.clientOrderId);
                }
              }
            }
          }
        },

        // ===== Update trailing stop orders =====
        updateTrailingStops: (symbol, currentPrice) => {
          const state = get();
          const currentPriceDec = new Decimal(currentPrice);
          
          const trailingOrders = state.orders.filter(o => 
            o.symbol === symbol && 
            o.status === 'open' && 
            o.type === 'trailing_stop' &&
            !o.isTriggered
          );

          for (const order of trailingOrders) {
            // Check activation price if set
            if (order.trailingActivationPrice) {
              const activationPrice = new Decimal(order.trailingActivationPrice);
              if (order.side === 'sell' && currentPriceDec.lt(activationPrice)) continue;
              if (order.side === 'buy' && currentPriceDec.gt(activationPrice)) continue;
            }

            let newStopPrice: Decimal;
            let shouldUpdate = false;
            let shouldTrigger = false;

            if (order.side === 'sell') {
              // For sell trailing stop: track highest price, stop below it
              const highestPrice = new Decimal(order.trailingHighestPrice || currentPrice);
              
              if (currentPriceDec.gt(highestPrice)) {
                // New high, update trailing stop
                const newHighest = currentPriceDec;
                if (order.trailingType === 'percent') {
                  newStopPrice = newHighest.times(1 - parseFloat(order.trailingValue!) / 100);
                } else {
                  newStopPrice = newHighest.minus(order.trailingValue!);
                }
                
                set((s) => ({
                  orders: s.orders.map(o => 
                    o.clientOrderId === order.clientOrderId 
                      ? { 
                          ...o, 
                          trailingHighestPrice: newHighest.toFixed(8),
                          trailingStopPrice: newStopPrice.toFixed(8),
                          updatedAt: Date.now() 
                        }
                      : o
                  )
                }));
                shouldUpdate = true;
              }

              // Check if stop triggered
              const stopPrice = new Decimal(order.trailingStopPrice || '0');
              if (currentPriceDec.lte(stopPrice)) {
                shouldTrigger = true;
              }
            } else {
              // For buy trailing stop: track lowest price, stop above it
              const lowestPrice = new Decimal(order.trailingLowestPrice || currentPrice);
              
              if (currentPriceDec.lt(lowestPrice)) {
                // New low, update trailing stop
                const newLowest = currentPriceDec;
                if (order.trailingType === 'percent') {
                  newStopPrice = newLowest.times(1 + parseFloat(order.trailingValue!) / 100);
                } else {
                  newStopPrice = newLowest.plus(order.trailingValue!);
                }
                
                set((s) => ({
                  orders: s.orders.map(o => 
                    o.clientOrderId === order.clientOrderId 
                      ? { 
                          ...o, 
                          trailingLowestPrice: newLowest.toFixed(8),
                          trailingStopPrice: newStopPrice.toFixed(8),
                          updatedAt: Date.now() 
                        }
                      : o
                  )
                }));
                shouldUpdate = true;
              }

              // Check if stop triggered
              const stopPrice = new Decimal(order.trailingStopPrice || '0');
              if (currentPriceDec.gte(stopPrice)) {
                shouldTrigger = true;
              }
            }

            if (shouldTrigger) {
              // Trigger the trailing stop as market order
              set((s) => ({
                orders: s.orders.map(o => 
                  o.clientOrderId === order.clientOrderId 
                    ? { ...o, isTriggered: true, triggeredAt: Date.now(), type: 'market', status: 'open', updatedAt: Date.now() }
                    : o
                )
              }));

              notification.info(`trailing:triggered:${Date.now()}`, `Trailing Stop triggered: ${symbol} ${order.side.toUpperCase()} @ ${order.trailingStopPrice}`);

              const marketStore = useMarketStore.getState();
              if (marketStore.orderBook) {
                setTimeout(() => get().updateOrderBookForMatching(marketStore.orderBook!), SIMULATED_DELAY_MIN_MS);
              }
            }
          }
        },

        updateOrderBookForMatching: (orderBook) => {
          const state = get();
          const walletStore = useWalletStore.getState();
          
          const bestBid = orderBook.bids[0];
          const bestAsk = orderBook.asks[0];
          if (!bestBid || !bestAsk) return;

          const midPrice = new Decimal(bestBid.price).plus(bestAsk.price).div(2).toFixed(8);
          
          // Get previous price for this symbol
          const prevPrice = previousPrices.get(orderBook.symbol) || midPrice;
          previousPrices.set(orderBook.symbol, midPrice);

          // Check conditional orders (stop-limit, take-profit, etc.)
          get().checkConditionalOrders(orderBook.symbol, midPrice, prevPrice);
          
          // Update trailing stops
          get().updateTrailingStops(orderBook.symbol, midPrice);
          
          // Check position TP/SL
          get().checkTPSL(orderBook.symbol, midPrice);

          // Get open orders that can be matched (limit and market orders, or triggered conditional orders)
          const openOrders = state.orders.filter(o => 
            o.symbol === orderBook.symbol && 
            (o.status === 'open' || o.status === 'partial') &&
            (o.type === 'limit' || o.type === 'market' || o.isTriggered)
          );
          
          if (openOrders.length === 0) return;

          const updates: { order: PaperOrder; fills: Fill[]; newStatus: OrderStatus }[] = [];
          for (const order of openOrders) {
            const matchResult = attemptMatch(order, orderBook);
            if (matchResult.fills.length > 0) {
              updates.push({ order, fills: matchResult.fills, newStatus: matchResult.fullyFilled ? 'filled' : 'partial' });
            }
          }

          if (updates.length === 0) return;

          set((s) => {
            const newPositions = new Map(s.positions);
            const newOrders = s.orders.map(o => {
              const update = updates.find(u => u.order.clientOrderId === o.clientOrderId);
              if (!update) return o;

              const allFills = [...o.fills, ...update.fills];
              const totalFilledQty = allFills.reduce((sum, f) => sum.plus(f.quantity), new Decimal(0));
              const totalCost = allFills.reduce((sum, f) => sum.plus(new Decimal(f.price).times(f.quantity)), new Decimal(0));
              const avgPrice = totalFilledQty.gt(0) ? totalCost.div(totalFilledQty).toFixed(8) : '0';

              const baseAsset = o.symbol.replace('USDT', '');
              const quoteAsset = 'USD';

              for (const fill of update.fills) {
                const fillQty = new Decimal(fill.quantity);
                const fillCost = new Decimal(fill.price).times(fillQty);
                const fee = new Decimal(fill.fee);

                if (o.side === 'buy') {
                  walletStore.deductFromFrozen(quoteAsset, fillCost.toFixed(8), fee.toFixed(8), fill.fillId, 'fill');
                  walletStore.creditToAvailable(baseAsset, fillQty.toFixed(8), fill.fillId, 'fill');
                } else {
                  walletStore.deductFromFrozen(baseAsset, fillQty.toFixed(8), '0', fill.fillId, 'fill');
                  walletStore.creditToAvailable(quoteAsset, fillCost.minus(fee).toFixed(8), fill.fillId, 'fill');
                }
              }

              const position = updatePosition(newPositions.get(o.symbol), o, update.fills);
              newPositions.set(o.symbol, position);

              return { ...o, fills: allFills, filledQty: totalFilledQty.toFixed(8), avgPrice, status: update.newStatus, updatedAt: Date.now() };
            });

            // Handle OCO orders - if one is filled, cancel the other
            const filledOcoOrders = newOrders.filter(o => o.isOcoOrder && o.status === 'filled');
            for (const filledOrder of filledOcoOrders) {
              if (filledOrder.ocoLinkedOrderId) {
                const linkedIdx = newOrders.findIndex(o => o.clientOrderId === filledOrder.ocoLinkedOrderId);
                if (linkedIdx >= 0 && newOrders[linkedIdx].status === 'open') {
                  newOrders[linkedIdx] = { ...newOrders[linkedIdx], status: 'cancelled', updatedAt: Date.now() };
                  
                  // Unfreeze balance for cancelled order is handled by OCO group
                }
              }
            }

            // Update OCO order status
            const newOcoOrders = s.ocoOrders.map(oco => {
              const limitOrder = newOrders.find(o => o.clientOrderId === oco.limitOrderId);
              const stopOrder = newOrders.find(o => o.clientOrderId === oco.stopOrderId);
              
              if (limitOrder?.status === 'filled' || stopOrder?.status === 'filled') {
                return { ...oco, status: 'filled' as const, updatedAt: Date.now() };
              }
              if (limitOrder?.status === 'cancelled' && stopOrder?.status === 'cancelled') {
                return { ...oco, status: 'cancelled' as const, updatedAt: Date.now() };
              }
              if (limitOrder?.status === 'partial' || stopOrder?.status === 'partial') {
                return { ...oco, status: 'partially_filled' as const, updatedAt: Date.now() };
              }
              return oco;
            });

            return { orders: newOrders, positions: newPositions, ocoOrders: newOcoOrders };
          });
        },

        checkTPSL: (symbol, midPriceStr) => {
          const state = get();
          const position = state.positions.get(symbol);
          if (!position || position.side === 'flat') return;

          const midPrice = new Decimal(midPriceStr);
          const { takeProfitPrice, stopLossPrice, quantity } = position;
          let shouldTrigger = false;

          if (takeProfitPrice && midPrice.gte(takeProfitPrice)) shouldTrigger = true;
          else if (stopLossPrice && midPrice.lte(stopLossPrice)) shouldTrigger = true;

          if (shouldTrigger) {
            get().createOrder({ symbol, side: 'sell', type: 'market', quantity }, midPriceStr);
            get().updatePositionTPSL(symbol, undefined, undefined);
          }
        },

        setFocusMode: (enabled) => set({ focusMode: enabled }),

        updatePositionTPSL: (symbol, takeProfitPrice, stopLossPrice) => {
          set((s) => {
            const newPositions = new Map(s.positions);
            const position = newPositions.get(symbol);
            if (position) newPositions.set(symbol, { ...position, takeProfitPrice, stopLossPrice, updatedAt: Date.now() });
            return { positions: newPositions };
          });
        },

        resetAccount: () => {
          useWalletStore.getState().resetWallet();
          set({ orders: [], ocoOrders: [], positions: new Map() });
          previousPrices.clear();
        },

        syncPositionsFromBackend: (backendPositions: Record<string, any>) => {
          const newPositions = new Map(get().positions);
          for (const [sym, pos] of Object.entries(backendPositions)) {
            const qty = String(pos.quantity || 0);
            if (parseFloat(qty) > 0) {
              newPositions.set(sym, {
                symbol: sym,
                side: 'long',
                quantity: qty,
                avgEntryPrice: String(pos.avg_cost || 0),
                unrealizedPnl: String(pos.unrealized_pnl || 0),
                realizedPnl: '0',
                updatedAt: Date.now(),
                // 后端按持仓所在市场给出币种，持仓表据此格式化金额。
                market: pos.market,
                currency: pos.currency,
              });
            } else {
              newPositions.delete(sym);
            }
          }
          set({ positions: newPositions });
        },

        fetchOrders: async () => {
          try {
            const res = await apiFetch('/orders');
            if (!res.ok) return;
            const backendOrders = await res.json();
            if (!Array.isArray(backendOrders)) return;

            set((state) => {
              const existingMap = new Map<string, PaperOrder>();
              for (const ord of state.orders) {
                if (ord.backendId) existingMap.set(ord.backendId, ord);
                existingMap.set(ord.clientOrderId, ord);
              }

              const mergedOrders = [...state.orders];

              for (const bo of backendOrders) {
                const backendId = bo.order_id || bo.id;
                if (!backendId) continue;
                const existing = existingMap.get(backendId);

                const statusMap: Record<string, OrderStatus> = {
                  'PENDING': 'pending',
                  'SUBMITTED': 'submitted',
                  'ACCEPTED': 'open',
                  'OPEN': 'open',
                  'PARTIALLY_FILLED': 'partial',
                  'FILLED': 'filled',
                  'CANCELLED': 'cancelled',
                  'CANCELED': 'cancelled',
                  'REJECTED': 'rejected',
                  'EXPIRED': 'expired',
                };

                const mappedStatus: OrderStatus = statusMap[bo.status?.toUpperCase()] || (bo.status?.toLowerCase() as OrderStatus) || 'open';

                if (existing) {
                  const idx = mergedOrders.findIndex(o => o.clientOrderId === existing.clientOrderId);
                  if (idx !== -1) {
                    mergedOrders[idx] = {
                      ...mergedOrders[idx],
                      backendId,
                      status: mappedStatus,
                      filledQty: String(bo.filled_quantity ?? bo.filled_qty ?? mergedOrders[idx].filledQty),
                      avgPrice: String(bo.filled_avg_price ?? bo.avg_price ?? mergedOrders[idx].avgPrice),
                      updatedAt: bo.updated_at ? Date.parse(bo.updated_at) : Date.now(),
                    };
                  }
                } else {
                  const newOrder: PaperOrder = {
                    clientOrderId: backendId,
                    backendId: backendId,
                    symbol: bo.symbol,
                    side: (bo.side?.toLowerCase() === 'sell' ? 'sell' : 'buy'),
                    type: (bo.order_type?.toLowerCase()?.includes('stop') ? 'stop_limit' : (bo.order_type?.toLowerCase()?.includes('limit') ? 'limit' : 'market')),
                    price: bo.price != null ? String(bo.price) : null,
                    quantity: String(bo.quantity || 1),
                    filledQty: String(bo.filled_quantity ?? bo.filled_qty ?? 0),
                    avgPrice: String(bo.filled_avg_price ?? bo.avg_price ?? 0),
                    status: mappedStatus,
                    createdAt: bo.created_at ? Date.parse(bo.created_at) : Date.now(),
                    updatedAt: bo.updated_at ? Date.parse(bo.updated_at) : Date.now(),
                    fills: [],
                    frozenAmount: bo.price && bo.side?.toUpperCase() === 'BUY' ? new Decimal(bo.price).times(bo.quantity || 1).toFixed(8) : undefined,
                    frozenAsset: 'USD',
                  };
                  mergedOrders.push(newOrder);
                }
              }

              // Cap the in-memory order list so a long history cannot
              // unboundedly grow memory or slow the Orders page.
              const MAX_ORDERS = 500;
              const capped = mergedOrders
                .slice()
                .sort((a, b) => (b.updatedAt || b.createdAt) - (a.updatedAt || a.createdAt))
                .slice(0, MAX_ORDERS);

              return { orders: capped };
            });
          } catch {}
        },

        getOrder: (id) => get().orders.find(o => o.clientOrderId === id),
        getOpenOrders: () => get().orders.filter(o => ['open', 'partial', 'pending', 'submitted'].includes(o.status) && !isConditionalOrder(o.type)),
        getConditionalOrders: () => get().orders.filter(o => o.status === 'open' && isConditionalOrder(o.type) && !o.isTriggered),
        getOrderHistory: () => get().orders.filter(o => ['filled', 'cancelled', 'rejected', 'expired'].includes(o.status)),
        getPosition: (symbol) => get().positions.get(symbol),
        getOCOOrders: () => get().ocoOrders.filter(o => o.status === 'active'),
      }),
      {
        name: 'paper-trading-storage',
        version: 4,
        storage: customStorage as any,
        partialize: (s) => ({ orders: s.orders, ocoOrders: s.ocoOrders, positions: s.positions, focusMode: false }),
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

function attemptMatch(order: PaperOrder, orderBook: OrderBook): { fills: Fill[]; fullyFilled: boolean } {
  const fills: Fill[] = [];
  let remainingQty = new Decimal(order.quantity).minus(order.filledQty);
  if (remainingQty.lte(0)) return { fills: [], fullyFilled: true };

  const levels = order.side === 'buy' ? orderBook.asks : orderBook.bids;
  for (const level of levels) {
    if (remainingQty.lte(0)) break;
    const levelPrice = new Decimal(level.price);
    const levelQty = new Decimal(level.quantity);
    let canMatch = order.type === 'market' || (order.side === 'buy' ? new Decimal(order.price!).gte(levelPrice) : new Decimal(order.price!).lte(levelPrice));
    if (!canMatch) break;

    const fillQty = Decimal.min(remainingQty, levelQty);
    const fee = fillQty.times(levelPrice).times(FEE_RATE);
    fills.push({ fillId: `fill_${uuidv4().slice(0, 8)}`, price: level.price, quantity: fillQty.toFixed(8), fee: fee.toFixed(8), feeAsset: 'USDT', time: Date.now() });
    remainingQty = remainingQty.minus(fillQty);
  }
  return { fills, fullyFilled: remainingQty.lte(0) };
}

function updatePosition(currentPosition: Position | undefined, order: PaperOrder, fills: Fill[]): Position {
  const symbol = order.symbol;
  const now = Date.now();
  if (!currentPosition) {
    currentPosition = { symbol, side: 'flat', quantity: '0', avgEntryPrice: '0', unrealizedPnl: '0', realizedPnl: '0', updatedAt: now, takeProfitPrice: order.takeProfitPrice, stopLossPrice: order.stopLossPrice };
  } else {
    if (order.takeProfitPrice) currentPosition.takeProfitPrice = order.takeProfitPrice;
    if (order.stopLossPrice) currentPosition.stopLossPrice = order.stopLossPrice;
  }

  let positionQty = new Decimal(currentPosition.quantity);
  let avgEntry = new Decimal(currentPosition.avgEntryPrice);
  let realizedPnl = new Decimal(currentPosition.realizedPnl);

  for (const fill of fills) {
    const fillQty = new Decimal(fill.quantity);
    const fillPrice = new Decimal(fill.price);
    if (order.side === 'buy') {
      const newQty = positionQty.plus(fillQty);
      avgEntry = positionQty.gt(0) ? positionQty.times(avgEntry).plus(fillQty.times(fillPrice)).div(newQty) : fillPrice;
      positionQty = newQty;
    } else {
      if (positionQty.gt(0)) realizedPnl = realizedPnl.plus(fillQty.times(fillPrice.minus(avgEntry)));
      positionQty = positionQty.minus(fillQty);
    }
  }
  return { symbol, side: positionQty.gt(0) ? 'long' : 'flat', quantity: Decimal.max(positionQty, 0).toFixed(8), avgEntryPrice: avgEntry.toFixed(8), unrealizedPnl: '0', realizedPnl: realizedPnl.toFixed(8), updatedAt: now };
}
