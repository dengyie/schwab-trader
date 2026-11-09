import { useState, useMemo, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { useWalletStore, selectBalances, resolveAccountTotals } from '../../store/walletStore';
import { useWatchlistStore } from '../../store/watchlistStore';
import { useI18n } from '../../i18n';
import { Icon } from '../../components/Icon';
import { MobileHeader } from '../../components/Layout';
import { PullToRefresh } from '../../components/mobile';
import { useHapticFeedback } from '../../hooks/useHapticFeedback';
import {
  DepositDrawer,
  WithdrawDrawer,
  OnboardingGuide,
  LinkedMethodsPanel,
} from '../../components/Wallet';
import styles from './WalletPage.mobile.module.css';

/** Instruments the trade screen can load. */
const TRADABLE_ASSETS = new Set([
  'SPY', 'QQQ', 'AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMD', 'AMZN', 'GOOGL', 'META', 'IWM', 'COIN', 'PLTR', 'NFLX',
]);

const ASSET_NAMES: Record<string, string> = {
  USD: 'Cash',
  SPY: 'S&P 500 ETF',
  QQQ: 'Nasdaq 100 ETF',
  AAPL: 'Apple Inc.',
  MSFT: 'Microsoft Corp.',
  NVDA: 'NVIDIA Corp.',
  TSLA: 'Tesla Inc.',
  AMD: 'Advanced Micro Devices',
  AMZN: 'Amazon.com Inc.',
  GOOGL: 'Alphabet Inc.',
  META: 'Meta Platforms',
  IWM: 'Russell 2000 ETF',
  COIN: 'Coinbase Global',
  PLTR: 'Palantir Technologies',
  NFLX: 'Netflix Inc.',
};

const isTradableAsset = (asset: string) => TRADABLE_ASSETS.has(asset);
const assetLabel = (asset: string) => ASSET_NAMES[asset] ?? asset;


export function MobileWalletPage() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { trigger } = useHapticFeedback();
  const methodsRef = useRef<HTMLDivElement>(null);
  const stage = useWalletStore((state) => state.getOnboardingStage());
  const balances = useWalletStore(selectBalances);
  const brokerMetrics = useWalletStore((state) => state.brokerMetrics);
  const hasLedger = useWalletStore((state) => state.ledgerLinked);
  const performance = useWalletStore((state) => state.performanceMetrics);
  const setSelectedSymbol = useWatchlistStore((state) => state.setSelectedSymbol);

  const [hideBalance, setHideBalance] = useState(false);
  const [depositOpen, setDepositOpen] = useState(false);
  const [withdrawOpen, setWithdrawOpen] = useState(false);
  const [showAllAssets, setShowAllAssets] = useState(false);

  const handleScrollToMethods = () => {
    trigger('selection');
    methodsRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  const handleToggleHide = () => {
    trigger('selection');
    setHideBalance(!hideBalance);
  };

  const handleDeposit = () => {
    trigger('medium');
    setDepositOpen(true);
  };

  const handleWithdraw = () => {
    trigger('medium');
    setWithdrawOpen(true);
  };

  const handleToggleAssets = () => {
    trigger('selection');
    setShowAllAssets(!showAllAssets);
  };

  // 点击资产跳转到交易页面
  const handleAssetClick = (asset: string) => {
    trigger('medium');
    if (!isTradableAsset(asset)) return;
    setSelectedSymbol(asset);
    navigate('/trade');
  };

  // The broker ledger is the account. Its total_equity already marks every
  // position to market reinvestment: cash + positions, so it is the one figure
  // this card may print. Only the USD row carries money before the first sync.
  const totals = useMemo(
    () => resolveAccountTotals(brokerMetrics, balances.find((b) => b.asset === 'USD')),
    [brokerMetrics, balances],
  );

  // Get non-zero balances. USDT is the legacy crypto placeholder: the store
  // keeps it pinned at 0, so it must never reach a row here.
  const activeBalances = useMemo(() => {
    return balances
      .filter((b) => b.asset !== 'USDT')
      .filter((b) => parseFloat(b.total) > 0);
  }, [balances]);

  const visibleBalances = useMemo(() => {
    return (showAllAssets ? balances : activeBalances).filter((b) => b.asset !== 'USDT');
  }, [showAllAssets, balances, activeBalances]);

  const formatQuantity = (total: string) => {
    if (hideBalance) return '****';
    const value = parseFloat(total);
    if (!Number.isFinite(value)) return '—';
    return value.toLocaleString('en-US', {
      maximumFractionDigits: 4,
    });
  };

  // Format balance for display
  const formatBalance = (value: number, decimals = 2) => {
    if (hideBalance) return '****';
    return value.toLocaleString('en-US', {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    });
  };

  // Version 4 of the persisted store repairs a funded account's ledgerLinked on
  // reload, and fetchPortfolio sets it on the first sync. Until either lands, the
  // "not_created" stage must not be trusted on its own or a funded account would
  // see "initialize a wallet" for the length of its first round trip.
  if (stage === 'not_created' && !hasLedger) {
    return (
      <div className={styles.container}>
        <MobileHeader title={t.wallet?.title || 'Wallet'} />
        <div className={styles.onboardingWrapper}>
          <OnboardingGuide stage={stage} />
        </div>
      </div>
    );
  }

  return (
    <PullToRefresh onRefresh={async () => { trigger('success'); }} className={styles.container}>
      {/* Header */}
      <MobileHeader
        title={t.wallet?.title || 'Wallet'}
        rightAction={
          <span className={styles.simulatedBadge}>
            {t.wallet?.simulatedBadge || 'Simulated'}
          </span>
        }
      />

      {/* Onboarding Banner */}
      {(stage === 'no_payment_method' || stage === 'no_funds') && (
        <div className={styles.onboardingBanner}>
          <OnboardingGuide 
            stage={stage} 
            onOpenDeposit={handleDeposit}
            onScrollToMethods={handleScrollToMethods}
          />
        </div>
      )}

      <div className={styles.scrollContent}>
        {/* Immersive Balance Card */}
        <div className={styles.balanceCard}>
          <div className={styles.balanceCardBg} />
          
          <div className={styles.balanceHeader}>
            <span className={styles.balanceLabel}>
              {t.wallet?.totalBalance || 'Total Balance'}
            </span>
            <button
              className={styles.hideBtn}
              onClick={handleToggleHide}
            >
              <Icon name={hideBalance ? 'eye-off' : 'eye'} size="sm" />
            </button>
          </div>

          <div className={styles.totalBalance}>
            <span className={styles.currency}>$</span>
            <span className={`${styles.amount} tabular-nums`}>
              {formatBalance(totals.totalEquity)}
            </span>
            <span className={styles.equivalent}>USD</span>
          </div>

          {/* Performance Metrics */}
          <div className={styles.performanceRow}>
            <div className={styles.perfItem}>
              <span className={styles.perfLabel}>Today's PnL</span>
              <span className={`${styles.perfValue} ${parseFloat(performance?.totalRealizedPnl || '0') >= 0 ? styles.positive : styles.negative}`}>
                {parseFloat(performance?.totalRealizedPnl || '0') >= 0 ? '+' : ''}
                {hideBalance ? '****' : `$${parseFloat(performance?.totalRealizedPnl || '0').toFixed(2)}`}
              </span>
            </div>
            <div className={styles.perfDivider} />
            <div className={styles.perfItem}>
              <span className={styles.perfLabel}>Win Rate</span>
              <span className={styles.perfValue}>
                {hideBalance
                  ? '****'
                  : (performance?.closedTrades ?? 0) > 0
                    ? `${((performance?.winRate ?? 0) * 100).toFixed(1)}%`
                    : '—'}
              </span>
            </div>
          </div>

          {/* Quick Actions */}
          <div className={styles.quickActions}>
            <button
              className={styles.actionBtn}
              onClick={handleDeposit}
            >
              <div className={styles.actionIcon}>
                <Icon name="download" size="md" />
              </div>
              <span>{t.wallet?.deposit || 'Deposit'}</span>
            </button>
            <button
              className={styles.actionBtn}
              onClick={handleWithdraw}
            >
              <div className={styles.actionIcon}>
                <Icon name="upload" size="md" />
              </div>
              <span>{t.wallet?.withdraw || 'Withdraw'}</span>
            </button>
            <button className={styles.actionBtn} disabled>
              <div className={styles.actionIcon}>
                <Icon name="repeat" size="md" />
              </div>
              <span>{t.wallet?.transfer || 'Transfer'}</span>
            </button>
            <button className={styles.actionBtn} disabled>
              <div className={styles.actionIcon}>
                <Icon name="history" size="md" />
              </div>
              <span>{t.wallet?.history || 'History'}</span>
            </button>
          </div>
        </div>

        {/* Asset List */}
        <div className={styles.assetSection}>
          <div className={styles.sectionHeader}>
            <span className={styles.sectionTitle}>
              {t.wallet?.assets || 'Assets'}
            </span>
            <button
              className={styles.toggleBtn}
              onClick={handleToggleAssets}
            >
              {showAllAssets 
                ? (t.wallet?.hideZero || 'Hide Zero')
                : (t.wallet?.showAll || 'Show All')
              }
            </button>
          </div>

          <div className={styles.assetList}>
            {visibleBalances.map((balance) => {
              const tradable = isTradableAsset(balance.asset);
              return (
                <div 
                  key={balance.asset} 
                  className={`${styles.assetItem} ${tradable ? styles.clickable : ''}`}
                  onClick={() => handleAssetClick(balance.asset)}
                >
                  <div className={styles.assetIcon}>
                    {balance.asset.slice(0, 2)}
                  </div>
                  <div className={styles.assetInfo}>
                    <span className={styles.assetName}>{balance.asset}</span>
                    <span className={styles.assetFullName}>{assetLabel(balance.asset)}</span>
                  </div>
                  <div className={styles.assetBalance}>
                    {/* Quantity for shares, money for cash. A share count is not a
                        dollar figure, so the two never share a precision rule. */}
                    <span className={`${styles.balanceAmount} tabular-nums`}>
                      {formatQuantity(balance.total)}
                    </span>
                  </div>
                  {tradable && (
                    <div className={styles.assetArrow}>
                      <Icon name="chevron-right" size="sm" />
                    </div>
                  )}
                </div>
              );
            })}

            {visibleBalances.length === 0 && (
              <div className={styles.emptyAssets}>
                <Icon name="wallet" size="xl" />
                <span>{t.wallet?.noAssets || 'No assets yet'}</span>
                <button
                  className={styles.depositPromptBtn}
                  onClick={handleDeposit}
                >
                  {t.wallet?.depositFirst || 'Make your first deposit'}
                </button>
              </div>
            )}
          </div>
        </div>

        {/* Linked Methods Section */}
        <div className={styles.methodsSection} ref={methodsRef}>
          <LinkedMethodsPanel highlightAdd={stage === 'no_payment_method'} />
        </div>
      </div>

      {/* Deposit Drawer */}
      <DepositDrawer
        isOpen={depositOpen}
        onClose={() => setDepositOpen(false)}
      />

      {/* Withdraw Drawer */}
      <WithdrawDrawer
        isOpen={withdrawOpen}
        onClose={() => setWithdrawOpen(false)}
      />
    </PullToRefresh>
  );
}

