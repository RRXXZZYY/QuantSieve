from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

SignalFunction = Callable[[pd.DataFrame, dict[str, float]], pd.Series]


class StrategyDefinition(BaseModel):
    id: str
    name: str
    description: str
    parameters: dict[str, float]
    category: str
    best_for: str = ""
    risk_note: str = ""
    recommended_intervals: list[str] = Field(default_factory=list)
    warmup_bars: int = Field(default=0, ge=0)


def _sma_cross(data: pd.DataFrame, params: dict[str, float]) -> pd.Series:
    close = data["close"]
    fast = close.rolling(int(params["fast"])).mean()
    slow = close.rolling(int(params["slow"])).mean()
    return (fast > slow).astype(float)


def _ema_cross(data: pd.DataFrame, params: dict[str, float]) -> pd.Series:
    close = data["close"]
    fast_period = int(params["fast"])
    slow_period = int(params["slow"])
    fast = close.ewm(
        span=fast_period,
        adjust=False,
        min_periods=fast_period,
    ).mean()
    slow = close.ewm(
        span=slow_period,
        adjust=False,
        min_periods=slow_period,
    ).mean()
    return ((fast > slow) & slow.notna()).astype(float)


def _rsi(data: pd.DataFrame, params: dict[str, float]) -> pd.Series:
    delta = data["close"].diff()
    gain = delta.clip(lower=0).rolling(int(params["period"])).mean()
    loss = -delta.clip(upper=0).rolling(int(params["period"])).mean()
    relative_strength = gain / loss.mask(loss == 0)
    rsi = 100 - (100 / (1 + relative_strength))
    rsi = rsi.mask((loss == 0) & (gain > 0), 100.0)
    rsi = rsi.mask((gain == 0) & (loss > 0), 0.0)
    rsi = rsi.mask((gain == 0) & (loss == 0), 50.0)
    entry = rsi < params["oversold"]
    exit_signal = rsi > params["overbought"]
    return _hold_between(entry, exit_signal)


def _bollinger(data: pd.DataFrame, params: dict[str, float]) -> pd.Series:
    close = data["close"]
    mean = close.rolling(int(params["period"])).mean()
    std = close.rolling(int(params["period"])).std()
    lower = mean - params["deviations"] * std
    return _hold_between(close < lower, close > mean)


def _momentum(data: pd.DataFrame, params: dict[str, float]) -> pd.Series:
    return (data["close"].pct_change(int(params["lookback"])) > params["threshold"]).astype(float)


def _mean_reversion(data: pd.DataFrame, params: dict[str, float]) -> pd.Series:
    close = data["close"]
    period = int(params["period"])
    regime_period = int(params["regime"])
    mean = close.rolling(period).mean()
    std = close.rolling(period).std()
    z_score = (close - mean) / std
    regime = close.ewm(
        span=regime_period,
        adjust=False,
        min_periods=regime_period,
    ).mean()

    # A raw Z-score entry and a Bollinger lower-band entry are the same rule
    # when they share a lookback and standard-deviation threshold.  This
    # template is deliberately different: it only buys a pullback while the
    # broader completed-bar trend remains positive, and leaves on either mean
    # recovery or a regime break.  The engine still executes any change on the
    # next bar, so no intrabar stop or same-bar fill is implied.
    state = 0.0
    values: list[float] = []
    for price, z_value, regime_value in zip(close, z_score, regime, strict=True):
        ready = pd.notna(z_value) and pd.notna(regime_value)
        if state == 1.0:
            recovered = pd.notna(z_value) and z_value > -params["exit_z"]
            regime_broken = not ready or price <= regime_value
            if recovered or regime_broken:
                state = 0.0
        elif ready and price > regime_value and z_value < -params["entry_z"]:
            state = 1.0
        values.append(state)
    return pd.Series(values, index=data.index)


def _breakout(data: pd.DataFrame, params: dict[str, float]) -> pd.Series:
    entry_period = int(params["entry_period"])
    exit_period = int(params["exit_period"])
    upper = data["high"].rolling(entry_period).max().shift(1)
    lower = data["low"].rolling(exit_period).min().shift(1)
    return _hold_between(data["close"] > upper, data["close"] < lower)


