export const INTEGER_PARAMETER_KEYS = new Set([
  "fast",
  "slow",
  "signal",
  "period",
  "rsi_period",
  "lookback",
  "entry_period",
  "exit_period",
  "regime",
  "atr_period",
  "trend_period",
  "volatility_period",
]);

export function manualParameterValidationError(
  strategyId: string,
  parameters: Record<string, number>,
): string | null {
  if (Object.values(parameters).some((value) => !Number.isFinite(value))) {
    return "参数必须是有效数字。";
  }
  for (const key of INTEGER_PARAMETER_KEYS) {
    if (key in parameters && (!Number.isInteger(parameters[key]) || parameters[key] < 1)) {
      return `${key} 必须是大于 0 的整数根数。`;
    }
  }
  if (
    ["sma-cross", "ema-cross", "macd", "macd-regime", "atr-trend"].includes(strategyId) &&
    parameters.fast >= parameters.slow
  ) {
    return "快速周期必须小于慢速周期。";
  }
  if (strategyId === "rsi" && !(0 <= parameters.oversold && parameters.oversold < parameters.overbought && parameters.overbought <= 100)) {
    return "RSI 超卖线必须低于超买线，且两者都在 0–100 内。";
  }
  if (strategyId === "bollinger" && parameters.deviations <= 0) {
    return "标准差倍数必须大于 0。";
  }
  if (strategyId === "momentum" && parameters.threshold < 0) {
    return "动量阈值不能小于 0。";
  }
  if (strategyId === "mean-reversion") {
    if (parameters.exit_z < 0) return "离场 Z 值不能小于 0。";
    if (parameters.entry_z <= parameters.exit_z) return "入场 Z 值必须大于离场 Z 值。";
  }
  if (
    ["breakout", "breakout-atr"].includes(strategyId) &&
    parameters.exit_period >= parameters.entry_period
  ) {
    return "退出周期必须短于突破周期。";
  }
  if (strategyId === "volume-breakout" && parameters.multiplier <= 0) {
    return "成交量倍数必须大于 0。";
  }
  if (strategyId === "core-trend-allocation" && !(0 < parameters.defensive_exposure && parameters.defensive_exposure < 1)) {
    return "弱势期核心仓位必须大于 0 且小于 100%。";
  }
  if (strategyId === "volatility-target-trend") {
    if (!(0 <= parameters.minimum_allocation && parameters.minimum_allocation < parameters.maximum_allocation && parameters.maximum_allocation <= 1)) {
      return "最低/最高风险仓位必须按 0–100% 从小到大设置。";
    }
    if (!(0 < parameters.adjustment_band && parameters.adjustment_band <= 1)) {
      return "仓位调整带必须大于 0 且不超过 100%。";
    }
  }
  if (strategyId === "rsi-regime-atr" && !(0 < parameters.oversold && parameters.oversold < parameters.exit_rsi && parameters.exit_rsi < 100)) {
    return "RSI 超卖线必须低于离场线，且两者都在 0–100 内。";
  }
  if (
    ["atr-trend", "breakout-atr", "rsi-regime-atr"].includes(strategyId) &&
    parameters.atr_multiplier <= 0
  ) {
    return "ATR 距离倍数必须大于 0。";
  }
  if (strategyId === "constant-allocation" && !(0 <= parameters.allocation && parameters.allocation <= 1)) {
    return "恒定资金仓位必须在 0–100% 内。";
  }
  return null;
}
