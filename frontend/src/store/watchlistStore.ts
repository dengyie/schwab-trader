import { create } from 'zustand';
import { persist } from 'zustand/middleware';

// 交易对数据
export interface SymbolInfo {
  symbol: string;
  baseAsset: string;
  quoteAsset: string;
  price?: string;
  priceChange24h?: number;
  change24h?: string;
  volume24h?: string;
  sparkline?: number[]; // 24h price points for sparkline
}

// 默认交易对列表（美股核心大盘与科技成长标的）
const DEFAULT_SYMBOLS: SymbolInfo[] = [
  { symbol: 'SPY', baseAsset: 'SPY', quoteAsset: 'USD' },
  { symbol: 'QQQ', baseAsset: 'QQQ', quoteAsset: 'USD' },
  { symbol: 'AAPL', baseAsset: 'AAPL', quoteAsset: 'USD' },
  { symbol: 'MSFT', baseAsset: 'MSFT', quoteAsset: 'USD' },
  { symbol: 'NVDA', baseAsset: 'NVDA', quoteAsset: 'USD' },
  { symbol: 'TSLA', baseAsset: 'TSLA', quoteAsset: 'USD' },
  { symbol: 'AMD', baseAsset: 'AMD', quoteAsset: 'USD' },
  { symbol: 'AMZN', baseAsset: 'AMZN', quoteAsset: 'USD' },
  { symbol: 'GOOGL', baseAsset: 'GOOGL', quoteAsset: 'USD' },
  { symbol: 'META', baseAsset: 'META', quoteAsset: 'USD' },
  { symbol: 'IWM', baseAsset: 'IWM', quoteAsset: 'USD' },
  { symbol: 'COIN', baseAsset: 'COIN', quoteAsset: 'USD' },
  { symbol: 'PLTR', baseAsset: 'PLTR', quoteAsset: 'USD' },
  { symbol: 'NFLX', baseAsset: 'NFLX', quoteAsset: 'USD' },
];

interface WatchlistState {
  // 交易对列表
  symbols: SymbolInfo[];
  
  // 收藏集合
  favorites: string[];
  
  // 置顶集合
  pinned: string[];
  
  // 当前选中的交易对
  selectedSymbol: string;
  
  // 搜索关键词
  searchQuery: string;
  
  // 是否只显示收藏
  showFavoritesOnly: boolean;
  
  // Actions
  setSelectedSymbol: (symbol: string) => void;
  setSearchQuery: (query: string) => void;
  setShowFavoritesOnly: (show: boolean) => void;
  toggleFavorite: (symbol: string) => void;
  togglePinned: (symbol: string) => void;
  addSymbol: (symbol: SymbolInfo) => void;
  removeSymbol: (symbol: string) => void;
  updateSymbolPrice: (symbol: string, price: string, priceChange24h?: number) => void;
  reorderSymbols: (fromIndex: number, toIndex: number) => void;
}

export const useWatchlistStore = create<WatchlistState>()(
  persist(
    (set, get) => ({
      symbols: DEFAULT_SYMBOLS,
      favorites: ['SPY', 'QQQ', 'NVDA', 'AAPL'],
      pinned: [],
      selectedSymbol: 'SPY',
      searchQuery: '',
      showFavoritesOnly: false,

      setSelectedSymbol: (symbol) => set({ selectedSymbol: symbol }),
      
      setSearchQuery: (query) => set({ searchQuery: query }),
      
      setShowFavoritesOnly: (show) => set({ showFavoritesOnly: show }),
      
      toggleFavorite: (symbol) => {
        const { favorites } = get();
        if (favorites.includes(symbol)) {
          set({ favorites: favorites.filter(s => s !== symbol) });
        } else {
          set({ favorites: [...favorites, symbol] });
        }
      },
      
      togglePinned: (symbol) => {
        const { pinned } = get();
        if (pinned.includes(symbol)) {
          set({ pinned: pinned.filter(s => s !== symbol) });
        } else {
          set({ pinned: [...pinned, symbol] });
        }
      },
      
      addSymbol: (symbolInfo) => {
        const { symbols } = get();
        if (!symbols.find(s => s.symbol === symbolInfo.symbol)) {
          set({ symbols: [...symbols, symbolInfo] });
        }
      },
      
      removeSymbol: (symbol) => {
        const { symbols, favorites, pinned, selectedSymbol } = get();
        set({
          symbols: symbols.filter(s => s.symbol !== symbol),
          favorites: favorites.filter(s => s !== symbol),
          pinned: pinned.filter(s => s !== symbol),
          selectedSymbol: selectedSymbol === symbol ? 'BTCUSDT' : selectedSymbol,
        });
      },
      
      updateSymbolPrice: (symbol, price, priceChange24h) => {
        const { symbols } = get();
        set({
          symbols: symbols.map(s => 
            s.symbol === symbol 
              ? { ...s, price, priceChange24h: priceChange24h ?? s.priceChange24h }
              : s
          ),
        });
      },
      
      reorderSymbols: (fromIndex, toIndex) => {
        const { symbols } = get();
        const newSymbols = [...symbols];
        const [removed] = newSymbols.splice(fromIndex, 1);
        if (removed) {
          newSymbols.splice(toIndex, 0, removed);
          set({ symbols: newSymbols });
        }
      },
    }),
    {
      name: 'watchlist_state',
      version: 2,
      migrate: (persistedState: any, version: number) => {
        if (version < 2) {
          // If version is older than 2, merge new default symbols that aren't already in the list
          const currentSymbols = (persistedState as any).symbols || [];
          const missingDefaults = DEFAULT_SYMBOLS.filter(
            ds => !currentSymbols.find((s: any) => s.symbol === ds.symbol)
          );
          return {
            ...persistedState,
            symbols: [...currentSymbols, ...missingDefaults],
          };
        }
        return persistedState;
      },
      partialize: (state) => ({
        symbols: state.symbols,
        favorites: state.favorites,
        pinned: state.pinned,
        selectedSymbol: state.selectedSymbol,
      }),
    }
  )
);

// Selectors
export const selectSymbols = (state: WatchlistState) => state.symbols;
export const selectFavorites = (state: WatchlistState) => state.favorites;
export const selectPinned = (state: WatchlistState) => state.pinned;
export const selectSelectedSymbol = (state: WatchlistState) => state.selectedSymbol;
export const selectSearchQuery = (state: WatchlistState) => state.searchQuery;
export const selectShowFavoritesOnly = (state: WatchlistState) => state.showFavoritesOnly;

// 过滤后的交易对列表
export const selectFilteredSymbols = (state: WatchlistState) => {
  let filtered = state.symbols;
  
  // 搜索过滤
  if (state.searchQuery) {
    const query = state.searchQuery.toUpperCase();
    filtered = filtered.filter(s => 
      s.symbol.includes(query) ||
      s.baseAsset.includes(query) ||
      s.quoteAsset.includes(query)
    );
  }
  
  // 只显示收藏
  if (state.showFavoritesOnly) {
    filtered = filtered.filter(s => state.favorites.includes(s.symbol));
  }
  
  // 置顶排序
  return filtered.sort((a, b) => {
    const aIsPinned = state.pinned.includes(a.symbol);
    const bIsPinned = state.pinned.includes(b.symbol);
    if (aIsPinned && !bIsPinned) return -1;
    if (!aIsPinned && bIsPinned) return 1;
    return 0;
  });
};

