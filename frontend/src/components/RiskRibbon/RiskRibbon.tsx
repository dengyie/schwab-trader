import { useMemo, useEffect } from 'react';
import { useWalletStore, selectBalances, resolveAccountTotals } from '../../store/walletStore';
import { useMarketStore, selectActiveSymbol, selectMetrics } from '../../store/marketStore';
import { useI18n } from '../../i18n';
import { Icon } from '../Icon';
import styles from './RiskRibbon.module.css';

interface RiskMetrics {
  /** Whole account's position market value over total equity. */
  positionSizePercent: number;
  /** Account unrealized P&L over its cost basis. */
  unrealizedPnlPercent: number;
  volatilityRisk: number;       // 0-100 based on microVolatility percentile
}

interface RiskRibbonProps {
  compact?: boolean;
  full?: boolean; // 新增 full 模式用于全屏展示
}

/** Placeholder for a metric the account has not produced a single sample for. */
const EMPTY = '—';

export function RiskRibbon({ compact = false, full = false }: RiskRibbonProps) {
  const { t } = useI18n();
  const balances = useWalletStore(selectBalances);
  const brokerMetrics = useWalletStore((state) => state.brokerMetrics);
  const performanceMetrics = useWalletStore((state) => state.performanceMetrics);
  const updatePerformanceMetrics = useWalletStore((state) => state.updatePerformanceMetrics);
  // The perf-price effect only needs the symbol + mid scalars; the risk
  // computation needs microVolatility, so the whole metrics object stays here
  // but orderBook is narrowed to its symbol.
  const metrics = useMarketStore(selectMetrics);
  const orderBookSymbol = useMarketStore(selectActiveSymbol);

  // 定期更新绩效指标
  useEffect(() => {
    if (!metrics) return;

    const prices: Record<string, string> = {};
    if (metrics.mid && orderBookSymbol) {
      prices[orderBookSymbol] = metrics.mid;
    }

    updatePerformanceMetrics(prices);
  }, [metrics, orderBookSymbol, updatePerformanceMetrics]);

  // Every figure below describes the whole account, never the symbol on screen:
  // mixing a selected-symbol position with account-level win rate made the
  // ribbon change meaning as the user clicked through the watchlist.
  const riskMetrics = useMemo((): RiskMetrics | null => {
    if (!metrics) return null;

    const usdBalance = balances.find(b => b.asset === 'USD');
    // Broker equity already contains position value; adding it on top inflated
    // the denominator and understated position size.
    const accountEquity = resolveAccountTotals(brokerMetrics, usdBalance).totalEquity;
    const positionValue = brokerMetrics?.positionValue ?? 0;
    const unrealizedPnl = brokerMetrics?.unrealizedPnl ?? null;
    const volatility = metrics.microVolatility;

    const positionSizePercent = accountEquity > 0 ? (positionValue / accountEquity) * 100 : 0;
    const costBasis = positionValue - (unrealizedPnl ?? 0);
    const unrealizedPnlPercent = unrealizedPnl !== null && costBasis > 0
      ? (unrealizedPnl / costBasis) * 100
      : 0;

    return {
      positionSizePercent,
      unrealizedPnlPercent,
      volatilityRisk: Math.min(100, (volatility / 100) * 100),
    };
  }, [balances, brokerMetrics, metrics]);

  if (!riskMetrics) return null;

  const overallRisk = Math.min(100, 
    (riskMetrics.positionSizePercent * 0.4) + 
    (Math.abs(riskMetrics.unrealizedPnlPercent) * 0.3) + 
    (riskMetrics.volatilityRisk * 0.3)
  );

  const getRiskColor = (risk: number) => {
    if (risk < 30) return 'low';
    if (risk < 60) return 'medium';
    return 'high';
  };

  const getRiskLabel = (level: string) => {
    switch (level) {
      case 'low': return t.riskRibbon.low;
      case 'medium': return t.riskRibbon.medium;
      case 'high': return t.riskRibbon.high;
      default: return level;
    }
  };

  const riskLevel = getRiskColor(overallRisk);

  // History metrics describe trades that have been settled by a sell. Before
  // that the ratios are mathematically undefined, so they read '—' instead of
  // a confident 0 that looks like real performance.
  const hasClosedTrades = performanceMetrics.closedTrades > 0;
  const winRateDisplay = hasClosedTrades
    ? `${(performanceMetrics.winRate * 100).toFixed(1)}%`
    : EMPTY;
  const profitFactorDisplay = !hasClosedTrades
    ? EMPTY
    : Number.isFinite(performanceMetrics.profitFactor)
      ? performanceMetrics.profitFactor.toFixed(2)
      : '∞';
  const realizedPnl = parseFloat(performanceMetrics.totalRealizedPnl) || 0;

  if (full) {
    return (
      <div className={`${styles.container} ${styles.full}`}>
        <div className={styles.scoreSection}>
          <div className={`${styles.gaugeContainer} ${styles[riskLevel]}`}>
            <div className={styles.gaugeValue}>{overallRisk.toFixed(0)}</div>
            <div className={styles.gaugeLabel}>{t.riskRibbon.title}</div>
          </div>
          <div className={styles.statusInfo}>
            <div className={`${styles.statusLevel} ${styles[riskLevel]}`}>
              <Icon name="shield" size="sm" />
              {getRiskLabel(riskLevel)}
            </div>
            <p className={styles.statusDesc}>
              {riskLevel === 'low' && '账户状态极佳，风险处于可控范围。'}
              {riskLevel === 'medium' && '注意仓位规模，波动风险有所上升。'}
              {riskLevel === 'high' && '风险极高！建议立即调整仓位或对冲。'}
            </p>
          </div>
        </div>

        <div className={styles.divider} />

        <h4 className={styles.gridTitle}>实时风险指标</h4>
        <div className={styles.fullGrid}>
          <div className={styles.gridItem}>
            <span className={styles.itemLabel}>{t.riskRibbon.positionRatio}</span>
            <span className={styles.itemValue}>{riskMetrics.positionSizePercent.toFixed(2)}%</span>
          </div>
          <div className={styles.gridItem}>
            <span className={styles.itemLabel}>未实现盈亏</span>
            <span className={`${styles.itemValue} ${riskMetrics.unrealizedPnlPercent >= 0 ? styles.positive : styles.negative}`}>
              {riskMetrics.unrealizedPnlPercent >= 0 ? '+' : ''}{riskMetrics.unrealizedPnlPercent.toFixed(2)}%
            </span>
          </div>
          <div className={styles.gridItem}>
            <span className={styles.itemLabel}>市场波动率</span>
            <span className={styles.itemValue}>{metrics?.microVolatility.toFixed(4)}</span>
          </div>
          <div className={styles.gridItem}>
            <span className={styles.itemLabel}>流动性深度</span>
            <span className={styles.itemValue}>{metrics?.liquidityScore.toFixed(0)}/100</span>
          </div>
        </div>

        <div className={styles.divider} />

        <h4 className={styles.gridTitle}>历史绩效表现</h4>
        <div className={styles.fullGrid}>
          <div className={styles.gridItem}>
            <span className={styles.itemLabel}>{t.riskRibbon.winRate}</span>
            <span className={styles.itemValue}>{winRateDisplay}</span>
          </div>
          <div className={styles.gridItem}>
            <span className={styles.itemLabel}>{t.riskRibbon.profitFactor}</span>
            <span className={styles.itemValue}>{profitFactorDisplay}</span>
          </div>
          <div className={styles.gridItem}>
            <span className={styles.itemLabel}>{t.riskRibbon.maxDrawdown}</span>
            <span className={`${styles.itemValue} ${styles.negative}`}>-{performanceMetrics.maxDrawdown.toFixed(1)}%</span>
          </div>
          <div className={styles.gridItem}>
            <span className={styles.itemLabel}>累计盈亏</span>
            <span className={`${styles.itemValue} ${realizedPnl >= 0 ? styles.positive : styles.negative}`}>
              {hasClosedTrades ? `$${performanceMetrics.totalRealizedPnl}` : EMPTY}
            </span>
          </div>
        </div>
      </div>
    );
  }

  if (compact) {
    return (
      <div className={styles.compactContainer}>
        <div className={`${styles.compactLevel} ${styles[riskLevel]}`}>
          <Icon name="shield" size="xs" />
          {getRiskLabel(riskLevel)}
        </div>
        <span className={styles.compactDivider}>|</span>
        <span className={styles.compactMetric}>
          Pos: {riskMetrics.positionSizePercent.toFixed(0)}%
        </span>
        <span className={`${styles.compactMetric} ${
          riskMetrics.unrealizedPnlPercent >= 0 ? styles.positive : styles.negative
        }`}>
          {riskMetrics.unrealizedPnlPercent >= 0 ? '+' : ''}
          {riskMetrics.unrealizedPnlPercent.toFixed(1)}%
        </span>
      </div>
    );
  }

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <div className={styles.titleGroup}>
          <Icon name="shield" size="xs" className={styles.riskIcon} />
          <span className={styles.title}>{t.riskRibbon.title}</span>
        </div>
        <div className={`${styles.level} ${styles[riskLevel]}`}>
          {getRiskLabel(riskLevel)}
        </div>
      </div>

      <div className={styles.ribbon}>
        <div 
          className={`${styles.segmentFill} ${
            riskMetrics.unrealizedPnlPercent >= 0 ? styles.profit : styles.loss
          }`}
          style={{ width: `${Math.min(riskMetrics.positionSizePercent, 100)}%` }}
        />
      </div>

      <div className={styles.perfGrid}>
        <div className={styles.perfItem} title={t.riskRibbon.positionRatio}>
          <span className={styles.perfLabel}>Pos</span>
          <span className={styles.perfValue}>
            {riskMetrics.positionSizePercent.toFixed(1)}%
          </span>
        </div>
        <div className={styles.perfItem} title={t.riskRibbon.unrealizedPnL}>
          <span className={styles.perfLabel}>PnL</span>
          <span className={`${styles.perfValue} ${
            riskMetrics.unrealizedPnlPercent >= 0 ? styles.positive : styles.negative
          }`}>
            {riskMetrics.unrealizedPnlPercent >= 0 ? '+' : ''}
            {riskMetrics.unrealizedPnlPercent.toFixed(2)}%
          </span>
        </div>
        <div className={styles.perfItem} title={t.riskRibbon.winRate}>
          <span className={styles.perfLabel}>Win</span>
          <span className={styles.perfValue}>
            {hasClosedTrades ? `${(performanceMetrics.winRate * 100).toFixed(0)}%` : EMPTY}
          </span>
        </div>
        <div className={styles.perfItem} title={t.riskRibbon.profitFactor}>
          <span className={styles.perfLabel}>PF</span>
          <span className={styles.perfValue}>
            {hasClosedTrades
              ? (Number.isFinite(performanceMetrics.profitFactor)
                  ? performanceMetrics.profitFactor.toFixed(2)
                  : '∞')
              : EMPTY}
          </span>
        </div>
        <div className={styles.perfItem} title={t.riskRibbon.maxDrawdown}>
          <span className={styles.perfLabel}>DD</span>
          <span className={`${styles.perfValue} ${styles.negative}`}>
            -{performanceMetrics.maxDrawdown.toFixed(0)}%
          </span>
        </div>
        <div className={styles.perfItem} title={t.riskRibbon.totalRealizedPnl}>
          <span className={styles.perfLabel}>Real</span>
          <span className={`${styles.perfValue} ${
            !hasClosedTrades ? '' : realizedPnl >= 0 ? styles.positive : styles.negative
          }`}>
            {hasClosedTrades
              ? `${realizedPnl >= 0 ? '+' : ''}${performanceMetrics.totalRealizedPnl}`
              : EMPTY}
          </span>
        </div>
      </div>
    </div>
  );
}
