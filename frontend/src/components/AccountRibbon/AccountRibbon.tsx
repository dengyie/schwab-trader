import { useMemo } from 'react';
import Decimal from 'decimal.js';
import { useTradingStore } from '../../store/tradingStore';
import { useWalletStore, selectBalances } from '../../store/walletStore';
import { useMarketStore, selectMetrics, selectOrderBook } from '../../store/marketStore';
import { useWatchlistStore, selectSelectedSymbol } from '../../store/watchlistStore';
import { useI18n } from '../../i18n';
import { Icon } from '../Icon';
import styles from './AccountRibbon.module.css';

interface AccountMetrics {
  totalValue: Decimal;
  available: Decimal;
  positionValue: Decimal;
  unrealizedPnl: Decimal;
  unrealizedPnlPercent: number;
  hasRealTimePrice: boolean;
}

export function AccountRibbon() {
  const { t } = useI18n();
  const balances = useWalletStore(selectBalances);
  const brokerMetrics = useWalletStore((state) => state.brokerMetrics);
  const positions = useTradingStore((state) => state.positions);
  const metrics = useMarketStore(selectMetrics);
  const orderBook = useMarketStore(selectOrderBook);
  const selectedSymbol = useWatchlistStore(selectSelectedSymbol);

  const currentSymbol = orderBook?.symbol || selectedSymbol;
  const currentSymbolMidPrice = metrics ? new Decimal(metrics.mid) : new Decimal(0);

  const accountMetrics = useMemo((): AccountMetrics => {
    const usdBalance = balances.find(b => b.asset === 'USD');
    const usdEquity = new Decimal(usdBalance?.total ?? '0');
    const usdAvailable = new Decimal(usdBalance?.available ?? '0');

    // Calculate position values (fallback when the broker metrics are unknown)
    let positionEntries: [string, any][] = [];
    if (positions instanceof Map) {
      positionEntries = Array.from(positions.entries());
    } else if (typeof positions === 'object' && positions !== null) {
      positionEntries = Object.entries(positions);
    }

    let derivedPositionValue = new Decimal(0);
    let derivedUnrealizedPnl = new Decimal(0);
    let hasRealTimePrice = true;

    positionEntries
      .filter(([_, pos]) => {
        if (!pos || pos.quantity === undefined || pos.avgEntryPrice === undefined) {
          return false;
        }
        return pos.side === 'long' && new Decimal(pos.quantity).gt(0);
      })
      .forEach(([symbol, pos]) => {
        const qty = new Decimal(pos.quantity || '0');
        const avgEntry = new Decimal(pos.avgEntryPrice || '0');

        const isCurrentSymbol = symbol === currentSymbol;
        const currentPrice = isCurrentSymbol ? currentSymbolMidPrice : avgEntry;
        const hasPrice = isCurrentSymbol && currentSymbolMidPrice.gt(0);

        const value = qty.times(currentPrice);
        const unrealizedPnl = hasPrice ? qty.times(currentPrice.minus(avgEntry)) : new Decimal(0);

        if (hasPrice) {
          derivedPositionValue = derivedPositionValue.plus(value);
          derivedUnrealizedPnl = derivedUnrealizedPnl.plus(unrealizedPnl);
        } else {
          derivedPositionValue = derivedPositionValue.plus(qty.times(avgEntry));
          if (isCurrentSymbol) hasRealTimePrice = false;
        }
      });

    // Broker-reported figures win: they are the account's real mark-to-market,
    // and they keep the header, the ribbon and the wallet page on one number.
    const totalValue = brokerMetrics ? new Decimal(brokerMetrics.totalEquity) : usdEquity;
    const available = brokerMetrics ? new Decimal(brokerMetrics.cash) : usdAvailable;
    const positionValue = brokerMetrics?.positionValue != null
      ? new Decimal(brokerMetrics.positionValue)
      : derivedPositionValue;
    const unrealizedPnl = brokerMetrics?.unrealizedPnl != null
      ? new Decimal(brokerMetrics.unrealizedPnl)
      : derivedUnrealizedPnl;

    const unrealizedPnlPercent = totalValue.gt(0)
      ? unrealizedPnl.div(totalValue).times(100).toNumber()
      : 0;

    return {
      totalValue,
      available,
      positionValue,
      unrealizedPnl,
      unrealizedPnlPercent,
      hasRealTimePrice,
    };
  }, [balances, brokerMetrics, positions, currentSymbol, currentSymbolMidPrice]);

  const formatMoney = (value: Decimal): string => {
    return value.toFixed(2);
  };

  const pnlIsPositive = accountMetrics.unrealizedPnl.gte(0);

  return (
    <div className={styles.container}>
      <div className={styles.item}>
        <span className={styles.label}>{t.account?.totalValue || 'Total'}</span>
        <span className={`${styles.value} tabular-nums`}>
          ${formatMoney(accountMetrics.totalValue)}
        </span>
      </div>

      <div className={styles.divider} />

      <div className={styles.item}>
        <span className={styles.label}>{t.account?.available || 'Available'}</span>
        <span className={`${styles.value} tabular-nums`}>
          ${formatMoney(accountMetrics.available)}
        </span>
      </div>

      <div className={styles.divider} />

      <div className={styles.item}>
        <span className={styles.label}>{'Position'}</span>
        <span className={`${styles.value} tabular-nums`}>
          ${formatMoney(accountMetrics.positionValue)}
        </span>
      </div>

      <div className={styles.divider} />

      <div className={styles.item}>
        <span className={styles.label}>{t.positions?.unrealizedPnL || 'Unrealized P&L'}</span>
        <span className={`${styles.value} ${styles.pnl} tabular-nums ${pnlIsPositive ? styles.positive : styles.negative}`}>
          {pnlIsPositive ? '+' : ''}{formatMoney(accountMetrics.unrealizedPnl)}
          <span className={styles.pnlPercent}>
            ({pnlIsPositive ? '+' : ''}{accountMetrics.unrealizedPnlPercent.toFixed(2)}%)
          </span>
        </span>
      </div>

      {!accountMetrics.hasRealTimePrice && (
        <div className={styles.warningIcon} title={'No real-time price'}>
          <Icon name="alert-circle" size="xs" />
        </div>
      )}
    </div>
  );
}

