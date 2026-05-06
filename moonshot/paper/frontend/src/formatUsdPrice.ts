/**
 * USDT-M 等 USD 标价：按数量级给足小数，避免入场/现价/止盈止损在 UI 上被四舍五入成同一数字。
 */
export function formatUsdPrice(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—';
  const a = Math.abs(v);
  const opts: Intl.NumberFormatOptions =
    a >= 10_000
      ? { minimumFractionDigits: 2, maximumFractionDigits: 3 }
      : a >= 1_000
        ? { minimumFractionDigits: 2, maximumFractionDigits: 4 }
        : a >= 100
          ? { minimumFractionDigits: 2, maximumFractionDigits: 5 }
          : a >= 1
            ? { minimumFractionDigits: 2, maximumFractionDigits: 6 }
            : a >= 0.01
              ? { minimumFractionDigits: 4, maximumFractionDigits: 8 }
              : { minimumFractionDigits: 6, maximumFractionDigits: 12 };
  return v.toLocaleString(undefined, opts);
}
