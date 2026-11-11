import { describe, it, expect } from 'vitest';
import { chartTimeLabels } from './AssetDetailPage';

describe('chartTimeLabels', () => {
  it('formats intraday series as HH:MM', () => {
    // One trading morning, 5-minute spacing (Alpaca 1D/5Min curve).
    const start = Date.UTC(2026, 10, 11, 14, 30) / 1000; // 09:30 ET
    const times = Array.from({ length: 79 }, (_, i) => start + i * 300);
    const labels = chartTimeLabels(times);
    expect(labels).toHaveLength(4);
    expect(labels[0]).toBe('09:30');
    expect(labels[3]).toBe('16:00');
  });

  it('formats daily series as month day (ET session dates)', () => {
    // Alpaca daily timestamps are session instants; noon UTC is safely
    // inside the ET day regardless of DST.
    const start = Date.UTC(2026, 9, 12, 12) / 1000;
    const times = Array.from({ length: 22 }, (_, i) => start + i * 86400);
    const labels = chartTimeLabels(times);
    expect(labels[0]).toBe('Oct 12');
    expect(labels[3]).toBe('Nov 2');
  });

  it('appends the year on multi-year spans', () => {
    const start = Date.UTC(2024, 0, 2, 12) / 1000;
    const times = Array.from({ length: 500 }, (_, i) => start + i * 86400);
    const labels = chartTimeLabels(times);
    expect(labels[0]).toMatch(/2024/);
    expect(labels[3]).toMatch(/2025/);
  });

  it('returns [] when there is nothing to label', () => {
    expect(chartTimeLabels([])).toEqual([]);
    expect(chartTimeLabels([1791849600])).toEqual([]);
  });
});
