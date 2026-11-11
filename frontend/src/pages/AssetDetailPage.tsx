import { useMemo, useState, useCallback, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  useWalletStore, selectAccount,
} from '../store/walletStore';
import { useTradingStore } from '../store/tradingStore';
import { useWatchlistStore } from '../store/watchlistStore';
import { useAutomationStore } from '../store/automationStore';
import { useMarketsStore } from '../store/marketsStore';
import { useMarketStore, selectMarketClock } from '../store/marketStore';
import { useI18n } from '../i18n';
import { Icon, IconName } from '../components/Icon';
import { Sparkline } from '../components/Chart/Sparkline';
import { formatMoney } from '../utils/money';
import { apiFetch } from '../services/apiClient';
import type { Position } from '../types/trading';
import styles from './AssetDetailPage.module.css';
import Decimal from 'decimal.js';

type TimeRange = '1D' | '7D' | '30D' | 'ALL';
type SortField = 'value' | 'balance' | 'pnl' | 'pnlPercent';
type SortOrder = 'asc' | 'desc';

/** Venue equity curve (Alpaca portfolio history), intraday for 1D, daily closes otherwise. */
type EquityHistory = { points: { time: number; equity: number }[] };

// The venue only maps day-granular periods for longer ranges; "1D" rides the
// intraday curve (Alpaca spells it "5Min", lowercase "5m" is rejected).
const HISTORY_PARAMS: Record<TimeRange, string> = {
  '1D': 'period=1D&timeframe=5Min',
  '7D': 'period=1W&timeframe=1D',
  '30D': 'period=1M&timeframe=1D',
  'ALL': 'period=ALL&timeframe=1D',
};

// 格式化数字
function formatNumber(value: number, decimals = 2): string {
  if (value >= 1000000) return `$${(value / 1000000).toFixed(2)}M`;
  if (value >= 1000) return `$${(value / 1000).toFixed(2)}K`;
  return `$${value.toFixed(decimals)}`;
}

function formatPercent(value: number): string {
  const prefix = value >= 0 ? '+' : '';
  return `${prefix}${value.toFixed(2)}%`;
}

/** 账户汇总一律以 USD 计（券商 base_currency）。 */
function usd(value: number): string {
  return formatMoney(value, 'USD');
}

/** 盈亏带正符号，便于扫读。 */
function usdSigned(value: number): string {
  return formatMoney(value, 'USD', { signed: true });
}

/**
 * 从真实数据点时间戳生成 4 个横轴刻度：首、两个内插点、尾。
 * Alpaca 时间戳锚定美股交易日，标签固定按 America/New_York 渲染——
 * 否则东八区浏览器会把日内曲线标成 "22:30–05:00"。日线显示 "Nov 11"，
 * 跨年补年份；日内显示 "HH:MM"。纯函数便于测试。
 */
export function chartTimeLabels(times: number[]): string[] {
  const n = times.length;
  if (n < 2) return [];
  const indices = [0, Math.round((n - 1) / 3), Math.round((2 * (n - 1)) / 3), n - 1];
  const span = times[n - 1] - times[0];
  const intraday = span < 24 * 60 * 60;
  const ET = 'America/New_York';
  const fmtDay = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', timeZone: ET });
  const fmtYear = new Intl.DateTimeFormat('en-US', { year: 'numeric', timeZone: ET });
  const fmtTime = new Intl.DateTimeFormat('en-US', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: ET });
  return indices.map((idx) => {
    const d = new Date(times[idx] * 1000);
    const label = intraday ? fmtTime.format(d) : fmtDay.format(d);
    // 年跨度的首尾刻度补年份，消除跨年误读。
    return !intraday && span > 365 * 24 * 60 * 60
      ? `${label} ${fmtYear.format(d)}`
      : label;
  });
}

