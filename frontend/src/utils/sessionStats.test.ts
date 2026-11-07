import { describe, it, expect } from 'vitest';
import {
  marketTimezoneFor,
  localDayKey,
  computeSessionStats,
  type SessionBar,
} from './sessionStats';

const s = (iso: string): number => Math.floor(new Date(iso).getTime() / 1000);

// Friday 2026-11-06, US session (EST = UTC-5).
const THU_CLOSE = s('2026-11-05T21:00:00Z'); // Thu 16:00 ET
const FRI_OPEN = s('2026-11-06T14:30:00Z'); // Fri 09:30 ET
const FRI_CLOSE = s('2026-11-06T21:00:00Z'); // Fri 16:00 ET

function bar(time: number, o: number, h: number, l: number, c: number, v = 1000): SessionBar {
  return { time, open: o, high: h, low: l, close: c, volume: v };
}

describe('marketTimezoneFor', () => {
  it('maps suffixed symbols to their venue timezone', () => {
    expect(marketTimezoneFor('0700.HK')).toBe('Asia/Hong_Kong');
    expect(marketTimezoneFor('7203.T')).toBe('Asia/Tokyo');
    expect(marketTimezoneFor('600519.SS')).toBe('Asia/Shanghai');
    expect(marketTimezoneFor('000858.SZ')).toBe('Asia/Shanghai');
  });

  it('defaults bare tickers to New York', () => {
    expect(marketTimezoneFor('SPY')).toBe('America/New_York');
    expect(marketTimezoneFor(null)).toBe('America/New_York');
  });
});

describe('localDayKey', () => {
  it('keys the day in the venue timezone, not UTC', () => {
    // 16:00 UTC on Nov 6 is already Nov 7 in Hong Kong.
    const t = s('2026-11-06T16:00:00Z');
    expect(localDayKey(t, 'Asia/Hong_Kong')).toBe('2026-11-07');
    expect(localDayKey(t, 'America/New_York')).toBe('2026-11-06');
  });
});

describe('computeSessionStats', () => {
  it('scopes high/low/volume to the last venue day and changes vs its previous close', () => {
    const bars = [
      bar(THU_CLOSE, 733.0, 733.0, 733.0, 733.0), // Thursday close print
      bar(FRI_OPEN, 734.82, 736.5, 734.5, 736.0, 30_000_000),
      bar(FRI_CLOSE, 735.29, 735.29, 735.29, 735.29, 28_000_000),
    ];

    const stats = computeSessionStats(bars, 'America/New_York', null);

    expect(stats).not.toBeNull();
    expect(stats!.current).toBe(735.29);
    expect(stats!.high).toBeCloseTo(736.5, 6);
    expect(stats!.low).toBeCloseTo(734.5, 6);
    expect(stats!.volume).toBe(58_000_000);
    // Change against Thursday's close, matching the watchlist row.
    expect(stats!.change).toBeCloseTo(2.29, 6);
    expect(stats!.changePercent).toBeCloseTo(0.3124147, 6);
  });

  it('includes a live price in the range and uses it as current', () => {
    const bars = [bar(FRI_OPEN, 734.82, 736.5, 734.5, 736.0), bar(FRI_CLOSE, 735.29, 735.29, 735.29, 735.29)];

    const stats = computeSessionStats(bars, 'America/New_York', 737.8);

    expect(stats!.current).toBe(737.8);
    expect(stats!.high).toBeCloseTo(737.8, 6);
    expect(stats!.low).toBeCloseTo(734.5, 6);
  });

  it('falls back to the session open when no previous day is in the window', () => {
    const bars = [bar(FRI_OPEN, 734.82, 736.5, 734.5, 736.0)];

    const stats = computeSessionStats(bars, 'America/New_York', null);

    expect(stats!.change).toBeCloseTo(736.0 - 734.82, 6);
  });

  it('treats each daily candle as its own session', () => {
    const bars = [
      bar(THU_CLOSE, 730.0, 734.0, 729.0, 733.0),
      bar(FRI_CLOSE, 734.8, 742.2, 734.6, 735.29),
    ];

    const stats = computeSessionStats(bars, 'America/New_York', null);

    expect(stats!.high).toBeCloseTo(742.2, 6);
    expect(stats!.low).toBeCloseTo(734.6, 6);
    expect(stats!.change).toBeCloseTo(2.29, 6);
  });

  it('returns null for an empty window', () => {
    expect(computeSessionStats([], 'America/New_York', null)).toBeNull();
  });
});
