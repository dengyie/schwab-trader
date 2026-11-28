import { create } from 'zustand';
import { subscribeWithSelector, persist } from 'zustand/middleware';
import { v4 as uuidv4 } from 'uuid';
import Decimal from 'decimal.js';
import { apiFetch } from '../services/apiClient';
import { computeTradePerformance } from './trading/performance';
import type {
  Account,
  WalletBalance,
  PaymentMethod,
  CryptoAddress,
  ChainType,
  Deposit,
  DepositStatus,
  Withdraw,
  LedgerEntry,
  LedgerType,
  ReferenceType,
  OnboardingStage,
  LedgerFilter,
  PerformanceMetrics,
} from '../types/wallet';
import type { PaperOrder } from '../types/trading';

// ===== Constants =====
const WITHDRAW_FEE_RATE = 0.001; // 0.1%
const MIN_WITHDRAW_FEE = 1; // minimum flat fee, in the withdrawn asset
// Deposit/Withdraw timing constants (reserved for future use)
// const DEPOSIT_CONFIRM_MIN_MS = 3000;
// const DEPOSIT_CONFIRM_MAX_MS = 10000;
// const WITHDRAW_PROCESS_MIN_MS = 5000;
// const WITHDRAW_PROCESS_MAX_MS = 15000;

const SUPPORTED_ASSETS = [
  'USD', 'USDT', 'SPY', 'QQQ', 'AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMD', 'AMZN', 'GOOGL', 'META', 'IWM', 'COIN', 'PLTR', 'NFLX',
];

// Balances start empty: the broker snapshot (/api/portfolio + WS) is the only
// thing allowed to put money in the account. Seeding a placeholder here made a
// fresh browser claim funds it did not have.
const createEmptyBalances = (): WalletBalance[] => 
  SUPPORTED_ASSETS.map(asset => ({
    asset,
    available: '0',
    frozen: '0',
    total: '0',
  }));

export interface BrokerMetrics {
  cash: number;
  totalEquity: number;
  positionValue: number | null;
  unrealizedPnl: number | null;
  realizedPnl: number | null;
  /** 券商给的初始资金；总收益百分比以它为分母。 */
  initialCapital: number | null;
  /** 券商口径的总收益百分比（已按初始资金计算）。 */
  totalPnlPct: number | null;
  /** 保证金购买力（cash 账户等于 cash）；未同步时为 null，消费方回退 cash。 */
  buyingPower: number | null;
}

/**
 * The broker portfolio snapshot as it arrives over REST and the socket.
 *
 * Only ``cash``/``total_equity`` are guaranteed; every other figure is optional
 * because the backend omits what the venue did not report rather than guessing.
 */
export interface BrokerPortfolio {
  cash: number;
  total_equity: number;
  market_value?: number;
  unrealized_pnl?: number;
  realized_pnl?: number;
  initial_capital?: number;
  total_pnl_pct?: number;
  /** Venue margin purchasing power (cash accounts: = cash). Optional for
   * older backend payloads; consumers fall back to cash. */
  buying_power?: number;
  positions?: Record<string, any>;
}

export interface AccountTotals {
  /** Broker total equity — already cash + market value of positions. */
  totalEquity: number;
  /** Cash free for new orders. */
  cash: number;
}

/**
 * The account figures every panel must agree on.
 *
 * The broker portfolio snapshot is the single source of truth and its
 * total_equity already includes position value, so callers must never add
 * position value on top of it (that double-counted the account by the full
 * market value). The USD balance mirrors the same snapshot for the window
 * before the first sync lands.
 */
export function resolveAccountTotals(
  brokerMetrics: BrokerMetrics | null,
  usdBalance: { available?: string; total?: string } | undefined
): AccountTotals {
  if (brokerMetrics) {
    return { totalEquity: brokerMetrics.totalEquity, cash: brokerMetrics.cash };
  }
  return {
    totalEquity: parseFloat(usdBalance?.total ?? '0') || 0,
    cash: parseFloat(usdBalance?.available ?? '0') || 0,
  };
}

