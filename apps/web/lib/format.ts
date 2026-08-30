export function formatPercent(value: number): string {
  return new Intl.NumberFormat("zh-CN", {
    style: "percent",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

export function formatAnnualizedReturn(metrics: {
  annualized_return: number;
  annualized_return_capped?: boolean;
  annualized_return_cap?: number;
}): string {
  if (!metrics.annualized_return_capped) {
    return formatPercent(metrics.annualized_return);
  }
  const cap = metrics.annualized_return_cap ?? metrics.annualized_return;
  return `≥ ${formatPercent(cap)}（年化截断）`;
}

export function formatCompact(value: number): string {
  return new Intl.NumberFormat("zh-CN", {
    notation: "compact",
    maximumFractionDigits: 2,
  }).format(value);
}

export function formatDate(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}
