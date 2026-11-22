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
  // 空窗（上游瞬时故障返回空体）也绝不清掉已翻看的历史——"轮询只增不减"
  // 的另一半契约；series 为空时保持返回空窗不变。
  if (!window.length) return series.length ? series : window;
  if (!series.length || series[0].time >= window[0].time) {
    // No paged history ahead: the result is the window itself. When the 60s
    // poller came back with content identical to what the chart already
    // holds, return the PREVIOUS reference instead of the freshly-parsed
    // array — the chart's [klines] effects (main + volume + indicators) all
    // key on this reference, and a new-but-equal array re-ran a full
    // setData + O(n×period) indicator recompute every poll
    // (2026-11-23 review P2). Full OHLCV compare: a same-length array with
    // a revised middle bar must still be caught.
    const identical =
      series.length === window.length &&
      series.every((k, i) => {
        const w = window[i];
        return (
          k.time === w.time &&
          k.open === w.open &&
          k.high === w.high &&
          k.low === w.low &&
          k.close === w.close &&
          k.volume === w.volume
        );
      });
    if (identical) return series;
    return window;
  }
  const cutoff = window[0].time;
  const older = series.filter(k => k.time < cutoff);
  if (!older.length) return window;
  return [...older, ...window];
}
