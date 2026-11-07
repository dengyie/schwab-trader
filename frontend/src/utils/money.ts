// ===== 货币格式化 =====
//
// 多市场账户里每个数字都属于某个具体币种：港股用 HKD、日股用 JPY、
// A 股用 CNY。直接给所有数字加 "$" 会把 1453 元的茅台显示成 1453 美元，
// 所以金额一律与币种一起格式化。

export const CURRENCY_SYMBOLS: Record<string, string> = {
  USD: '$',
  HKD: 'HK$',
  JPY: '¥',
  CNY: 'CN¥',
};

export function currencySymbol(currency: string | null | undefined): string {
  if (!currency) return '$';
  return CURRENCY_SYMBOLS[currency.toUpperCase()] ?? `${currency.toUpperCase()} `;
}

/** 币种的小数位：日元不显示小数，其余按市场精度。 */
export function currencyDecimals(currency: string | null | undefined, fallback = 2): number {
  if ((currency || '').toUpperCase() === 'JPY') return 0;
  return fallback;
}

export interface FormatMoneyOptions {
  decimals?: number;
  signed?: boolean;
}

/**
 * 格式化金额。`signed` 会为正值加上 "+"，用于涨跌额与盈亏。
 */
export function formatMoney(
  value: number | null | undefined,
  currency: string | null | undefined,
  options: FormatMoneyOptions = {},
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  const decimals = options.decimals ?? currencyDecimals(currency, 2);
  const sign = options.signed && value > 0 ? '+' : value < 0 ? '-' : '';
  const abs = Math.abs(value);
  const body = abs.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  return `${sign}${currencySymbol(currency)}${body}`;
}

/** 价格展示：按市场 tick 精度显示，不带货币符号（盘口/图表用）。 */
export function formatPriceByDecimals(value: number | null | undefined, decimals = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  return value.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

/** 把价格对齐到最小跳动单位，避免提交非法价位。
 *
 * 直接做 `price / tick` 会踩浮点误差（419.9 / 0.2 = 2099.4999...），
 * 因此先按 tick 的小数位放大成整数再取整。
 */
export function roundToTick(price: number, tick: number): number {
  if (!(tick > 0)) return price;
  const tickDecimals = (String(tick).split('.')[1] || '').length;
  const scale = 10 ** tickDecimals;
  const scaledTick = Math.round(tick * scale);
  if (scaledTick <= 0) return price;
  const rounded = Math.round(Math.round(price * scale) / scaledTick) * scaledTick;
  return Number((rounded / scale).toFixed(tickDecimals + 2));
}