def _macd(data: pd.DataFrame, params: dict[str, float]) -> pd.Series:
    close = data["close"]
    fast_period = int(params["fast"])
    slow_period = int(params["slow"])
    signal_period = int(params["signal"])
    fast = close.ewm(
        span=fast_period,
        adjust=False,
        min_periods=fast_period,
    ).mean()
    slow = close.ewm(
        span=slow_period,
        adjust=False,
        min_periods=slow_period,
    ).mean()
    macd = fast - slow
    signal = macd.ewm(
        span=signal_period,
        adjust=False,
        min_periods=signal_period,
    ).mean()
    return (
        _numerically_above(macd, signal, scale=close)
        & signal.notna()
    ).astype(float)


def _volume_breakout(data: pd.DataFrame, params: dict[str, float]) -> pd.Series:
    volume_mean = data["volume"].rolling(int(params["period"])).mean()
    price_average = data["close"].rolling(int(params["period"])).mean()
    entry = (data["volume"] > volume_mean * params["multiplier"]) & (
        data["close"] > price_average
    )
    return _hold_between(entry, data["close"] < price_average)


def _trend_filter(data: pd.DataFrame, params: dict[str, float]) -> pd.Series:
    period = int(params["period"])
    trend = data["close"].ewm(
        span=period,
        adjust=False,
        min_periods=period,
    ).mean()
    return ((data["close"] > trend) & trend.notna()).astype(float)


def _core_trend_allocation(
    data: pd.DataFrame,
    params: dict[str, float],
) -> pd.Series:
    """Keep a core allocation in weak regimes and add the satellite above trend."""
    period = int(params["period"])
    defensive_exposure = float(params["defensive_exposure"])
    if not 0 < defensive_exposure < 1:
        raise ValueError("Defensive exposure must be between 0 and 1.")
    trend = data["close"].ewm(
        span=period,
        adjust=False,
        min_periods=period,
    ).mean()
    allocation = pd.Series(defensive_exposure, index=data.index)
    allocation = allocation.mask(data["close"] > trend, 1.0)
    return allocation.where(trend.notna(), 0.0)


def _volatility_target_trend(
    data: pd.DataFrame,
    params: dict[str, float],
) -> pd.Series:
    """Scale a long-only trend exposure by completed-bar volatility.

    This is not leverage or a promise of a fixed future volatility.  It holds
    cash below the trend, and, above it, compares short realised volatility to
    a slower baseline from the same market.  Exposure is bounded at 100%, so
    a quiet period cannot silently introduce leverage.
    """
    trend_period = int(params["trend_period"])
    volatility_period = int(params["volatility_period"])
    minimum_allocation = float(params["minimum_allocation"])
    maximum_allocation = float(params["maximum_allocation"])
    adjustment_band = float(params["adjustment_band"])
    if not 0 <= minimum_allocation < maximum_allocation <= 1:
        raise ValueError("Volatility-target allocations must be ordered within 0 and 1.")
    if not 0 < adjustment_band <= 1:
        raise ValueError("Volatility-target adjustment band must be within 0 and 1.")

    close = data["close"]
    trend = close.ewm(
        span=trend_period,
        adjust=False,
        min_periods=trend_period,
    ).mean()
    returns = close.pct_change()
    short_volatility = returns.rolling(
        volatility_period,
        min_periods=volatility_period,
    ).std(ddof=0)
    baseline_volatility = returns.rolling(
        volatility_period * 4,
        min_periods=volatility_period * 4,
    ).std(ddof=0)
    # A mathematically constant return can acquire an apparent standard
    # deviation around 1e-16 from repeated price division. Dividing two such
    # round-off residues creates arbitrary allocation jumps. Treat dispersion
    # below the square-root machine-precision floor as numerically zero; when
    # both windows are zero-volatility, their neutral relative scale is 1.
    return_scale = returns.abs().rolling(
        volatility_period * 4,
        min_periods=volatility_period * 4,
    ).mean()
    volatility_floor = return_scale * np.sqrt(np.finfo(float).eps)
    short_is_zero = short_volatility <= volatility_floor
    baseline_is_zero = baseline_volatility <= volatility_floor
    scale = baseline_volatility.div(short_volatility.mask(short_is_zero))
    scale = scale.mask(short_is_zero & baseline_is_zero, 1.0)
    scale = scale.mask(short_is_zero & ~baseline_is_zero, np.inf)
    desired_allocation = scale.clip(
        lower=minimum_allocation,
        upper=maximum_allocation,
    )
    ready = trend.notna() & short_volatility.notna() & baseline_volatility.notna()
    desired_allocation = desired_allocation.where(ready & (close > trend), 0.0).fillna(0.0)

    # A volatility estimate changes on every bar. Executing every tiny change
    # would turn a risk-control idea into unrealistic day-to-day churn, so we
    # only rebalance after a material allocation difference. A trend break is
    # still an immediate de-risking signal and is executed by the engine on the
    # next opening bar.
    current_allocation = 0.0
    allocations: list[float] = []
    for desired in desired_allocation:
        target = float(desired)
        if target == 0.0:
            current_allocation = 0.0
        elif current_allocation == 0.0 or abs(target - current_allocation) >= adjustment_band:
            current_allocation = target
        allocations.append(current_allocation)
    return pd.Series(allocations, index=data.index)


