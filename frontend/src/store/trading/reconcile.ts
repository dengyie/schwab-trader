/**
 * Pure reconciliation between the local trading store and the broker snapshot.
 *
 * The backend is the single ledger for every market: it owns cash, positions
 * and every order that was actually submitted. The frontend only keeps what
 * the backend cannot hold — client-side conditional automation (stop / take
 * profit / trailing / OCO) before it is triggered. These helpers rebuild that
 * state from broker truth instead of merging into it, so a position closed in
 * another tab (or a phantom order from a previous local-matching build) can
 * never survive a refresh.
 */
import Decimal from 'decimal.js';
import type {
  Fill,
  OCOOrder,
  OrderSide,
  OrderStatus,
  OrderType,
  PaperOrder,
  Position,
} from '../../types/trading';
import { isConditionalOrder } from './triggers';

export interface BackendPosition {
  symbol?: string;
  quantity?: number | string | null;
  avg_cost?: number | string | null;
  current_price?: number | string | null;
  market_value?: number | string | null;
  market_value_usd?: number | string | null;
  unrealized_pnl?: number | string | null;
  unrealized_pnl_usd?: number | string | null;
  realized_pnl?: number | string | null;
  market?: string;
  currency?: string;
}

export interface BackendOrderRow {
  order_id?: string;
  id?: string;
  symbol?: string;
  side?: string;
  order_type?: string;
  quantity?: number | string | null;
  price?: number | string | null;
  stop_price?: number | string | null;
  status?: string;
  filled_quantity?: number | string | null;
  filled_qty?: number | string | null;
  filled_avg_price?: number | string | null;
  avg_price?: number | string | null;
  currency?: string;
  created_at?: string;
  updated_at?: string;
  /** Venue execution time; falls back below for brokers that omit it. */
  filled_at?: string;
}

/** How long an order may sit without a backend id while its POST is in flight. */
export const PENDING_SUBMIT_GRACE_MS = 60_000;
export const MAX_ORDERS = 500;

const BACKEND_STATUS: Record<string, OrderStatus> = {
  PENDING: 'pending',
  SUBMITTED: 'submitted',
  ACCEPTED: 'open',
  OPEN: 'open',
  PARTIALLY_FILLED: 'partial',
  FILLED: 'filled',
  CANCELLED: 'cancelled',
  CANCELED: 'cancelled',
  REJECTED: 'rejected',
  EXPIRED: 'expired',
};

export function mapBackendStatus(raw: string | undefined): OrderStatus {
  const key = String(raw || '').toUpperCase();
  return BACKEND_STATUS[key] ?? (key ? (key.toLowerCase() as OrderStatus) : 'open');
}

function mapBackendOrderType(row: BackendOrderRow): OrderType {
  const raw = String(row.order_type || '').toUpperCase();
  if (raw.includes('LIMIT')) return 'limit';
  if (raw.includes('STOP')) {
    // A stop with a limit price is a stop-limit; otherwise it is a stop-market.
    return row.price != null && Number(row.price) > 0 ? 'stop_limit' : 'stop_market';
  }
  return 'market';
}

function toSide(raw: string | undefined): OrderSide {
  return String(raw || '').toUpperCase() === 'SELL' ? 'sell' : 'buy';
}

function toTimestamp(raw: string | undefined): number {
  const parsed = raw ? Date.parse(raw) : NaN;
  return Number.isFinite(parsed) ? parsed : Date.now();
}

function num(value: number | string | null | undefined): Decimal {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? new Decimal(parsed) : new Decimal(0);
}

function text(value: number | string | null | undefined, fallback = '0'): string {
  if (value === null || value === undefined || value === '') return fallback;
  return String(value);
}

// ---------------------------------------------------------------------------
// Positions
// ---------------------------------------------------------------------------

