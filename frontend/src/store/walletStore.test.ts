import { describe, it, expect, beforeEach } from 'vitest';
import { useWalletStore, resolveAccountTotals } from './walletStore';

// Captured before any test mutates the store, so a reload test can start from
// the same empty state a fresh browser boots with.
const pristine = useWalletStore.getState();
const STORAGE_KEY = 'paper-wallet-storage';

const balanceRows = (usdTotal: string) => [
  { asset: 'USD', available: usdTotal, frozen: '0', total: usdTotal },
  { asset: 'USDT', available: '0', frozen: '0', total: '0' },
];

const seedStorage = (version: number | null, state: Record<string, unknown>) => {
  const payload: Record<string, unknown> = { state };
  if (version !== null) payload.version = version;
  localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
};

describe('WalletStore - broker portfolio sync', () => {
  beforeEach(() => {
    useWalletStore.getState().resetWallet();
  });

  it('stores broker equity on USD only so totals are not double-counted', () => {
    useWalletStore.getState().syncFromBackendPortfolio({
      cash: 90000,
      total_equity: 95000,
      market_value: 5000,
      unrealized_pnl: 120.5,
      realized_pnl: 10,
      positions: {
        SPY: { quantity: 2, avg_cost: 700, current_price: 710 },
      },
    });

    const balances = useWalletStore.getState().balances;
    const usd = balances.find((b) => b.asset === 'USD');
    const usdt = balances.find((b) => b.asset === 'USDT');
    const spy = balances.find((b) => b.asset === 'SPY');

    expect(usd?.available).toBe('90000.00');
    expect(usd?.total).toBe('95000.00');
    // USDT is a legacy crypto placeholder and must not carry equity.
    expect(usdt?.total).toBe('0');
    expect(usdt?.available).toBe('0');
    // Stock rows hold share counts, not currency.
    expect(spy?.total).toBe('2');

    // Equity is reported once, exactly as the broker sent it.
    expect(useWalletStore.getState().getTotalEquity({})).toBe('95000.00');
  });

  it('exposes broker metrics for the account ribbon', () => {
    useWalletStore.getState().syncFromBackendPortfolio({
      cash: 50000,
      total_equity: 50500,
      market_value: 500,
      unrealized_pnl: 25,
      realized_pnl: 5,
    });

    const metrics = useWalletStore.getState().brokerMetrics;
    expect(metrics).not.toBeNull();
    expect(metrics?.cash).toBe(50000);
    expect(metrics?.totalEquity).toBe(50500);
    expect(metrics?.positionValue).toBe(500);
    expect(metrics?.unrealizedPnl).toBe(25);
    expect(metrics?.realizedPnl).toBe(5);
  });

  it('falls back to equity minus cash when market_value is missing', () => {
    useWalletStore.getState().syncFromBackendPortfolio({
      cash: 40000,
      total_equity: 42000,
    });

    expect(useWalletStore.getState().brokerMetrics?.positionValue).toBe(2000);
  });

  it('clears positions the broker no longer reports (fully sold)', () => {
    useWalletStore.getState().syncFromBackendPortfolio({
      cash: 90000,
      total_equity: 95000,
      positions: {
        SPY: { quantity: 2, avg_cost: 700, current_price: 710 },
      },
    });
    expect(useWalletStore.getState().balances.find((b) => b.asset === 'SPY')?.total).toBe('2');

    // After a sell the broker omits the symbol entirely; the row must zero out
    // instead of keeping the last-known share count on screen forever.
    useWalletStore.getState().syncFromBackendPortfolio({
      cash: 91420,
      total_equity: 91420,
      positions: {},
    });

    const spy = useWalletStore.getState().balances.find((b) => b.asset === 'SPY');
    expect(spy?.total).toBe('0');
    expect(spy?.available).toBe('0');
    // USD and the legacy USDT placeholder are never wiped by this path.
    expect(useWalletStore.getState().balances.find((b) => b.asset === 'USD')?.total).toBe('91420.00');
  });
});