def _macd_regime(data: pd.DataFrame, params: dict[str, float]) -> pd.Series:
    close = data["close"]
    fast_period = int(params["fast"])
    slow_period = int(params["slow"])
    signal_period = int(params["signal"])
    regime_period = int(params["regime"])
    fast = close.ewm(
        span=fast_period,
        adjust=False,
        min_periods=fast_period,
    ).mean()
    slow = close.ewm(
        span=slow_period,
        adjust=False,
        min_periods=slow_period,
    ).mean()
    macd = fast - slow
    signal = macd.ewm(
        span=signal_period,
        adjust=False,
        min_periods=signal_period,
    ).mean()
    regime = close.ewm(
        span=regime_period,
        adjust=False,
        min_periods=regime_period,
    ).mean()
    return (
        _numerically_above(macd, signal, scale=close)
        & signal.notna()
        & (close > regime)
        & regime.notna()
    ).astype(float)


def _atr_trend(data: pd.DataFrame, params: dict[str, float]) -> pd.Series:
    """Trend entry with a bar-close ATR trailing exit.

    Signals are intentionally calculated from completed OHLC bars.  The
    backtest engine then applies the existing next-bar execution delay, so this
    is a research approximation of a trailing exit rather than a broker-native
    stop order or a promise of stop-fill prices.
    """
    fast_period = int(params["fast"])
    slow_period = int(params["slow"])
    atr_period = int(params["atr_period"])
    atr_multiplier = float(params["atr_multiplier"])
    if atr_multiplier <= 0:
        raise ValueError("ATR multiplier must be positive.")

    close = data["close"]
    high = data["high"]
    low = data["low"]
    fast = close.ewm(
        span=fast_period,
        adjust=False,
        min_periods=fast_period,
    ).mean()
    slow = close.ewm(
        span=slow_period,
        adjust=False,
        min_periods=slow_period,
    ).mean()
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.rolling(atr_period, min_periods=atr_period).mean()

    state = 0.0
    highest_high: float | None = None
    values: list[float] = []
    for price, bar_high, fast_value, slow_value, atr_value in zip(
        close,
        high,
        fast,
        slow,
        atr,
        strict=True,
    ):
        trend_ready = pd.notna(fast_value) and pd.notna(slow_value)
        atr_ready = pd.notna(atr_value)
        if state == 0.0:
            if trend_ready and atr_ready and fast_value > slow_value:
                state = 1.0
                highest_high = float(bar_high)
        else:
            highest_high = max(highest_high or float(bar_high), float(bar_high))
            trailing_exit = price <= highest_high - atr_multiplier * float(atr_value)
            trend_exit = not trend_ready or fast_value <= slow_value
            if trailing_exit or trend_exit:
                state = 0.0
                highest_high = None
        values.append(state)
    return pd.Series(values, index=data.index)


def _breakout_atr(data: pd.DataFrame, params: dict[str, float]) -> pd.Series:
    """Donchian breakout entry with a completed-bar ATR trailing exit."""
    entry_period = int(params["entry_period"])
    exit_period = int(params["exit_period"])
    atr_period = int(params["atr_period"])
    atr_multiplier = float(params["atr_multiplier"])
    if exit_period >= entry_period:
        raise ValueError("Breakout exit period must be shorter than entry period.")
    if atr_multiplier <= 0:
        raise ValueError("ATR multiplier must be positive.")

    close = data["close"]
    high = data["high"]
    low = data["low"]
    upper = high.rolling(entry_period).max().shift(1)
    lower = low.rolling(exit_period).min().shift(1)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.rolling(atr_period, min_periods=atr_period).mean()

    state = 0.0
    highest_high: float | None = None
    values: list[float] = []
    for price, bar_high, upper_value, lower_value, atr_value in zip(
        close,
        high,
        upper,
        lower,
        atr,
        strict=True,
    ):
        entry_ready = pd.notna(upper_value) and pd.notna(atr_value)
        if state == 0.0:
            if entry_ready and price > upper_value:
                state = 1.0
                highest_high = float(bar_high)
        else:
            highest_high = max(highest_high or float(bar_high), float(bar_high))
            trailing_exit = price <= highest_high - atr_multiplier * float(atr_value)
            channel_exit = pd.notna(lower_value) and price < lower_value
            if trailing_exit or channel_exit:
                state = 0.0
                highest_high = None
        values.append(state)
    return pd.Series(values, index=data.index)


