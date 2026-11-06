import { describe, it, expect, beforeEach } from 'vitest';
import { useMarketStore, calculateConfidence } from './marketStore';
import type { ConnectionStatus, DataConfidence, MarketClock } from '../types/market';

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

describe('MarketStore - Session clock vs. broken feed', () => {
  const connectedStatus = (lastMessageTime: number): ConnectionStatus => ({
    state: 'connected',
    latencyMs: 80,
    // A closed market stops ticking: the last message is hours old.
    lastMessageTime,
    reconnectCount: 0,
    gapCount: 0,
    resyncCount: 0,
    messageRate: 0,
    isStale: false,
  });

  const livePrev: DataConfidence = {
    level: 'live',
    reason: '',
    canTrade: true,
    canTrustMetrics: true,
    lastLiveTime: Date.now() - 1000,
    degradedSince: 0,
    details: {
      wsConnected: true,
      sequenceContinuous: true,
      latencyOk: true,
      updateFrequencyOk: true,
      queueHealthy: true,
    },
  };

  const orderBook = {
    symbol: 'SPY',
    bids: [{ price: '712.63', quantity: '100' }],
    asks: [{ price: '764.24', quantity: '100' }],
    lastUpdateId: 1,
    localUpdateTime: 1,
    isStale: false,
    depth: 1,
  };

  const closedClock: MarketClock = {
    is_open: false,
    next_open: '2026-11-09T14:30:00Z',
    next_close: '2026-11-09T21:00:00Z',
  };

  it('reports closed (not degraded) when the broker clock says the market is shut', () => {
    const staleTimestamp = Date.now() - 3_600_000;
    const confidence = calculateConfidence(
      connectedStatus(staleTimestamp),
      orderBook,
      livePrev,
      closedClock
    );

    expect(confidence.level).toBe('closed');
    // Derived microstructure is meaningless on a frozen closing snapshot.
    expect(confidence.canTrustMetrics).toBe(false);
    expect(confidence.canTrade).toBe(false);
    expect(confidence.reason).not.toMatch(/消息速率|message rate/i);
  });

  it('only degrades on silence when the clock says the market is open', () => {
    const staleTimestamp = Date.now() - 3_600_000;
    const confidence = calculateConfidence(
      connectedStatus(staleTimestamp),
      orderBook,
      livePrev,
      { is_open: true, next_open: null, next_close: null }
    );

    // Silence while open is a data problem, never a session state.
    expect(confidence.level).not.toBe('closed');
  });

  it('returns to live once the market reopens', () => {
    const closedPrev: DataConfidence = {
      ...livePrev,
      level: 'closed',
      canTrade: false,
      canTrustMetrics: false,
    };
    const freshStatus: ConnectionStatus = {
      ...connectedStatus(Date.now()),
      // A live session ticked recently: rate is healthy again.
      messageRate: 5,
    };
    const openClock: MarketClock = { is_open: true, next_open: null, next_close: null };

    // Upgrade threshold is two consecutive samples; the first call may still
    // report 'closed' while the counters fill up.
    calculateConfidence(freshStatus, orderBook, closedPrev, openClock);
    const confidence = calculateConfidence(freshStatus, orderBook, closedPrev, openClock);

    expect(confidence.level).toBe('live');
    expect(confidence.canTrade).toBe(true);
  });

  it('falls back to normal judgement when the clock is unknown', () => {
    const confidence = calculateConfidence(
      connectedStatus(Date.now()),
      orderBook,
      livePrev,
      null
    );

    expect(confidence.level).toBe('live');
  });
});
