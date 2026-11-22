/**
 * K-line series merging for the chart's "load older" paging.
 *
 * The contract, pinned by tests: prepend an immutable historical segment
 * without duplicating candles, and never let a fresh-window refetch (the 60s
 * poller) discard history the user has already paged through. Both rules
 * exist because the chart's `setData` receives whatever this module returns.
 */
import { describe, it, expect } from 'vitest';

import { prependOlderSegment, keepHistoryAheadOfWindow } from './klineMerge';
import type { KlineData } from '../components/Chart/PriceChart';

function bar(time: number, close = 100): KlineData {
  return { time: time as KlineData['time'], open: close, high: close + 1, low: close - 1, close, volume: 10 };
}

describe('prependOlderSegment', () => {
  it('prepends the older segment ahead of the current series', () => {
    const series = [bar(2000), bar(2100)];
    const older = [bar(1800), bar(1900)];
    const merged = prependOlderSegment(series, older);
    expect(merged.map(k => k.time)).toEqual([1800, 1900, 2000, 2100]);
    expect(series).toHaveLength(2); // inputs untouched
  });

  it('drops candles that duplicate the series head (segment overlap)', () => {
    const series = [bar(2000), bar(2100)];
    const older = [bar(1800), bar(1900), bar(2000)]; // 2000 overlaps
    const merged = prependOlderSegment(series, older);
    expect(merged.map(k => k.time)).toEqual([1800, 1900, 2000, 2100]);
  });

  it('returns the series unchanged when the segment adds nothing', () => {
    const series = [bar(2000), bar(2100)];
    expect(prependOlderSegment(series, [bar(2000)])).toBe(series); // all overlap → same ref
    expect(prependOlderSegment(series, [])).toBe(series);          // empty segment
  });

  it('adopts the segment wholesale when the series is empty', () => {
    const older = [bar(1800), bar(1900)];
    expect(prependOlderSegment([], older)).toEqual(older);
  });
});

describe('keepHistoryAheadOfWindow', () => {
  it('keeps paged history older than the fresh window', () => {
    const pagedHistory = [bar(1000), bar(1100), bar(2000)];
    const freshWindow = [bar(2000), bar(2100), bar(2200)];
    const merged = keepHistoryAheadOfWindow(pagedHistory, freshWindow);
    expect(merged.map(k => k.time)).toEqual([1000, 1100, 2000, 2100, 2200]);
  });

  it('lets the fresh window win when no history exists ahead of it', () => {
    const series = [bar(2500)]; // newer than the window head: a stale series
    const freshWindow = [bar(2000), bar(2100)];
    expect(keepHistoryAheadOfWindow(series, freshWindow)).toEqual(freshWindow);
  });

  it('returns the window untouched for an empty or fresh-only series', () => {
    const freshWindow = [bar(2000), bar(2100)];
    expect(keepHistoryAheadOfWindow([], freshWindow)).toEqual(freshWindow);
    expect(keepHistoryAheadOfWindow([bar(2100)], freshWindow)).toEqual(freshWindow);
  });

  it('returns the previous reference when the poll is content-identical (chart skip contract)', () => {
    // 2026-11-23 P2: the 60s poller re-parses the same bars into a NEW
    // array every cycle; keepHistoryAheadOfWindow must hand back the
    // ORIGINAL reference so the chart's [klines] effects skip their full
    // setData + indicator recompute. A revised middle bar must still be
    // caught (full OHLCV compare).
    const prev = [bar(2000), bar(2100), bar(2200)];
    const same = [bar(2000), bar(2100), bar(2200)];
    expect(keepHistoryAheadOfWindow(prev, same)).toBe(prev);

    const revised = [bar(2000), { ...bar(2100), close: 999 }, bar(2200)];
    expect(keepHistoryAheadOfWindow(prev, revised)).not.toBe(prev);
  });

  it('preserves history even when the fresh window is empty (upstream hiccup)', () => {
    // The poller returned an empty body: the paged history must survive,
    // not be wiped by the transient empty response.
    const pagedHistory = [bar(1000), bar(1100)];
    expect(keepHistoryAheadOfWindow(pagedHistory, [])).toEqual(pagedHistory);
  });
});