def _rsi_regime_atr(data: pd.DataFrame, params: dict[str, float]) -> pd.Series:
    """Buy an RSI pullback only above regime, with a completed-bar ATR defence.

    This is deliberately a research signal, not a broker stop order.  The
    strategy sees a completed bar and the engine executes on the next opening
    bar, so gaps and intrabar stop fills remain outside the model.
    """

    rsi_period = int(params["rsi_period"])
    oversold = float(params["oversold"])
    exit_rsi = float(params["exit_rsi"])
    regime_period = int(params["regime"])
    atr_period = int(params["atr_period"])
    atr_multiplier = float(params["atr_multiplier"])
    if not 0 < oversold < exit_rsi < 100:
        raise ValueError("RSI entry and exit thresholds must be ordered within 0–100.")
    if atr_multiplier <= 0:
        raise ValueError("ATR multiplier must be positive.")

    close = data["close"]
    high = data["high"]
    low = data["low"]
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(rsi_period).mean()
    loss = -delta.clip(upper=0).rolling(rsi_period).mean()
    relative_strength = gain / loss.mask(loss == 0)
    rsi = 100 - (100 / (1 + relative_strength))
    rsi = rsi.mask((loss == 0) & (gain > 0), 100.0)
    rsi = rsi.mask((gain == 0) & (loss > 0), 0.0)
    rsi = rsi.mask((gain == 0) & (loss == 0), 50.0)
    regime = close.ewm(
        span=regime_period,
        adjust=False,
        min_periods=regime_period,
    ).mean()
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.rolling(atr_period, min_periods=atr_period).mean()

    state = 0.0
    entry_price: float | None = None
    values: list[float] = []
    for price, rsi_value, regime_value, atr_value in zip(
        close,
        rsi,
        regime,
        atr,
        strict=True,
    ):
        ready = pd.notna(rsi_value) and pd.notna(regime_value) and pd.notna(atr_value)
        if state == 0.0:
            if ready and price > regime_value and rsi_value < oversold:
                state = 1.0
                entry_price = float(price)
        else:
            atr_exit = price <= (entry_price or float(price)) - atr_multiplier * float(atr_value)
            regime_exit = not ready or price <= regime_value
            rsi_exit = pd.notna(rsi_value) and rsi_value >= exit_rsi
            if atr_exit or regime_exit or rsi_exit:
                state = 0.0
                entry_price = None
        values.append(state)
    return pd.Series(values, index=data.index)


def _buy_and_hold(data: pd.DataFrame, _params: dict[str, float]) -> pd.Series:
    return pd.Series(1.0, index=data.index)


def _constant_allocation(
    data: pd.DataFrame,
    params: dict[str, float],
) -> pd.Series:
    allocation = float(params["allocation"])
    if not 0 <= allocation <= 1:
        raise ValueError("Constant allocation must be between 0 and 1.")
    return pd.Series(allocation, index=data.index)


def _hold_between(entry: pd.Series, exit_signal: pd.Series) -> pd.Series:
    state = 0.0
    values: list[float] = []
    for enter, exit_now in zip(entry.fillna(False), exit_signal.fillna(False), strict=True):
        if bool(exit_now):
            state = 0.0
        elif bool(enter):
            state = 1.0
        values.append(state)
    return pd.Series(values, index=entry.index)


def _numerically_above(
    left: pd.Series,
    right: pd.Series,
    *,
    scale: pd.Series,
) -> pd.Series:
    """Compare derived price indicators without trading on round-off noise.

    Two recursively calculated moving-average series can converge to the same
    value while differing by a few floating-point ulps. A raw ``left > right``
    then chatters between long and cash even though there is no economically
    observable crossover. Inside that machine-precision equality band, retain
    the last real comparison state. The tolerance is tied only to machine
    precision and the source-price scale, so it does not introduce a tunable
    trading band.
    """

    tolerance = scale.abs() * np.finfo(float).eps * 128
    difference = left - right
    comparison = pd.Series(np.nan, index=difference.index, dtype=float)
    comparison = comparison.mask(difference > tolerance, 1.0)
    comparison = comparison.mask(difference < -tolerance, 0.0)
    return comparison.ffill().fillna(0.0).astype(bool)


