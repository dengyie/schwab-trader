import { useState, useCallback, useEffect, useRef } from 'react';
import { useMarketStore, selectActiveSymbol, selectMid, selectSlippageEst, selectBestBid, selectBestAsk, selectDataConfidence } from '../../store/marketStore';
import { useTradingStore, selectFocusMode } from '../../store/tradingStore';
import { useWalletStore, selectBalances } from '../../store/walletStore';
import { useWatchlistStore, selectSelectedSymbol } from '../../store/watchlistStore';
import { useMarketsStore, marketCodeOfSymbol } from '../../store/marketsStore';
import { notification, type ToastKey } from '../../store/notificationStore';
import { currencySymbol, roundToTick } from '../../utils/money';
import { formatClockMoment } from '../../utils/timeFormat';
import { useI18n, formatMessage } from '../../i18n';
import { toast } from '../Toast';
import { Icon } from '../Icon';
import { QuantitySlider } from './QuantitySlider';
import type { OrderSide, OrderType, TriggerDirection, TrailingType } from '../../types/trading';
import styles from './OrderEntry.module.css';

// Order type categories for UI
type OrderCategory = 'spot' | 'conditional' | 'oco';

interface ConfirmModalProps {
  isOpen: boolean;
  title: string;
  message: string;
  detail?: string;
  confirmText: string;
  cancelText: string;
  onConfirm: () => void;
  onCancel: () => void;
  type?: 'buy' | 'sell';
}

function ConfirmModal({ isOpen, title, message, detail, confirmText, cancelText, onConfirm, onCancel, type = 'buy' }: ConfirmModalProps) {
  if (!isOpen) return null;
  return (
    <div className={styles.modalOverlay} onClick={onCancel}>
      <div className={styles.modalContent} onClick={e => e.stopPropagation()}>
        <div className={styles.modalHeader}>
          <Icon name="alert-circle" size="sm" className={type === 'buy' ? styles.buyIcon : styles.sellIcon} />
          <h3 className={styles.modalTitle}>{title}</h3>
        </div>
        <div className={styles.modalBody}>
          <p className={styles.modalMessage}>{message}</p>
          {detail && <p className={styles.modalDetail}>{detail}</p>}
        </div>
        <div className={styles.modalActions}>
          <button className={styles.modalCancelBtn} onClick={onCancel}>{cancelText}</button>
          <button className={`${styles.modalConfirmBtn} ${type === 'buy' ? styles.buyConfirm : styles.sellConfirm}`} onClick={onConfirm}>{confirmText}</button>
        </div>
      </div>
    </div>
  );
}

interface OrderEntryProps {
  priceFromOrderBook?: string;
  sideFromOrderBook?: OrderSide;
  compact?: boolean;
}

