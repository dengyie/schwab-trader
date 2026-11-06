import { describe, it, expect, beforeEach } from 'vitest';
import { useWatchlistStore, selectFilteredSymbols } from './watchlistStore';

describe('WatchlistStore', () => {
  beforeEach(() => {
    // Reset store state
    useWatchlistStore.setState({
      symbols: [
        { symbol: 'BTCUSDT', baseAsset: 'BTC', quoteAsset: 'USDT' },
        { symbol: 'ETHUSDT', baseAsset: 'ETH', quoteAsset: 'USDT' },
        { symbol: 'BNBUSDT', baseAsset: 'BNB', quoteAsset: 'USDT' },
      ],
      favorites: ['BTCUSDT'],
      pinned: [],
      selectedSymbol: 'BTCUSDT',
      searchQuery: '',
      showFavoritesOnly: false,
    });
  });

  describe('symbol selection', () => {
    it('should select a symbol', () => {
      const { setSelectedSymbol } = useWatchlistStore.getState();
      setSelectedSymbol('ETHUSDT');
      expect(useWatchlistStore.getState().selectedSymbol).toBe('ETHUSDT');
    });
  });

  describe('favorites', () => {
    it('should toggle favorite', () => {
      const { toggleFavorite } = useWatchlistStore.getState();
      
      // Add to favorites
      toggleFavorite('ETHUSDT');
      expect(useWatchlistStore.getState().favorites).toContain('ETHUSDT');
      
      // Remove from favorites
      toggleFavorite('ETHUSDT');
      expect(useWatchlistStore.getState().favorites).not.toContain('ETHUSDT');
    });
  });

  describe('pinned', () => {
    it('should toggle pinned', () => {
      const { togglePinned } = useWatchlistStore.getState();
      
      togglePinned('ETHUSDT');
      expect(useWatchlistStore.getState().pinned).toContain('ETHUSDT');
      
      togglePinned('ETHUSDT');
      expect(useWatchlistStore.getState().pinned).not.toContain('ETHUSDT');
    });
  });

  describe('search filtering', () => {
    it('should filter symbols by search query', () => {
      const { setSearchQuery } = useWatchlistStore.getState();
      setSearchQuery('BTC');
      
      const filtered = selectFilteredSymbols(useWatchlistStore.getState());
      expect(filtered).toHaveLength(1);
      expect(filtered[0]?.symbol).toBe('BTCUSDT');
    });

    it('should be case insensitive', () => {
      const { setSearchQuery } = useWatchlistStore.getState();
      setSearchQuery('eth');
      
      const filtered = selectFilteredSymbols(useWatchlistStore.getState());
      expect(filtered).toHaveLength(1);
      expect(filtered[0]?.symbol).toBe('ETHUSDT');
    });
  });

  describe('favorites only filter', () => {
    it('should show only favorites when enabled', () => {
      const { setShowFavoritesOnly } = useWatchlistStore.getState();
      setShowFavoritesOnly(true);
      
      const filtered = selectFilteredSymbols(useWatchlistStore.getState());
      expect(filtered).toHaveLength(1);
      expect(filtered[0]?.symbol).toBe('BTCUSDT');
    });
  });

  describe('pinned sorting', () => {
    it('should sort pinned symbols first', () => {
      const { togglePinned } = useWatchlistStore.getState();
      togglePinned('BNBUSDT');
      
      const filtered = selectFilteredSymbols(useWatchlistStore.getState());
      expect(filtered[0]?.symbol).toBe('BNBUSDT');
    });
  });

  describe('symbol removal', () => {
    it('selects the first remaining symbol instead of a legacy placeholder', () => {
      useWatchlistStore.setState({
        symbols: [
          { symbol: 'SPY', baseAsset: 'SPY', quoteAsset: 'USD' },
          { symbol: 'QQQ', baseAsset: 'QQQ', quoteAsset: 'USD' },
        ],
        selectedSymbol: 'SPY',
      });

      useWatchlistStore.getState().removeSymbol('SPY');

      // 'BTCUSDT' is a leftover from the crypto era; the terminal only trades
      // US equities, so selecting it leaves the feed empty.
      expect(useWatchlistStore.getState().selectedSymbol).toBe('QQQ');
    });

    it('leaves the selection empty when the last symbol is removed', () => {
      useWatchlistStore.setState({
        symbols: [{ symbol: 'SPY', baseAsset: 'SPY', quoteAsset: 'USD' }],
        selectedSymbol: 'SPY',
      });

      useWatchlistStore.getState().removeSymbol('SPY');

      expect(useWatchlistStore.getState().selectedSymbol).toBe('');
    });
  });
});





