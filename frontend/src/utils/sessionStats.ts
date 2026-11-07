/**
 * Session-scoped chart statistics.
 *
 * The chart header used to take its "24H" stats from the whole kline window,
 * which for 15m bars spans roughly ten trading days -- a high/low/change the
 * session never traded. These helpers scope every figure to the last trading
 * day present in the data, keyed by the venue's own calendar day, so a US
 * chart splits at midnight ET and a HK chart at midnight HKT.
 */

export interface SessionBar {
  time: number; // UTC seconds
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface SessionStats {
  current: number;
  high: number;
  low: number;
  volume: number;
  change: number;
  changePercent: number;
  amplitude: number;
}

const TZ_BY_SUFFIX: Array<[string, string]> = [
  ['.HK', 'Asia/Hong_Kong'],
  ['.T', 'Asia/Tokyo'],
  ['.SS', 'Asia/Shanghai'],
  ['.SZ', 'Asia/Shanghai'],
  ['.SH', 'Asia/Shanghai'],
];

export function marketTimezoneFor(symbol: string | null | undefined): string {
  const s = (symbol || '').toUpperCase();
  for (const [suffix, tz] of TZ_BY_SUFFIX) {
    if (s.endsWith(suffix)) return tz;
  }
  return 'America/New_York';
}

// Intl.DateTimeFormat construction is expensive; one per timezone, reused.
const formatters = new Map<string, Intl.DateTimeFormat>();

function dayFormatter(tz: string): Intl.DateTimeFormat {
  let fmt = formatters.get(tz);
  if (!fmt) {
    fmt = new Intl.DateTimeFormat('en-CA', {
      timeZone: tz,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    });
    formatters.set(tz, fmt);
  }
  return fmt;
}

/** Calendar day (YYYY-MM-DD) of a UTC-seconds timestamp in the venue timezone. */
export function localDayKey(timeSec: number, tz: string): string {
  return dayFormatter(tz).format(new Date(timeSec * 1000));
}

/**
 * Stats for the venue day of the last bar. The change reference is the
 * previous day's close when the window reaches back that far, so the header
 * matches the watchlist's day change; otherwise the session open.
 */
export function computeSessionStats(
  bars: SessionBar[],
  tz: string,
  livePrice: number | null,
): SessionStats | null {
  if (!bars || bars.length === 0) return null;

  const fmt = dayFormatter(tz);
  const last = bars[bars.length - 1];
  const lastDay = fmt.format(new Date(last.time * 1000));

  // Walk backwards over the bars sharing the last bar's venue day.
  let start = bars.length - 1;
  while (
    start > 0 &&
    fmt.format(new Date(bars[start - 1].time * 1000)) === lastDay
  ) {
    start -= 1;
  }

  const sessionBars = bars.slice(start);
  const prevBar = start > 0 ? bars[start - 1] : null;

  const current = livePrice && livePrice > 0 ? livePrice : last.close;
  let high = current;
  let low = current;
  let volume = 0;
  for (const k of sessionBars) {
    if (k.high > high) high = k.high;
    if (k.low > 0 && (low <= 0 || k.low < low)) low = k.low;
    volume += k.volume > 0 ? k.volume : 0;
  }

  const ref = prevBar && prevBar.close > 0 ? prevBar.close : sessionBars[0].open;
  const change = ref > 0 ? current - ref : 0;
  const changePercent = ref > 0 ? (change / ref) * 100 : 0;
  const amplitude = low > 0 ? ((high - low) / low) * 100 : 0;

  return { current, high, low, volume, change, changePercent, amplitude };
}
