import { describe, it, expect, beforeEach } from 'vitest';
import { useMarketStore } from './marketStore';
import type { Trade } from '../types/market';

describe('MarketStore - Real-time Incremental Data & Bar Stream', () => {
  beforeEach(() => {
    useMarketStore.setState({
      recentTrades: [],
      latestTrade: null,
      latestBar: null,
    });
  });

  it('updates recentTrades and latestTrade on incoming trade ticks', () => {
    const mockTrade: Trade = {
      id: 'trade_1',
      symbol: 'SPY',
      price: '585.50',
      quantity: '100',
      quoteQty: '58550.00',
      time: Date.now(),
      isBuyerMaker: false,
      localReceiveTime: performance.now(),
    };

    useMarketStore.setState((state) => ({
      recentTrades: [mockTrade, ...state.recentTrades],
      latestTrade: mockTrade,
    }));

    const state = useMarketStore.getState();
    expect(state.recentTrades).toHaveLength(1);
    expect(state.latestTrade).toEqual(mockTrade);
    expect(state.latestTrade?.price).toBe('585.50');
  });

  it('updates latestBar on BAR_UPDATE message', () => {
    const mockBar = {
      symbol: 'SPY',
      time: 1730823480,
      open: 585.0,
      high: 586.2,
      low: 584.8,
      close: 585.9,
      volume: 15400,
    };

    useMarketStore.setState({
      latestBar: mockBar,
    });

    const state = useMarketStore.getState();
    expect(state.latestBar).toEqual(mockBar);
    expect(state.latestBar?.close).toBe(585.9);
  });

  it('notifies subscribers via subscribeWithSelector when latestTrade or latestBar updates', () => {
    let notifiedTrade: Trade | null = null;
    let notifiedBar: any = null;

    const unsubTrade = useMarketStore.subscribe(
      (s) => s.latestTrade,
      (trade) => {
        notifiedTrade = trade;
      }
    );

    const unsubBar = useMarketStore.subscribe(
      (s) => s.latestBar,
      (bar) => {
        notifiedBar = bar;
      }
    );

    const trade: Trade = {
      id: 't2',
      symbol: 'AAPL',
      price: '235.50',
      quantity: '50',
      quoteQty: '11775.00',
      time: 1730823500000,
      isBuyerMaker: true,
      localReceiveTime: 1234,
    };

    useMarketStore.setState({ latestTrade: trade });
    expect(notifiedTrade).toEqual(trade);

    const bar = {
      symbol: 'AAPL',
      time: 1730823540,
      open: 235.0,
      high: 236.0,
      low: 234.9,
      close: 235.8,
      volume: 8000,
    };

    useMarketStore.setState({ latestBar: bar });
    expect(notifiedBar).toEqual(bar);

    unsubTrade();
    unsubBar();
  });
});
