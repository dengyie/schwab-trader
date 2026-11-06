import { describe, it, expect, beforeEach } from 'vitest';
import { useWalletStore } from './walletStore';

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
});
