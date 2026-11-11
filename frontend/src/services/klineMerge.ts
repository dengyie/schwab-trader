/**
 * K-line series merging for the chart's "load older" paging.
 *
 * The contract, pinned by tests: prepend an immutable historical segment
 * without duplicating candles, and never let a fresh-window refetch (the 60s
 * poller) discard history the user has already paged through. Both rules
 * exist because the chart's `setData` receives whatever this module returns.
 */
import type { KlineData } from '../components/Chart/PriceChart';

/**
 * Prepend strictly-older bars to a series, dropping duplicates by time.
 * Assumes both inputs are ascending; returns a new array, inputs untouched.
 * Returns the original reference when nothing new is added, so the chart
 * effect can skip a redundant setData.
 */
export function prependOlderSegment(series: KlineData[], older: KlineData[]): KlineData[] {
  if (!older.length) return series;
  if (!series.length) return older.slice();
  const cutoff = series[0].time;
  const fresh = older.filter(k => k.time < cutoff);
  if (!fresh.length) return series;
  return [...fresh, ...series];
}

/**
 * Merge a fresh full-window response with a series that may carry paged
 * history: keep every existing bar older than the window's first bar.
 * (A full-window fetch cannot contain those bars — its upstream range starts
 * later — so "keep" here is exact, not best-effort.)
 */
export function keepHistoryAheadOfWindow(series: KlineData[], window: KlineData[]): KlineData[] {
  if (!window.length || !series.length || series[0].time >= window[0].time) return window;
  const cutoff = window[0].time;
  const older = series.filter(k => k.time < cutoff);
  if (!older.length) return window;
  return [...older, ...window];
}