/** Shared budget for the local ledger view, aligned with reconcile.ts MAX_ORDERS. */
export const MAX_LEDGER_ENTRIES = 500;

/**
 * Bound the local ledger without touching any number the account depends on:
 * balances are Decimal arithmetic on ``state.balances`` and are NEVER recomputed
 * from ledger rows, and trade performance comes from executed orders (FIFO in
 * performance.ts), so trimming the ledger is display/audit-metadata only.
 *
 * Kept unconditionally: every entry a PENDING deposit still needs for its
 * dedup check (``confirmDeposit`` and ``onRehydrateStorage`` both scan the
 * ledger for ``DEPOSIT`` rows by ``referenceId``) — dropping one would let a
 * reloaded page double-credit a deposit that already landed. The rest of the
 * budget goes to the most recent history, newest-first.
 */
export function retainLedgerWithHistory(
  ledger: LedgerEntry[],
  pendingDepositIds: Set<string>,
  cap = MAX_LEDGER_ENTRIES,
): LedgerEntry[] {
  const pinned = ledger.filter((entry) => (
    entry.type === 'DEPOSIT' && entry.referenceType === 'deposit' && pendingDepositIds.has(entry.referenceId)
  ));
  const pinnedIds = new Set(pinned.map((entry) => entry.entryId));
  const history = ledger.filter((entry) => !pinnedIds.has(entry.entryId));
  const historyBudget = Math.max(0, cap - pinned.length);
  return [...pinned, ...history.slice(0, historyBudget)];
}

interface WalletState {
  account: Account | null;
  balances: WalletBalance[];
  brokerMetrics: BrokerMetrics | null;
  /**
   * True once this browser has received a broker portfolio snapshot.
   *
   * ``brokerMetrics`` is deliberately not persisted (stale numbers must not
   * outlive their session), so this flag is what tells a reloaded page that the
   * ledger exists and the wallet is already provisioned.
   */
  ledgerLinked: boolean;
  paymentMethods: PaymentMethod[];
  cryptoAddresses: CryptoAddress[];
  deposits: Deposit[];
  withdraws: Withdraw[];
  ledger: LedgerEntry[];
  performanceMetrics: PerformanceMetrics;
  _pendingTimers: Map<string, ReturnType<typeof setTimeout>>;
  
  // Actions
  createAccount: () => Account;
  addPaymentMethod: (bankName: string, lastFour: string, alias: string) => PaymentMethod;
  removePaymentMethod: (id: string) => void;
  addCryptoAddress: (chain: ChainType, address: string, alias: string) => CryptoAddress;
  removeCryptoAddress: (id: string) => void;
  
  createDeposit: (asset: string, amount: string, sourceType: 'bank' | 'crypto', sourceId: string) => Deposit;
  confirmDeposit: (depositId: string) => void;
  createWithdraw: (asset: string, amount: string, destinationType: 'bank' | 'crypto', destinationId: string) => Withdraw | null;
  
  // Initial Grant (only once per account)
  
  // Backend Portfolio Sync
  syncFromBackendPortfolio: (data: BrokerPortfolio) => void;
  fetchPortfolio: () => Promise<void>;
  
  // Strict Balance Actions
  freezeBalance: (asset: string, amount: string, referenceId: string, referenceType: ReferenceType) => boolean;
  unfreezeBalance: (asset: string, amount: string, referenceId: string, referenceType: ReferenceType) => boolean;
  deductFromFrozen: (asset: string, amount: string, fee: string, referenceId: string, referenceType: ReferenceType) => boolean;
  creditToAvailable: (asset: string, amount: string, referenceId: string, referenceType: ReferenceType) => boolean;
  
  // Getters & Utils
  getOnboardingStage: () => OnboardingStage;
  getBalance: (asset: string) => WalletBalance | undefined;
  getTotalEquity: (prices: Record<string, string>) => string;
  getFilteredLedger: (filter: LedgerFilter) => LedgerEntry[];
  updatePerformanceMetrics: (prices: Record<string, string>) => void;
  syncPerformanceFromOrders: (orders: PaperOrder[]) => void;
  resetWallet: () => void;
}