_DEFINITIONS: list[tuple[StrategyDefinition, SignalFunction]] = [
    (
        StrategyDefinition(
            id="sma-cross",
            name="双均线",
            description="短期均线上穿长期均线持有。",
            parameters={"fast": 20, "slow": 60},
            category="趋势",
            best_for="有持续方向的日线或 4 小时趋势",
            risk_note="震荡市会反复产生假突破。",
            recommended_intervals=["4h", "1d", "1wk"],
            warmup_bars=60,
        ),
        _sma_cross,
    ),
    (
        StrategyDefinition(
            id="ema-cross",
            name="EMA 交叉",
            description="指数均线趋势跟随。",
            parameters={"fast": 12, "slow": 26},
            category="趋势",
            best_for="反应较快的中期趋势",
            risk_note="快速反转时可能连续止损。",
            recommended_intervals=["1h", "4h", "1d"],
            warmup_bars=26,
        ),
        _ema_cross,
    ),
    (
        StrategyDefinition(
            id="rsi",
            name="RSI 反转",
            description="超卖入场、超买离场。",
            parameters={"period": 14, "oversold": 30, "overbought": 70},
            category="震荡",
            best_for="宽幅震荡、均值回归明显的市场",
            risk_note="强趋势中可能过早逆势入场。",
            recommended_intervals=["1h", "4h", "1d"],
            warmup_bars=15,
        ),
        _rsi,
    ),
    (
        StrategyDefinition(
            id="bollinger",
            name="布林带反转",
            description="跌破下轨后等待均值回归。",
            parameters={"period": 20, "deviations": 2},
            category="震荡",
            best_for="波动回归中枢的区间行情",
            risk_note="单边下跌时可能持续接飞刀。",
            recommended_intervals=["1h", "4h", "1d"],
            warmup_bars=20,
        ),
        _bollinger,
    ),
    (
        StrategyDefinition(
            id="momentum",
            name="价格动量",
            description="过去一段时间收益为正时持有。",
            parameters={"lookback": 20, "threshold": 0},
            category="趋势",
            best_for="趋势延续和轮动行情",
            risk_note="零阈值附近容易频繁切换。",
            recommended_intervals=["4h", "1d", "1wk"],
            warmup_bars=21,
        ),
        _momentum,
    ),
    (
        StrategyDefinition(
            id="mean-reversion",
            name="趋势内 Z-Score 回撤",
            description="只在长期趋势向上时买入显著低于短期均值的回撤。",
            parameters={"period": 20, "entry_z": 2, "exit_z": 0, "regime": 150},
            category="趋势内回撤",
            best_for="长期趋势中的回撤，而非下跌趋势的逆势抄底",
            risk_note=(
                "趋势过滤会错过快速 V 型反转；仅依据完成 K 线，"
                "回测按下一根开盘执行。"
            ),
            recommended_intervals=["4h", "1d", "1wk"],
            warmup_bars=150,
        ),
        _mean_reversion,
    ),
    (
        StrategyDefinition(
            id="breakout",
            name="唐奇安突破",
            description="55 根 K 线突破入场，20 根 K 线跌破离场。",
            parameters={"entry_period": 55, "exit_period": 20},
            category="突破",
            best_for="持续时间较长的趋势行情",
            risk_note="震荡区间可能出现连续小亏损。",
            recommended_intervals=["4h", "1d", "1wk"],
            warmup_bars=56,
        ),
        _breakout,
    ),
    (
        StrategyDefinition(
            id="macd",
            name="MACD",
            description="MACD 线上穿信号线时持有。",
            parameters={"fast": 12, "slow": 26, "signal": 9},
            category="趋势",
            best_for="中期趋势和波段行情",
            risk_note="参数不是通用最优值，必须做留出验证。",
            recommended_intervals=["1h", "4h", "1d"],
            warmup_bars=34,
        ),
        _macd,
    ),
    (
        StrategyDefinition(
            id="volume-breakout",
            name="放量突破",
            description="趋势向上且成交量放大时入场。",
            parameters={"period": 20, "multiplier": 1.5},
            category="量价",
            best_for="有可靠成交量的突破行情",
            risk_note="低流动性标的容易出现虚假放量。",
            recommended_intervals=["1h", "4h", "1d"],
            warmup_bars=20,
        ),
        _volume_breakout,
    ),
    (
        StrategyDefinition(
            id="trend-filter",
            name="长期趋势防守",
            description="价格位于长期 EMA 上方时持有，跌破后转为空仓。",
            parameters={"period": 150},
            category="趋势",
            best_for="加密资产、指数和大宗商品的长期趋势",
            risk_note="横盘阶段可能落后买入持有。",
            recommended_intervals=["4h", "1d", "1wk"],
            warmup_bars=150,
        ),
        _trend_filter,
    ),
    (
        StrategyDefinition(
            id="core-trend-allocation",
            name="核心仓位 + 趋势防守",
            description="长期趋势向上时满仓，弱势期保留核心仓位而非完全清仓。",
            parameters={"period": 200, "defensive_exposure": 0.35},
            category="配置型趋势",
            best_for="指数、加密与大宗商品的中长期配置",
            risk_note="弱势期仍保留核心敞口，无法完全规避深度下跌；趋势附近也可能反复调仓。",
            recommended_intervals=["1d", "1wk"],
            warmup_bars=200,
        ),
        _core_trend_allocation,
    ),
    (
        StrategyDefinition(
            id="volatility-target-trend",
            name="波动自适应趋势配置",
            description=(
                "价格位于长期趋势上方时持有，并按近期波动相对慢速基准自动收缩或放大"
                "未杠杆仓位；跌破趋势转为现金。"
            ),
            parameters={
                "trend_period": 150,
                "volatility_period": 20,
                "minimum_allocation": 0.25,
                "maximum_allocation": 1.0,
                "adjustment_band": 0.15,
            },
            category="波动率配置趋势",
            best_for="希望在日线或周线趋势中把仓位随近期波动收缩、且不使用杠杆的研究",
            risk_note=(
                "波动率来自已完成 K 线，仓位变化仍按下一根开盘执行；慢速历史波动"
                "基准不能保证未来风险恒定，跳空和流动性冲击也不在模型承诺之内。"
            ),
            recommended_intervals=["1d", "1wk"],
            warmup_bars=150,
        ),
        _volatility_target_trend,
    ),
    (
        StrategyDefinition(
            id="macd-regime",
            name="MACD + 长期趋势过滤",
            description="只在长期趋势向上时采用 MACD 多头信号。",
            parameters={"fast": 12, "slow": 26, "signal": 9, "regime": 150},
            category="趋势增强",
            best_for="波动较大的加密资产和成长型资产",
            risk_note="过滤回撤的同时也会错过快速 V 型反转。",
            recommended_intervals=["4h", "1d"],
            warmup_bars=150,
        ),
        _macd_regime,
    ),
    (
        StrategyDefinition(
            id="atr-trend",
            name="ATR 跟踪退出趋势",
            description=(
                "EMA 趋势向上时持有；收盘价跌破相对入场后最高价的 ATR 跟踪线，"
                "或趋势反转时离场。"
            ),
            parameters={
                "fast": 20,
                "slow": 60,
                "atr_period": 14,
                "atr_multiplier": 3,
            },
            category="风险退出趋势",
            best_for="有持续趋势、且希望用波动率定义退出距离的 4 小时、日线或周线市场",
            risk_note=(
                "这是基于已完成 K 线收盘价的研究信号，回测按下一根开盘执行；"
                "跳空、盘中穿透和真实止损成交价均不保证。"
            ),
            recommended_intervals=["4h", "1d", "1wk"],
            warmup_bars=60,
        ),
        _atr_trend,
    ),
    (
        StrategyDefinition(
            id="breakout-atr",
            name="唐奇安突破 + ATR 跟踪退出",
            description=(
                "突破前期最高价后持有；跌破短期通道或收盘跌破 ATR 跟踪线时离场。"
            ),
            parameters={
                "entry_period": 55,
                "exit_period": 20,
                "atr_period": 14,
                "atr_multiplier": 3,
            },
            category="风险退出突破",
            best_for="有持续方向突破、且需要以波动率定义退出距离的 4 小时、日线或周线市场",
            risk_note=(
                "只在完成 K 线后判断突破和跟踪退出，回测按下一根开盘成交；"
                "震荡市会有连续小亏，跳空与盘中止损成交价不保证。"
            ),
            recommended_intervals=["4h", "1d", "1wk"],
            warmup_bars=56,
        ),
        _breakout_atr,
    ),
    (
        StrategyDefinition(
            id="rsi-regime-atr",
            name="趋势过滤 RSI + ATR 防守退出",
            description=(
                "长期趋势向上时只买入 RSI 超卖回撤；回到均衡、跌破趋势或触及 "
                "ATR 防守距离时离场。"
            ),
            parameters={
                "rsi_period": 14,
                "oversold": 30,
                "exit_rsi": 55,
                "regime": 150,
                "atr_period": 14,
                "atr_multiplier": 2,
            },
            category="风险退出反转",
            best_for="长期趋势中的短期回撤；适合 4 小时、日线或周线研究",
            risk_note=(
                "只以完成 K 线生成研究信号，回测按下一根开盘成交；强趋势下回撤可能继续扩大，"
                "跳空、盘中穿透和真实止损成交价均不保证。"
            ),
            recommended_intervals=["4h", "1d", "1wk"],
            warmup_bars=150,
        ),
        _rsi_regime_atr,
    ),
    (
        StrategyDefinition(
            id="constant-allocation",
            name="固定份额被动配置",
            description="起点按资金比例买入，之后固定份额与剩余现金，不做择时或再平衡。",
            parameters={"allocation": 0.5},
            category="被动配置",
            best_for="按风险预算控制单一高波动资产的资金占用",
            risk_note=(
                "历史回撤预算不保证未来不被突破；实际持仓率会随价格漂移，现金收益暂按 0 计算。"
            ),
            recommended_intervals=["1d", "1wk"],
        ),
        _constant_allocation,
    ),
    (
        StrategyDefinition(
            id="buy-hold",
            name="买入并持有",
            description="基准策略：全周期持有。",
            parameters={},
            category="基准",
            best_for="检验主动策略是否真正增加价值",
            risk_note="完整承受标的自身最大回撤。",
            recommended_intervals=["15m", "1h", "4h", "1d", "1wk"],
        ),
        _buy_and_hold,
    ),
]