// 增强型投资组合图表组件
function PortfolioChart({
  data,
  times,
  color,
  height = 100,
  timeRange,
  fmt
}: {
  data: number[];
  times?: number[];
  color: string;
  height?: number;
  timeRange: TimeRange;
  fmt: (v: number) => string;
}) {
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);
  
  if (data.length < 2) return null;
  
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const padding = { top: 8, right: 60, bottom: 24, left: 8 };
  const chartWidth = 560;
  // 使用一个合理的默认高度，SVG 会通过 CSS 自适应容器
  const chartHeight = height;
  const graphWidth = chartWidth - padding.left - padding.right;
  const graphHeight = chartHeight - padding.top - padding.bottom;
  
  // 计算点坐标
  const points = data.map((v, i) => {
    const x = padding.left + (i / (data.length - 1)) * graphWidth;
    const y = padding.top + graphHeight - ((v - min) / range) * graphHeight;
    return { x, y, value: v };
  });
  
  const pathPoints = points.map(p => `${p.x},${p.y}`).join(' ');
  const areaPoints = `${padding.left},${padding.top + graphHeight} ${pathPoints} ${padding.left + graphWidth},${padding.top + graphHeight}`;
  
  // 生成Y轴刻度
  const yTicks = [min, (min + max) / 2, max];
  
  // 生成X轴时间标签：优先用真实数据点的时间戳（与 hover 数值一一对应）；
  // 没有时间戳才退回按当前时间推算的近似刻度。
  const getTimeLabels = () => {
    if (times && times.length === data.length && times.length >= 2) {
      return chartTimeLabels(times);
    }
    const now = new Date();
    switch (timeRange) {
      case '1D':
        return ['00:00', '06:00', '12:00', '18:00', 'Now'];
      case '7D':
        return Array.from({ length: 5 }, (_, i) => {
          const d = new Date(now.getTime() - (4 - i) * 24 * 60 * 60 * 1000 * 1.75);
          return d.toLocaleDateString('en-US', { weekday: 'short' });
        });
      case '30D':
        return Array.from({ length: 5 }, (_, i) => {
          const d = new Date(now.getTime() - (4 - i) * 24 * 60 * 60 * 1000 * 7.5);
          return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
        });
      case 'ALL':
        return ['Start', '', '', '', 'Now'];
      default:
        return [];
    }
  };
  
  const timeLabels = getTimeLabels();
  const startValue = data.length > 0 ? (data[0] || 0) : 0;
  const endValue = data.length > 0 ? (data[data.length - 1] || 0) : 0;
  const changeValue = endValue - startValue;
  const changePercent = startValue !== 0 ? ((changeValue / startValue) * 100).toFixed(2) : '0.00';
  
  // 格式化数值（币种由调用方传入，美元以外的账户不能套 "$"）
  const formatValue = fmt;
  
  const hoveredPoint = hoveredIndex !== null ? points[hoveredIndex] : null;
  
  return (
    <div className={styles.portfolioChart}>
      <svg 
        width="100%" 
        height="100%"
        viewBox={`0 0 ${chartWidth} ${chartHeight}`}
        preserveAspectRatio="xMidYMid meet"
        className={styles.chartSvg}
        onMouseLeave={() => setHoveredIndex(null)}
      >
        <defs>
          <linearGradient id="portfolio-gradient" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" stopColor={color} stopOpacity="0.25" />
            <stop offset="50%" stopColor={color} stopOpacity="0.1" />
            <stop offset="100%" stopColor={color} stopOpacity="0.02" />
          </linearGradient>
          <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="2" result="coloredBlur"/>
            <feMerge>
              <feMergeNode in="coloredBlur"/>
              <feMergeNode in="SourceGraphic"/>
            </feMerge>
          </filter>
        </defs>
        
        {/* 网格线 */}
        <g className={styles.gridLines}>
          {[0, 0.25, 0.5, 0.75, 1].map((ratio, i) => (
            <line
              key={i}
              x1={padding.left}
              y1={padding.top + graphHeight * ratio}
              x2={padding.left + graphWidth}
              y2={padding.top + graphHeight * ratio}
              stroke="var(--border-light)"
              strokeWidth="1"
              strokeDasharray={i === 2 ? "none" : "3,3"}
              opacity={i === 2 ? 0.5 : 0.3}
            />
          ))}
        </g>
        
        {/* 填充区域 */}
        <polygon 
          points={areaPoints} 
          fill="url(#portfolio-gradient)" 
        />
        
        {/* 主曲线 */}
        <polyline
          points={pathPoints}
          fill="none"
          stroke={color}
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          filter="url(#glow)"
        />
        
        {/* Y轴标签 */}
        <g className={styles.yAxis}>
          {yTicks.map((tick, i) => {
            const y = padding.top + graphHeight - ((tick - min) / range) * graphHeight;
            return (
              <text
                key={i}
                x={chartWidth - 4}
                y={y}
                textAnchor="end"
                dominantBaseline="middle"
                fill="var(--text-tertiary)"
                fontSize="10"
                fontFamily="var(--font-mono)"
              >
                {formatValue(tick)}
              </text>
            );
          })}
        </g>
        
        {/* X轴标签 */}
        <g className={styles.xAxis}>
          {timeLabels.map((label, i) => {
            const x = padding.left + (i / (timeLabels.length - 1)) * graphWidth;
            return (
              <text
                key={i}
                x={x}
                y={chartHeight - 4}
                textAnchor="middle"
                fill="var(--text-muted)"
                fontSize="9"
                fontFamily="var(--font-mono)"
              >
                {label}
              </text>
            );
          })}
        </g>
        
        {/* 交互层 - 透明矩形用于检测鼠标位置 */}
        {points.map((point, i) => (
          <rect
            key={i}
            x={point.x - graphWidth / (data.length * 2)}
            y={padding.top}
            width={graphWidth / data.length}
            height={graphHeight}
            fill="transparent"
            onMouseEnter={() => setHoveredIndex(i)}
          />
        ))}
        
        {/* 悬停指示器 */}
        {hoveredPoint && (
          <g>
            <line
              x1={hoveredPoint.x}
              y1={padding.top}
              x2={hoveredPoint.x}
              y2={padding.top + graphHeight}
              stroke={color}
              strokeWidth="1"
              strokeDasharray="4,4"
              opacity="0.6"
            />
            <circle
              cx={hoveredPoint.x}
              cy={hoveredPoint.y}
              r="5"
              fill={color}
              stroke="var(--bg-secondary)"
              strokeWidth="2"
            />
          </g>
        )}
        
        {/* 起止点标记 */}
        {points.length > 0 && points[0] && points[points.length - 1] && (
          <>
            <circle
              cx={points[0].x}
              cy={points[0].y}
              r="3"
              fill="var(--text-tertiary)"
            />
            <circle
              cx={points[points.length - 1]!.x}
              cy={points[points.length - 1]!.y}
              r="4"
              fill={color}
              stroke="var(--bg-secondary)"
              strokeWidth="2"
            />
          </>
        )}
      </svg>
      
      {/* 悬停提示框 */}
      {hoveredPoint && hoveredIndex !== null && (
        <div 
          className={styles.chartTooltip}
          style={{
            left: `${(hoveredPoint.x / chartWidth) * 100}%`,
            top: `${((hoveredPoint.y - 30) / chartHeight) * 100}%`,
          }}
        >
          <span className={styles.tooltipValue}>{formatValue(hoveredPoint.value)}</span>
          <span className={styles.tooltipIndex}>
            {timeRange === '1D' ? `${Math.floor((hoveredIndex / data.length) * 24)}:00` :
             timeRange === '7D' ? `Day ${Math.floor((hoveredIndex / data.length) * 7) + 1}` :
             `${Math.floor((hoveredIndex / data.length) * 30) + 1}d`}
          </span>
        </div>
      )}
      
      {/* 底部统计 */}
      <div className={styles.chartStats}>
        <div className={styles.chartStatItem}>
          <span className={styles.chartStatLabel}>Start</span>
          <span className={styles.chartStatValue}>{formatValue(startValue as number)}</span>
        </div>
        <div className={styles.chartStatItem}>
          <span className={styles.chartStatLabel}>Change</span>
          <span className={`${styles.chartStatValue} ${changeValue >= 0 ? styles.positive : styles.negative}`}>
            {changeValue >= 0 ? '+' : ''}{formatValue(changeValue)} ({changeValue >= 0 ? '+' : ''}{changePercent}%)
          </span>
        </div>
        <div className={styles.chartStatItem}>
          <span className={styles.chartStatLabel}>Current</span>
          <span className={styles.chartStatValue}>{formatValue(endValue as number)}</span>
        </div>
      </div>
    </div>
  );
}