export function reconcilePositions(
  previous: Map<string, Position>,
  backend: Record<string, BackendPosition | undefined> | null | undefined
): Map<string, Position> {
  const next = new Map<string, Position>();

  for (const [key, raw] of Object.entries(backend || {})) {
    if (!raw) continue;
    const quantity = num(raw.quantity);
    if (quantity.lte(0)) continue;

    const symbol = raw.symbol || key;
    const prior = previous.get(symbol) ?? previous.get(key);

    next.set(symbol, {
      symbol,
      side: 'long',
      quantity: text(raw.quantity, quantity.toFixed(8)),
      avgEntryPrice: text(raw.avg_cost),
      unrealizedPnl: text(raw.unrealized_pnl),
      // The broker snapshot does not carry session realized PnL per position;
      // keep the locally known figure across refreshes when it exists.
      realizedPnl: prior?.realizedPnl ?? text(raw.realized_pnl),
      updatedAt: Date.now(),
      // 券商快照里的估值：展示持仓市值/AI 雷达都要用真实报价，
      // 不能再用本地拼接的 "SYMBOLUSDT" 行情来估值。
      currentPrice: text(raw.current_price, prior?.currentPrice),
      marketValue: text(raw.market_value, prior?.marketValue),
      marketValueUsd: text(raw.market_value_usd, prior?.marketValueUsd),
      unrealizedPnlUsd: text(raw.unrealized_pnl_usd, prior?.unrealizedPnlUsd),
      // TP/SL are client-side automation targets, not broker data — they must
      // survive a position refresh for the same symbol.
      takeProfitPrice: prior?.takeProfitPrice,
      stopLossPrice: prior?.stopLossPrice,
      market: raw.market ?? prior?.market,
      currency: raw.currency ?? prior?.currency,
    });
  }

  return next;
}

// ---------------------------------------------------------------------------
// Orders
// ---------------------------------------------------------------------------

/**
 * The broker persists an order's aggregate fill (quantity + average price)
 * instead of the individual executions. Rebuild the single fill the UI needs
 * so trade history, PnL statistics and the orders page reflect real activity;
 * a cancelled order without a fill simply yields no fills.
 */
function backendFills(row: BackendOrderRow, backendId: string): Fill[] {
  const quantity = num(row.filled_quantity ?? row.filled_qty);
  const price = num(row.filled_avg_price ?? row.avg_price);
  if (quantity.lte(0) || price.lte(0)) return [];

  return [{
    fillId: `${backendId}:fill`,
    price: price.toString(),
    quantity: quantity.toString(),
    fee: '0',
    feeAsset: row.currency || 'USD',
    // filled_at is the venue's execution time. updated_at only dates the last
    // status change, so it is a fallback for older brokers, not the truth.
    time: toTimestamp(row.filled_at ?? row.updated_at ?? row.created_at),
  }];
}

export function paperOrderFromBackendRow(row: BackendOrderRow): PaperOrder | null {
  const backendId = row.order_id || row.id;
  if (!backendId || !row.symbol) return null;

  const orderType = mapBackendOrderType(row);
  const price = row.price != null && Number(row.price) > 0 ? String(row.price) : null;
  const triggerPrice = row.stop_price != null && Number(row.stop_price) > 0
    ? String(row.stop_price)
    : undefined;

  return {
    clientOrderId: backendId,
    backendId,
    symbol: row.symbol,
    currency: row.currency,
    side: toSide(row.side),
    type: orderType,
    price,
    quantity: text(row.quantity, '1'),
    filledQty: text(row.filled_quantity ?? row.filled_qty),
    avgPrice: text(row.filled_avg_price ?? row.avg_price),
    status: mapBackendStatus(row.status),
    createdAt: toTimestamp(row.created_at),
    updatedAt: toTimestamp(row.updated_at),
    fills: backendFills(row, backendId),
    triggerPrice,
  };
}

/**
 * An order the backend has never seen and that still represents pending
 * client-side work. Conditional orders live here until they trigger; a
 * regular order only lives here while its POST is in flight.
 */