STRATEGIES = {definition.id: definition for definition, _ in _DEFINITIONS}
_FUNCTIONS = {definition.id: function for definition, function in _DEFINITIONS}
PASSIVE_STRATEGY_IDS = frozenset({"buy-hold", "constant-allocation"})

_INTEGER_PARAMETER_NAMES = frozenset(
    {
        "fast",
        "slow",
        "signal",
        "period",
        "lookback",
        "regime",
        "entry_period",
        "exit_period",
        "trend_period",
        "volatility_period",
        "atr_period",
        "rsi_period",
    }
)


def _positive_integer(value: float, name: str) -> None:
    if not float(value).is_integer() or value < 1:
        raise ValueError(f"{name} must be a positive whole-number bar count.")


def _between(value: float, lower: float, upper: float, name: str) -> None:
    if not lower <= value <= upper:
        raise ValueError(f"{name} must be between {lower:g} and {upper:g}.")


def _resolved_parameters(
    strategy_id: str,
    parameters: dict[str, float] | None,
) -> dict[str, float]:
    """Merge and validate user parameters before a signal or warm-up is calculated.

    Grid search already removes invalid combinations, but a manual workbench run
    must receive the exact same protection.  Rejecting unknown names also keeps
    a misspelled user setting from silently falling back to a template default.
    """
    definition, _ = get_strategy(strategy_id)
    supplied = parameters or {}
    unknown = set(supplied) - set(definition.parameters)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"{definition.name} does not accept parameter(s): {names}.")
    values = {**definition.parameters, **supplied}
    for name in _INTEGER_PARAMETER_NAMES & set(values):
        _positive_integer(values[name], name)

    if (
        strategy_id in {"sma-cross", "ema-cross", "macd", "macd-regime", "atr-trend"}
        and values["fast"] >= values["slow"]
    ):
        raise ValueError("fast must be shorter than slow.")
    if strategy_id == "rsi":
        _between(values["oversold"], 0, 100, "oversold")
        _between(values["overbought"], 0, 100, "overbought")
        if values["oversold"] >= values["overbought"]:
            raise ValueError("oversold must be below overbought.")
    if strategy_id == "bollinger" and values["deviations"] <= 0:
        raise ValueError("deviations must be positive.")
    if strategy_id == "momentum" and values["threshold"] < 0:
        raise ValueError("threshold must not be negative.")
    if strategy_id == "mean-reversion":
        if values["entry_z"] <= values["exit_z"]:
            raise ValueError("entry_z must be greater than exit_z.")
        if values["exit_z"] < 0:
            raise ValueError("exit_z must not be negative.")
    if (
        strategy_id in {"breakout", "breakout-atr"}
        and values["exit_period"] >= values["entry_period"]
    ):
        raise ValueError("exit_period must be shorter than entry_period.")
    if strategy_id == "volume-breakout" and values["multiplier"] <= 0:
        raise ValueError("multiplier must be positive.")
    if strategy_id == "core-trend-allocation" and not 0 < values["defensive_exposure"] < 1:
        raise ValueError("defensive_exposure must be between 0 and 1.")
    if strategy_id == "volatility-target-trend":
        if not 0 <= values["minimum_allocation"] < values["maximum_allocation"] <= 1:
            raise ValueError("allocation bounds must be ordered within 0 and 1.")
        if not 0 < values["adjustment_band"] <= 1:
            raise ValueError("adjustment_band must be between 0 and 1.")
    if strategy_id == "rsi-regime-atr" and not 0 < values["oversold"] < values["exit_rsi"] < 100:
        raise ValueError("RSI thresholds must be ordered within 0 and 100.")
    if (
        strategy_id in {"atr-trend", "breakout-atr", "rsi-regime-atr"}
        and values["atr_multiplier"] <= 0
    ):
        raise ValueError("atr_multiplier must be positive.")
    if strategy_id == "constant-allocation":
        _between(values["allocation"], 0, 1, "allocation")
    return values