// 市场卡片组件
function MarketCard({ 
  symbol, 
  name, 
  price, 
  change, 
  sparklineData,
  onClick 
}: { 
  symbol: string; 
  name: string; 
  price: string; 
  change: number;
  sparklineData: number[];
  onClick: () => void;
}) {
  const isPositive = change >= 0;
  
  return (
    <div className={styles.marketCard} onClick={onClick}>
      <div className={styles.marketHeader}>
        <div className={styles.marketIcon}>{symbol[0]}</div>
        <div className={styles.marketInfo}>
          <span className={styles.marketSymbol}>{symbol}</span>
          <span className={styles.marketName}>{name}</span>
        </div>
      </div>
      <div className={styles.marketBody}>
        <div className={styles.marketPrice}>${price}</div>
        <div className={`${styles.marketChange} ${isPositive ? styles.positive : styles.negative}`}>
          <Icon name={isPositive ? 'trending-up' : 'trending-down'} size="xs" />
          {formatPercent(change)}
        </div>
      </div>
      <div className={styles.marketChart}>
        <Sparkline 
          data={sparklineData} 
          width={100} 
          height={32}
          lineWidth={1.5}
        />
      </div>
    </div>
  );
}

// 统计卡片组件
function StatCard({ 
  label, 
  value, 
  subValue, 
  icon, 
  color,
  onClick 
}: { 
  label: string; 
  value: string | number; 
  subValue?: string;
  icon: IconName;
  color: 'blue' | 'green' | 'orange' | 'purple' | 'red';
  onClick?: () => void;
}) {
  const colorMap = {
    blue: 'var(--color-info)',
    green: 'var(--color-success)',
    orange: 'var(--color-warning)',
    purple: '#8b5cf6',
    red: 'var(--color-error)',
  };
  
  return (
    <div 
      className={`${styles.statCard} ${onClick ? styles.clickable : ''}`}
      onClick={onClick}
      style={{ '--stat-color': colorMap[color] } as React.CSSProperties}
    >
      <div className={styles.statIcon}>
        <Icon name={icon} size="sm" />
      </div>
      <div className={styles.statContent}>
        <span className={styles.statValue}>{value}</span>
        <span className={styles.statLabel}>{label}</span>
        {subValue && <span className={styles.statSub}>{subValue}</span>}
      </div>
    </div>
  );
}

// 活动 Feed 项组件
function ActivityItem({ 
  type, 
  title, 
  description, 
  time, 
  value, 
  isPositive 
}: { 
  type: 'trade' | 'order' | 'trigger' | 'deposit';
  title: string;
  description: string;
  time: string;
  value?: string;
  isPositive?: boolean;
}) {
  const iconMap: Record<'trade' | 'order' | 'trigger' | 'deposit', IconName> = {
    trade: 'repeat',
    order: 'check-circle',
    trigger: 'zap',
    deposit: 'download',
  };
  
  const colorMap = {
    trade: styles.activityTrade,
    order: styles.activityOrder,
    trigger: styles.activityTrigger,
    deposit: styles.activityDeposit,
  };
  
  return (
    <div className={styles.activityItem}>
      <div className={`${styles.activityIcon} ${colorMap[type]}`}>
        <Icon name={iconMap[type]} size="xs" />
      </div>
      <div className={styles.activityContent}>
        <div className={styles.activityHeader}>
          <span className={styles.activityTitle}>{title}</span>
          <span className={styles.activityTime}>{time}</span>
        </div>
        <span className={styles.activityDesc}>{description}</span>
      </div>
      {value && (
        <span className={`${styles.activityValue} ${isPositive === true ? styles.positive : isPositive === false ? styles.negative : ''}`}>
          {value}
        </span>
      )}
    </div>
  );
}