export function OrderEntry({ priceFromOrderBook, sideFromOrderBook, compact: _compact = false }: OrderEntryProps) {
  const { t } = useI18n();
  const [side, setSide] = useState<OrderSide>('buy');
  const [orderCategory, setOrderCategory] = useState<OrderCategory>('spot');
  const [type, setType] = useState<OrderType>('limit');
  const [price, setPrice] = useState('');
  const [quantity, setQuantity] = useState('');
  const [total, setTotal] = useState('0');
  const [quantityPercent, setQuantityPercent] = useState(0);
  const [takeProfitPrice, setTakeProfitPrice] = useState('');
  const [stopLossPrice, setStopLossPrice] = useState('');
  const [showDegradedConfirm, setShowDegradedConfirm] = useState(false);
  const [showAllInConfirm, setShowAllInConfirm] = useState(false);
  // In-flight guard (2026-11-23 review P2): every submit generates a NEW
  // client_order_id, so the backend idempotency key cannot dedupe two
  // separate clicks — the guard must live here, covering the degraded
  // confirm path too.
  const submittingRef = useRef(false);
  
  // Conditional order fields
  const [triggerPrice, setTriggerPrice] = useState('');
  const [limitPrice, setLimitPrice] = useState('');
  
  // OCO order fields
  const [ocoLimitPrice, setOcoLimitPrice] = useState('');
  const [ocoStopPrice, setOcoStopPrice] = useState('');
  const [ocoStopLimitPrice, setOcoStopLimitPrice] = useState('');
  
  // Trailing stop fields
  const [trailingType, setTrailingType] = useState<TrailingType>('percent');
  const [trailingValue, setTrailingValue] = useState('');
  const [trailingActivationPrice, setTrailingActivationPrice] = useState('');
  
  // Scalar projections only: subscribing to the whole orderBook/metrics
  // objects re-rendered this form on every tick that moved any sibling field.
  const orderBookSymbol = useMarketStore(selectActiveSymbol);
  const mid = useMarketStore(selectMid);
  const slippageEstRaw = useMarketStore(selectSlippageEst);
  const metrics = mid !== undefined ? { mid, slippageEst: slippageEstRaw } : null;
  const bestBid = useMarketStore(selectBestBid);
  const bestAsk = useMarketStore(selectBestAsk);
  const dataConfidence = useMarketStore(selectDataConfidence);
  const balances = useWalletStore(selectBalances);
  const focusMode = useTradingStore(selectFocusMode);
  const createOrder = useTradingStore((state) => state.createOrder);
  const createOCOOrder = useTradingStore((state) => state.createOCOOrder);
  const createTrailingStopOrder = useTradingStore((state) => state.createTrailingStopOrder);
  const setFocusMode = useTradingStore((state) => state.setFocusMode);
  
  const priceInputRef = useRef<HTMLInputElement>(null);
  const quantityInputRef = useRef<HTMLInputElement>(null);
  const tpInputRef = useRef<HTMLInputElement>(null);
  const slInputRef = useRef<HTMLInputElement>(null);
  const isInputFocused = useRef(false);
  const activeInputRef = useRef<'price' | 'quantity' | 'tp' | 'sl' | 'trigger' | 'limit' | null>(null);

  const selectedSymbol = useWatchlistStore(selectSelectedSymbol);
  const symbol = selectedSymbol || orderBookSymbol || 'SPY';
  const baseAsset = symbol.replace('/USD', '').replace('USDT', '');

  // 多市场：币种、每手、最小跳动、涨跌停、交易时段全部来自市场注册表。
  const markets = useMarketsStore((s) => s.markets);
  const market = marketCodeOfSymbol(symbol, markets);
  const marketDef = markets.find((m) => m.code === market);
  const quoteAsset = marketDef?.currency || 'USD';
  const isForeignMarket = market !== 'US';
  const lotSize = Math.max(1, marketDef?.lot_size ?? (isForeignMarket ? 100 : 1));
  const priceDecimals = marketDef?.decimals ?? 2;
  const baseBalance = balances.find(b => b.asset === baseAsset);
  const quoteBalance = balances.find(b => b.asset === quoteAsset);
  // F2 (2026-11-28 GUI test): venue purchasing power — for the Alpaca margin
  // account this is ~2x cash (RegT). Fall back to cash when the field hasn't
  // synced yet (older backend / cash-only venue).
  const buyingPower = useWalletStore((s) => s.brokerMetrics?.buyingPower
    ?? s.brokerMetrics?.cash
    ?? parseFloat(quoteBalance?.available || '0'));
  const priceLimitPct = useMarketsStore((s) => s.priceLimitPctFor(symbol));
  const marketClock = marketDef?.clock;
  const marketClosed = marketClock ? !marketClock.is_open : false;
  const tickFor = (value: number) => useMarketsStore.getState().tickSizeFor(symbol, value);

  // A 股涨跌停区间：以昨收/最新价为基准，超出直接拦在提交前。
  const priceBand = (() => {
    if (!priceLimitPct) return null;
    const ref = parseFloat(metrics?.mid || '0');
    if (!(ref > 0)) return null;
    const pct = priceLimitPct / 100;
    return { low: ref * (1 - pct), high: ref * (1 + pct) };
  })();

  const priceOutOfBand = (value: string): boolean => {
    if (!priceBand || !value) return false;
    const p = parseFloat(value);
    if (!(p > 0)) return false;
    return p < priceBand.low - 1e-9 || p > priceBand.high + 1e-9;
  };

  // 数量必须是整手。
  const quantityNotLot = (value: string): boolean => {
    if (lotSize <= 1 || !value) return false;
    const q = parseFloat(value);
    if (!(q > 0)) return false;
    return Math.abs(q / lotSize - Math.round(q / lotSize)) > 1e-9;
  };

  // Reset fields when category changes
  useEffect(() => {
    if (orderCategory === 'spot') {
      setType('limit');
      setTriggerPrice('');
      setLimitPrice('');
    } else if (orderCategory === 'conditional') {
      setType('stop_limit');
    } else if (orderCategory === 'oco') {
      setOcoLimitPrice('');
      setOcoStopPrice('');
      setOcoStopLimitPrice('');
    }
  }, [orderCategory]);

  useEffect(() => {
    if (priceFromOrderBook) {
      setPrice(priceFromOrderBook);
      if (orderCategory === 'conditional') {
        setTriggerPrice(priceFromOrderBook);
      }
    }
    if (sideFromOrderBook) setSide(sideFromOrderBook);
  }, [priceFromOrderBook, sideFromOrderBook, orderCategory]);

  useEffect(() => {
    // Calculate total based on order type
    let priceForCalc = '0';
    
    if (orderCategory === 'spot') {
      if (type === 'limit' && price) {
        priceForCalc = price;
      } else if (type === 'market' && metrics) {
        priceForCalc = metrics.mid;
      }
    } else if (orderCategory === 'conditional') {
      if (['stop_limit', 'take_profit_limit'].includes(type) && limitPrice) {
        priceForCalc = limitPrice;
      } else if (triggerPrice) {
        priceForCalc = triggerPrice;
      }
    } else if (orderCategory === 'oco') {
      if (ocoLimitPrice) {
        priceForCalc = ocoLimitPrice;
      }
    }
    
    if (priceForCalc && quantity) {
      const p = parseFloat(priceForCalc);
      const q = parseFloat(quantity);
      // F7 (2026-11-28 GUI test): parseFloat("abc") is NaN and parseFloat
      // ("-1") is negative — both must render as $0, not "NaN" or a debt.
      setTotal(Number.isFinite(p) && Number.isFinite(q) && p * q >= 0 ? (p * q).toFixed(2) : '0');
    } else {
      setTotal('0');
    }
  }, [price, quantity, type, metrics, orderCategory, triggerPrice, limitPrice, ocoLimitPrice]);

  const getMaxQuantity = useCallback(() => {
    if (side === 'buy' && (quoteBalance || (isForeignMarket && balances?.length))) {
      // 外币买入：账户现金始终以 USD 记账，购买力按汇率折算成当地币种。
      // F2：口径改为券商购买力（保证金账户 ≈ 2x cash），未同步时回退 cash。
      const usdAv = buyingPower;
      const rate = isForeignMarket ? useMarketsStore.getState().usdPerUnit(quoteAsset) : 1;
      const av = rate > 0 ? usdAv / rate : usdAv;
      const p = type === 'limit' && price ? parseFloat(price) : parseFloat(metrics?.mid || '0');
      return p > 0 ? av / p : 0;
    } else if (side === 'sell' && baseBalance) return parseFloat(baseBalance.available);
    return 0;
  }, [side, type, price, quoteBalance, baseBalance, metrics, isForeignMarket, quoteAsset, balances, buyingPower]);

  const updateQuantityFromPercent = useCallback((pct: number) => {
    const max = getMaxQuantity();
    if (max > 0) {
      // 整手向下取整：港股/日股/A 股买入必须是每手股数的整数倍。
      const raw = (max * pct) / 100;
      const stepped = lotSize > 1 ? Math.floor(raw / lotSize) * lotSize : raw;
      if (stepped > 0) setQuantity(String(stepped));
    }
  }, [getMaxQuantity, lotSize]);

  useEffect(() => {
    const max = getMaxQuantity();
    if (max > 0 && quantity) {
      const q = parseFloat(quantity);
      // F7: non-numeric input must read as %, not NaN%.
      const pct = Number.isFinite(q) ? Math.round((q / max) * 100) : 0;
      setQuantityPercent(Math.min(100, Math.max(0, pct)));
    } else setQuantityPercent(0);
  }, [quantity, getMaxQuantity]);

  const setFromBestBid = () => { if (bestBid) { setPrice(bestBid.price); setSide('buy'); } };
  const setFromBestAsk = () => { if (bestAsk) { setPrice(bestAsk.price); setSide('sell'); } };
  const setFromMid = () => { if (metrics) setPrice(metrics.mid); };

  const handleStepUp = useCallback(() => {
    if (price) {
      const p = parseFloat(price);
      if (!isNaN(p)) {
        const step = tickFor(p);
        setPrice(roundToTick(p + step, step).toFixed(priceDecimals));
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [price, symbol, priceDecimals]);

  const handleStepDown = useCallback(() => {
    if (price) {
      const p = parseFloat(price);
      if (!isNaN(p)) {
        const step = tickFor(p);
        setPrice(roundToTick(Math.max(0, p - step), step).toFixed(priceDecimals));
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [price, symbol, priceDecimals]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (document.activeElement === priceInputRef.current) {
        if (e.key === 'ArrowUp') { e.preventDefault(); handleStepUp(); }
        else if (e.key === 'ArrowDown') { e.preventDefault(); handleStepDown(); }
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [handleStepUp, handleStepDown]);

  const handleInputFocus = (inputName: 'price' | 'quantity' | 'tp' | 'sl' | 'trigger' | 'limit') => () => { 
    isInputFocused.current = true; 
    activeInputRef.current = inputName;
    setFocusMode(true); 
  };
  const handleInputBlur = () => { 
    isInputFocused.current = false; 
    setTimeout(() => { 
      if (!isInputFocused.current) {
        setFocusMode(false);
        activeInputRef.current = null;
      }
    }, 100); 
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    // In-flight lock: the ref is declared-and-checked but was never SET
    // before (2026-11-23 second review P1 — dead guard, double-click still
    // produced two orders with distinct client_order_ids that backend
    // idempotency cannot dedupe). createOrder is fully synchronous, so the
    // whole submit occupies one event-loop turn; holding the flag across
    // that turn plus a macrotask gap closes the rapid-double-click window.
    if (submittingRef.current) return;
    submittingRef.current = true;
    setTimeout(() => { submittingRef.current = false; }, 0);
    if (dataConfidence.level === 'stale') { toast.warning(t.dataConfidence.staleDesc); return; }
    // 收盘时市价单无法成交；限价/条件单会被券商挂起等下次开盘
    if (dataConfidence.level === 'closed' && type === 'market' && orderCategory === 'spot') {
      toast.warning(t.dataConfidence.closedDesc);
      return;
    }
    // 该市场自身休市时，市价单同样无处成交。
    if (marketClosed && type === 'market' && orderCategory === 'spot') {
      toast.warning(
        marketClock?.next_open
          ? `${market} closed · next open ${marketClock.next_open}`
          : `${market} is closed`,
      );
      return;
    }
    if (dataConfidence.level === 'degraded' && !showDegradedConfirm) { setShowDegradedConfirm(true); return; }
    // Number.isFinite guards: parseFloat("1e999") = Infinity and parseFloat("abc")
    // = NaN both slip through `<= 0` checks and die as a backend 422 AFTER the
    // success toast (2026-11-23 review P2). The store re-validates too.
    const qtyNum = parseFloat(quantity);
    if (!quantity || !Number.isFinite(qtyNum) || qtyNum <= 0 || qtyNum > 1e9) {
      toast.warning(t.orderEntry.invalidAmount);
      return;
    }
    if (quantityNotLot(quantity)) {
      toast.warning(t.orderEntry.lotError || `Quantity must be a multiple of ${lotSize}`);
      return;
    }
    // F2 (2026-11-28 GUI test): tiered buying-power check. Hard-block only
    // when the venue couldn't fill it at all; cash < total ≤ buying power is
    // legitimate RegT margin — warn and let it through.
    const costUsd = (() => {
      const effectivePrice = orderCategory === 'spot'
        ? (type === 'limit' ? parseFloat(price) : parseFloat(metrics?.mid || '0'))
        : parseFloat(limitPrice || triggerPrice || ocoLimitPrice || '0');
      const q = parseFloat(quantity);
      return Number.isFinite(effectivePrice) && Number.isFinite(q) ? effectivePrice * q : 0;
    })();
    if (side === 'buy' && costUsd > 0) {
      if (costUsd > buyingPower + 1e-6) {
        toast.error(
          (t.toast.insufficientBuyingPower || '购买力不足：订单总额 {total} 超过购买力 {power}')
            .replace('{total}', `${currencySymbol(quoteAsset)}${costUsd.toFixed(2)}`)
            .replace('{power}', `${currencySymbol(quoteAsset)}${buyingPower.toFixed(2)}`),
        );
        return;
      }
      const cashAvail = parseFloat(quoteBalance?.available || '0');
      if (costUsd > cashAvail + 1e-6) {
        // margin territory
        toast.warning(
          (t.toast.marginWarning || '订单将动用保证金（总额 {total} > 现金 {cash}），成交依赖购买力 {power}')
            .replace('{total}', `${currencySymbol(quoteAsset)}${costUsd.toFixed(2)}`)
            .replace('{cash}', `${currencySymbol(quoteAsset)}${parseFloat(quoteBalance?.available || '0').toFixed(2)}`)
            .replace('{power}', `${currencySymbol(quoteAsset)}${buyingPower.toFixed(2)}`),
        );
      }
    }
    // F1 (2026-11-28 GUI test): naked short is accepted by the Alpaca margin
    // account but hard-rejected at fill time by the virtual ledger — warn,
    // don't block.
    if (side === 'sell' && baseBalance) {
      const held = parseFloat(baseBalance.available);
      const q = parseFloat(quantity);
      if (Number.isFinite(q) && q > held + 1e-9) {
        toast.warning(
          (t.toast.shortSellWarning || '卖出数量超过持仓：超出部分将做空（Alpaca 保证金账户支持，虚拟账本成交时会拒绝）'),
        );
      }
    }
    // OCO / 追踪止损是本地撮合特性，外币市场由后端虚拟引擎记账，不支持。
    if (isForeignMarket && (orderCategory === 'oco' || type === 'trailing_stop')) {
      toast.warning(t.orderEntry.unsupportedOnMarket || `Not supported on ${market}`);
      return;
    }
    // 涨跌停校验：A 股超价直接被交易所拒单，前端先拦住。
    const pricedForBand = type === 'limit' ? price : (limitPrice || '');
    if (priceOutOfBand(pricedForBand)) {
      toast.warning(
        t.orderEntry.priceBandError ||
          `Price outside ${priceLimitPct}% band (${priceBand!.low.toFixed(priceDecimals)}–${priceBand!.high.toFixed(priceDecimals)})`,
      );
      return;
    }
    // 港交所/东证/上交所只接受其最小跳动倍数。
    if (isForeignMarket && pricedForBand) {
      const p = parseFloat(pricedForBand);
      if (p > 0) {
        const tick = tickFor(p);
        if (Math.abs(roundToTick(p, tick) - p) > 1e-9) {
          setPrice(roundToTick(p, tick).toFixed(priceDecimals));
          setLimitPrice(roundToTick(p, tick).toFixed(priceDecimals));
          toast.warning(t.orderEntry.tickError || `Price snapped to the ${tick} tick`);
          return;
        }
      }
    }

    let order = null;

    // Handle different order categories
    if (orderCategory === 'spot') {
      // Regular limit/market orders
      const limitNum = price ? parseFloat(price) : NaN;
      if (type === 'limit' && (!price || !Number.isFinite(limitNum) || limitNum <= 0 || limitNum > 1e9)) { 
        toast.warning(t.orderEntry.invalidPrice); 
        return; 
      }

      order = createOrder({
        symbol, side, type,
        price: type === 'limit' ? price : undefined,
        quantity,
        takeProfitPrice: takeProfitPrice || undefined,
        stopLossPrice: stopLossPrice || undefined,
      }, metrics?.mid);
      
    } else if (orderCategory === 'conditional') {
      // Stop-limit, take-profit-limit, stop-market, take-profit-market, trailing stop
      if (type === 'trailing_stop') {
        if (!trailingValue || parseFloat(trailingValue) <= 0) {
          toast.warning(t.orderEntry?.invalidTrailingValue || 'Invalid trailing value');
          return;
        }
        order = createTrailingStopOrder({
          symbol, side, quantity,
          trailingType,
          trailingValue,
          activationPrice: trailingActivationPrice || undefined,
        });
      } else {
        // Stop-limit or take-profit-limit
        if (!triggerPrice || parseFloat(triggerPrice) <= 0) {
          toast.warning(t.orderEntry?.invalidTriggerPrice || 'Invalid trigger price');
          return;
        }
        if (['stop_limit', 'take_profit_limit'].includes(type) && (!limitPrice || parseFloat(limitPrice) <= 0)) {
          toast.warning(t.orderEntry?.invalidLimitPrice || 'Invalid limit price');
          return;
        }

        order = createOrder({
          symbol, side, type,
          triggerPrice,
          price: ['stop_limit', 'take_profit_limit'].includes(type) ? limitPrice : undefined,
          quantity,
        }, metrics?.mid);
      }
      
    } else if (orderCategory === 'oco') {
      // OCO order — finite/range guards must match the spot path. parseFloat
      // of "1e999" is Infinity and `Infinity <= 0` is false, so the old
      // bare `<= 0` check let Infinity through to createOCOOrder, which
      // toasted success before JSON.stringify turned it into null (backend
      // 422) — the exact failure spot orders were hardened against
      // (2026-11-23 second review P2-B).
      const ocoLimit = parseFloat(ocoLimitPrice);
      if (!ocoLimitPrice || !Number.isFinite(ocoLimit) || ocoLimit <= 0 || ocoLimit > 1e9) {
        toast.warning(t.orderEntry?.invalidLimitPrice || 'Invalid limit price');
        return;
      }
      const ocoStop = parseFloat(ocoStopPrice);
      if (!ocoStopPrice || !Number.isFinite(ocoStop) || ocoStop <= 0 || ocoStop > 1e9) {
        toast.warning(t.orderEntry?.invalidStopPrice || 'Invalid stop price');
        return;
      }
      const ocoStopLimit = parseFloat(ocoStopLimitPrice);
      if (!ocoStopLimitPrice || !Number.isFinite(ocoStopLimit) || ocoStopLimit <= 0 || ocoStopLimit > 1e9) {
        toast.warning(t.orderEntry?.invalidStopLimitPrice || 'Invalid stop limit price');
        return;
      }

      order = createOCOOrder({
        symbol, side, quantity,
        limitPrice: ocoLimitPrice,
        stopPrice: ocoStopPrice,
        stopLimitPrice: ocoStopLimitPrice,
      });
    }

    if (order && !('blocked' in order)) {
      // 收盘时段：市价单已被前面拦下，能提交的只有挂单型订单。这类订单由
      // 券商在下次开盘后撮合，所以提示里必须写明"何时生效"，否则用户会把
      // "已提交"误读成"已即时成交"。
      const restingNow = marketClosed && orderCategory === 'spot' && type === 'limit';
      if (restingNow) {
        const nextOpen = formatClockMoment(marketClock?.next_open, marketClock?.timezone);
        toast.warning(
          nextOpen
            ? (t.toast.orderQueuedForOpen || '订单已提交，将在下次开盘后生效：{time}').replace('{time}', nextOpen)
            : (t.toast.orderQueued || '订单已提交，将在下次开盘后生效'),
          6000,
        );
      } else {
        // F5 (2026-11-28 GUI test): key the optimistic success toast to the
        // order's clientOrderId so tradingStore's reject handler can withdraw
        // it (notification.removeByKey) — the legacy Date.now() key made the
        // success toast unretractable and left success+reject pairs on screen.
        // OCO carries a group id instead of a per-leg clientOrderId.
        const submitKey: ToastKey = 'ocoGroupId' in order && order.ocoGroupId
          ? `oco:${order.ocoGroupId}:created`
          : `order:submit:${(order as { clientOrderId: string }).clientOrderId}`;
        notification.success(submitKey, t.toast.orderSubmitted);
      }
      // Reset form
      setQuantity(''); 
      setQuantityPercent(0); 
      setTakeProfitPrice(''); 
      setStopLossPrice('');
      setTriggerPrice('');
      setLimitPrice('');
      setOcoLimitPrice('');
      setOcoStopPrice('');
      setOcoStopLimitPrice('');
      setTrailingValue('');
      setTrailingActivationPrice('');
      if (type === 'limit') setPrice('');
      setShowDegradedConfirm(false);
      setFocusMode(false);
    } else if (order && 'blocked' in order) {
      // The store refused to build the order. Name the real cause — the old
      // catch-all "insufficient balance" sent users chasing the wrong
      // problem during a routine feed resync (2026-11-16 review).
      // (2026-11-23 review P3) createOCOOrder 也返回结构化拒绝原因了，新增
      // invalid_params / markets_loading 两个映射，不再落进 insufficientBalance。
      const blockToast: Record<string, string | undefined> = {
        data_confidence: t.orderEntry.resyncingBlock,
        invalid_quantity: t.orderEntry.invalidAmount,
        invalid_params: t.orderEntry.invalidPrice,
        missing_price: t.orderEntry.invalidPrice,
        missing_trigger: t.orderEntry?.invalidTriggerPrice,
        lot_floor_zero: t.orderEntry.lotError,
        markets_loading: t.orderEntry.marketsLoading,
      };
      toast.warning(
        blockToast[order.blocked]
          || t.orderEntry.invalidAmount
          || 'Order could not be created',
      );
    } else {
      toast.error(t.orderEntry.insufficientBalance);
    }
  };
  
  // Validation for submit button
  const getIsSubmitDisabled = () => {
    if (dataConfidence.level === 'stale') return true;
    // 收盘时市价单无法成交，保持禁用直到开盘
    if (dataConfidence.level === 'closed' && type === 'market' && orderCategory === 'spot') return true;
    if (marketClosed && type === 'market' && orderCategory === 'spot') return true;
    if (!quantity || parseFloat(quantity) <= 0) return true;
    if (quantityNotLot(quantity)) return true;
    if (priceOutOfBand(type === 'limit' ? price : (orderCategory === 'conditional' ? limitPrice : ''))) return true;
    
    if (orderCategory === 'spot') {
      if (type === 'limit' && (!price || parseFloat(price) <= 0)) return true;
    } else if (orderCategory === 'conditional') {
      if (type === 'trailing_stop') {
        if (!trailingValue || parseFloat(trailingValue) <= 0) return true;
      } else {
        if (!triggerPrice || parseFloat(triggerPrice) <= 0) return true;
        if (['stop_limit', 'take_profit_limit'].includes(type) && (!limitPrice || parseFloat(limitPrice) <= 0)) return true;
      }
    } else if (orderCategory === 'oco') {
      if (!ocoLimitPrice || !ocoStopPrice || !ocoStopLimitPrice) return true;
    }
    
    return false;
  };
  
  const isSubmitDisabled = getIsSubmitDisabled();
  const estimatedPriceValue = type === 'market' && metrics ? metrics.mid : (type === 'limit' ? price : (triggerPrice || '—'));
  // 收盘时盘口是冻结快照，滑点预估没有意义
  const slippageEst = dataConfidence.level === 'closed'
    ? '—'
    : (slippageEstRaw && slippageEstRaw !== 'N/A' ? `${slippageEstRaw}bp` : '—');
  const feeValue = total !== '0' ? (parseFloat(total) * 0.001).toFixed(2) : '0';

  // Get order type label
  const getOrderTypeLabel = (t: OrderType): string => {
    const labels: Record<OrderType, string> = {
      limit: 'Limit',
      market: 'Market',
      stop_limit: 'Stop-Limit',
      take_profit_limit: 'TP-Limit',
      stop_market: 'Stop-Market',
      take_profit_market: 'TP-Market',
      trailing_stop: 'Trailing',
    };
    return labels[t] || t;
  };

  return (
    <div className={`card ${styles.container} ${focusMode ? styles.focused : ''} animate-fade`}>
      <div className="card-header">
        <span className="card-title">{t.orderEntry.title}</span>
        <span className={styles.marketBadge} title={marketDef ? `${marketDef.name} · ${marketDef.exchange} · ${marketDef.settlement}` : undefined}>
          {market} · {quoteAsset}
          {marketClock && (
            <span className={marketClock.is_open ? styles.sessionOpen : styles.sessionClosed}>
              {marketClock.is_open ? (marketClock.session === 'break' ? 'BREAK' : 'OPEN') : 'CLOSED'}
            </span>
          )}
        </span>
        {focusMode && <span className={styles.focusBadge}>Focus</span>}
      </div>
      
      <form onSubmit={handleSubmit} className={styles.form}>
        {/* Order Category Tabs */}
        <div className={styles.categoryTabs}>
          <button 
            type="button" 
            className={`${styles.categoryTab} ${orderCategory === 'spot' ? styles.active : ''}`}
            onClick={() => setOrderCategory('spot')}
          >
            Spot
          </button>
          <button 
            type="button" 
            className={`${styles.categoryTab} ${orderCategory === 'conditional' ? styles.active : ''}`}
            onClick={() => setOrderCategory('conditional')}
          >
            Conditional
          </button>
          <button 
            type="button" 
            className={`${styles.categoryTab} ${orderCategory === 'oco' ? styles.active : ''}`}
            onClick={() => setOrderCategory('oco')}
            disabled={isForeignMarket}
            title={isForeignMarket ? (t.orderEntry.unsupportedOnMarket || `Not supported on ${market}`) : undefined}
          >
            OCO
          </button>
        </div>

        {/* Combined Side & Type Toggle Row */}
        <div className={styles.headerRow}>
          <div className={styles.sideToggle}>
            <button type="button" className={`${styles.sideBtn} ${styles.buyBtn} ${side === 'buy' ? styles.active : ''}`} onClick={() => setSide('buy')}>{t.orderEntry.buy}</button>
            <button type="button" className={`${styles.sideBtn} ${styles.sellBtn} ${side === 'sell' ? styles.active : ''}`} onClick={() => setSide('sell')}>{t.orderEntry.sell}</button>
          </div>
          
          {/* Type toggle based on category */}
          {orderCategory === 'spot' && (
            <div className={styles.typeToggle}>
              <button type="button" className={`${styles.typeBtn} ${type === 'limit' ? styles.active : ''}`} onClick={() => setType('limit')}>{t.orderEntry.limit}</button>
              <button type="button" className={`${styles.typeBtn} ${type === 'market' ? styles.active : ''}`} onClick={() => setType('market')}>{t.orderEntry.market}</button>
            </div>
          )}
          
          {orderCategory === 'conditional' && (
            <div className={styles.typeToggle}>
              <button type="button" className={`${styles.typeBtn} ${type === 'stop_limit' ? styles.active : ''}`} onClick={() => setType('stop_limit')}>Stop</button>
              <button type="button" className={`${styles.typeBtn} ${type === 'take_profit_limit' ? styles.active : ''}`} onClick={() => setType('take_profit_limit')}>TP</button>
              {!isForeignMarket && (
                <button type="button" className={`${styles.typeBtn} ${type === 'trailing_stop' ? styles.active : ''}`} onClick={() => setType('trailing_stop')}>Trail</button>
              )}
            </div>
          )}
        </div>

        {/* === SPOT ORDER INPUTS === */}
        {orderCategory === 'spot' && (
          <>
            {/* Price Input (Limit only) */}
            {type === 'limit' && (
              <div className={styles.inputGroup}>
                <label className={styles.label}>{t.orderEntry.price}</label>
                <div className={styles.inputWrapper}>
                  <button type="button" className={styles.stepBtn} onClick={handleStepDown}><Icon name="minus" size="xs" /></button>
                  <input ref={priceInputRef} type="text" inputMode="decimal" className={`input ${styles.input}`} value={price} onChange={(e) => setPrice(e.target.value)} onFocus={handleInputFocus('price')} onBlur={handleInputBlur} placeholder="0.00" />
                  <button type="button" className={styles.stepBtn} onClick={handleStepUp}><Icon name="plus" size="xs" /></button>
                  <span className={styles.inputSuffix}>{quoteAsset}</span>
                </div>
                <div className={styles.quickFillButtons}>
                  <button type="button" className={styles.quickFillBtn} onClick={setFromBestBid}>{t.orderEntry.bid1}</button>
                  <button type="button" className={styles.quickFillBtn} onClick={setFromMid}>{t.orderEntry.mid}</button>
                  <button type="button" className={styles.quickFillBtn} onClick={setFromBestAsk}>{t.orderEntry.ask1}</button>
                </div>
                {priceBand && (
                  <div className={styles.bandHint}>
                    {t.orderEntry.priceBand || 'Limit band'}: {priceBand.low.toFixed(priceDecimals)} – {priceBand.high.toFixed(priceDecimals)}
                  </div>
                )}
              </div>
            )}
          </>
        )}

        {/* === CONDITIONAL ORDER INPUTS === */}
        {orderCategory === 'conditional' && (
          <>
            {/* 2026-11-16: 条件单已镜像到券商 7×24 监控（GTC 长期有效）。
                TP 限价单在限价位挂起生效——比旧的"触发即市价"更早触达、
                且成交价不会差于限价，与旧本地触发的语义差异在此说明。 */}
            <div className={styles.sessionHint || ''} style={{ fontSize: 11, color: 'var(--text-tertiary, #8a8a8a)', margin: '4px 0 8px' }}>
              {type === 'trailing_stop'
                ? (isForeignMarket || trailingActivationPrice
                    ? (t.orderEntry?.localMirrorHint || '跟踪止损在本机评估执行')
                    : (t.orderEntry?.venueMirrorHint || '已同步至券商跟踪委托，7×24 监控（GTC）'))
                : (t.orderEntry?.venueMirrorHint || '已同步至券商委托，7×24 监控（GTC 长期有效）')}
            </div>
            {type === 'trailing_stop' ? (
              <>
                {/* Trailing Stop Inputs */}
                <div className={styles.inputGroup}>
                  <label className={styles.label}>Trailing Type</label>
                  <div className={styles.typeToggle} style={{ marginBottom: '8px' }}>
                    <button type="button" className={`${styles.typeBtn} ${trailingType === 'percent' ? styles.active : ''}`} onClick={() => setTrailingType('percent')}>%</button>
                    <button type="button" className={`${styles.typeBtn} ${trailingType === 'absolute' ? styles.active : ''}`} onClick={() => setTrailingType('absolute')}>$</button>
                  </div>
                </div>
                <div className={styles.inputGroup}>
                  <label className={styles.label}>Callback {trailingType === 'percent' ? '(%)' : '(USD)'}</label>
                  <div className={styles.inputWrapper}>
                    <input type="text" inputMode="decimal" className={`input ${styles.input}`} value={trailingValue} onChange={(e) => setTrailingValue(e.target.value)} onFocus={handleInputFocus('trigger')} onBlur={handleInputBlur} placeholder={trailingType === 'percent' ? '1.0' : '100'} />
                    <span className={styles.inputSuffix}>{trailingType === 'percent' ? '%' : quoteAsset}</span>
                  </div>
                </div>
                <div className={styles.inputGroup}>
                  <label className={styles.label}>Activation Price (Optional)</label>
                  <div className={styles.inputWrapper}>
                    <input type="text" inputMode="decimal" className={`input ${styles.input}`} value={trailingActivationPrice} onChange={(e) => setTrailingActivationPrice(e.target.value)} onFocus={handleInputFocus('trigger')} onBlur={handleInputBlur} placeholder="—" />
                    <span className={styles.inputSuffix}>{quoteAsset}</span>
                  </div>
                </div>
              </>
            ) : (
              <>
                {/* Stop-Limit / Take-Profit-Limit Inputs */}
                <div className={styles.inputGroup}>
                  <label className={styles.label}>{type === 'stop_limit' ? 'Stop Price' : 'TP Price'}</label>
                  <div className={styles.inputWrapper}>
                    <input type="text" inputMode="decimal" className={`input ${styles.input}`} value={triggerPrice} onChange={(e) => setTriggerPrice(e.target.value)} onFocus={handleInputFocus('trigger')} onBlur={handleInputBlur} placeholder="Trigger at..." />
                    <span className={styles.inputSuffix}>{quoteAsset}</span>
                  </div>
                  <div className={styles.quickFillButtons}>
                    <button type="button" className={styles.quickFillBtn} onClick={() => metrics && setTriggerPrice(metrics.mid)}>Mid</button>
                    <button type="button" className={styles.quickFillBtn} onClick={() => bestBid && setTriggerPrice(bestBid.price)}>Bid</button>
                    <button type="button" className={styles.quickFillBtn} onClick={() => bestAsk && setTriggerPrice(bestAsk.price)}>Ask</button>
                  </div>
                </div>
                <div className={styles.inputGroup}>
                  <label className={styles.label}>Limit Price</label>
                  <div className={styles.inputWrapper}>
                    <input type="text" inputMode="decimal" className={`input ${styles.input}`} value={limitPrice} onChange={(e) => setLimitPrice(e.target.value)} onFocus={handleInputFocus('limit')} onBlur={handleInputBlur} placeholder="Execute at..." />
                    <span className={styles.inputSuffix}>{quoteAsset}</span>
                  </div>
                </div>
              </>
            )}
          </>
        )}

        {/* === OCO ORDER INPUTS === */}
        {orderCategory === 'oco' && (
          <>
            <div className={styles.ocoSection}>
              <div className={styles.ocoLabel}>
                <Icon name="git-branch" size="xs" />
                <span>Limit Order</span>
              </div>
              <div className={styles.inputGroup}>
                <label className={styles.label}>Limit Price</label>
                <div className={styles.inputWrapper}>
                  <input type="text" inputMode="decimal" className={`input ${styles.input}`} value={ocoLimitPrice} onChange={(e) => setOcoLimitPrice(e.target.value)} onFocus={handleInputFocus('limit')} onBlur={handleInputBlur} placeholder="0.00" />
                  <span className={styles.inputSuffix}>{quoteAsset}</span>
                </div>
              </div>
            </div>
            
            <div className={styles.ocoSection}>
              <div className={styles.ocoLabel}>
                <Icon name="shield" size="xs" />
                <span>Stop-Limit Order</span>
              </div>
              <div className={styles.inputGroup}>
                <label className={styles.label}>Stop Price</label>
                <div className={styles.inputWrapper}>
                  <input type="text" inputMode="decimal" className={`input ${styles.input}`} value={ocoStopPrice} onChange={(e) => setOcoStopPrice(e.target.value)} onFocus={handleInputFocus('trigger')} onBlur={handleInputBlur} placeholder="Trigger at..." />
                  <span className={styles.inputSuffix}>{quoteAsset}</span>
                </div>
              </div>
              <div className={styles.inputGroup}>
                <label className={styles.label}>Stop Limit Price</label>
                <div className={styles.inputWrapper}>
                  <input type="text" inputMode="decimal" className={`input ${styles.input}`} value={ocoStopLimitPrice} onChange={(e) => setOcoStopLimitPrice(e.target.value)} onFocus={handleInputFocus('limit')} onBlur={handleInputBlur} placeholder="Execute at..." />
                  <span className={styles.inputSuffix}>{quoteAsset}</span>
                </div>
              </div>
            </div>
          </>
        )}

        {/* Amount Input */}
        <div className={styles.inputGroup}>
          <label className={styles.label}>
            {t.orderEntry.amount}
            {lotSize > 1 && <span className={styles.lotHint}> · {lotSize} {t.orderEntry.perLot || 'per lot'}</span>}
          </label>
          <div className={styles.inputWrapper}>
            <button type="button" className={styles.stepBtn} onClick={() => setQuantity((q) => String(Math.max(0, (parseFloat(q) || 0) - lotSize)))}><Icon name="minus" size="xs" /></button>
            <input ref={quantityInputRef} type="text" inputMode="decimal" className={`input ${styles.input}`} value={quantity} onChange={(e) => setQuantity(e.target.value)} onFocus={handleInputFocus('quantity')} onBlur={handleInputBlur} placeholder={lotSize > 1 ? String(lotSize) : '0.00'} style={{ paddingRight: '48px' }} />
            <button type="button" className={styles.stepBtn} onClick={() => setQuantity((q) => String((parseFloat(q) || 0) + lotSize))}><Icon name="plus" size="xs" /></button>
            <span className={styles.inputSuffix} style={{ right: '8px' }}>{baseAsset}</span>
          </div>
          {quantityNotLot(quantity) && (
            <div className={styles.inputError}>
              {t.orderEntry.lotError || `Quantity must be a multiple of ${lotSize}`}
            </div>
          )}
          <div className={styles.sliderRow}>
            <div className={styles.sliderContainer}>
              <QuantitySlider value={quantityPercent} onChange={(p) => { setQuantityPercent(p); updateQuantityFromPercent(p); }} estimatedQty={quantity || '0'} />
            </div>
            <div className={styles.percentButtons}>
              {[25, 50, 75, 100].map((pct) => (
                <button key={pct} type="button" className={styles.percentBtn} onClick={() => updateQuantityFromPercent(pct)}>{pct}%</button>
              ))}
            </div>
          </div>
        </div>

        {/* TP/SL Compact */}
        <div className={styles.tpslContainer}>
          <div className={styles.inputGroupSmall}>
            <label className={styles.labelSmall}>{t.orderEntry.takeProfit}</label>
            <input ref={tpInputRef} type="text" inputMode="decimal" className={`input ${styles.inputSmall}`} value={takeProfitPrice} onChange={(e) => setTakeProfitPrice(e.target.value)} onFocus={handleInputFocus('tp')} onBlur={handleInputBlur} placeholder="—" />
          </div>
          <div className={styles.inputGroupSmall}>
            <label className={styles.labelSmall}>{t.orderEntry.stopLoss}</label>
            <input ref={slInputRef} type="text" inputMode="decimal" className={`input ${styles.inputSmall}`} value={stopLossPrice} onChange={(e) => setStopLossPrice(e.target.value)} onFocus={handleInputFocus('sl')} onBlur={handleInputBlur} placeholder="—" />
          </div>
        </div>
        {Boolean(takeProfitPrice || stopLossPrice) && (
          <p className={styles.tpslHint}>
            {t.orderEntry.tpslArmsOnFill || 'TP/SL activates automatically once the order fills'}
          </p>
        )}

        {/* Compact Estimated Info */}
        <div className={styles.estimatedInfo}>
          <div className={styles.estimatedRow}>
            <span className={styles.estimatedLabel}>{t.orderEntry.estimatedPrice}</span>
            <span className={`${styles.estimatedValue} tabular-nums`}>{estimatedPriceValue || '—'}</span>
          </div>
          <div className={styles.estimatedRow}>
            <span className={styles.estimatedLabel}>{t.orderEntry.slippage}</span>
            <span className={`${styles.estimatedValue} tabular-nums`}>{slippageEst}</span>
          </div>
          <div className={styles.estimatedRow}>
            <span className={styles.estimatedLabel}>{t.orderEntry.fee}</span>
            <span className={`${styles.estimatedValue} tabular-nums`}>{currencySymbol(quoteAsset)}{feeValue}</span>
          </div>
        </div>

        {/* Total & Available */}
        <div className={styles.totalRow}>
          <span className={styles.totalLabel}>{t.orderEntry.total}</span>
          <span className={`${styles.totalValue} tabular-nums`}>{currencySymbol(quoteAsset)}{total}</span>
        </div>

        <div className={styles.balanceRow}>
          <span className={styles.balanceLabel}>{t.orderEntry.available}</span>
          <span className={`${styles.balanceValue} tabular-nums`}>
            {side === 'buy'
              ? isForeignMarket
                ? (() => {
                    const rate = useMarketsStore.getState().usdPerUnit(quoteAsset);
                    const native = rate > 0 ? buyingPower / rate : 0;
                    return `≈${currencySymbol(quoteAsset)}${native.toFixed(priceDecimals)}`;
                  })()
                : `${buyingPower.toFixed(2)} USD`
              : `${parseFloat(baseBalance?.available ?? '0').toFixed(6)} ${baseAsset}`}
          </span>
        </div>
        {/* F2: venue purchasing power — margin accounts can spend beyond cash. */}
        {side === 'buy' && buyingPower > parseFloat(quoteBalance?.available || '0') && (
          <div className={styles.balanceRow}>
            <span className={styles.balanceLabel}>{t.orderEntry.buyingPower || '购买力'}</span>
            <span className={`${styles.balanceValue} tabular-nums`}>
              {isForeignMarket
                ? (() => {
                    const rate = useMarketsStore.getState().usdPerUnit(quoteAsset);
                    const native = rate > 0 ? buyingPower / rate : 0;
                    return `≈${currencySymbol(quoteAsset)}${native.toFixed(priceDecimals)}`;
                  })()
                : `${currencySymbol(quoteAsset)}${buyingPower.toFixed(2)}`}
            </span>
          </div>
        )}

        {/* Data Confidence Warning */}
        {dataConfidence.level !== 'live' && (
          <div className={`${styles.confidenceWarning} ${styles[dataConfidence.level]}`}>
            <div className={styles.warningBar} />
            <Icon name="alert-triangle" size="xs" />
            <span className={styles.warningText}>{dataConfidence.reason}</span>
          </div>
        )}

        {/* Degraded Confirm */}
        {showDegradedConfirm && (
          <div className={styles.degradedConfirm}>
            <p className={styles.confirmText}>{t.orderEntry.confirmDegraded}</p>
            <div className={styles.confirmActions}>
              <button type="button" className={styles.confirmBtn} onClick={() => { setShowDegradedConfirm(false); handleSubmit({ preventDefault: () => {} } as React.FormEvent); }}>{t.common.confirm}</button>
              <button type="button" className={styles.cancelBtn} onClick={() => setShowDegradedConfirm(false)}>{t.common.cancel}</button>
            </div>
          </div>
        )}

        {/* Submit Button */}
        <button type="submit" className={`btn ${styles.submitBtn} ${side === 'buy' ? styles.buySubmit : styles.sellSubmit}`} disabled={isSubmitDisabled}>
          {side === 'buy' ? formatMessage(t.orderEntry.placeBuyOrder, { symbol: baseAsset }) : formatMessage(t.orderEntry.placeSellOrder, { symbol: baseAsset })}
        </button>

        {/* Quick Actions */}
        <div className={styles.quickActions}>
          <button type="button" className={`${styles.quickBtn} ${side === 'buy' ? styles.allInBuyBtn : styles.allInSellBtn}`} onClick={() => setShowAllInConfirm(true)} disabled={!metrics}>
            {side === 'buy' ? (t.orderEntry.allInBuy || 'All-In Buy') : (t.orderEntry.allInSell || 'Sell All')}
          </button>
        </div>
      </form>

      <ConfirmModal
        isOpen={showAllInConfirm}
        title={side === 'buy' ? (t.orderEntry.confirmAllInBuyTitle || 'Confirm All-In') : (t.orderEntry.confirmAllInSellTitle || 'Confirm Sell All')}
        message={side === 'buy' ? (t.orderEntry.confirmAllInBuyMessage || `Use all available ${quoteAsset} to buy ${baseAsset}`) : (t.orderEntry.confirmAllInSellMessage || `Sell all holdings of ${baseAsset}`)}
        detail={side === 'buy' ? `${parseFloat(quoteBalance?.available ?? '0').toFixed(2)} ${quoteAsset} → ${baseAsset}` : `${parseFloat(baseBalance?.available ?? '0').toFixed(6)} ${baseAsset} → ${quoteAsset}`}
        confirmText={side === 'buy' ? (t.orderEntry.confirmAllInBuy || 'Confirm Buy') : (t.orderEntry.confirmAllInSell || 'Confirm Sell')}
        cancelText={t.common.cancel}
        onConfirm={() => { setShowAllInConfirm(false); updateQuantityFromPercent(100); if (type === 'limit' && !price && metrics) setPrice(side === 'buy' ? bestAsk?.price || metrics.mid : bestBid?.price || metrics.mid); }}
        onCancel={() => setShowAllInConfirm(false)}
        type={side}
      />
    </div>
  );
}
