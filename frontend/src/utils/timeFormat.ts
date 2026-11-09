/**
 * 格式化相对时间
 * 格式：Xs ago / Xm Ys ago / Xh Ym ago
 * 超过 1 小时显示绝对时间：HH:MM:SS
 */
export function formatRelativeTime(timestamp: number): string {
  const now = Date.now();
  const diff = Math.floor((now - timestamp) / 1000);
  
  if (diff < 60) {
    return `${diff}s ago`;
  }
  
  if (diff < 3600) {
    const m = Math.floor(diff / 60);
    const s = diff % 60;
    return `${m}m ${s}s ago`;
  }
  
  // 超过 1 小时显示绝对日期+时间，避免仅剩的 HH:MM:SS 被误读成今天。
  return formatTime(timestamp, false);
}

/**
 * 格式化时间戳为 HH:MM:SS · Xs ago 格式
 */
export function formatLastUpdateTime(timestamp: number): string {
  const diff = Date.now() - timestamp;
  // Old data already renders as an absolute date-time; appending the same
  // clock again ("18:28:38 · 18:28:38") told the user nothing.
  if (diff >= 3600_000) return formatTime(timestamp, false);

  const timeStr = new Date(timestamp).toLocaleTimeString('en-US', {
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
  return `${timeStr} · ${formatRelativeTime(timestamp)}`;
}

/**
 * 格式化通用时间显示 (MM-DD HH:MM:SS)
 */
export function formatTime(timestamp: number, showSeconds: boolean = true): string {
  return new Date(timestamp).toLocaleString('en-US', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: showSeconds ? '2-digit' : undefined,
    hour12: false,
  });
}





