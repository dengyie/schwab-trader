import { useCallback, useMemo, useRef, useEffect, useState } from 'react';
import { 
  useWatchlistStore, 
  selectFilteredSymbols, 
  selectFavorites, 
  selectPinned,
  selectSelectedSymbol,
  selectSearchQuery,
  selectShowFavoritesOnly,
  type SymbolInfo 
} from '../../store/watchlistStore';
import { useTradingStore, selectPositions } from '../../store/tradingStore';
import { useMarketsStore, MARKET_CODES } from '../../store/marketsStore';
import { useI18n } from '../../i18n';
import { Icon } from '../Icon';
import { apiFetch } from '../../services/apiClient';
import { formatPriceByDecimals, currencySymbol, currencyDecimals } from '../../utils/money';
import styles from './Watchlist.module.css';
import Decimal from 'decimal.js';

// SVG 图标组件
function StarIcon({ filled }: { filled: boolean }) {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill={filled ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="2">
      <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
    </svg>
  );
}

function PinIcon({ filled }: { filled: boolean }) {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill={filled ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="2">
      <line x1="12" y1="17" x2="12" y2="22" />
      <path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z" />
    </svg>
  );
}

function SearchIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="11" cy="11" r="8" />
      <line x1="21" y1="21" x2="16.65" y2="16.65" />
    </svg>
  );
}

function ClearIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  );
}

interface WatchlistItemProps {
  symbol: SymbolInfo;
  isSelected: boolean;
  isFavorite: boolean;
  isPinned: boolean;
  position?: { quantity: string; avgEntryPrice: string; unrealizedPnl: string };
  onSelect: () => void;
  onToggleFavorite: () => void;
  onTogglePinned: () => void;
  isCollapsed?: boolean;
}