export function isUnsentLocalOrder(order: PaperOrder, now = Date.now()): boolean {
  if (order.backendId) return false;
  if (isConditionalOrder(order.type)) {
    return order.status === 'open' || order.status === 'triggered';
  }
  // An OCO's profit-taking leg is a plain limit that lives locally too.
  if (order.isOcoOrder && order.type === 'limit') {
    return order.status === 'open' || order.status === 'triggered';
  }
  // A regular order is local only while its POST is in flight.
  return order.status === 'pending' && now - order.createdAt < PENDING_SUBMIT_GRACE_MS;
}

/**
 * Rebuild the order list from broker truth.
 *
 * Every row returned by the backend replaces its local counterpart. A local
 * order carrying a `backendId` that the backend no longer returns is deleted:
 * it was either fully handled elsewhere or it never existed on the broker.
 * Only unsent automation orders survive.
 */
export function reconcileOrders(
  previous: PaperOrder[],
  rows: BackendOrderRow[],
  now = Date.now()
): PaperOrder[] {
  const localByBackendId = new Map<string, PaperOrder>();
  for (const order of previous) {
    if (order.backendId) localByBackendId.set(order.backendId, order);
  }

  const next: PaperOrder[] = [];
  const seen = new Set<string>();

  for (const row of rows) {
    const backendId = row.order_id || row.id;
    if (!backendId || seen.has(backendId)) continue;
    seen.add(backendId);

    const fresh = paperOrderFromBackendRow(row);
    if (!fresh) continue;

    const local = localByBackendId.get(backendId);
    next.push(local
      ? {
          ...fresh,
          // Keep the UUID the UI and OCO links already reference, plus the
          // client-side metadata the broker does not know about.
          clientOrderId: local.clientOrderId,
          takeProfitPrice: local.takeProfitPrice,
          stopLossPrice: local.stopLossPrice,
          ocoGroupId: local.ocoGroupId,
          ocoLinkedOrderId: local.ocoLinkedOrderId,
          isOcoOrder: local.isOcoOrder,
        }
      : fresh);
  }

  for (const order of previous) {
    if (!isUnsentLocalOrder(order, now)) continue;
    if (next.some((candidate) => candidate.clientOrderId === order.clientOrderId)) continue;
    next.push(order);
  }

  return next
    .sort((a, b) => (b.updatedAt || b.createdAt) - (a.updatedAt || a.createdAt))
    .slice(0, MAX_ORDERS);
}

// ---------------------------------------------------------------------------
// OCO groups
// ---------------------------------------------------------------------------

const LIVE_STATUSES: OrderStatus[] = ['pending', 'submitted', 'open', 'partial', 'triggered'];

/** Derive each OCO group's status from its two legs. */
export function reconcileOcoGroups(groups: OCOOrder[], orders: PaperOrder[]): OCOOrder[] {
  const byId = new Map(orders.map((order) => [order.clientOrderId, order]));

  return groups.map((group) => {
    const legs = [group.limitOrderId, group.stopOrderId]
      .map((id) => byId.get(id))
      .filter((order): order is PaperOrder => Boolean(order));

    if (legs.length === 0) {
      return group.status === 'cancelled' ? group : { ...group, status: 'cancelled', updatedAt: Date.now() };
    }

    const knownLegs = [group.limitOrderId, group.stopOrderId].filter((id) => byId.has(id)).length;

    let status: OCOOrder['status'] = group.status;
    if (legs.some((leg) => leg.status === 'filled')) {
      status = 'filled';
    } else if (knownLegs === 2 && legs.every((leg) => leg.status === 'cancelled')) {
      status = 'cancelled';
    } else if (legs.some((leg) => leg.status === 'partial')) {
      status = 'partially_filled';
    } else if (group.status === 'triggered' || (
      legs.some((leg) => LIVE_STATUSES.includes(leg.status)) &&
      legs.some((leg) => leg.status === 'cancelled')
    )) {
      status = 'triggered';
    }

    return status === group.status ? group : { ...group, status, updatedAt: Date.now() };
  });
}