describe('resolveAccountTotals - one account total for every panel', () => {
  it('reports broker equity as-is, never adding position value on top', () => {
    // Regression: the Positions panel showed 104,038.53 for this account
    // because it printed total_equity + market_value.
    const totals = resolveAccountTotals(
      { cash: 95846.87, totalEquity: 99947.97, positionValue: 4101.1, unrealizedPnl: -52.01, realizedPnl: 0 },
      { available: '95846.87', total: '99947.97' },
    );

    expect(totals.totalEquity).toBe(99947.97);
    expect(totals.cash).toBe(95846.87);
  });

  it('falls back to the USD balance before the broker snapshot lands', () => {
    const totals = resolveAccountTotals(null, { available: '90000.00', total: '95000.00' });

    expect(totals.totalEquity).toBe(95000);
    expect(totals.cash).toBe(90000);
  });

  it('starts an unsynced account at zero instead of a placeholder balance', () => {
    useWalletStore.getState().resetWallet();

    const usd = useWalletStore.getState().balances.find((b) => b.asset === 'USD');
    expect(usd?.available).toBe('0');
    expect(usd?.total).toBe('0');
    expect(resolveAccountTotals(null, usd)).toEqual({ totalEquity: 0, cash: 0 });
  });

  it('treats a broker-funded account as provisioned without the onboarding click', () => {
    // A browser that never pressed "Initialize wallet" still owns a funded
    // broker account: the local account record is only a placeholder.
    expect(useWalletStore.getState().getOnboardingStage()).toBe('not_created');

    useWalletStore.getState().syncFromBackendPortfolio({ cash: 190000, total_equity: 195846.87 });

    expect(useWalletStore.getState().account).toBeNull();
    const st = useWalletStore.getState();
    expect(st.getOnboardingStage()).toBe("funded");
  });

  it('stays provisioned across a reload even though broker figures are not persisted', async () => {
    useWalletStore.getState().syncFromBackendPortfolio({ cash: 190000, total_equity: 195846.87 });
    const options = (useWalletStore as unknown as {
      persist: { getOptions: () => { partialize: (s: unknown) => Record<string, unknown> } };
    }).persist.getOptions();
    const persisted = options.partialize(useWalletStore.getState());

    // Stale numbers must not outlive their session, but the fact that this
    // browser has a broker ledger must: otherwise the next reload onboards again.
    expect(persisted).not.toHaveProperty('brokerMetrics');
    expect(persisted.ledgerLinked).toBe(true);

    // A real reload: boot empty, put the persisted payload in storage, rehydrate.
    useWalletStore.setState(pristine, true);
    seedStorage(4, persisted);
    await useWalletStore.persist.rehydrate();

    expect(useWalletStore.getState().account).toBeNull();
    expect(useWalletStore.getState().brokerMetrics).toBeNull();
    expect(useWalletStore.getState().getOnboardingStage()).toBe('funded');
  });

});

describe('WalletStore - upgrade from a version 3 payload', () => {
  // Version 3 persisted balances but never `ledgerLinked`. Zustand only runs
  // `migrate` when the stored version is a number different from the current
  // one, which is exactly this case (verified against zustand 4.5.7).
  it('recovers the ledger fact from a funded balances snapshot', async () => {
    useWalletStore.setState(pristine, true);
    seedStorage(3, { account: null, balances: balanceRows('195846.87') });
    await useWalletStore.persist.rehydrate();

    const state = useWalletStore.getState();
    expect(state.ledgerLinked).toBe(true);
    expect(state.account).toBeNull();
    expect(state.getOnboardingStage()).toBe('funded');
  });

  it('does not invent a ledger for an empty version 3 wallet', async () => {
    useWalletStore.setState(pristine, true);
    seedStorage(3, { account: null, balances: balanceRows('0') });
    await useWalletStore.persist.rehydrate();

    const state = useWalletStore.getState();
    expect(state.ledgerLinked).toBe(false);
    expect(state.getOnboardingStage()).toBe('not_created');
  });

  it('returns to onboarding after a wallet reset', () => {
    useWalletStore.getState().syncFromBackendPortfolio({ cash: 190000, total_equity: 195846.87 });
    useWalletStore.getState().resetWallet();

    expect(useWalletStore.getState().ledgerLinked).toBe(false);
    expect(useWalletStore.getState().brokerMetrics).toBeNull();
    expect(useWalletStore.getState().getOnboardingStage()).toBe('not_created');
  });
});
