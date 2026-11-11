import { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import { createChart, IChartApi, ColorType, UTCTimestamp, LineSeries, CandlestickSeries, HistogramSeries, CrosshairMode } from 'lightweight-charts';
import { handleApiError, logError } from '../../utils/errorHandler';
import { apiFetch } from '../../services/apiClient';
import { computeSessionStats, marketTimezoneFor } from '../../utils/sessionStats';
import { useWatchlistStore, selectSelectedSymbol } from '../../store/watchlistStore';
import { useMarketStore } from '../../store/marketStore';
import { useAutomationStore } from '../../store/automationStore';
import { useTradingStore } from '../../store/tradingStore';
import { useIsMobile } from '../../hooks/useMediaQuery';
import { useI18n } from '../../i18n';
import { Icon } from '../Icon';
import { prependOlderSegment, keepHistoryAheadOfWindow } from '../../services/klineMerge';
import styles from './PriceChart.module.css';

export type ChartType = 'line' | 'candlestick';
export type TimeRange = '1m' | '5m' | '15m' | '1h' | '4h' | '1d' | '1wk' | '1mo';
export type Indicator = 'MA' | 'EMA' | 'BOLL' | 'VOL';

// 图表颜色配置
const CHART_COLORS = {
  accent: '#58A6FF',
  buy: '#3FB950',
  sell: '#F85149',
  text: '#9AA5B1',
  textSecondary: '#6E7681',
  border: '#30363D',
  bg: '#161B22',
  bgSecondary: '#0D1117',
  ma7: '#F0B90B',
  ma25: '#E377C2',
  ma99: '#9467BD',
  ema12: '#00D4AA',
  ema26: '#FF6B9D',
  bollUpper: '#FF6B6B',
  bollMiddle: '#4ECDC4',
  bollLower: '#45B7D1',
  volumeUp: 'rgba(63, 185, 80, 0.5)',
  volumeDown: 'rgba(248, 81, 73, 0.5)',
  grid: 'rgba(48, 54, 61, 0.5)',
};

export interface KlineData {
  time: UTCTimestamp;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

interface CrosshairData {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  change: number;
  changePercent: number;
}

// K线间隔映射与秒数定义
const INTERVAL_MAP: Record<TimeRange, string> = {
  '1m': '1m',
  '5m': '5m',
  '15m': '15m',
  '1h': '1h',
  '4h': '4h',
  '1d': '1d',
  // Backend keys are deliberately "1wk"/"1mo" (the API lowercases input, so a
  // bare "1M" would collide with the 1-minute key).
  '1wk': '1wk',
  '1mo': '1mo',
};

const TIMEFRAME_SECONDS: Record<TimeRange, number> = {
  '1m': 60,
  '5m': 300,
  '15m': 900,
  '1h': 3600,
  '4h': 14400,
  '1d': 86400,
  '1wk': 604800,
  '1mo': 2592000,
};

// 计算移动平均线
// 历史补载按后端 timeframe 键请求（后端 1wk/1mo 键刻意避开 "1M" 歧义）。
function tfForHistory(range: TimeRange): string {
  return INTERVAL_MAP[range] || range;
}

function calculateMA(data: KlineData[], period: number) {
  const result = [];
  for (let i = period - 1; i < data.length; i++) {
    let sum = 0;
    for (let j = 0; j < period; j++) {
      const item = data[i - j];
      if (item) sum += item.close;
    }
    const current = data[i];
    if (current) result.push({ time: current.time, value: sum / period });
  }
  return result;
}

// 计算 EMA
function calculateEMA(data: KlineData[], period: number) {
  const result = [];
  const multiplier = 2 / (period + 1);
  let ema = data[0]?.close || 0;
  
  for (let i = 0; i < data.length; i++) {
    const current = data[i];
    if (!current) continue;
    ema = i === 0 ? current.close : (current.close - ema) * multiplier + ema;
    if (i >= period - 1) {
      result.push({ time: current.time, value: ema });
    }
  }
  return result;
}

// 计算布林带
function calculateBOLL(data: KlineData[], period: number = 20, stdDev: number = 2) {
  const upper = [], middle = [], lower = [];
  
  for (let i = period - 1; i < data.length; i++) {
    let sum = 0;
    for (let j = 0; j < period; j++) {
      const item = data[i - j];
      if (item) sum += item.close;
    }
    const ma = sum / period;
    
    let squaredDiffSum = 0;
    for (let j = 0; j < period; j++) {
      const item = data[i - j];
      if (item) squaredDiffSum += Math.pow(item.close - ma, 2);
    }
    const std = Math.sqrt(squaredDiffSum / period);
    
    const current = data[i];
    if (current) {
      middle.push({ time: current.time, value: ma });
      upper.push({ time: current.time, value: ma + stdDev * std });
      lower.push({ time: current.time, value: ma - stdDev * std });
    }
  }
  
  return { upper, middle, lower };
}

export function PriceChart() {
  const { t } = useI18n();
  const isMobile = useIsMobile();
  const mainChartRef = useRef<HTMLDivElement>(null);
  const mainChartApiRef = useRef<IChartApi | null>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const mainSeriesRef = useRef<any>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const volumeSeriesRef = useRef<any>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const indicatorSeriesRef = useRef<Map<string, any>>(new Map());
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const priceLinesRef = useRef<any[]>([]);
  
  const selectedSymbol = useWatchlistStore(selectSelectedSymbol);
  const selectedSymbolRef = useRef<string | null>(null);
  selectedSymbolRef.current = selectedSymbol;
  const triggers = useAutomationStore((state) => state.triggers);
  const openOrders = useTradingStore((state) => state.orders.filter(o => o.status === 'open'));
  
  const [chartType, setChartType] = useState<ChartType>('candlestick');
  const [timeRange, setTimeRange] = useState<TimeRange>('15m');
  const timeRangeRef = useRef<TimeRange>('15m');
  timeRangeRef.current = timeRange;
  const [klines, setKlines] = useState<KlineData[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeIndicators, setActiveIndicators] = useState<Set<Indicator>>(new Set(['MA', 'VOL']));
  const [crosshairData, setCrosshairData] = useState<CrosshairData | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [chartReady, setChartReady] = useState(false);
  const [livePrice, setLivePrice] = useState<number | null>(null);
  const isInitialFitRef = useRef<boolean>(true);

  // 切换指标
  const toggleIndicator = useCallback((indicator: Indicator) => {
    setActiveIndicators(prev => {
      const newSet = new Set(prev);
      if (newSet.has(indicator)) newSet.delete(indicator);
      else newSet.add(indicator);
      return newSet;
    });
  }, []);

  // K线数据缓存与引用
  const klinesRef = useRef<KlineData[]>([]);
  const klinesCacheRef = useRef<Map<string, { data: KlineData[]; timestamp: number; etag?: string }>>(new Map()); // etag: 后端弱校验器，命中则 304 免下载整段K线
  const CACHE_FRESH_TTL = 15000; // 15秒内视为极速新鲜缓存，直接返回
  const MAX_CACHE_ENTRIES = 50; // 最大缓存条目数，防止无界增长

  // 历史补载（左边界懒加载）：滑到图表左侧时按 before 游标拉更早的不可变段。
  // 段数据永不过期（历史不会改变），本次会话内看过的 K 线永久保留。
  const loadingMoreRef = useRef(false);
  const hasMoreRef = useRef(true);
  const loadOlderRef = useRef<(() => void) | null>(null);
  const segmentsCacheRef = useRef<Map<string, { data: KlineData[]; hasMore: boolean }>>(new Map());
  const MAX_SEGMENT_ENTRIES = 60; // 段缓存上限（不可变数据，仅防内存失控）
  const HISTORY_CAPABLE: Record<string, boolean> = {
    '1m': false,  // Yahoo 1m 物理上限即 7 天，初始拉取已是全量
    '5m': true, '15m': true, '1h': true, '4h': true,
    '1d': false, '1wk': false, '1mo': false, // 日级三键后端整段供给，不分页
  };

  // 获取历史K线数据（带 SWR 零延迟双层缓存和重试）
  const fetchKlines = useCallback(async (symbol: string, interval: string, retryCount = 0) => {
    const tf = INTERVAL_MAP[interval as TimeRange] || interval || '15m';
    const cacheKey = `${symbol}-${tf}`;
    const cached = klinesCacheRef.current.get(cacheKey);
    const now = Date.now();
    
    // SWR 策略：如果有历史缓存，瞬时展示，切股票/切周期零延迟！
    if (cached) {
      klinesRef.current = cached.data;
      setKlines(cached.data);
      setLoading(false);
      setError(null);
      // 如果处于极度新鲜期（15秒内），免去网络请求
      if (now - cached.timestamp < CACHE_FRESH_TTL) {
        return;
      }
    } else {
      // 全新未缓存标的才展示 loading 骨架
      setLoading(true);
    }
    
    try {
      const url = `/market/bars?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(tf)}`;
      // 后端为每段K线发弱 ETag；带上一次的值，未变化时整段下载变 304。
      const condHeaders: Record<string, string> = {};
      if (cached?.etag) condHeaders['If-None-Match'] = cached.etag;
      const response = await apiFetch(url, { headers: condHeaders });

      if (response.status === 304) {
        // 数据未变：续鲜 SWR 缓存即可，画面保持当前内容。
        if (cached) {
          klinesCacheRef.current.set(cacheKey, { ...cached, timestamp: now });
        }
        return;
      }

      if (!response.ok) throw response;
      
      const data = await response.json();
      const formattedData: KlineData[] = data.map((k: any) => ({
        time: (typeof k.time === 'number' ? k.time : Math.floor(new Date(k.time).getTime() / 1000)) as UTCTimestamp,
        open: parseFloat(k.open),
        high: parseFloat(k.high),
        low: parseFloat(k.low),
        close: parseFloat(k.close),
        volume: parseFloat(k.volume || 0),
      }));
      
      // 竞态保护：请求期间用户切走了标的，丢弃过期响应
      if (selectedSymbolRef.current !== symbol) return;

      // 只增不减：同一序列已有更早的历史段时，把新鲜段接到旧数据后面，
      // 而不是整段替换——60s 轮询绝不能吞掉用户已经翻看过的历史。
      const finalData = keepHistoryAheadOfWindow(klinesRef.current, formattedData);

      // 更新瞬时缓存，并实施 LRU 简单淘汰
      if (klinesCacheRef.current.size >= MAX_CACHE_ENTRIES) {
        const oldestKey = klinesCacheRef.current.keys().next().value;
        if (oldestKey) klinesCacheRef.current.delete(oldestKey);
      }
      const etag = response.headers.get('etag') || undefined;
      klinesCacheRef.current.set(cacheKey, { data: finalData, timestamp: now, etag });

      klinesRef.current = finalData;
      setKlines(finalData);
      setError(null);
      setLoading(false);
    } catch (err) {
      const appError = handleApiError(err);
      logError(appError);
      
      // 有缓存数据时使用旧缓存
      if (cached) {
        klinesRef.current = cached.data;
        setKlines(cached.data);
        setError(null);
        setLoading(false);
        return;
      }
      
      // 重试最多3次
      if (retryCount < 3) {
        setTimeout(() => fetchKlines(symbol, interval, retryCount + 1), 2000 * (retryCount + 1));
        return;
      }

      setError(appError.message);
      setLoading(false);
    }
  }, []);

  // 数据获取（切换标的或时间周期时标记初次视口自适应）
  useEffect(() => {
    isInitialFitRef.current = true;
    setLivePrice(null);
    // 换标的/换周期 = 新的序列：重置补载状态（段缓存仍保留，切回不重拉）
    hasMoreRef.current = HISTORY_CAPABLE[tfForHistory(timeRange)] ?? false;
    if (selectedSymbol) fetchKlines(selectedSymbol, INTERVAL_MAP[timeRange]);
  }, [selectedSymbol, timeRange, fetchKlines]);

  // 左边界历史补载：请求 time < 最旧一根 的不可变段，prepend 后整段重画。
  const loadOlder = useCallback(async () => {
    const symbol = selectedSymbolRef.current;
    const tf = tfForHistory(timeRange);
    if (!symbol || loadingMoreRef.current || !hasMoreRef.current) return;
    const list = klinesRef.current;
    if (!list.length) return;

    const oldest = list[0].time;
    const segKey = `${symbol}-${tf}-h${oldest}`;
    const segCached = segmentsCacheRef.current.get(segKey);
    if (segCached) {
      // 段缓存永不失效（不可变数据）：直接合并，零网络。
      mergeOlderSegment(segCached.data, segCached.hasMore);
      return;
    }

    loadingMoreRef.current = true;
    try {
      const url = `/market/bars?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(tf)}&before=${oldest}`;
      const response = await apiFetch(url);
      if (!response.ok) throw response;
      // 竞态保护：请求期间用户切走了标的或周期，丢弃过期响应
      // （在途 15m 段绝不许落进已切到 1h 的序列）
      if (selectedSymbolRef.current !== symbol) return;
      if (tfForHistory(timeRangeRef.current) !== tf) return;

      const data = await response.json();
      const more: KlineData[] = data.map((k: any) => ({
        time: (typeof k.time === 'number' ? k.time : Math.floor(new Date(k.time).getTime() / 1000)) as UTCTimestamp,
        open: parseFloat(k.open),
        high: parseFloat(k.high),
        low: parseFloat(k.low),
        close: parseFloat(k.close),
        volume: parseFloat(k.volume || 0),
      }));
      const hasMore = response.headers.get('X-Bars-Has-More') === '1';

      if (segmentsCacheRef.current.size >= MAX_SEGMENT_ENTRIES) {
        const oldestSeg = segmentsCacheRef.current.keys().next().value;
        if (oldestSeg) segmentsCacheRef.current.delete(oldestSeg);
      }
      segmentsCacheRef.current.set(segKey, { data: more, hasMore });
      mergeOlderSegment(more, hasMore);
    } catch (err) {
      // 补载失败不打扰主图：静默降级，下个滑动手势可重试。
      logError(handleApiError(err));
    } finally {
      loadingMoreRef.current = false;
    }
  }, [timeRange]);
  // 图表初始化订阅持此 ref：回调永远调用最新的 loadOlder，不受 render 时序影响。
  loadOlderRef.current = loadOlder;

  // 合并更早的段到当前序列：按 time 去重、只增不减（纯函数在 klineMerge.ts，测试钉住）。
  const mergeOlderSegment = useCallback((older: KlineData[], hasMore: boolean) => {
    if (!older.length) {
      hasMoreRef.current = false;
      return;
    }
    hasMoreRef.current = hasMore;
    const merged = prependOlderSegment(klinesRef.current, older);
    if (merged === klinesRef.current) return; // 无新增（全部重复）
    klinesRef.current = merged;
    setKlines(merged);
  }, []);

  // 定时刷新（60秒间隔，避免速率限制）
  useEffect(() => {
    if (!selectedSymbol) return;
    const interval = setInterval(() => fetchKlines(selectedSymbol, INTERVAL_MAP[timeRange]), 60000);
    return () => clearInterval(interval);
  }, [selectedSymbol, timeRange, fetchKlines]);

  // 格式化时间
  const formatTime = useCallback((timestamp: number) => {
    const date = new Date(timestamp * 1000);
    return date.toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  }, []);

  // 初始化主图表
  useEffect(() => {
    if (!mainChartRef.current) return;

    // 等待布局稳定后初始化图表
    const initChart = () => {
      if (!mainChartRef.current) return null;
      
      const containerWidth = mainChartRef.current.clientWidth || mainChartRef.current.offsetWidth;
      const containerHeight = mainChartRef.current.clientHeight || mainChartRef.current.offsetHeight || 350;

      const chart = createChart(mainChartRef.current, {
        layout: {
          background: { type: ColorType.Solid, color: 'transparent' },
          textColor: CHART_COLORS.text,
          attributionLogo: false,
        },
        width: containerWidth,
        height: containerHeight,
        grid: {
          vertLines: { color: CHART_COLORS.grid, style: 1 },
          horzLines: { color: CHART_COLORS.grid, style: 1 },
        },
        timeScale: {
          timeVisible: true,
          secondsVisible: false,
          borderColor: CHART_COLORS.border,
          rightOffset: 12,
          barSpacing: 8,
          minBarSpacing: 1,
          fixLeftEdge: false,
          fixRightEdge: false,
        },
        rightPriceScale: {
          borderColor: CHART_COLORS.border,
          scaleMargins: { top: 0.05, bottom: 0.05 },
          autoScale: true,
        },
        crosshair: {
          mode: CrosshairMode.Normal,
          vertLine: {
            color: CHART_COLORS.accent,
            width: 1,
            style: 2,
            labelBackgroundColor: CHART_COLORS.bg,
          },
          horzLine: {
            color: CHART_COLORS.accent,
            width: 1,
            style: 2,
            labelBackgroundColor: CHART_COLORS.bg,
          },
        },
        handleScroll: {
          mouseWheel: true,
          pressedMouseMove: true,
          horzTouchDrag: true,
          vertTouchDrag: false,
        },
        handleScale: {
          axisPressedMouseMove: {
            time: true,
            price: true,
          },
          mouseWheel: true,
          pinch: true,
        },
      });

      return chart;
    };

    // 使用 requestAnimationFrame 确保布局完成后初始化
    const rafId = requestAnimationFrame(() => {
      const chart = initChart();
      if (!chart) return;

      mainChartApiRef.current = chart;

      // 左边界历史补载：可见区间左缘越过 <10 根剩余时触发一次补载。
      // subscribe 回调持有 ref，不随 render 重建；节流由 loadingMoreRef 保证。
      chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
        if (!range || range.from > 10) return;
        void loadOlderRef.current?.();
      });

      // 创建初始 series
      const candlestickSeries = chart.addSeries(CandlestickSeries, {
        upColor: CHART_COLORS.buy,
        downColor: CHART_COLORS.sell,
        borderVisible: false,
        wickUpColor: CHART_COLORS.buy,
        wickDownColor: CHART_COLORS.sell,
        priceLineVisible: true,
        lastValueVisible: true,
      });
      mainSeriesRef.current = candlestickSeries;

      // 十字光标订阅 (兼容蜡烛图与折线图模式)
      chart.subscribeCrosshairMove((param) => {
        if (!param.time || !param.seriesData || !mainSeriesRef.current) {
          setCrosshairData(null);
          return;
        }
        
        const data = param.seriesData.get(mainSeriesRef.current);
        const matchingKline = klinesRef.current.find(k => k.time === param.time);
        
        if (data && 'open' in data) {
          const kline = data as { open: number; high: number; low: number; close: number };
          setCrosshairData({
            time: formatTime(param.time as number),
            open: kline.open,
            high: kline.high,
            low: kline.low,
            close: kline.close,
            volume: matchingKline?.volume || 0,
            change: kline.close - kline.open,
            changePercent: ((kline.close - kline.open) / kline.open) * 100,
          });
        } else if (matchingKline) {
          // 折线图模式下同样完整展示 OHLC 及变动数据
          setCrosshairData({
            time: formatTime(param.time as number),
            open: matchingKline.open,
            high: matchingKline.high,
            low: matchingKline.low,
            close: matchingKline.close,
            volume: matchingKline.volume || 0,
            change: matchingKline.close - matchingKline.open,
            changePercent: ((matchingKline.close - matchingKline.open) / matchingKline.open) * 100,
          });
        }
      });

      setChartReady(true);
    });

    // 使用 ResizeObserver 监听容器大小变化
    const resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        if (mainChartApiRef.current && entry.target === mainChartRef.current) {
          const { width, height } = entry.contentRect;
          if (width > 0 && height > 0) {
            mainChartApiRef.current.applyOptions({ 
              width: Math.floor(width),
              height: Math.floor(height),
            });
          }
        }
      }
    });

    if (mainChartRef.current) {
      resizeObserver.observe(mainChartRef.current);
    }

    const handleResize = () => {
      if (mainChartRef.current && mainChartApiRef.current) {
        mainChartApiRef.current.applyOptions({ 
          width: mainChartRef.current.clientWidth,
          height: mainChartRef.current.clientHeight || 350,
        });
      }
    };

    window.addEventListener('resize', handleResize);

    return () => {
      cancelAnimationFrame(rafId);
      resizeObserver.disconnect();
      window.removeEventListener('resize', handleResize);
      mainSeriesRef.current = null;
      priceLinesRef.current = [];
      indicatorSeriesRef.current.clear();
      if (mainChartApiRef.current) {
        mainChartApiRef.current.remove();
        mainChartApiRef.current = null;
      }
      setChartReady(false);
    };
  }, [formatTime]);

  // 切换图表类型 (仅在 chartType 或 chartReady 变化时触发，不依赖 klines，消除频繁重建)
  useEffect(() => {
    const chart = mainChartApiRef.current;
    if (!chart || !chartReady) return;

    // 移除旧 series 及附着的价格线句柄
    if (mainSeriesRef.current) {
      priceLinesRef.current = [];
      try { chart.removeSeries(mainSeriesRef.current); } catch { /* ignore */ }
      mainSeriesRef.current = null;
    }

    // 创建新 series
    try {
      if (chartType === 'line') {
        mainSeriesRef.current = chart.addSeries(LineSeries, {
          color: CHART_COLORS.accent,
          lineWidth: 2,
          priceLineVisible: true,
          lastValueVisible: true,
          crosshairMarkerVisible: true,
          crosshairMarkerRadius: 4,
        });
      } else {
        mainSeriesRef.current = chart.addSeries(CandlestickSeries, {
          upColor: CHART_COLORS.buy,
          downColor: CHART_COLORS.sell,
          borderVisible: false,
          wickUpColor: CHART_COLORS.buy,
          wickDownColor: CHART_COLORS.sell,
          priceLineVisible: true,
          lastValueVisible: true,
        });
      }

      // 立即填充已有数据，并平滑保留用户的缩放视图区间
      if (klinesRef.current.length > 0) {
        const prevRange = chart.timeScale().getVisibleLogicalRange();
        if (chartType === 'line') {
          mainSeriesRef.current.setData(klinesRef.current.map(k => ({ time: k.time, value: k.close })));
        } else {
          mainSeriesRef.current.setData(klinesRef.current);
        }
        if (prevRange) {
          chart.timeScale().setVisibleLogicalRange(prevRange);
        } else if (isInitialFitRef.current) {
          chart.timeScale().fitContent();
          isInitialFitRef.current = false;
        }
      }
    } catch (err) {
      console.error('Failed to create series:', err);
    }
  }, [chartType, chartReady]);

  // 更新主数据 (仅在首度加载或主动重置时调用 fitContent，保护用户缩放与手势操作)
  useEffect(() => {
    if (!mainSeriesRef.current || !chartReady || klines.length === 0) return;

    try {
      if (chartType === 'line') {
        mainSeriesRef.current.setData(klines.map(k => ({ time: k.time, value: k.close })));
      } else {
        mainSeriesRef.current.setData(klines);
      }
      if (isInitialFitRef.current) {
        mainChartApiRef.current?.timeScale().fitContent();
        isInitialFitRef.current = false;
      }
    } catch (err) {
      console.error('Failed to update chart data:', err);
    }
  }, [klines, chartReady, chartType]);

  // 初始化成交量
  useEffect(() => {
    const chart = mainChartApiRef.current;
    if (!chart || !chartReady || !activeIndicators.has('VOL')) {
      if (volumeSeriesRef.current) {
        try { chart?.removeSeries(volumeSeriesRef.current); } catch { /* ignore */ }
        volumeSeriesRef.current = null;
      }
      return;
    }

    try {
      // 在主图表上添加成交量作为叠加层
      const volumeSeries = chart.addSeries(HistogramSeries, {
        color: CHART_COLORS.volumeUp,
        priceFormat: { type: 'volume' },
        priceScaleId: '', // 叠加模式
      });
      
      // 设置成交量在底部的边距
      volumeSeries.priceScale().applyOptions({
        scaleMargins: {
          top: 0.8, // 成交量占底部 20%
          bottom: 0,
        },
      });

      volumeSeriesRef.current = volumeSeries;

      if (klines.length > 0) {
        const volumeData = klines.map(k => ({
          time: k.time,
          value: k.volume,
          color: k.close >= k.open ? CHART_COLORS.volumeUp : CHART_COLORS.volumeDown,
        }));
        volumeSeries.setData(volumeData);
      }
    } catch (err) {
      console.error('Failed to init volume series:', err);
    }

    return () => {
      if (volumeSeriesRef.current) {
        try { chart.removeSeries(volumeSeriesRef.current); } catch { /* ignore */ }
        volumeSeriesRef.current = null;
      }
    };
  }, [activeIndicators, chartReady, klines.length]);

  // 更新成交量数据
  useEffect(() => {
    if (!volumeSeriesRef.current || klines.length === 0 || !activeIndicators.has('VOL')) return;

    try {
      const volumeData = klines.map(k => ({
        time: k.time,
        value: k.volume,
        color: k.close >= k.open ? CHART_COLORS.volumeUp : CHART_COLORS.volumeDown,
      }));
      volumeSeriesRef.current.setData(volumeData);
    } catch { /* ignore */ }
  }, [klines, activeIndicators]);

  // 维护长寿命指示器 series 实例，仅通过 setData 更新，彻底消除 Canvas/WebGL 销毁重建卡顿
  useEffect(() => {
    const chart = mainChartApiRef.current;
    if (!chart || !chartReady || klines.length === 0) return;

    // Helper: 获取或创建 LineSeries
    const getOrCreate = (key: string, options: any) => {
      let series = indicatorSeriesRef.current.get(key);
      if (!series) {
        series = chart.addSeries(LineSeries, options);
        indicatorSeriesRef.current.set(key, series);
      }
      return series;
    };

    // Helper: 移除某 key 的 series
    const removeIfPresent = (key: string) => {
      const series = indicatorSeriesRef.current.get(key);
      if (series) {
        try { chart.removeSeries(series); } catch { /* ignore */ }
        indicatorSeriesRef.current.delete(key);
      }
    };

    // 1. MA
    const maPeriods = [7, 25, 99];
    const maColors = [CHART_COLORS.ma7, CHART_COLORS.ma25, CHART_COLORS.ma99];
    if (activeIndicators.has('MA')) {
      maPeriods.forEach((period, i) => {
        const key = `ma${period}`;
        const data = calculateMA(klines, period);
        const series = getOrCreate(key, {
          color: maColors[i],
          lineWidth: 1,
          priceLineVisible: false,
          lastValueVisible: false,
        });
        series.setData(data);
      });
    } else {
      maPeriods.forEach(period => removeIfPresent(`ma${period}`));
    }

    // 2. EMA
    const emaPeriods = [12, 26];
    const emaColors = [CHART_COLORS.ema12, CHART_COLORS.ema26];
    if (activeIndicators.has('EMA')) {
      emaPeriods.forEach((period, i) => {
        const key = `ema${period}`;
        const data = calculateEMA(klines, period);
        const series = getOrCreate(key, {
          color: emaColors[i],
          lineWidth: 1,
          priceLineVisible: false,
          lastValueVisible: false,
        });
        series.setData(data);
      });
    } else {
      emaPeriods.forEach(period => removeIfPresent(`ema${period}`));
    }

    // 3. BOLL
    const bollKeys = ['bollUpper', 'bollMiddle', 'bollLower'];
    if (activeIndicators.has('BOLL')) {
      const boll = calculateBOLL(klines);
      const bollConfigs = [
        { key: 'bollUpper', color: CHART_COLORS.bollUpper, style: 2, data: boll.upper },
        { key: 'bollMiddle', color: CHART_COLORS.bollMiddle, style: 0, data: boll.middle },
        { key: 'bollLower', color: CHART_COLORS.bollLower, style: 2, data: boll.lower },
      ];
      bollConfigs.forEach(({ key, color, style, data }) => {
        const series = getOrCreate(key, {
          color,
          lineWidth: 1,
          lineStyle: style,
          priceLineVisible: false,
          lastValueVisible: false,
        });
        series.setData(data);
      });
    } else {
      bollKeys.forEach(k => removeIfPresent(k));
    }
  }, [klines, activeIndicators, chartReady]);

  // 添加价格线（触发器和挂单）
  useEffect(() => {
    if (!mainSeriesRef.current || !chartReady) return;

    try {
      const series = mainSeriesRef.current;
      
      // 清除旧价格线，避免叠加泄露
      priceLinesRef.current.forEach(line => {
        try { series.removePriceLine(line); } catch { /* ignore */ }
      });
      priceLinesRef.current = [];

      // 添加触发器价格线
      triggers.filter(t => t.enabled && t.symbol === selectedSymbol).forEach(trigger => {
        const triggerPrice = trigger.triggerPrice || trigger.condition.threshold;
        const side = trigger.action.side;
        const type = trigger.action.type;
        
        const line = series.createPriceLine({
          price: parseFloat(triggerPrice),
          color: side === 'buy' ? CHART_COLORS.buy : CHART_COLORS.sell,
          lineWidth: 1,
          lineStyle: 2,
          axisLabelVisible: true,
          title: `T: ${type.toUpperCase()}${side ? ` ${side.toUpperCase()}` : ''}`,
        });
        priceLinesRef.current.push(line);
      });

      // 添加挂单价格线
      openOrders.filter(o => o.symbol === selectedSymbol && o.price).forEach(order => {
        const line = series.createPriceLine({
          price: parseFloat(order.price!),
          color: order.side === 'buy' ? CHART_COLORS.buy : CHART_COLORS.sell,
          lineWidth: 1,
          lineStyle: 1,
          axisLabelVisible: true,
          title: `${order.side.toUpperCase()} @ ${order.price}`,
        });
        priceLinesRef.current.push(line);
      });
    } catch { /* ignore */ }
  }, [triggers, openOrders, selectedSymbol, chartReady]);

  // 实时增量更新最新 K 线与价格线 (通过 series.update 进行 O(1) 增量推流，不触发全图重建或重置缩放)
  useEffect(() => {
    if (!chartReady || !mainSeriesRef.current || !selectedSymbol) return;

    const intervalSec = TIMEFRAME_SECONDS[timeRange] || 900;
    let lastLivePriceTs = 0;

    // 辅助函数：根据实时 tick (price, timestamp, volume) 增量更新当前 K 线
    // volumeMode: 'add' 用于逐笔成交累加，'set' 用于后端权威 bar 覆盖
    const applyRealtimeTick = (
      price: number,
      timeMs: number,
      volume: number = 0,
      volumeMode: 'add' | 'set' = 'add',
    ) => {
      if (!mainSeriesRef.current || !price || isNaN(price) || price <= 0) return;

      const tradeSec = Math.floor(timeMs / 1000);
      const bucketTime = (Math.floor(tradeSec / intervalSec) * intervalSec) as UTCTimestamp;
      const klinesList = klinesRef.current;

      if (klinesList.length === 0) return;

      const lastIndex = klinesList.length - 1;
      const lastKline = klinesList[lastIndex];

      // 情况 1: 该 tick 仍在当前蜡烛周期内（包含美股开盘等自然偏移桶）
      if (lastKline && (bucketTime === lastKline.time || (tradeSec >= lastKline.time && tradeSec < lastKline.time + intervalSec))) {
        lastKline.high = Math.max(lastKline.high, price);
        lastKline.low = Math.min(lastKline.low, price);
        lastKline.close = price;
        lastKline.volume = volumeMode === 'set' ? volume : (lastKline.volume || 0) + (volume || 0);

        if (chartType === 'line') {
          mainSeriesRef.current.update({ time: lastKline.time, value: price });
        } else {
          mainSeriesRef.current.update({
            time: lastKline.time,
            open: lastKline.open,
            high: lastKline.high,
            low: lastKline.low,
            close: lastKline.close,
          });
        }

        if (volumeSeriesRef.current && activeIndicators.has('VOL')) {
          volumeSeriesRef.current.update({
            time: lastKline.time,
            value: lastKline.volume,
            color: lastKline.close >= lastKline.open ? CHART_COLORS.volumeUp : CHART_COLORS.volumeDown,
          });
        }
      } 
      // 情况 2: 时间跨入新的蜡烛周期，顺延对齐追加新蜡烛。
      // 仅限分钟/小时桶：日/周/月的日历边界不等长（31 天月 ≠ 30 天），
      // 固定秒数顺延会画出错位蜡烛；这些视图由 60s 轮询整段刷新兜底。
      else if (lastKline && intervalSec < 86400 && tradeSec >= lastKline.time + intervalSec) {
        const offsetBuckets = Math.floor((tradeSec - lastKline.time) / intervalSec);
        const newBarTime = (lastKline.time + offsetBuckets * intervalSec) as UTCTimestamp;
        const newKline: KlineData = {
          time: newBarTime,
          open: price,
          high: price,
          low: price,
          close: price,
          volume: volume || 0,
        };
        klinesList.push(newKline);

        if (chartType === 'line') {
          mainSeriesRef.current.update({ time: newKline.time, value: price });
        } else {
          mainSeriesRef.current.update({
            time: newKline.time,
            open: newKline.open,
            high: newKline.high,
            low: newKline.low,
            close: newKline.close,
          });
        }

        if (volumeSeriesRef.current && activeIndicators.has('VOL')) {
          volumeSeriesRef.current.update({
            time: newKline.time,
            value: newKline.volume,
            color: CHART_COLORS.volumeUp,
          });
        }
      }

      // 更新顶部实时现价（节流到 4Hz，避免每笔成交都触发整树重渲染）
      const now = performance.now();
      if (now - lastLivePriceTs >= 250) {
        lastLivePriceTs = now;
        setLivePrice(price);
      }
    };

    // 1. 订阅实时成交推流 (TRADE_UPDATE)
    const unsubTrade = useMarketStore.subscribe(
      (state) => state.latestTrade,
      (trade) => {
        if (!trade || trade.symbol?.toUpperCase() !== selectedSymbol.toUpperCase()) return;
        const p = parseFloat(trade.price);
        const v = parseFloat(trade.quantity || '0');
        if (p > 0) {
          applyRealtimeTick(p, trade.time || Date.now(), v);
        }
      }
    );

    // 2. 订阅后端推送的实时 Bar (BAR_UPDATE)
    const unsubBar = useMarketStore.subscribe(
      (state) => state.latestBar,
      (bar) => {
        if (!bar || bar.symbol?.toUpperCase() !== selectedSymbol.toUpperCase()) return;
        const barTime = (typeof bar.time === 'number' ? bar.time : Math.floor(new Date(bar.time).getTime() / 1000)) as UTCTimestamp;
        const barClose = parseFloat(bar.close as any);
        if (barClose > 0) {
          applyRealtimeTick(barClose, barTime * 1000, parseFloat((bar.volume as any) || 0), 'set');
        }
      }
    );

    return () => {
      unsubTrade();
      unsubBar();
    };
  }, [chartReady, chartType, timeRange, selectedSymbol, activeIndicators]);

  // 价格信息计算：只统计最近一个交易日的 K 线，涨跌对照前一日收盘。
  // 旧实现把整个 K 线窗口（15m 约 10 个交易日）当成"24H"，高低点和
  // 涨跌幅都是窗口口径，收盘后还把 Yahoo 的 forming 尾根算进去。
  const priceInfo = useMemo(() => {
    const list = klinesRef.current.length > 0 ? klinesRef.current : klines;
    if (list.length === 0) return null;
    return computeSessionStats(list, marketTimezoneFor(selectedSymbol), livePrice ?? null);
  }, [klines, livePrice, selectedSymbol]);

  // 重置图表视图
  const handleReset = useCallback(() => {
    mainChartApiRef.current?.timeScale().fitContent();
  }, []);

  // 切换全屏
  const toggleFullscreen = useCallback(() => {
    setIsFullscreen(prev => !prev);
  }, []);

  const indicators: Indicator[] = ['MA', 'EMA', 'BOLL', 'VOL'];

  return (
    <div className={`card ${styles.container} ${isFullscreen ? styles.fullscreen : ''} ${isMobile ? styles.mobile : ''}`}>
      {/* 头部工具栏 */}
      <div className={styles.toolbar}>
        <div className={styles.toolbarScrollArea}>
          {/* 指标按钮 */}
          <div className={styles.indicatorGroup}>
            {indicators.map((indicator) => (
              <button
                key={indicator}
                className={`${styles.indicatorBtn} ${activeIndicators.has(indicator) ? styles.active : ''}`}
                onClick={() => toggleIndicator(indicator)}
              >
                {indicator}
              </button>
            ))}
          </div>
          
          {/* 图表类型 */}
          <div className={styles.chartTypeGroup}>
            <button
              className={`${styles.chartTypeBtn} ${chartType === 'line' ? styles.active : ''}`}
              onClick={() => setChartType('line')}
              title={t.chart?.lineChart || 'Line Chart'}
            >
              <Icon name="activity" size="sm" />
            </button>
            <button
              className={`${styles.chartTypeBtn} ${chartType === 'candlestick' ? styles.active : ''}`}
              onClick={() => setChartType('candlestick')}
              title={t.chart?.candlestickChart || 'Candlestick Chart'}
            >
              <Icon name="bar-chart-2" size="sm" />
            </button>
          </div>

          {/* 时间周期 */}
          <div className={styles.timeRangeGroup}>
            {(['1m', '5m', '15m', '1h', '4h', '1d', '1wk', '1mo'] as TimeRange[]).map((range) => (
              <button
                key={range}
                className={`${styles.timeRangeBtn} ${timeRange === range ? styles.active : ''}`}
                onClick={() => setTimeRange(range)}
                title={t.chart?.rangeHints?.[range]}
              >
                {range === '1wk' ? (t.chart?.rangeLabel1wk ?? '1wk')
                  : range === '1mo' ? (t.chart?.rangeLabel1mo ?? '1mo')
                  : range}
              </button>
            ))}
          </div>
        </div>

        <div className={styles.toolbarRight}>
          <button className={styles.toolBtn} onClick={handleReset} title="Reset">
            <Icon name="refresh-cw" size="sm" />
          </button>
          <button className={styles.toolBtn} onClick={toggleFullscreen} title="Fullscreen">
            <Icon name={isFullscreen ? 'minimize-2' : 'maximize-2'} size="sm" />
          </button>
        </div>
      </div>

      {/* OHLC 信息栏 */}
      {(!isMobile || crosshairData) && (
        <div className={styles.ohlcBar}>
          <div className={styles.symbolInfo}>
            {!isMobile && <span className={styles.symbolName}>{selectedSymbol}</span>}
            {priceInfo && (
              <>
                <span className={styles.currentPrice}>
                  {priceInfo.current.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </span>
                <span className={`${styles.priceChange} ${priceInfo.change >= 0 ? styles.up : styles.down}`}>
                  {priceInfo.change >= 0 ? '+' : ''}{priceInfo.changePercent.toFixed(2)}%
                </span>
              </>
            )}
          </div>
          
          {/* OHLCV 数据 */}
          <div className={styles.ohlcData}>
            {crosshairData ? (
              <>
                <span className={styles.ohlcItem}><label>O</label>{crosshairData.open.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
                <span className={styles.ohlcItem}><label>H</label>{crosshairData.high.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
                <span className={styles.ohlcItem}><label>L</label>{crosshairData.low.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
                <span className={`${styles.ohlcItem} ${crosshairData.change >= 0 ? styles.up : styles.down}`}>
                  <label>C</label>{crosshairData.close.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </span>
                <span className={styles.ohlcItem}><label>V</label>{(crosshairData.volume / 1000).toFixed(2)}K</span>
              </>
            ) : priceInfo && (
              <>
                <span className={styles.ohlcItem}><label>{isMobile ? 'H' : (t.chart?.dayHigh ?? 'Day High')}</label>{priceInfo.high.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
                <span className={styles.ohlcItem}><label>{isMobile ? 'L' : (t.chart?.dayLow ?? 'Day Low')}</label>{priceInfo.low.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
                {!isMobile && (
                  <>
                    <span className={styles.ohlcItem}><label>{t.chart?.dayVol ?? 'Day Vol'}</label>{(priceInfo.volume / 1000000).toFixed(2)}M</span>
                    <span className={styles.ohlcItem}><label>Amp</label>{priceInfo.amplitude.toFixed(2)}%</span>
                  </>
                )}
              </>
            )}
          </div>
        </div>
      )}

      {/* 指标图例 */}
      {(activeIndicators.has('MA') || activeIndicators.has('EMA') || activeIndicators.has('BOLL')) && (
        <div className={styles.legend}>
          {activeIndicators.has('MA') && (
            <>
              <span style={{ color: CHART_COLORS.ma7 }}>●MA(7)</span>
              <span style={{ color: CHART_COLORS.ma25 }}>●MA(25)</span>
              <span style={{ color: CHART_COLORS.ma99 }}>●MA(99)</span>
            </>
          )}
          {activeIndicators.has('EMA') && (
            <>
              <span style={{ color: CHART_COLORS.ema12 }}>●EMA(12)</span>
              <span style={{ color: CHART_COLORS.ema26 }}>●EMA(26)</span>
            </>
          )}
          {activeIndicators.has('BOLL') && (
            <span style={{ color: CHART_COLORS.bollMiddle }}>●BOLL(20,2)</span>
          )}
        </div>
      )}

      {/* 主图表 */}
      <div className={styles.mainChart} ref={mainChartRef}>
        {loading && (
          <div className={styles.loadingOverlay}>
            <div className={styles.spinner} />
            <span>加载中...</span>
          </div>
        )}
        {error && !loading && (
          <div className={styles.errorOverlay}>
            <Icon name="alert-circle" size="lg" />
            <span>{error}</span>
            <button className={styles.retryBtn} onClick={() => fetchKlines(selectedSymbol, INTERVAL_MAP[timeRange])}>
              重试
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