def get_strategy(strategy_id: str) -> tuple[StrategyDefinition, SignalFunction]:
    try:
        return STRATEGIES[strategy_id], _FUNCTIONS[strategy_id]
    except KeyError as exc:
        raise KeyError(f"Unknown strategy: {strategy_id}") from exc


def strategy_warmup_bars(
    strategy_id: str,
    parameters: dict[str, float] | None = None,
) -> int:
    values = _resolved_parameters(strategy_id, parameters)
    if strategy_id in {"sma-cross", "ema-cross"}:
        return int(values["slow"])
    if strategy_id == "rsi":
        return int(values["period"]) + 1
    if strategy_id in {
        "bollinger",
        "volume-breakout",
        "trend-filter",
        "core-trend-allocation",
    }:
        return int(values["period"])
    if strategy_id == "volatility-target-trend":
        return max(
            int(values["trend_period"]),
            int(values["volatility_period"]) * 4 + 1,
        )
    if strategy_id == "mean-reversion":
        return max(int(values["period"]), int(values["regime"]))
    if strategy_id == "momentum":
        return int(values["lookback"]) + 1
    if strategy_id == "breakout":
        return max(int(values["entry_period"]) + 1, int(values["exit_period"]) + 1)
    if strategy_id == "breakout-atr":
        return max(
            int(values["entry_period"]) + 1,
            int(values["exit_period"]) + 1,
            int(values["atr_period"]),
        )
    if strategy_id == "macd":
        return int(values["slow"]) + int(values["signal"]) - 1
    if strategy_id == "macd-regime":
        return max(
            int(values["slow"]) + int(values["signal"]) - 1,
            int(values["regime"]),
        )
    if strategy_id == "atr-trend":
        return max(int(values["slow"]), int(values["atr_period"]))
    if strategy_id == "rsi-regime-atr":
        return max(
            int(values["rsi_period"]) + 1,
            int(values["regime"]),
            int(values["atr_period"]),
        )
    return 0