function WatchlistItem({ 
  symbol, 
  isSelected, 
  isFavorite,
  isPinned,
  position,
  onSelect, 
  onToggleFavorite,
  onTogglePinned: _onTogglePinned,
  isCollapsed = false,
}: WatchlistItemProps) {
  const { t } = useI18n();
  const changeBadgeClass = useMemo(() => {
    if (!symbol || symbol.priceChange24h === undefined) return '';
    return symbol.priceChange24h > 0 ? styles.changeUp : symbol.priceChange24h < 0 ? styles.changeDown : '';
  }, [symbol]);

  const pnlPercent = useMemo(() => {
    if (!position || !symbol || !symbol.price) return null;
    try {
      const currentPrice = new Decimal(symbol.price);
      const avgPrice = new Decimal(position.avgEntryPrice || 0);
      if (avgPrice.lte(0) || currentPrice.lte(0)) return null;
      return currentPrice.minus(avgPrice).div(avgPrice).times(100);
    } catch {
      return null;
    }
  }, [position, symbol]);

  const hasPrice = symbol.price && parseFloat(symbol.price) > 0;

  if (!symbol) return null;

  if (isCollapsed) {
    return (
      <div 
        className={`${styles.item} ${styles.itemCollapsed} ${isSelected ? styles.selected : ''}`}
        onClick={onSelect}
        title={`${symbol.baseAsset}/${symbol.quoteAsset}`}
      >
        <div className={styles.symbolIconCollapsed}>
          {symbol.baseAsset?.charAt(0)}
        </div>
        {position && parseFloat(position.quantity || '0') > 0 && (
          <div className={styles.positionIndicator} />
        )}
      </div>
    );
  }

  return (
    <div 
      className={`${styles.item} ${isSelected ? styles.selected : ''} ${isCollapsed ? styles.itemCollapsed : ''}`}
      onClick={onSelect}
    >
      <div className={styles.itemLeft}>
        {!isCollapsed && (
          <button 
            className={`${styles.favoriteBtn} ${isFavorite ? styles.active : ''}`}
            onClick={(e) => { e.stopPropagation(); onToggleFavorite(); }}
          >
            <StarIcon filled={isFavorite} />
          </button>
        )}
        <div className={styles.symbolInfo}>
          <div className={styles.symbolHeader}>
            {isCollapsed ? (
              <div className={styles.symbolIconCollapsed} title={`${symbol.baseAsset}/${symbol.quoteAsset}`}>
                {symbol.baseAsset?.charAt(0)}
              </div>
            ) : (
              <div className={styles.symbolName}>
                {isPinned && <PinIcon filled={true} />}
                {symbol.baseAsset || '--'}
              </div>
            )}
            {!isCollapsed && symbol.quoteAsset && symbol.quoteAsset !== 'USD' && <div className={styles.symbolQuote}>/{symbol.quoteAsset}</div>}
          </div>
          {position && parseFloat(position.quantity || '0') > 0 && (
            isCollapsed ? (
              <div className={styles.positionIndicator} />
            ) : (
              <div className={styles.pnlRow}>
                <span className={styles.pnlLabel}>PnL:</span>
                <span className={`${styles.pnlValue} ${pnlPercent && pnlPercent.gte(0) ? styles.priceUp : styles.priceDown}`}>
                  {pnlPercent ? (pnlPercent.gte(0) ? '+' : '') : ''}{pnlPercent ? pnlPercent.toFixed(2) : '--'}%
                </span>
              </div>
            )
          )}
        </div>
      </div>
      {!isCollapsed && (
        <div className={styles.itemRight}>
          <div className={`${styles.price} tabular-nums ${symbol.isStale ? styles.stalePrice : ''}`}
            title={symbol.isStale ? (t.watchlist?.stalePrice || 'No recent trade') : undefined}>
            {hasPrice ? (
              <>
                <span className={styles.priceCurrency}>{currencySymbol(symbol.quoteAsset)}</span>
                {formatPriceByDecimals(
                  parseFloat(symbol.price!),
                  currencyDecimals(symbol.quoteAsset, parseFloat(symbol.price!) < 1 ? 4 : 2),
                )}
              </>
            ) : (
              <span className={styles.loading}>...</span>
            )}
          </div>
          {symbol.priceChange24h !== undefined ? (
            <div className={`${styles.priceChange} ${changeBadgeClass} tabular-nums`}>
              {symbol.priceChange24h > 0 ? '+' : ''}{symbol.priceChange24h.toFixed(2)}%
            </div>
          ) : (
            <div className={`${styles.priceChange} ${styles.loading} tabular-nums`}>--%</div>
          )}
        </div>
      )}
    </div>
  );
}

interface WatchlistProps {
  onSymbolChange?: (symbol: string) => void;
  isCollapsed?: boolean;
  compact?: boolean;
}