export function AssetDetailPage() {
  const { t } = useI18n();
  const navigate = useNavigate();
  
  // State
  const [timeRange, setTimeRange] = useState<TimeRange>('30D');
  const [equityHistory, setEquityHistory] = useState<EquityHistory | null>(null);
  const [equityLoading, setEquityLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState('');
  const [sortField, setSortField] = useState<SortField>('value');
  const [sortOrder, setSortOrder] = useState<SortOrder>('desc');
  
  // Store data
  const account = useWalletStore(selectAccount);
  const brokerMetrics = useWalletStore((state) => state.brokerMetrics);
  const performanceMetrics = useWalletStore((state) => state.performanceMetrics);
  const positions = useTradingStore((state) => state.positions);
  const orders = useTradingStore((state) => state.orders);
  const triggers = useAutomationStore((state) => state.triggers);
  const executionLogs = useAutomationStore((state) => state.executionLogs);
  const symbols = useWatchlistStore((state) => state.symbols);
  const setSelectedSymbol = useWatchlistStore((state) => state.setSelectedSymbol);
  const fxRates = useMarketsStore((state) => state.fxRates);
  const marketClock = useMarketStore(selectMarketClock);

  // 券商端的净值曲线（Alpaca portfolio history；1D 走日内 5 分钟粒度）。
  // 404 表示账户太新、场内还没有历史——保留 null 让空态文案出现，绝不拿
  // 0 值伪造走势。注意 apiFetch 对 401 是 throw（登录门接管），进的是 catch。
  useEffect(() => {
    let cancelled = false;
    setEquityLoading(true);
    apiFetch(`/portfolio/history?${HISTORY_PARAMS[timeRange]}`)
      .then(async (res) => {
        if (cancelled) return;
        if (!res.ok) {
          setEquityHistory(null);
          return;
        }
        const data = await res.json();
        setEquityHistory({
          points: (data.points || []).map((p: { time: number; equity: number }) => ({
            time: p.time,
            equity: p.equity,
          })),
        });
      })
      .catch(() => { if (!cancelled) setEquityHistory(null); })
      .finally(() => { if (!cancelled) setEquityLoading(false); });
    return () => { cancelled = true; };
  }, [timeRange]);

  // 年轻账户的日线档（7D/30D/ALL）在第二次收盘前必然 404：加载结束后若
  // 当前档无数据而日内档有，默认切到 1D，让页面首屏就能看到真实曲线。
  useEffect(() => {
    if (equityLoading || equityHistory || timeRange === '1D') return;
    let cancelled = false;
    apiFetch(`/portfolio/history?${HISTORY_PARAMS['1D']}`)
      .then(async (res) => {
        if (cancelled || !res.ok) return;
        const data = await res.json();
        if (!cancelled && (data.points || []).length >= 2) setTimeRange('1D');
      })
      .catch(() => { /* keep the daily empty state */ });
    return () => { cancelled = true; };
  }, [equityLoading, equityHistory, timeRange]);

  const usdPerUnit = useCallback(
    (currency?: string) => fxRates[(currency || 'USD').toUpperCase()] ?? 1,
    [fxRates],
  );

  // Helper to get position
  const getPosition = useCallback((symbol: string) => {
    if (!positions) return undefined;
    if (positions instanceof Map) return positions.get(symbol);
    if (typeof positions === 'object') return (positions as any)[symbol];
    return undefined;
  }, [positions]);

  // 账户持仓（Map 或持久化后的对象都要兼容）
  const positionList = useMemo<Position[]>(() => {
    if (!positions) return [];
    if (positions instanceof Map) return [...positions.values()];
    return Object.values(positions) as Position[];
  }, [positions]);

  // 资产列表：现金 + 券商持仓。金额统一折算成 USD 汇总，
  // 价格列保留标的自己的计价币种。
  const assetList = useMemo(() => {
    const rows: Array<{
      asset: string;
      symbol: string;
      currency: string;
      total: string;
      available: string;
      frozen: string;
      currentPrice: string;
      avgPrice: string;
      priceChange24h: number;
      unrealizedPnl: number;
      unrealizedPnlPercent: number;
      value: number;
      isCash: boolean;
      sparklineData: number[];
    }> = [];

    const cash = brokerMetrics?.cash ?? 0;
    rows.push({
      asset: 'USD',
      symbol: 'USD',
      currency: 'USD',
      total: String(cash),
      available: String(cash),
      frozen: '0',
      currentPrice: '1',
      avgPrice: '1',
      priceChange24h: 0,
      unrealizedPnl: 0,
      unrealizedPnlPercent: 0,
      value: cash,
      isCash: true,
      sparklineData: [],
    });

    for (const pos of positionList) {
      const qty = new Decimal(pos.quantity || 0);
      if (!qty.gt(0)) continue;
      const currency = (pos.currency || 'USD').toUpperCase();
      const fx = usdPerUnit(currency);
      const priceNative = new Decimal(pos.currentPrice || pos.avgEntryPrice || 0);
      const avgNative = new Decimal(pos.avgEntryPrice || 0);
      const valueUsd = pos.marketValueUsd
        ? new Decimal(pos.marketValueUsd)
        : qty.times(priceNative).times(fx);
      const pnlUsd = pos.unrealizedPnlUsd
        ? new Decimal(pos.unrealizedPnlUsd)
        : qty.times(priceNative.minus(avgNative)).times(fx);
      const costUsd = qty.times(avgNative).times(fx);
      const pnlPct = costUsd.gt(0) ? pnlUsd.div(costUsd).times(100) : new Decimal(0);

      rows.push({
        asset: pos.symbol,
        symbol: pos.symbol,
        currency,
        total: qty.toString(),
        available: qty.toString(),
        frozen: '0',
        currentPrice: priceNative.toString(),
        avgPrice: avgNative.toString(),
        priceChange24h: 0,
        unrealizedPnl: pnlUsd.toNumber(),
        unrealizedPnlPercent: pnlPct.toNumber(),
        value: valueUsd.toNumber(),
        isCash: false,
        sparklineData: [],
      });
    }

    return rows;
  }, [brokerMetrics, positionList, usdPerUnit]);

  // Filtered and sorted asset list
  const filteredAssets = useMemo(() => {
    let filtered = assetList.filter(a => 
      a.asset.toLowerCase().includes(searchQuery.toLowerCase())
    );
    
    return filtered.sort((a, b) => {
      let aVal: number, bVal: number;
      switch (sortField) {
        case 'value': aVal = a.value; bVal = b.value; break;
        case 'balance': aVal = parseFloat(a.total); bVal = parseFloat(b.total); break;
        case 'pnl': aVal = a.unrealizedPnl; bVal = b.unrealizedPnl; break;
        case 'pnlPercent': aVal = a.unrealizedPnlPercent; bVal = b.unrealizedPnlPercent; break;
        default: aVal = a.value; bVal = b.value;
      }
      return sortOrder === 'desc' ? bVal - aVal : aVal - bVal;
    });
  }, [assetList, searchQuery, sortField, sortOrder]);

  // Portfolio totals: the broker snapshot is the single source of truth.
  // 汇总一律用 USD：港股/日股/A 股的市值不能直接和美元现金相加。
  const totals = useMemo(() => {
    const totalValue = brokerMetrics?.totalEquity
      ?? assetList.reduce((acc, asset) => acc + asset.value, 0);
    const availableBalance = brokerMetrics?.cash
      ?? (assetList.find(a => a.isCash)?.value ?? 0);
    const positionValue = brokerMetrics?.positionValue
      ?? (totalValue - availableBalance);
    const totalUnrealizedPnl = brokerMetrics?.unrealizedPnl
      ?? assetList.reduce((acc, asset) => acc + asset.unrealizedPnl, 0);
    const totalRealizedPnl = brokerMetrics?.realizedPnl ?? 0;

    // Total P&L (realized + unrealized)
    const totalPnl = totalRealizedPnl + totalUnrealizedPnl;

    // ROI 用券商口径（以初始资金为分母）；没有快照时才退回自算。
    const initialCapital = brokerMetrics?.initialCapital ?? 0;
    const roi = brokerMetrics?.totalPnlPct
      ?? (initialCapital > 0 ? (totalPnl / initialCapital) * 100 : 0);

    // P&L percentage against the cost basis implied by the current equity.
    const pnlPercent = totalValue > 0 && totalValue - totalPnl > 0
      ? (totalPnl / (totalValue - totalPnl)) * 100
      : 0;

    return {
      totalValue,
      totalUnrealizedPnl,
      totalRealizedPnl,
      totalPnl,
      pnlPercent,
      totalChange24h: 0,
      availableBalance,
      positionValue,
      roi,
    };
  }, [assetList, brokerMetrics]);

  // 券商净值曲线 → 图表点序列；没有真实历史就保持空数组走空态。
  const portfolioHistoryData = useMemo<number[]>(
    () => (equityHistory ? equityHistory.points.map((p) => p.equity) : []),
    [equityHistory],
  );
  const portfolioHistoryTimes = useMemo<number[]>(
    () => (equityHistory ? equityHistory.points.map((p) => p.time) : []),
    [equityHistory],
  );

  // Trading stats. 成交额按标的币种换算成 USD，否则港币/日元成交会被
  // 当成美元累加；价格同样优先用券商快照，本地行情只是回退。
  const tradingStats = useMemo(() => {
    const filledOrders = orders.filter(o => o.status === 'filled');
    const openOrders = orders.filter(o => ['pending', 'open', 'partial'].includes(o.status));
    const activeTriggers = triggers.filter(t => t.enabled && t.status === 'armed');
    
    // Calculate position count
    const positionCount = positionList.filter(pos => parseFloat(pos.quantity) > 0).length;
    
    // Calculate 24h volume (USD)
    const oneDayAgo = Date.now() - 24 * 60 * 60 * 1000;
    const volume24h = orders
      .filter(o => o.fills.length > 0)
      .flatMap(o => o.fills.map(f => ({ fill: f, currency: o.currency })))
      .filter(({ fill }) => fill.time > oneDayAgo)
      .reduce((sum, { fill, currency }) => {
        const fx = currency ? usdPerUnit(currency) : 1;
        return sum + parseFloat(fill.price) * parseFloat(fill.quantity) * fx;
      }, 0);
    
    // Win rate is account-level FIFO over closed lots (performanceMetrics), and
    // it only means something once a sell has actually closed one. Classifying
    // open fills by the direction of today's unrealized PnL is not a win rate:
    // it counted every still-open buy as a loss the moment price dipped, and it
    // printed a number even for an account that had never sold anything.
    const hasClosedTrades = performanceMetrics.closedTrades > 0;
    const winRate = hasClosedTrades ? performanceMetrics.winRate * 100 : null;
    
    // Best performing asset
    const bestAsset = assetList
      .filter(a => !a.isCash && a.unrealizedPnlPercent !== 0)
      .sort((a, b) => b.unrealizedPnlPercent - a.unrealizedPnlPercent)[0];
    
    return {
      positionCount,
      openOrderCount: openOrders.length,
      activeTriggerCount: activeTriggers.length,
      totalTrades: filledOrders.length,
      volume24h,
      winRate,
      hasClosedTrades,
      bestAsset,
    };
  }, [orders, triggers, positionList, assetList, getPosition, usdPerUnit, performanceMetrics]);

  // Recent activity
  const recentActivity = useMemo(() => {
    const activities: Array<{
      type: 'trade' | 'order' | 'trigger';
      title: string;
      description: string;
      time: number;
      value?: string;
      isPositive?: boolean;
    }> = [];
    
    // Recent filled orders
    orders
      .filter(o => o.status === 'filled')
      .slice(0, 5)
      .forEach(o => {
        const value = o.fills.reduce((sum, f) => 
          sum + parseFloat(f.price) * parseFloat(f.quantity), 0);
        activities.push({
          type: 'trade',
          title: `${o.side.toUpperCase()} ${o.symbol.replace('USDT', '')}`,
          description: `${parseFloat(o.filledQty).toFixed(6)} @ ${formatMoney(parseFloat(o.avgPrice || '0'), o.currency)}`,
          time: o.updatedAt,
          value: formatMoney(value, o.currency),
          isPositive: o.side === 'sell',
        });
      });
    
    // Recent trigger executions
    executionLogs
      .slice(0, 3)
      .forEach(log => {
        activities.push({
          type: 'trigger',
          title: `Trigger ${log.result === 'success' ? 'Executed' : 'Failed'}`,
          description: log.reason || 'Automation completed',
          time: log.timestamp || Date.now(),
        });
      });
    
    return activities
      .sort((a, b) => b.time - a.time)
      .slice(0, 6);
  }, [orders, executionLogs]);

  // 市场概览：用自选里的美股指数（SPY/QQQ），不再展示不存在的 BTC/ETH。
  const marketData = useMemo(() => {
    const card = (symbol: string) => {
      const row = symbols?.find(s => s.symbol === symbol);
      const price = parseFloat(row?.price || '0');
      return {
        symbol,
        price: row?.price || '0',
        change: row?.change24h || 0,
        sparkline: [] as number[],
      };
    };
    return { spy: card('SPY'), qqq: card('QQQ') };
  }, [symbols]);

  // Handlers
  const handleTrade = (symbol: string) => {
    setSelectedSymbol(symbol);
    navigate('/trade');
  };

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortOrder(sortOrder === 'desc' ? 'asc' : 'desc');
    } else {
      setSortField(field);
      setSortOrder('desc');
    }
  };

  const formatTimeAgo = (timestamp: number) => {
    const diff = Date.now() - timestamp;
    const minutes = Math.floor(diff / 60000);
    const hours = Math.floor(diff / 3600000);
    const days = Math.floor(diff / 86400000);
    
    if (days > 0) return `${days}d ago`;
    if (hours > 0) return `${hours}h ago`;
    if (minutes > 0) return `${minutes}m ago`;
    return 'Just now';
  };

  return (
    <div className={styles.container}>
      {/* Hero Section - Portfolio Summary */}
      <section className={styles.heroSection}>
        <div className={styles.heroMain}>
          <div className={styles.portfolioHeader}>
            <div className={styles.portfolioLabel}>
              <Icon name="briefcase" size="sm" />
              <span>Portfolio Value</span>
            </div>
            <div className={styles.accountBadge}>
              <span>ID: {account?.accountId || 'PTT-DEMO'}</span>
              <span className={styles.simulatedTag}>Paper Trading</span>
            </div>
          </div>
          
          <div className={styles.portfolioValue}>
            <span className={styles.currency}>$</span>
            <span className={styles.amount}>{totals.totalValue.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
          </div>
          
          <div className={styles.portfolioMeta}>
            <div className={`${styles.changeIndicator} ${totals.totalPnl >= 0 ? styles.positive : styles.negative}`}>
              <Icon name={totals.totalPnl >= 0 ? 'trending-up' : 'trending-down'} size="xs" />
              <span>{formatPercent(totals.pnlPercent)}</span>
              <span className={styles.changeLabel}>on cost</span>
            </div>
            <div className={styles.pnlIndicator}>
              <span className={`${styles.pnlValue} ${totals.totalPnl >= 0 ? styles.positive : styles.negative}`}>
                {totals.totalPnl >= 0 ? '+' : ''}${totals.totalPnl.toFixed(2)}
              </span>
              <span className={styles.pnlLabel}>Total P&L</span>
            </div>
            <div className={styles.roiIndicator}>
              <span className={`${styles.roiValue} ${totals.roi >= 0 ? styles.positive : styles.negative}`}>
                {totals.roi >= 0 ? '+' : ''}{totals.roi.toFixed(2)}%
              </span>
              <span className={styles.roiLabel}>ROI</span>
            </div>
          </div>
          
          {/* Enhanced Metrics Grid */}
          <div className={styles.metricsGrid}>
            <div className={styles.metricItem}>
              <div className={styles.metricIcon}>
                <Icon name="wallet" size="xs" />
              </div>
              <div className={styles.metricContent}>
                <span className={styles.metricValue}>{usd(totals.availableBalance)}</span>
                <span className={styles.metricLabel}>Available Balance</span>
              </div>
            </div>
            
            <div className={styles.metricItem}>
              <div className={styles.metricIcon}>
                <Icon name="layers" size="xs" />
              </div>
              <div className={styles.metricContent}>
                <span className={styles.metricValue}>{usd(totals.positionValue)}</span>
                <span className={styles.metricLabel}>Position Value</span>
              </div>
            </div>
            
            <div className={styles.metricItem}>
              <div className={styles.metricIcon}>
                <Icon name="check-circle" size="xs" />
              </div>
              <div className={styles.metricContent}>
                <span className={`${styles.metricValue} ${totals.totalRealizedPnl >= 0 ? styles.positive : styles.negative}`}>
                  {usdSigned(totals.totalRealizedPnl)}
                </span>
                <span className={styles.metricLabel}>Realized P&L</span>
              </div>
            </div>
            
            <div className={styles.metricItem}>
              <div className={styles.metricIcon}>
                <Icon name="activity" size="xs" />
              </div>
              <div className={styles.metricContent}>
                <span className={`${styles.metricValue} ${totals.totalUnrealizedPnl >= 0 ? styles.positive : styles.negative}`}>
                  {usdSigned(totals.totalUnrealizedPnl)}
                </span>
                <span className={styles.metricLabel}>Unrealized P&L</span>
              </div>
            </div>
            
            <div className={styles.metricItem}>
              <div className={styles.metricIcon}>
                <Icon name="target" size="xs" />
              </div>
              <div className={styles.metricContent}>
                <span className={styles.metricValue}>
                  {tradingStats.winRate === null ? '—' : `${tradingStats.winRate.toFixed(1)}%`}
                </span>
                <span className={styles.metricLabel}>Win Rate</span>
              </div>
            </div>
            
            <div className={styles.metricItem}>
              <div className={styles.metricIcon}>
                <Icon name="bar-chart-2" size="xs" />
              </div>
              <div className={styles.metricContent}>
                <span className={styles.metricValue}>{performanceMetrics.profitFactor.toFixed(2)}</span>
                <span className={styles.metricLabel}>Profit Factor</span>
              </div>
            </div>
            
            <div className={styles.metricItem}>
              <div className={styles.metricIcon}>
                <Icon name="trending-down" size="xs" />
              </div>
              <div className={styles.metricContent}>
                <span className={`${styles.metricValue} ${performanceMetrics.maxDrawdown > 0 ? styles.negative : ''}`}>
                  {performanceMetrics.maxDrawdown.toFixed(2)}%
                </span>
                <span className={styles.metricLabel}>Max Drawdown</span>
              </div>
            </div>
            
            <div className={styles.metricItem}>
              <div className={styles.metricIcon}>
                <Icon name="repeat" size="xs" />
              </div>
              <div className={styles.metricContent}>
                <span className={styles.metricValue}>{tradingStats.totalTrades}</span>
                <span className={styles.metricLabel}>Total Trades</span>
              </div>
            </div>
          </div>
        </div>
        
        <div className={styles.heroChart}>
          <div className={styles.chartHeader}>
            <span className={styles.chartTitle}>{t.assets?.portfolioGrowth ?? 'Portfolio Growth'}</span>
            {portfolioHistoryData.length >= 2 && (
              <div className={styles.timeRangeSelector}>
                {(['1D', '7D', '30D', 'ALL'] as TimeRange[]).map(range => (
                  <button
                    key={range}
                    className={`${styles.timeRangeBtn} ${timeRange === range ? styles.active : ''}`}
                    onClick={() => setTimeRange(range)}
                  >
                    {range}
                  </button>
                ))}
              </div>
            )}
          </div>
          <div className={styles.chartContainer}>
            {portfolioHistoryData.length >= 2 ? (
              <PortfolioChart
                data={portfolioHistoryData}
                times={portfolioHistoryTimes}
                color={totals.totalPnl >= 0 ? 'var(--color-success)' : 'var(--color-error)'}
                height={120}
                timeRange={timeRange}
                fmt={(v) => formatMoney(v, 'USD', { decimals: 0 })}
              />
            ) : equityLoading ? (
              <div className={styles.chartEmpty}>
                <Icon name="bar-chart-2" size="lg" />
                <span>{t.assets?.equityLoading ?? 'Loading equity history…'}</span>
              </div>
            ) : (
              // 券商不返回净值曲线（账户太新或上游不可用）；宁可留空也不画一条
              // 随机生成的假走势。日线档在年轻账户上会等到下一次收盘，说明等待点。
              <div className={styles.chartEmpty}>
                <Icon name="bar-chart-2" size="lg" />
                <span>
                  {timeRange !== '1D' && marketClock?.next_close
                    ? (t.assets?.equityFirstClose ?? 'Daily curve starts after the next session close')
                    : (t.assets?.equityUnavailable ?? 'Equity history is not recorded by the venue yet.')}
                </span>
              </div>
            )}
          </div>
        </div>
        
        <div className={styles.heroActions}>
          <button className={styles.primaryAction} onClick={() => navigate('/wallet')}>
            <Icon name="download" size="sm" />
            <span>Deposit</span>
          </button>
          <button className={styles.secondaryAction} onClick={() => navigate('/trade')}>
            <Icon name="activity" size="sm" />
            <span>Trade</span>
          </button>
          <button className={styles.tertiaryAction} onClick={() => navigate('/orders')}>
            <Icon name="layers" size="sm" />
            <span>Orders</span>
          </button>
        </div>
      </section>

      {/* Market Overview */}
      <section className={styles.marketSection}>
        <div className={styles.sectionHeader}>
          <h2 className={styles.sectionTitle}>Market Overview</h2>
          <button className={styles.viewAllBtn} onClick={() => navigate('/markets')}>
            View All Markets
            <Icon name="chevron-right" size="xs" />
          </button>
        </div>
        <div className={styles.marketCards}>
          <MarketCard
            symbol="SPY"
            name="S&P 500 ETF"
            price={parseFloat(marketData.spy.price).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
            change={parseFloat(String(marketData.spy.change))}
            sparklineData={marketData.spy.sparkline}
            onClick={() => handleTrade('SPY')}
          />
          <MarketCard
            symbol="QQQ"
            name="Nasdaq 100 ETF"
            price={parseFloat(marketData.qqq.price).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
            change={parseFloat(String(marketData.qqq.change))}
            sparklineData={marketData.qqq.sparkline}
            onClick={() => handleTrade('QQQ')}
          />
        </div>
      </section>

      {/* Quick Stats */}
      <section className={styles.statsSection}>
        <div className={styles.statsGrid}>
          <StatCard
            label="Open Positions"
            value={tradingStats.positionCount}
            icon="briefcase"
            color="blue"
            onClick={() => navigate('/trade')}
          />
          <StatCard
            label="Open Orders"
            value={tradingStats.openOrderCount}
            icon="list"
            color="orange"
            onClick={() => navigate('/orders')}
          />
          <StatCard
            label="Active Triggers"
            value={tradingStats.activeTriggerCount}
            icon="zap"
            color="purple"
            onClick={() => navigate('/orders')}
          />
          <StatCard
            label="24h Volume"
            value={formatNumber(tradingStats.volume24h)}
            icon="bar-chart-2"
            color="green"
          />
          <StatCard
            label="Total Trades"
            value={tradingStats.totalTrades}
            icon="repeat"
            color="blue"
          />
          <StatCard
            label="Win Rate"
            value={
              tradingStats.winRate === null
                ? '—'
                : `${tradingStats.winRate.toFixed(0)}%`
            }
            subValue={
              tradingStats.winRate === null
                ? 'No closed trades yet'
                : tradingStats.winRate >= 50
                  ? 'Above average'
                  : 'Below average'
            }
            icon="target"
            color={
              tradingStats.winRate === null
                ? 'blue'
                : tradingStats.winRate >= 50
                  ? 'green'
                  : 'red'
            }
          />
        </div>
      </section>

      {/* Main Content Grid */}
      <div className={styles.mainGrid}>
        {/* Asset Holdings */}
        <section className={styles.assetsSection}>
          <div className={styles.sectionHeader}>
            <h2 className={styles.sectionTitle}>
              <Icon name="wallet" size="sm" />
              Asset Holdings
            </h2>
            <div className={styles.tableControls}>
              <div className={styles.searchBox}>
                <Icon name="search" size="xs" />
                <input
                  type="text"
                  placeholder="Search assets..."
                  value={searchQuery}
                  onChange={e => setSearchQuery(e.target.value)}
                />
              </div>
            </div>
          </div>
          
          <div className={styles.tableWrapper}>
            <table className={styles.assetTable}>
              <thead>
                <tr>
                  <th>Asset</th>
                  <th className={styles.sortable} onClick={() => handleSort('balance')}>
                    Balance
                    {sortField === 'balance' && <Icon name={sortOrder === 'desc' ? 'chevron-down' : 'chevron-up'} size="xs" />}
                  </th>
                  <th className={styles.sortable} onClick={() => handleSort('value')}>
                    Value
                    {sortField === 'value' && <Icon name={sortOrder === 'desc' ? 'chevron-down' : 'chevron-up'} size="xs" />}
                  </th>
                  <th>Price</th>
                  <th className={styles.sortable} onClick={() => handleSort('pnl')}>
                    P&L
                    {sortField === 'pnl' && <Icon name={sortOrder === 'desc' ? 'chevron-down' : 'chevron-up'} size="xs" />}
                  </th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {filteredAssets.map(asset => {
                  const allocation = totals.totalValue > 0 
                    ? (asset.value / totals.totalValue) * 100 
                    : 0;
                  
                  return (
                    <tr key={asset.symbol}>
                      <td>
                        <div className={styles.assetCell}>
                          <div className={styles.assetIcon}>{asset.asset[0]}</div>
                          <div className={styles.assetInfo}>
                            <span className={styles.assetSymbol}>{asset.asset}</span>
                            <span className={styles.assetAlloc}>{allocation.toFixed(1)}% of portfolio</span>
                          </div>
                        </div>
                      </td>
                      <td className={styles.numericCell}>
                        <span className={styles.balanceValue}>{parseFloat(asset.total).toFixed(asset.isCash ? 2 : 6)}</span>
                      </td>
                      <td className={styles.numericCell}>
                        <span className={styles.valueAmount}>{usd(asset.value)}</span>
                      </td>
                      <td className={styles.numericCell}>
                        {asset.isCash ? '—' : (
                          <span className={styles.priceValue}>
                            {formatMoney(parseFloat(asset.currentPrice), asset.currency)}
                          </span>
                        )}
                      </td>
                      <td className={styles.numericCell}>
                        {asset.unrealizedPnl !== 0 ? (
                          <div className={`${styles.pnlCell} ${asset.unrealizedPnl >= 0 ? styles.positive : styles.negative}`}>
                            <span>{usdSigned(asset.unrealizedPnl)}</span>
                            <span className={styles.pnlPercent}>{formatPercent(asset.unrealizedPnlPercent)}</span>
                          </div>
                        ) : '—'}
                      </td>
                      <td>
                        {!asset.isCash && (
                          <button 
                            className={styles.tradeBtn}
                            onClick={() => handleTrade(asset.symbol)}
                          >
                            Trade
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>

        {/* Side Panel */}
        <aside className={styles.sidePanel}>
          {/* Allocation Chart */}
          <div className={styles.allocationCard}>
            <h3 className={styles.cardTitle}>Portfolio Allocation</h3>
            <div className={styles.allocationList}>
              {filteredAssets.slice(0, 5).map((asset, i) => {
                const ratio = totals.totalValue > 0 
                  ? (asset.value / totals.totalValue) * 100 
                  : 0;
                const colors = ['#3b82f6', '#22c55e', '#f59e0b', '#8b5cf6', '#ef4444'];
                return (
                  <div key={asset.asset} className={styles.allocItem}>
                    <div className={styles.allocHeader}>
                      <span className={styles.allocBullet} style={{ backgroundColor: colors[i % colors.length] }} />
                      <span className={styles.allocSymbol}>{asset.asset}</span>
                      <span className={styles.allocPercent}>{ratio.toFixed(1)}%</span>
                    </div>
                    <div className={styles.allocBar}>
                      <div 
                        className={styles.allocBarFill}
                        style={{ width: `${ratio}%`, backgroundColor: colors[i % colors.length] }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Recent Activity */}
          <div className={styles.activityCard}>
            <div className={styles.activityHeader}>
              <h3 className={styles.cardTitle}>Recent Activity</h3>
              <button className={styles.viewAllBtn} onClick={() => navigate('/orders')}>
                View All
                <Icon name="chevron-right" size="xs" />
              </button>
            </div>
            <div className={styles.activityList}>
              {recentActivity.length > 0 ? (
                recentActivity.map((activity, i) => (
                  <ActivityItem
                    key={i}
                    type={activity.type}
                    title={activity.title}
                    description={activity.description}
                    time={formatTimeAgo(activity.time)}
                    value={activity.value}
                    isPositive={activity.isPositive}
                  />
                ))
              ) : (
                <div className={styles.emptyActivity}>
                  <Icon name="clock" size="lg" />
                  <p>No recent activity</p>
                  <button onClick={() => navigate('/trade')}>Start Trading</button>
                </div>
              )}
            </div>
          </div>

          {/* Best Performer */}
          {tradingStats.bestAsset && (
            <div className={styles.performerCard}>
              <h3 className={styles.cardTitle}>Top Performer</h3>
              <div className={styles.performerContent}>
                <div className={styles.performerIcon}>{tradingStats.bestAsset.asset[0]}</div>
                <div className={styles.performerInfo}>
                  <span className={styles.performerSymbol}>{tradingStats.bestAsset.asset}</span>
                  <span className={`${styles.performerPnl} ${tradingStats.bestAsset.unrealizedPnlPercent >= 0 ? styles.positive : styles.negative}`}>
                    {formatPercent(tradingStats.bestAsset.unrealizedPnlPercent)}
                  </span>
                </div>
                <button 
                  className={styles.performerTradeBtn}
                  onClick={() => handleTrade(tradingStats.bestAsset!.asset)}
                >
                  Trade
                </button>
              </div>
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}