export const useWalletStore = create<WalletState>()(
  subscribeWithSelector(
    persist(
      (set, get) => ({
        account: null,
        balances: createEmptyBalances(),
        brokerMetrics: null,
        ledgerLinked: false,
        paymentMethods: [],
        cryptoAddresses: [],
        deposits: [],
        withdraws: [],
        ledger: [],
        performanceMetrics: {
          totalTrades: 0,
          closedTrades: 0,
          winRate: 0,
          profitFactor: 0,
          maxDrawdown: 0,
          peakEquity: '0',
          totalRealizedPnl: '0',
        },
        _pendingTimers: new Map(),

        createAccount: () => {
          const account: Account = {
            accountId: `PTT-${Math.random().toString(36).substring(2, 8).toUpperCase()}`,
            status: 'active',
            createdAt: Date.now(),
          };
          set({ account });
          return account;
        },

        addPaymentMethod: (bankName, lastFour, alias) => {
          const method: PaymentMethod = { id: uuidv4(), type: 'bank', bankName, lastFour, alias, createdAt: Date.now() };
          set((state) => ({ paymentMethods: [...state.paymentMethods, method] }));
          return method;
        },

        removePaymentMethod: (id) => set((state) => ({ paymentMethods: state.paymentMethods.filter((m) => m.id !== id) })),

        addCryptoAddress: (chain, address, alias) => {
          const addr: CryptoAddress = { id: uuidv4(), chain, address, alias, createdAt: Date.now() };
          set((state) => ({ cryptoAddresses: [...state.cryptoAddresses, addr] }));
          return addr;
        },

        removeCryptoAddress: (id) => set((state) => ({ cryptoAddresses: state.cryptoAddresses.filter((a) => a.id !== id) })),

        syncFromBackendPortfolio: (data: BrokerPortfolio) => {
          const state = get();
          const cashStr = (data.cash ?? 0).toFixed(2);
          const totalStr = (data.total_equity ?? data.cash ?? 0).toFixed(2);

          // Broker equity already includes position market value, so only USD
          // carries it. USDT is a legacy crypto placeholder and stays zero —
          // writing equity into both currencies double-counted the account.
          //
          // Frozen amounts are local order reservations that no longer exist:
          // the broker holds every resting order and already excludes its
          // commitment from `cash`. Carrying a stale `frozen` forward would
          // show cash that is both spent and locked.
          // Withdraw-in-flight semantics: a simulated withdraw freezes the
          // amount for 5-10s locally, but this sync used to wipe EVERY frozen
          // value — the 30s portfolio poll landing inside that window broke
          // deductFromFrozen and the withdraw stayed 'processing' forever
          // with an orphan WITHDRAW_FREEZE ledger entry (2026-11-23 review).
          // Broker-side cash already excludes resting orders, so ORDER-freeze
          // rows are still correctly zeroed; only withdraw freezes survive.
          const processingWithdrawAssets = new Set(
            (state.withdraws || [])
              .filter((w: { status: string; asset: string }) => w.status === 'processing')
              .map((w: { asset: string }) => w.asset)
          );
          const newBalances = state.balances.map(b => ({
            ...b,
            frozen: processingWithdrawAssets.has(b.asset) ? b.frozen : '0',
          })).map(b => {
            if (b.asset === 'USD') {
              return {
                ...b,
                available: cashStr,
                total: totalStr,
              };
            }
            if (b.asset === 'USDT') {
              return {
                ...b,
                available: '0',
                total: '0',
              };
            }
            if (data.positions && data.positions[b.asset]) {
              const pos = data.positions[b.asset];
              const qty = String(pos.quantity || 0);
              return {
                ...b,
                available: qty,
                total: qty,
              };
            }
            // The broker only reports open positions. A symbol it no longer
            // lists was fully sold — keeping the last known quantity would leave
            // a closed position on screen as if it were still held.
            if (data.positions && b.asset !== 'USD' && b.asset !== 'USDT' && new Decimal(b.total || '0').gt(0)) {
              return {
                ...b,
                available: '0',
                total: '0',
              };
            }
            return b;
          });

          // Foreign-currency positions (0700.HK / 7203.T / 600519.SS) never
          // produced a balance row at all — the wallet hid them and every
          // automation sell-protection failed INSUFFICIENT_BALANCE
          // (2026-11-23 review P1). Surface each open foreign position as
          // its own row.
          if (data.positions) {
            const known = new Set(newBalances.map(b => b.asset));
            for (const [asset, pos] of Object.entries(data.positions)) {
              const qty = Number((pos as { quantity?: number | string | null })?.quantity ?? 0);
              if (!Number.isFinite(qty) || qty <= 0 || known.has(asset)) continue;
              newBalances.push({
                asset,
                available: String(qty),
                frozen: '0',
                total: String(qty),
              });
            }
          }

          set({
            balances: newBalances,
            ledgerLinked: true,
            brokerMetrics: {
              cash: data.cash ?? 0,
              totalEquity: data.total_equity ?? data.cash ?? 0,
              positionValue: data.market_value ?? ((data.total_equity ?? data.cash ?? 0) - (data.cash ?? 0)),
              unrealizedPnl: data.unrealized_pnl ?? null,
              realizedPnl: data.realized_pnl ?? null,
              initialCapital: data.initial_capital ?? null,
              totalPnlPct: data.total_pnl_pct ?? null,
              buyingPower: data.buying_power ?? null,
            },
          });
        },

        fetchPortfolio: async () => {
          try {
            const res = await apiFetch('/portfolio');
            if (!res.ok) {
              // A 401 already cleared the token and signed the user out in
              // apiFetch; anything else is a broker hiccup, and keeping the
              // last known view beats blanking the account.
              if (res.status !== 401) {
                console.warn(`[wallet] portfolio sync failed: HTTP ${res.status}`);
              }
              return;
            }
            const data = await res.json();
            get().syncFromBackendPortfolio(data);
            const { useTradingStore } = await import('./tradingStore');
            useTradingStore.getState().syncPositionsFromBackend(data.positions || {});
            useTradingStore.getState().fetchOrders();
          } catch (error) {
            // Offline or timed out: keep the previous snapshot, but leave a
            // trace so a permanently failing sync is not silent.
            console.warn('[wallet] portfolio sync failed', error);
          }
        },

        createDeposit: (asset, amount, sourceType, sourceId) => {
          const deposit: Deposit = {
            depositId: `dep_${uuidv4().slice(0, 8)}`,
            asset, amount, sourceType, sourceId,
            status: 'pending',
            createdAt: Date.now(),
          };
          set((state) => ({ deposits: [deposit, ...state.deposits] }));

          const delay = 3000 + Math.random() * 5000;
          const timer = setTimeout(() => get().confirmDeposit(deposit.depositId), delay);
          get()._pendingTimers.set(deposit.depositId, timer);

          return deposit;
        },

        confirmDeposit: (depositId) => {
          const state = get();
          const deposit = state.deposits.find((d) => d.depositId === depositId);
          
          // Guard: Only confirm if deposit exists and is pending
          if (!deposit || deposit.status !== 'pending') {
            // Clean up any stale timer
            const timer = state._pendingTimers.get(depositId);
            if (timer) {
              clearTimeout(timer);
              state._pendingTimers.delete(depositId);
            }
            return;
          }

          // Check if we already have a ledger entry for this deposit (prevent duplicate)
          const existingEntry = state.ledger.find(
            e => e.referenceType === 'deposit' && e.referenceId === depositId && e.type === 'DEPOSIT'
          );
          if (existingEntry) {
            // Already confirmed, just update deposit status
            const timer = state._pendingTimers.get(depositId);
            if (timer) {
              clearTimeout(timer);
              state._pendingTimers.delete(depositId);
            }
            set({
              deposits: state.deposits.map(d => 
                d.depositId === depositId ? { ...d, status: 'confirmed', confirmedAt: Date.now() } : d
              ),
            });
            return;
          }

          const timer = state._pendingTimers.get(depositId);
          if (timer) clearTimeout(timer);
          state._pendingTimers.delete(depositId);

          const amountDec = new Decimal(deposit.amount);
          const newBalances = state.balances.map(b => {
            if (b.asset === deposit.asset) {
              const available = new Decimal(b.available).plus(amountDec);
              const total = new Decimal(b.total).plus(amountDec);
              return { ...b, available: available.toFixed(8), total: total.toFixed(8) };
            }
            return b;
          });

          const balanceAfter = newBalances.find(b => b.asset === deposit.asset)?.available || '0';
          const entry: LedgerEntry = {
            entryId: `led_${uuidv4().slice(0, 8)}`,
            type: 'DEPOSIT', direction: '+',
            asset: deposit.asset, amount: deposit.amount, fee: '0',
            balanceAfter, referenceType: 'deposit', referenceId: depositId,
            createdAt: Date.now()
          };

          set({
            balances: newBalances,
            deposits: state.deposits.map(d => d.depositId === depositId ? { ...d, status: 'confirmed', confirmedAt: Date.now() } : d),
            // The entry just written is the newest; the pending-deposit dedup
            // set shrinks by one as this deposit flips to confirmed.
            ledger: retainLedgerWithHistory(
              [entry, ...state.ledger],
              new Set(state.deposits.filter(d => d.status === 'pending' && d.depositId !== depositId).map(d => d.depositId)),
            ),
          });
        },

        createWithdraw: (asset, amount, destinationType, destinationId) => {
          const state = get();
          const balance = state.balances.find(b => b.asset === asset);
          if (!balance) return null;

          const amountDec = new Decimal(amount);
          const fee = Decimal.max(amountDec.times(WITHDRAW_FEE_RATE), asset === 'USDT' ? MIN_WITHDRAW_FEE : 0);
          const totalRequired = amountDec.plus(fee);

          if (new Decimal(balance.available).lt(totalRequired)) return null;

          const withdrawId = `wdr_${uuidv4().slice(0, 8)}`;
          const success = get().freezeBalance(asset, totalRequired.toFixed(8), withdrawId, 'withdraw');
          
          if (!success) return null;

          const withdraw: Withdraw = {
            withdrawId, asset, amount, fee: fee.toFixed(8),
            destinationType, destinationId, status: 'processing',
            createdAt: Date.now()
          };

          set(s => ({ withdraws: [withdraw, ...s.withdraws] }));

          const withdrawTimer = setTimeout(() => {
            const s = get();
            const w = s.withdraws.find(x => x.withdrawId === withdrawId);
            if (!w || w.status !== 'processing') return;

            const finalSuccess = get().deductFromFrozen(asset, amount, fee.toFixed(8), withdrawId, 'withdraw');
            if (finalSuccess) {
              set(state => ({
                withdraws: state.withdraws.map(x => x.withdrawId === withdrawId ? { ...x, status: 'completed', completedAt: Date.now() } : x)
              }));
            }
            get()._pendingTimers.delete(`withdraw:${withdrawId}`);
          }, 5000 + Math.random() * 5000);
          // The old timer was anonymous and unreferenced — a component
          // unmount could not cancel it and it escaped cleanup entirely
          // (2026-11-23 review P2).
          get()._pendingTimers.set(`withdraw:${withdrawId}`, withdrawTimer);

          return withdraw;
        },

        freezeBalance: (asset, amount, referenceId, referenceType) => {
          const state = get();
          const amountDec = new Decimal(amount);
          const balance = state.balances.find(b => b.asset === asset);
          
          if (!balance || new Decimal(balance.available).lt(amountDec)) return false;

          const newBalances = state.balances.map(b => {
            if (b.asset === asset) {
              return {
                ...b,
                available: new Decimal(b.available).minus(amountDec).toFixed(8),
                frozen: new Decimal(b.frozen).plus(amountDec).toFixed(8)
              };
            }
            return b;
          });

          const balanceAfter = newBalances.find(b => b.asset === asset)?.available || '0';
          const entry: LedgerEntry = {
            entryId: `led_${uuidv4().slice(0, 8)}`,
            type: referenceType === 'order' ? 'ORDER_FREEZE' : 'WITHDRAW_FREEZE',
            direction: '-', asset, amount, fee: '0', balanceAfter,
            referenceType, referenceId, createdAt: Date.now()
          };

          set({
            balances: newBalances,
            // Newest entry first; pending-deposit dedup rows are pinned so a
            // reload can never double-credit a deposit that already landed.
            ledger: retainLedgerWithHistory(
              [entry, ...state.ledger],
              new Set(state.deposits.filter(d => d.status === 'pending').map(d => d.depositId)),
            ),
          });
          return true;
        },

        unfreezeBalance: (asset, amount, referenceId, referenceType) => {
          const state = get();
          const amountDec = new Decimal(amount);
          const balance = state.balances.find(b => b.asset === asset);
          
          if (!balance || new Decimal(balance.frozen).lt(amountDec)) return false;

          const newBalances = state.balances.map(b => {
            if (b.asset === asset) {
              return {
                ...b,
                available: new Decimal(b.available).plus(amountDec).toFixed(8),
                frozen: new Decimal(b.frozen).minus(amountDec).toFixed(8)
              };
            }
            return b;
          });

          const balanceAfter = newBalances.find(b => b.asset === asset)?.available || '0';
          const entry: LedgerEntry = {
            entryId: `led_${uuidv4().slice(0, 8)}`,
            type: referenceType === 'order' ? 'ORDER_UNFREEZE' : 'WITHDRAW_REFUND',
            direction: '+', asset, amount, fee: '0', balanceAfter,
            referenceType, referenceId, createdAt: Date.now()
          };

          set({
            balances: newBalances,
            // Newest entry first; pending-deposit dedup rows are pinned so a
            // reload can never double-credit a deposit that already landed.
            ledger: retainLedgerWithHistory(
              [entry, ...state.ledger],
              new Set(state.deposits.filter(d => d.status === 'pending').map(d => d.depositId)),
            ),
          });
          return true;
        },

        deductFromFrozen: (asset, amount, fee, referenceId, referenceType) => {
          const state = get();
          const totalDeduct = new Decimal(amount).plus(new Decimal(fee));
          const balance = state.balances.find(b => b.asset === asset);
          
          if (!balance || new Decimal(balance.frozen).lt(totalDeduct)) return false;

          const newBalances = state.balances.map(b => {
            if (b.asset === asset) {
              return {
                ...b,
                frozen: new Decimal(b.frozen).minus(totalDeduct).toFixed(8),
                total: new Decimal(b.total).minus(totalDeduct).toFixed(8)
              };
            }
            return b;
          });

          const balanceAfter = newBalances.find(b => b.asset === asset)?.total || '0';
          const entry: LedgerEntry = {
            entryId: `led_${uuidv4().slice(0, 8)}`,
            type: referenceType === 'withdraw' ? 'WITHDRAW_COMPLETE' : 'FILL',
            direction: '-', asset, amount, fee, balanceAfter,
            referenceType, referenceId, createdAt: Date.now()
          };

          set({
            balances: newBalances,
            // Newest entry first; pending-deposit dedup rows are pinned so a
            // reload can never double-credit a deposit that already landed.
            ledger: retainLedgerWithHistory(
              [entry, ...state.ledger],
              new Set(state.deposits.filter(d => d.status === 'pending').map(d => d.depositId)),
            ),
          });
          return true;
        },

        creditToAvailable: (asset, amount, referenceId, referenceType) => {
          const state = get();
          const amountDec = new Decimal(amount);
          const newBalances = state.balances.map(b => {
            if (b.asset === asset) {
              return {
                ...b,
                available: new Decimal(b.available).plus(amountDec).toFixed(8),
                total: new Decimal(b.total).plus(amountDec).toFixed(8)
              };
            }
            return b;
          });

          const balanceAfter = newBalances.find(b => b.asset === asset)?.available || '0';
          const entry: LedgerEntry = {
            entryId: `led_${uuidv4().slice(0, 8)}`,
            type: referenceType === 'deposit' ? 'DEPOSIT' : 'FILL',
            direction: '+', asset, amount, fee: '0', balanceAfter,
            referenceType, referenceId, createdAt: Date.now()
          };

          set({
            balances: newBalances,
            // Newest entry first; pending-deposit dedup rows are pinned so a
            // reload can never double-credit a deposit that already landed.
            ledger: retainLedgerWithHistory(
              [entry, ...state.ledger],
              new Set(state.deposits.filter(d => d.status === 'pending').map(d => d.depositId)),
            ),
          });
          return true;
        },

        getOnboardingStage: () => {
          const s = get();
          // The broker ledger is the real account. The local wallet account is
          // only a browser placeholder for it (it lives in localStorage and an
          // onboarding click creates it), so once portfolio figures have synced
          // this wallet is provisioned whether or not that click ever happened.
          if (!s.account && !s.ledgerLinked) return 'not_created';
          if (s.paymentMethods.length === 0 && s.cryptoAddresses.length === 0) {
            // A funded broker account needs no local funding channel: the funds
            // are already there, so the "link a card" step is not an onboarding
            // step for it, only an upsell the wallet page can offer.
            if (!s.ledgerLinked || !s.balances.some(b => new Decimal(b.total).gt(0))) {
              return 'no_payment_method';
            }
          }
          if (!s.balances.some(b => new Decimal(b.total).gt(0))) return 'no_funds';
          return 'funded';
        },

        getBalance: (asset) => {
          if (asset === 'USD' || asset === 'USDT') {
            return get().balances.find(b => b.asset === 'USD' || b.asset === 'USDT');
          }
          return get().balances.find(b => b.asset === asset);
        },

        getTotalEquity: (prices) => {
          const s = get();
          // USD.total mirrors the broker's total_equity (cash + positions marked
          // to market). Stock rows hold share counts, never currency, so they must
          // not be added again here.
          const usd = s.balances.find((b) => b.asset === 'USD');
          return new Decimal(usd?.total ?? '0').toFixed(2);
        },

        getFilteredLedger: (filter) => {
          const s = get();
          if (filter === 'all') return s.ledger;
          const types: Record<LedgerFilter, LedgerType[]> = {
            all: [],
            deposit: ['DEPOSIT'],
            withdraw: ['WITHDRAW_FREEZE', 'WITHDRAW_COMPLETE', 'WITHDRAW_REFUND'],
            trade: ['ORDER_FREEZE', 'ORDER_UNFREEZE', 'FILL'],
            fee: ['FEE']
          };
          const selectedTypes = types[filter];
          return s.ledger.filter(e => selectedTypes.includes(e.type));
        },

        updatePerformanceMetrics: (prices) => {
          const s = get();
          const equityValue = s.getTotalEquity(prices);
          const currentEquity = new Decimal(equityValue);
          const m = { ...s.performanceMetrics };
          const peak = new Decimal(m.peakEquity);
          if (currentEquity.gt(peak)) m.peakEquity = currentEquity.toFixed(2);
          else if (peak.gt(0)) {
            const dd = peak.minus(currentEquity).div(peak).times(100).toNumber();
            m.maxDrawdown = Math.max(m.maxDrawdown, dd);
          }
          set({ performanceMetrics: m });
        },

        // Trade statistics come from executed fills, never from open orders, so
        // they are recomputed from broker truth on every order refresh.
        syncPerformanceFromOrders: (orders) => {
          const trades = computeTradePerformance(orders);
          set((s) => ({ performanceMetrics: { ...s.performanceMetrics, ...trades } }));
        },

        resetWallet: () => {
          get()._pendingTimers.forEach(clearTimeout);
          set({
            account: null, balances: createEmptyBalances(), paymentMethods: [], cryptoAddresses: [],
            deposits: [], withdraws: [], ledger: [],
            // A reset wallet must not keep serving the previous broker snapshot.
            brokerMetrics: null,
            ledgerLinked: false,
            performanceMetrics: { totalTrades: 0, closedTrades: 0, winRate: 0, profitFactor: 0, maxDrawdown: 0, peakEquity: '0', totalRealizedPnl: '0' },
          });
        }
      }),
      {
        name: 'paper-wallet-storage',
        version: 4,
        /**
         * Every browser that synced a portfolio before version 4 persisted its
         * balances but not ``ledgerLinked`` (the key did not exist). A shallow
         * merge would leave the flag at its ``false`` default and put a funded
         * account back on the "initialize a wallet" guide on its first reload.
         *
         * A non-zero balance is itself the proof of the ledger: the only paths
         * that raise one run after a broker sync (``syncFromBackendPortfolio``
         * rewrites USD from ``total_equity``, and the deposit/withdraw actions
         * only ever move what that sync put there). So the upgrade recovers the
         * fact it can no longer read, instead of asking the user to onboard
         * again onto money that is already theirs.
         */
        migrate: (persisted, fromVersion) => {
          const state = (persisted ?? {}) as Partial<WalletState>;
          // Zustand reports the stored version as undefined when the payload
          // predates versioning at all; both cases are older than 4.
          const from = typeof fromVersion === 'number' ? fromVersion : 0;
          if (from < 4 && typeof state.ledgerLinked !== 'boolean') {
            state.ledgerLinked = (state.balances ?? []).some(
              (b) => new Decimal(b.total ?? '0').gt(0),
            );
          }
          // Hydration cap: an older build (or another tab) may have written
          // more ledger history than today's budget allows.
          if (Array.isArray(state.ledger)) {
            state.ledger = retainLedgerWithHistory(
              state.ledger,
              new Set((state.deposits ?? []).filter(d => d.status === 'pending').map(d => d.depositId)),
            );
          }
          return state as unknown as WalletState;
        },
        partialize: (s) => ({
          account: s.account,
          balances: s.balances,
          ledgerLinked: s.ledgerLinked,
          paymentMethods: s.paymentMethods,
          cryptoAddresses: s.cryptoAddresses,
          deposits: s.deposits,
          withdraws: s.withdraws,
          // localStorage must not grow without bound on a long-lived account.
          // Balances are persisted separately and never rebuilt from these
          // rows, so trimming history here cannot move a number the account
          // depends on; entries a pending deposit still needs are pinned.
          ledger: retainLedgerWithHistory(
            s.ledger,
            new Set(s.deposits.filter(d => d.status === 'pending').map(d => d.depositId)),
          ),
        }),
        onRehydrateStorage: () => (s) => {
          if (s) {
            s._pendingTimers = new Map();
            // Only set timers for deposits that are truly pending and not already confirmed
            s.deposits.filter(d => d.status === 'pending').forEach(d => {
              // Skip if there's already a ledger entry for this deposit (indicates it was confirmed)
              const hasLedgerEntry = s.ledger.some(
                e => e.referenceType === 'deposit' && e.referenceId === d.depositId && e.type === 'DEPOSIT'
              );
              if (hasLedgerEntry) {
                // Fix inconsistent state: mark as confirmed
                d.status = 'confirmed' as DepositStatus;
                d.confirmedAt = Date.now();
                return;
              }
              
              const elapsed = Date.now() - d.createdAt;
              const timer = setTimeout(() => s.confirmDeposit(d.depositId), Math.max(5000 - elapsed, 1000));
              s._pendingTimers.set(d.depositId, timer);
            });
          }
        }
      }
    )
  )
);

// ===== Selectors =====
export const selectAccount = (state: WalletState) => state.account;
export const selectBalances = (state: WalletState) => state.balances;
export const selectPaymentMethods = (state: WalletState) => state.paymentMethods;
export const selectCryptoAddresses = (state: WalletState) => state.cryptoAddresses;
export const selectDeposits = (state: WalletState) => state.deposits;
export const selectWithdraws = (state: WalletState) => state.withdraws;
export const selectLedger = (state: WalletState) => state.ledger;
export const selectOnboardingStage = (state: WalletState) => state.getOnboardingStage();