export function Watchlist({ onSymbolChange, isCollapsed = false, compact: _compact = false }: WatchlistProps) {
  const { t } = useI18n();
  const inputRef = useRef<HTMLInputElement>(null);
  
  const activeMarket = useMarketsStore((s) => s.activeMarket);
  const setActiveMarket = useMarketsStore((s) => s.setActiveMarket);
  const markets = useMarketsStore((s) => s.markets);
  // 当前市场的后端行情行：{symbol -> {price, priceChangePercent, currency}}
  const [catalogTickers, setCatalogTickers] = useState<Record<string, { price: number; changePct: number; currency?: string; isStale?: boolean }>>({});

  const filteredSymbols = useWatchlistStore(selectFilteredSymbols);
  const favorites = useWatchlistStore(selectFavorites);
  const pinned = useWatchlistStore(selectPinned);
  const selectedSymbol = useWatchlistStore(selectSelectedSymbol);
  const searchQuery = useWatchlistStore(selectSearchQuery);
  const showFavoritesOnly = useWatchlistStore(selectShowFavoritesOnly);
  const positions = useTradingStore(selectPositions);
  const updateSymbolPrice = useWatchlistStore(state => state.updateSymbolPrice);
  
  // 切换市场时，把该市场的 catalog 注入 watchlist，作为可交易标的列表。
  const marketCatalog = useMemo(() => {
    const def = markets.find((m) => m.code === activeMarket);
    if (def && def.catalog && def.catalog.length > 0) {
      return def.catalog.map((symbol) => ({ symbol, baseAsset: symbol, quoteAsset: def.currency }));
    }
    return null;
  }, [markets, activeMarket]);

  // 显示列表：优先用当前市场 catalog；取不到时才回退到本地 watchlist。
  const displaySymbols = useMemo<SymbolInfo[]>(() => {
    let list = marketCatalog ?? filteredSymbols;

    if (showFavoritesOnly) {
      list = list.filter((s) => favorites.includes(s.symbol));
    }

    const query = searchQuery.trim().toLowerCase();
    if (query) {
      list = list.filter(
        (s) =>
          s.symbol.toLowerCase().includes(query) ||
          (s.baseAsset || '').toLowerCase().includes(query),
      );
    }

    return list;
  }, [marketCatalog, filteredSymbols, showFavoritesOnly, favorites, searchQuery]);

  // catalog 行只有符号，价格来自当前市场的行情接口，按符号合并后再渲染。
  const displayRows = useMemo<SymbolInfo[]>(
    () =>
      displaySymbols.map((info) => {
        const ticker = catalogTickers[info.symbol];
        if (!ticker) return info;
        return {
          ...info,
          price: ticker.price ? String(ticker.price) : info.price,
          priceChange24h: ticker.changePct,
          quoteAsset: ticker.currency || info.quoteAsset,
          isStale: ticker.isStale,
        };
      }),
    [displaySymbols, catalogTickers],
  );

  // 拉取当前市场全部标的价格（对接多市场后端行情）。
  // 节奏跟随该市场的交易时钟：开盘中 15s；收盘/午休 60s —— 收盘后
  // 每行是昨天的定格价，15s 轮询只是空转打后端（/api/markets 的
  // clock 随 markets payload 一起返回）。
  useEffect(() => {
    let isSubscribed = true;
    let timer: ReturnType<typeof setTimeout> | undefined;

    // 条件请求：行情未变时 304 免下载；etag 随组件生命周期保存在 ref 里。
    const etagRef = { current: undefined as string | undefined };

    const fetchPrices = async () => {
      try {
        const headers: Record<string, string> = {};
        if (etagRef.current) headers['If-None-Match'] = etagRef.current;
        const response = await apiFetch(`/market/tickers?market=${encodeURIComponent(activeMarket)}`, { headers });
        if (response.status === 304) {
          return; // 行情未变，沿用现有 catalogTickers
        }
        if (!response.ok || !isSubscribed) {
          return;
        }
        etagRef.current = response.headers.get('etag') || undefined;
        const data = await response.json();

        const rows: Record<string, { price: number; changePct: number; currency?: string; isStale?: boolean }> = {};
        for (const ticker of data) {
          const price = parseFloat(ticker.price) || 0;
          const changePct = parseFloat(ticker.priceChangePercent) || 0;
          rows[ticker.symbol] = { price, changePct, currency: ticker.currency, isStale: ticker.isStale === true };
          // 同步本地 watchlist 的价格（供收藏/持仓徽标使用）
          updateSymbolPrice(ticker.symbol, price ? String(price) : '0', changePct);
        }
        if (isSubscribed) setCatalogTickers(rows);
      } catch (err) {
        console.error('Failed to fetch prices:', err);
      }
    };

    const marketDef = markets.find((m) => m.code === activeMarket);
    const tickPace = () => (marketDef?.clock?.is_open === true ? 15000 : 60000);

    const schedule = () => {
      if (!isSubscribed) return;
      timer = setTimeout(async () => {
        await fetchPrices();
        schedule();
      }, tickPace());
    };

    fetchPrices().then(schedule);

    return () => {
      isSubscribed = false;
      if (timer) clearTimeout(timer);
    };
  }, [activeMarket, updateSymbolPrice, markets]);

  const getPosition = useCallback((symbol: string) => {
    if (positions instanceof Map) {
      return positions.get(symbol);
    }
    // Fallback if positions is not a Map (e.g. during hydration or if storage is corrupted)
    if (typeof positions === 'object' && positions !== null) {
      return (positions as Record<string, { quantity: string; avgEntryPrice: string; unrealizedPnl: string }>)[symbol];
    }
    return undefined;
  }, [positions]);

  const {
    setSelectedSymbol,
    setSearchQuery,
    setShowFavoritesOnly,
    toggleFavorite,
    togglePinned,
    addSymbol,
  } = useWatchlistStore();

  // 选中当前市场的第一个标的，确保切市场后图表/下单不会指向空符号。
  useEffect(() => {
    const catalog = markets.find((m) => m.code === activeMarket)?.catalog || [];
    const list = catalog.length > 0 ? catalog : displaySymbols.map((s) => s.symbol);
    const first = list[0];
    if (first && !list.includes(selectedSymbol)) {
      setSelectedSymbol(first);
      onSymbolChange?.(first);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeMarket]);

  const handleSymbolSelect = useCallback((symbol: string) => {
    setSelectedSymbol(symbol);
    onSymbolChange?.(symbol);
  }, [setSelectedSymbol, onSymbolChange]);

  const handleSearchChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    setSearchQuery(e.target.value);
  }, [setSearchQuery]);

  const handleClearSearch = useCallback(() => {
    setSearchQuery('');
    inputRef.current?.focus();
  }, [setSearchQuery]);

  // 任一代码直达：query 不在当前目录时，走后端 resolve+Yahoo 探测。
  // 命中即加入列表并选中；未命中保持 "No matching symbols" 原状。
  const [lookupState, setLookupState] = useState<'idle' | 'probing' | 'miss'>('idle');
  const handleLookupOpen = useCallback(async (raw: string) => {
    setLookupState('probing');
    try {
      const response = await apiFetch(
        `/market/symbol-lookup?symbol=${encodeURIComponent(raw)}&market=${encodeURIComponent(activeMarket)}`,
      );
      if (!response.ok) {
        setLookupState('miss');
        return;
      }
      const found = (await response.json()) as { symbol: string; currency?: string };
      addSymbol({ symbol: found.symbol, baseAsset: found.symbol, quoteAsset: found.currency || '' });
      setSelectedSymbol(found.symbol);
      onSymbolChange?.(found.symbol);
      setSearchQuery('');
      setLookupState('idle');
    } catch {
      setLookupState('miss');
    }
  }, [activeMarket, addSymbol, setSelectedSymbol, onSymbolChange, setSearchQuery]);
  const currentIndexRef = useRef(-1);
  currentIndexRef.current = displaySymbols.findIndex((s) => s.symbol === selectedSymbol);

  // 键盘导航
  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      // Enter on a non-catalog query probes the symbol-lookup endpoint:
      // any valid Yahoo code (BRK-B, 9984.T, bare 002415 under a CN hint)
      // opens directly instead of dying in "No matching symbols".
      const query = searchQuery.trim();
      const exact = displaySymbols.find((s) => s.symbol.toLowerCase() === query.toLowerCase());
      if (query && !exact) {
        e.preventDefault();
        void handleLookupOpen(query);
      } else if (currentIndexRef.current >= 0) {
        e.preventDefault();
        onSymbolChange?.(selectedSymbol);
      }
      return;
    }
    if (displaySymbols.length === 0) return;

    const currentIndex = displaySymbols.findIndex(s => s.symbol === selectedSymbol);

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      const nextIndex = currentIndex < displaySymbols.length - 1 ? currentIndex + 1 : 0;
      const nextSymbol = displaySymbols[nextIndex];
      if (nextSymbol) handleSymbolSelect(nextSymbol.symbol);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      const prevIndex = currentIndex > 0 ? currentIndex - 1 : displaySymbols.length - 1;
      const prevSymbol = displaySymbols[prevIndex];
      if (prevSymbol) handleSymbolSelect(prevSymbol.symbol);
    }
  }, [displaySymbols, selectedSymbol, handleSymbolSelect, onSymbolChange, searchQuery, handleLookupOpen]);

  return (
    <div className={`card ${styles.container} ${isCollapsed ? styles.collapsed : ''}`}>
      {!isCollapsed && (
        <div className="card-header">
          <span>{t.watchlist?.title || 'Watchlist'}</span>
          <button 
            className={`${styles.filterBtn} ${showFavoritesOnly ? styles.active : ''}`}
            onClick={() => setShowFavoritesOnly(!showFavoritesOnly)}
            title={showFavoritesOnly ? t.watchlist?.showAll : t.watchlist?.showFavorites}
          >
            <StarIcon filled={showFavoritesOnly} />
            <span>{showFavoritesOnly ? t.watchlist?.favorites || 'Favorites Only' : t.watchlist?.all || 'All'}</span>
          </button>
        </div>
      )}
      
      {/* 市场切换：US / HK / JP / CN */}
      {!isCollapsed && (
        <div className={styles.marketTabs} role="tablist" aria-label="Market">
          {MARKET_CODES.map((code) => {
            const def = markets.find((m) => m.code === code);
            const label = def?.name || code;
            return (
              <button
                key={code}
                role="tab"
                aria-selected={activeMarket === code}
                className={`${styles.marketTab} ${activeMarket === code ? styles.active : ''}`}
                onClick={() => setActiveMarket(code)}
                title={def ? `${label} · ${def.currency}` : code}
              >
                {code}
              </button>
            );
          })}
        </div>
      )}

      {/* 搜索框 */}
      {!isCollapsed && (
        <div className={styles.searchWrapper}>
          <div className={styles.searchIcon}>
            <SearchIcon />
          </div>
          <input
            ref={inputRef}
            type="text"
            className={styles.searchInput}
            placeholder={t.watchlist?.searchPlaceholder || 'Search symbols...'}
            value={searchQuery}
            onChange={handleSearchChange}
            onKeyDown={handleKeyDown}
          />
          {searchQuery && (
            <button className={styles.clearBtn} onClick={handleClearSearch}>
              <ClearIcon />
            </button>
          )}
        </div>
      )}

      {/* 列表 */}
      <div className={styles.list}>
        {displayRows.length > 0 ? (
          displayRows.map((symbol) => (
            <WatchlistItem
              key={symbol.symbol}
              symbol={symbol}
              isSelected={selectedSymbol === symbol.symbol}
              isFavorite={favorites.includes(symbol.symbol)}
              isPinned={pinned.includes(symbol.symbol)}
              position={getPosition(symbol.symbol)}
              onSelect={() => handleSymbolSelect(symbol.symbol)}
              onToggleFavorite={() => toggleFavorite(symbol.symbol)}
              onTogglePinned={() => togglePinned(symbol.symbol)}
              isCollapsed={isCollapsed}
            />
          ))
        ) : (
          <div className={styles.empty}>
            {isCollapsed ? <Icon name="search" size="sm" /> : (lookupState === 'probing'
              ? (t.watchlist?.probing || 'Looking up…')
              : lookupState === 'miss' && searchQuery
                ? (t.watchlist?.noSuchSymbol || 'Unknown symbol — check the code, e.g. BRK-B / 9984.T / 002415')
                : (searchQuery
                  ? (t.watchlist?.noResults || 'No matching symbols')
                  : (t.watchlist?.empty || 'No symbols in watchlist')))}
          </div>
        )}
      </div>

      {/* 底部统计 */}
      {!isCollapsed && (
        <div className={styles.footer}>
          <span className={styles.count}>
            {displayRows.length} {t.watchlist?.symbols || 'symbols'}
          </span>
        </div>
      )}
    </div>
  );
}
