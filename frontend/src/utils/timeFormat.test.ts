import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { formatLastUpdateTime, formatRelativeTime } from './timeFormat';

const now = Date.parse('2026-11-09T18:28:38Z');

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(now);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('formatRelativeTime', () => {
  it('counts seconds under a minute', () => {
    expect(formatRelativeTime(now - 5_000)).toBe('5s ago');
  });

  it('counts minutes and seconds under an hour', () => {
    expect(formatRelativeTime(now - 125_000)).toBe('2m 5s ago');
  });
});

describe('formatLastUpdateTime', () => {
  it('shows one absolute date-time for data older than an hour', () => {
    // Friday's closing print, viewed on Sunday: the relative suffix used to be
    // the same clock again ("18:28:38 · 18:28:38").
    const label = formatLastUpdateTime(Date.parse('2026-11-06T21:00:00Z'));

    expect(label).not.toContain('·');
    expect(label).toMatch(/^11\/0[67], \d{2}:\d{2}/);
  });
});