def strategy_cycle_baseline_signals(
    strategy_id: str,
    signals: pd.Series,
    parameters: dict[str, float] | None = None,
) -> pd.Series | None:
    """Return an explicit core-only allocation for cycle-quality attribution.

    Most strategies are evaluated as flat-to-position-to-flat cycles and return
    ``None``. ``core-trend-allocation`` never goes flat after warm-up, so its
    independent timing sample is the satellite allocation above the configured
    defensive core. Clipping its own signals preserves warm-up cash without
    asking the backtest engine to infer strategy parameters.
    """
    values = _resolved_parameters(strategy_id, parameters)
    if strategy_id != "core-trend-allocation":
        return None
    return signals.clip(upper=float(values["defensive_exposure"])).astype(float)


def strategy_signals(
    data: pd.DataFrame,
    strategy_id: str,
    parameters: dict[str, float] | None = None,
    *,
    signal_history: pd.DataFrame | None = None,
) -> pd.Series:
    """Build signals with optional earlier bars while returning only evaluation rows."""
    evaluation = data.sort_index()
    if evaluation.empty:
        return pd.Series(dtype=float, index=evaluation.index)
    frames = []
    if signal_history is not None and not signal_history.empty:
        frames.append(signal_history.loc[signal_history.index < evaluation.index[0]])
    frames.append(evaluation)
    signal_frame = pd.concat(frames).sort_index()
    signal_frame = signal_frame.loc[~signal_frame.index.duplicated(keep="last")]
    _, strategy = get_strategy(strategy_id)
    values = _resolved_parameters(strategy_id, parameters)
    return strategy(signal_frame, values).reindex(evaluation.index).fillna(0.0)
