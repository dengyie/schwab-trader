import { useState, useCallback, useEffect, useMemo } from 'react';
import { useMarketStore, selectOrderBook, selectMetrics, selectBestBid, selectBestAsk, selectDataConfidence } from '../../store/marketStore';
import { useTradingStore } from '../../store/tradingStore';
import { useWalletStore, selectBalances } from '../../store/walletStore';
import { useMarketsStore, marketCodeOfSymbol } from '../../store/marketsStore';
import { useI18n, formatMessage } from '../../i18n';
import { formatClockMoment } from '../../utils/timeFormat';
import { toast } from '../../components/Toast';
import { notification, type ToastKey } from '../../store/notificationStore';
import { Icon } from '../../components/Icon';
import { useHapticFeedback } from '../../hooks/useHapticFeedback';
import type { OrderSide, OrderType } from '../../types/trading';
import styles from './MobileOrderEntry.module.css';

interface MobileOrderEntryProps {
  side: OrderSide;
  onSideChange: (side: OrderSide) => void;
  priceFromOrderBook?: string;
  onSuccess?: () => void;
}

export function MobileOrderEntry({
  side,
  onSideChange,
  priceFromOrderBook,
  onSuccess,
}: MobileOrderEntryProps) {
  const { t } = useI18n();
  const { trigger } = useHapticFeedback();
  const [type, setType] = useState<OrderType>('limit');
  const [price, setPrice] = useState(priceFromOrderBook || '');
  const [quantity, setQuantity] = useState('');
  const [quantityPercent, setQuantityPercent] = useState(0);

  const orderBook = useMarketStore(selectOrderBook);
  const metrics = useMarketStore(selectMetrics);
  const bestBid = useMarketStore(selectBestBid);
  const bestAsk = useMarketStore(selectBestAsk);
  const dataConfidence = useMarketStore(selectDataConfidence);
  const balances = useWalletStore(selectBalances);
  const createOrder = useTradingStore((state) => state.createOrder);

  const symbol = orderBook?.symbol ?? 'SPY';
  const baseAsset = symbol.replace('/USD', '').replace('USDT', '');
  // 币种随标的市场变化：港股 HKD、日股 JPY、A 股 CNY。
  const market = marketCodeOfSymbol(symbol, useMarketsStore.getState().markets);
  const quoteAsset = useMarketsStore.getState().currencyOf(symbol);
  const isForeignMarket = market !== 'US';
  const lotSize = Math.max(1, useMarketsStore.getState().lotSizeFor(symbol));
  const baseBalance = balances.find(b => b.asset === baseAsset);
  const quoteBalance = balances.find(b => b.asset === quoteAsset) || balances.find(b => b.asset === 'USD');
  // F2 (2026-11-28 GUI test): venue purchasing power — Alpaca margin ≈ 2x
  // cash (RegT); falls back to cash when the field hasn't synced yet.
  const buyingPower = useWalletStore((s) => s.brokerMetrics?.buyingPower
    ?? s.brokerMetrics?.cash
    ?? parseFloat(quoteBalance?.available || '0'));

  // Sync price from orderbook
  useEffect(() => {
    if (priceFromOrderBook) setPrice(priceFromOrderBook);
  }, [priceFromOrderBook]);

  // Auto-fill price when switching to limit
  useEffect(() => {
    if (type === 'limit' && !price && metrics) {
      setPrice(side === 'buy' ? (bestAsk?.price || metrics.mid) : (bestBid?.price || metrics.mid));
    }
  }, [type, price, metrics, side, bestAsk, bestBid]);

  const total = useMemo(() => {
    if (type === 'limit' && price && quantity) {
      const p = parseFloat(price);
      const q = parseFloat(quantity);
      // F7 (2026-11-28 GUI test): NaN/negative must render as $0.
      return Number.isFinite(p) && Number.isFinite(q) && p * q >= 0 ? (p * q).toFixed(2) : '0';
    } else if (type === 'market' && quantity && metrics) {
      const q = parseFloat(quantity);
      const mid = parseFloat(metrics.mid);
      return Number.isFinite(q) && Number.isFinite(mid) && mid * q >= 0 ? (mid * q).toFixed(2) : '0';
    }
    return '0';
  }, [price, quantity, type, metrics]);

  const getMaxQuantity = useCallback(() => {
    if (side === 'buy' && quoteBalance && metrics) {
      // F2：口径改为券商购买力（保证金账户 ≈ 2x cash），未同步时回退 cash。
      const av = buyingPower;
      const p = type === 'limit' && price ? parseFloat(price) : parseFloat(metrics.mid);
      return p > 0 ? av / p : 0;
    } else if (side === 'sell' && baseBalance) {
      return parseFloat(baseBalance.available);
    }
    return 0;
  }, [side, type, price, quoteBalance, baseBalance, metrics, buyingPower]);

  const updateQuantityFromPercent = useCallback((pct: number) => {
    const max = getMaxQuantity();
    if (max > 0) {
      setQuantity((max * pct / 100).toFixed(6));
      setQuantityPercent(pct);
      trigger('selection');
    }
  }, [getMaxQuantity, trigger]);

  const handleSubmit = () => {
    if (dataConfidence.level === 'stale') {
      toast.warning(t.dataConfidence?.staleDesc || 'Data is stale');
      return;
    }
    // 收盘时市价单无法成交：直接给出解释而不是让交易所报错
    if (dataConfidence.level === 'closed' && type === 'market') {
      toast.warning(t.dataConfidence?.closedDesc || 'Market closed');
      return;
    }
    // Number.isFinite guards (2026-11-23 review P2): Infinity/NaN slipped
    // through the `<= 0` checks (same fix as the desktop OrderEntry).
    const qtyNum = parseFloat(quantity);
    if (!quantity || !Number.isFinite(qtyNum) || qtyNum <= 0 || qtyNum > 1e9) {
      toast.warning(t.orderEntry?.invalidAmount || 'Invalid amount');
      return;
    }
    // 非美股按整手交易，数量必须是每手股数的整数倍。
    if (lotSize > 1) {
      const q = parseFloat(quantity);
      if (Math.abs(q / lotSize - Math.round(q / lotSize)) > 1e-9) {
        toast.warning(t.orderEntry?.lotError || `Quantity must be a multiple of ${lotSize}`);
        return;
      }
    }
    const limitNum = price ? parseFloat(price) : NaN;
    if (type === 'limit' && (!price || !Number.isFinite(limitNum) || limitNum <= 0 || limitNum > 1e9)) {
      toast.warning(t.orderEntry?.invalidPrice || 'Invalid price');
      return;
    }
    // F2 (2026-11-28 GUI test): tiered buying-power check, same policy as
    // the desktop OrderEntry — hard-block above buying power, warn on margin.
    const costUsd = (() => {
      const p = type === 'limit' ? parseFloat(price) : parseFloat(metrics?.mid || '0');
      const q = parseFloat(quantity);
      return Number.isFinite(p) && Number.isFinite(q) ? p * q : 0;
    })();
    if (side === 'buy' && costUsd > 0) {
      if (costUsd > buyingPower + 1e-6) {
        toast.error(
          formatMessage(t.toast?.insufficientBuyingPower || '购买力不足：订单总额 {total} 超过购买力 {power}', {
            total: `${costUsd.toFixed(2)}`,
            power: `${buyingPower.toFixed(2)}`,
          }),
        );
        return;
      }
      const cashAvail = parseFloat(quoteBalance?.available || '0');
      if (costUsd > cashAvail + 1e-6) {
        toast.warning(
          formatMessage(t.toast?.marginWarning || '订单将动用保证金（总额 {total} > 现金 {cash}），成交依赖购买力 {power}', {
            total: `${costUsd.toFixed(2)}`,
            cash: `${cashAvail.toFixed(2)}`,
            power: `${buyingPower.toFixed(2)}`,
          }),
        );
      }
    }
    // F1 (2026-11-28 GUI test): warn on naked short, don't block.
    if (side === 'sell' && baseBalance) {
      const held = parseFloat(baseBalance.available);
      if (Number.isFinite(qtyNum) && qtyNum > held + 1e-9) {
        toast.warning(t.toast?.shortSellWarning || '卖出数量超过持仓：超出部分将做空（Alpaca 保证金账户支持，虚拟账本成交时会拒绝）');
      }
    }

    const order = createOrder({
      symbol,
      side,
      type,
      price: type === 'limit' ? price : undefined,
      quantity,
    }, metrics?.mid);

    if (order && !('blocked' in order)) {
      // 收盘时段：市价单已在上面被拦下，能提交的只有挂单型订单，由券商在
      // 下次开盘后撮合。不写清楚的话，“已提交”会被读成“已成交”。
      // 时钟取自市场注册表（带 timezone），与桌面端 OrderEntry 同一来源。
      const marketClock = useMarketsStore.getState().marketOf(symbol)?.clock;
      const marketClosed = marketClock ? !marketClock.is_open : false;
      if (marketClosed && type === 'limit') {
        const nextOpen = formatClockMoment(marketClock?.next_open, marketClock?.timezone);
        toast.warning(
          nextOpen
            ? formatMessage(t.toast?.orderQueuedForOpen || '订单已提交，将在下次开盘后生效：{time}', { time: nextOpen })
            : (t.toast?.orderQueued || '订单已提交，将在下次开盘后生效'),
          6000,
        );
      } else {
        // F5 (2026-11-28 GUI test): keyed toast so tradingStore can withdraw
        // it on venue rejection (matches the desktop OrderEntry).
        notification.success(`order:submit:${order.clientOrderId}` as ToastKey, t.toast?.orderSubmitted || 'Order submitted');
      }
      trigger('success');
      setQuantity('');
      setQuantityPercent(0);
      onSuccess?.();
    } else if (order && 'blocked' in order) {
      // 商店拒单要报真实原因，不能一律甩"余额不足"（2026-11-16 review）。
      // invalid_params / markets_loading 与桌面端同步补齐（2026-11-23
      // second review P2-B）：移动端此前落进兜底"余额不足"，误导排查方向。
      const blockToast: Record<string, string | undefined> = {
        data_confidence: t.orderEntry?.resyncingBlock,
        invalid_quantity: t.orderEntry?.invalidAmount,
        invalid_params: t.orderEntry?.invalidPrice,
        missing_price: t.orderEntry?.invalidPrice,
        missing_trigger: t.orderEntry?.invalidTriggerPrice,
        lot_floor_zero: t.orderEntry?.lotError,
        markets_loading: t.orderEntry?.marketsLoading,
      };
      toast.warning(blockToast[order.blocked] || t.orderEntry?.invalidAmount || 'Order could not be created');
      trigger('error');
    } else {
      toast.error(t.orderEntry?.insufficientBalance || 'Insufficient balance');
      trigger('error');
    }
  };

  const availableBalance = side === 'buy'
    ? `${parseFloat(quoteBalance?.available ?? '0').toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${quoteAsset}`
    : `${parseFloat(baseBalance?.available ?? '0').toFixed(4)} ${baseAsset}`;

  return (
    <div className={styles.container}>
      {/* Side Toggle */}
      <div className={styles.sideToggle}>
        <button
          className={`${styles.sideBtn} ${styles.buyBtn} ${side === 'buy' ? styles.active : ''}`}
          onClick={() => onSideChange('buy')}
        >
          {t.orderEntry?.buy || 'Buy'}
        </button>
        <button
          className={`${styles.sideBtn} ${styles.sellBtn} ${side === 'sell' ? styles.active : ''}`}
          onClick={() => onSideChange('sell')}
        >
          {t.orderEntry?.sell || 'Sell'}
        </button>
      </div>

      {/* Type Toggle */}
      <div className={styles.typeToggle}>
        <button
          className={`${styles.typeBtn} ${type === 'limit' ? styles.active : ''}`}
          onClick={() => setType('limit')}
        >
          {t.orderEntry?.limit || 'Limit'}
        </button>
        <button
          className={`${styles.typeBtn} ${type === 'market' ? styles.active : ''}`}
          onClick={() => setType('market')}
        >
          {t.orderEntry?.market || 'Market'}
        </button>
      </div>

      {/* Price Input */}
      {type === 'limit' && (
        <div className={styles.inputGroup}>
          <label className={styles.label}>{t.orderEntry?.price || 'Price'}</label>
          <div className={styles.inputWrapper}>
            <input
              type="text"
              inputMode="decimal"
              className={styles.input}
              value={price}
              onChange={(e) => setPrice(e.target.value)}
              placeholder="0.00"
            />
            <span className={styles.inputSuffix}>{quoteAsset}</span>
          </div>
          <div className={styles.quickPriceRow}>
            <button
              className={styles.quickPriceBtn}
              onClick={() => bestBid && setPrice(bestBid.price)}
            >
              Bid
            </button>
            <button
              className={styles.quickPriceBtn}
              onClick={() => metrics && setPrice(metrics.mid)}
            >
              Mid
            </button>
            <button
              className={styles.quickPriceBtn}
              onClick={() => bestAsk && setPrice(bestAsk.price)}
            >
              Ask
            </button>
          </div>
        </div>
      )}

      {/* Quantity Input */}
      <div className={styles.inputGroup}>
        <div className={styles.labelRow}>
          <label className={styles.label}>{t.orderEntry?.amount || 'Amount'}</label>
          <span className={styles.availableHint}>
            {t.orderEntry?.available || 'Available'}: {availableBalance}
          </span>
        </div>
        <div className={styles.inputWrapper}>
          <input
            type="text"
            inputMode="decimal"
            className={styles.input}
            value={quantity}
            onChange={(e) => {
              setQuantity(e.target.value);
              const val = parseFloat(e.target.value);
              const max = getMaxQuantity();
              if (!isNaN(val) && max > 0) {
                setQuantityPercent(Math.min(100, Math.max(0, (val / max) * 100)));
              } else {
                setQuantityPercent(0);
              }
            }}
            placeholder="0.00"
          />
          <span className={styles.inputSuffix}>{baseAsset}</span>
        </div>

        {/* Quantity Slider */}
        <div className={styles.sliderContainer}>
          <div className={styles.sliderWrapper}>
            <div className={styles.sliderSteps}>
              {[0, 25, 50, 75, 100].map(step => (
                <div 
                  key={step} 
                  className={`${styles.sliderStep} ${quantityPercent >= step ? styles.active : ''}`}
                  style={{ left: `${step}%` }}
                />
              ))}
            </div>
            <div 
              className={styles.sliderTrack} 
              style={{ width: `${quantityPercent}%` }}
            />
            <input
              type="range"
              min="0"
              max="100"
              step="1"
              value={quantityPercent}
              onChange={(e) => updateQuantityFromPercent(parseInt(e.target.value))}
              className={styles.slider}
            />
          </div>
          <div className={styles.percentRow}>
            {[0, 25, 50, 75, 100].map((pct) => (
              <span
                key={pct}
                className={`${styles.percentLabel} ${Math.round(quantityPercent) === pct ? styles.active : ''}`}
                onClick={() => updateQuantityFromPercent(pct)}
              >
                {pct}%
              </span>
            ))}
          </div>
        </div>
      </div>

      {/* Total */}
      <div className={styles.totalRow}>
        <span className={styles.totalLabel}>{t.orderEntry?.total || 'Total'}</span>
        <span className={`${styles.totalValue} tabular-nums`}>
          ≈ {total} {quoteAsset}
        </span>
      </div>

      {/* Data Confidence Warning */}
      {dataConfidence.level !== 'live' && (
        <div className={`${styles.warning} ${styles[dataConfidence.level]}`}>
          <Icon name="alert-triangle" size="sm" />
          <span>{dataConfidence.reason}</span>
        </div>
      )}

      {/* Submit Button */}
      <button
        className={`${styles.submitBtn} ${side === 'buy' ? styles.buySubmit : styles.sellSubmit}`}
        onClick={handleSubmit}
        disabled={dataConfidence.level === 'stale' || (dataConfidence.level === 'closed' && type === 'market') || !quantity}
      >
        {side === 'buy'
          ? formatMessage(t.orderEntry?.placeBuyOrder || 'Buy {symbol}', { symbol: baseAsset })
          : formatMessage(t.orderEntry?.placeSellOrder || 'Sell {symbol}', { symbol: baseAsset })
        }
      </button>
    </div>
  );
}

