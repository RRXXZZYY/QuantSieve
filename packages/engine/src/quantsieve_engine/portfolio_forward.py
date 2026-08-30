from __future__ import annotations

from collections.abc import Mapping
from math import isclose
from typing import Literal, Self

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .portfolio import PortfolioMethod, _cap_weights, _target_values

SCHEMA_VERSION = 1
_WEIGHT_TOLERANCE = 1e-10
_ACCOUNTING_RELATIVE_TOLERANCE = 1e-9
_ACCOUNTING_ABSOLUTE_TOLERANCE = 1e-8


class PortfolioForwardTarget(BaseModel):
    """A causal allocation decision made after one finalized close."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    method: PortfolioMethod
    information_session: str
    weights: dict[str, float]

    @field_validator("information_session", mode="before")
    @classmethod
    def normalize_information_session(cls, value: object) -> str:
        return _normalize_session(value)

    @field_validator("weights", mode="before")
    @classmethod
    def normalize_weights(cls, value: object) -> dict[str, float]:
        return _canonical_numeric_map(value, "Target weights")

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if len(self.weights) < 2 or len(self.weights) > 8:
            raise ValueError("Portfolio targets require between 2 and 8 symbols.")
        values = np.asarray(list(self.weights.values()), dtype=float)
        if (values < 0).any():
            raise ValueError("Target weights must be non-negative.")
        if not isclose(
            float(values.sum()),
            1.0,
            rel_tol=0,
            abs_tol=_WEIGHT_TOLERANCE,
        ):
            raise ValueError("Target weights must sum to 1.")
        return self


class PortfolioForwardExecution(BaseModel):
    """Per-asset audit record for one open-price allocation step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    symbol: str
    session: str
    side: Literal["buy", "sell", "hold"]
    raw_open_price: float
    modeled_fill_price: float
    shares_before: float
    shares_after: float
    shares_delta: float
    current_value: float
    target_value: float
    traded_notional: float
    fee: float
    slippage: float
    total_cost: float

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return _canonical_symbol(value)

    @field_validator("session", mode="before")
    @classmethod
    def normalize_session(cls, value: object) -> str:
        return _normalize_session(value)

    @model_validator(mode="after")
    def validate_execution(self) -> Self:
        numeric = (
            self.raw_open_price,
            self.modeled_fill_price,
            self.shares_before,
            self.shares_after,
            self.shares_delta,
            self.current_value,
            self.target_value,
            self.traded_notional,
            self.fee,
            self.slippage,
            self.total_cost,
        )
        if not np.isfinite(np.asarray(numeric, dtype=float)).all():
            raise ValueError("Execution values must be finite.")
        if self.raw_open_price <= 0 or self.modeled_fill_price <= 0:
            raise ValueError("Execution prices must be positive.")
        non_negative = (
            self.shares_before,
            self.shares_after,
            self.current_value,
            self.target_value,
            self.traded_notional,
            self.fee,
            self.slippage,
            self.total_cost,
        )
        if min(non_negative) < 0:
            raise ValueError("Execution accounting values must be non-negative.")
        _require_accounting_close(
            self.shares_delta,
            self.shares_after - self.shares_before,
            "Execution share delta does not reconcile.",
        )
        _require_accounting_close(
            self.current_value,
            self.shares_before * self.raw_open_price,
            "Execution current value does not reconcile with shares and open price.",
        )
        _require_accounting_close(
            self.target_value,
            self.shares_after * self.raw_open_price,
            "Execution target value does not reconcile with shares and open price.",
        )
        signed_value_change = self.target_value - self.current_value
        _require_accounting_close(
            signed_value_change,
            self.shares_delta * self.raw_open_price,
            "Execution value change does not reconcile with share delta.",
        )
        _require_accounting_close(
            self.traded_notional,
            abs(signed_value_change),
            "Execution traded notional does not reconcile.",
        )
        _require_accounting_close(
            self.total_cost,
            self.fee + self.slippage,
            "Execution costs do not reconcile.",
        )
        expected_side = (
            "buy"
            if signed_value_change > 0
            else "sell"
            if signed_value_change < 0
            else "hold"
        )
        if self.side != expected_side:
            raise ValueError("Execution side does not match its signed value change.")
        if self.side == "hold":
            if self.fee != 0 or self.slippage != 0 or self.total_cost != 0:
                raise ValueError("Hold executions must have zero fees and slippage.")
            _require_accounting_close(
                self.modeled_fill_price,
                self.raw_open_price,
                "Hold execution fill price must equal its raw open price.",
            )
        else:
            if self.traded_notional <= 0:
                raise ValueError("Buy and sell executions require positive notional.")
            implied_slippage_rate = self.slippage / self.traded_notional
            expected_fill_price = _modeled_fill_price(
                self.raw_open_price,
                self.side,
                implied_slippage_rate,
            )
            _require_accounting_close(
                self.modeled_fill_price,
                expected_fill_price,
                "Execution fill price does not reconcile with slippage.",
            )
        return self


class PortfolioForwardState(BaseModel):
    """Serializable close-to-close state for a single portfolio ledger."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    method: PortfolioMethod
    symbols: tuple[str, ...]
    volatility_lookback: int = Field(default=60, ge=2)
    maximum_asset_weight: float = Field(default=0.4, gt=0, le=1)
    fee_rate: float = Field(default=0.0003, ge=0, le=0.1)
    slippage_rate: float = Field(default=0.0002, ge=0, le=0.1)
    session: str | None = None
    initial_cash: float = Field(gt=0)
    cash: float = Field(ge=0)
    shares: dict[str, float]
    equity: float = Field(gt=0)
    total_return: float = Field(gt=-1)
    peak_equity: float = Field(gt=0)
    max_drawdown: float = Field(ge=-1, le=0)
    last_prices: dict[str, float]
    target_weights: dict[str, float]
    realized_weights: dict[str, float]
    pending_target: PortfolioForwardTarget | None = None
    valuation_count: int = Field(default=0, ge=0)
    rebalance_count: int = Field(default=0, ge=0)
    turnover_ratio: float = Field(default=0, ge=0)
    turnover_notional: float = Field(default=0, ge=0)
    fee_paid: float = Field(default=0, ge=0)
    slippage_paid: float = Field(default=0, ge=0)
    total_cost: float = Field(default=0, ge=0)

    @field_validator("symbols", mode="before")
    @classmethod
    def normalize_symbols(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError("Portfolio symbols must be a sequence.")
        symbols = tuple(_canonical_symbol(symbol) for symbol in value)
        if len(symbols) < 2 or len(symbols) > 8:
            raise ValueError("Portfolio ledgers require between 2 and 8 symbols.")
        if len(set(symbols)) != len(symbols):
            raise ValueError("Portfolio symbols must be unique after normalization.")
        return symbols

    @field_validator("session", mode="before")
    @classmethod
    def normalize_optional_session(cls, value: object) -> str | None:
        return None if value is None else _normalize_session(value)

    @field_validator(
        "shares",
        "last_prices",
        "target_weights",
        "realized_weights",
        mode="before",
    )
    @classmethod
    def normalize_account_maps(cls, value: object) -> dict[str, float]:
        return _canonical_numeric_map(value, "Portfolio account map")

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        scalar_values = (
            self.initial_cash,
            self.cash,
            self.equity,
            self.total_return,
            self.peak_equity,
            self.max_drawdown,
            self.maximum_asset_weight,
            self.fee_rate,
            self.slippage_rate,
            self.turnover_ratio,
            self.turnover_notional,
            self.fee_paid,
            self.slippage_paid,
            self.total_cost,
        )
        if not np.isfinite(np.asarray(scalar_values, dtype=float)).all():
            raise ValueError("Portfolio state values must be finite.")
        expected_symbols = set(self.symbols)
        for name, values in (
            ("shares", self.shares),
            ("target_weights", self.target_weights),
            ("realized_weights", self.realized_weights),
        ):
            if set(values) != expected_symbols:
                raise ValueError(f"Portfolio {name} must exactly match state symbols.")
            if min(values.values()) < 0:
                raise ValueError(f"Portfolio {name} must be non-negative.")
        if self.valuation_count == 0:
            if self.last_prices:
                raise ValueError("An unvalued portfolio must not contain last prices.")
            if self.session is None and self.pending_target is not None:
                raise ValueError("A pending target requires an information session.")
            if self.max_drawdown != 0:
                raise ValueError("An unvalued portfolio must have zero max drawdown.")
        else:
            if self.session is None:
                raise ValueError("A valued portfolio must have a session.")
            if set(self.last_prices) != expected_symbols:
                raise ValueError("Portfolio last prices must exactly match state symbols.")
            if min(self.last_prices.values()) <= 0:
                raise ValueError("Portfolio last prices must be positive.")
            current_drawdown = self.equity / self.peak_equity - 1
            if self.max_drawdown > current_drawdown and not isclose(
                self.max_drawdown,
                current_drawdown,
                rel_tol=_ACCOUNTING_RELATIVE_TOLERANCE,
                abs_tol=_ACCOUNTING_ABSOLUTE_TOLERANCE,
            ):
                raise ValueError(
                    "Portfolio max drawdown cannot be above its current drawdown."
                )
        target_sum = float(sum(self.target_weights.values()))
        if not (
            isclose(target_sum, 0.0, rel_tol=0, abs_tol=_WEIGHT_TOLERANCE)
            or isclose(target_sum, 1.0, rel_tol=0, abs_tol=_WEIGHT_TOLERANCE)
        ):
            raise ValueError("State target weights must be all zero or sum to 1.")
        if self.rebalance_count == 0 and not isclose(
            target_sum,
            0.0,
            rel_tol=0,
            abs_tol=_WEIGHT_TOLERANCE,
        ):
            raise ValueError("A never-rebalanced state must have zero target weights.")
        if self.rebalance_count > 0 and not isclose(
            target_sum,
            1.0,
            rel_tol=0,
            abs_tol=_WEIGHT_TOLERANCE,
        ):
            raise ValueError("A rebalanced state must have target weights summing to 1.")
        realized_sum = float(sum(self.realized_weights.values()))
        if realized_sum > 1 + _WEIGHT_TOLERANCE:
            raise ValueError("Realized asset weights cannot exceed total equity.")
        if self.rebalance_count > self.valuation_count:
            raise ValueError("Rebalance count cannot exceed valuation count.")
        if self.method == "periodic_inverse_volatility":
            if self.maximum_asset_weight * len(self.symbols) < 1 - _WEIGHT_TOLERANCE:
                raise ValueError(
                    "Maximum asset weight is too small for the number of assets."
                )
            if (
                self.rebalance_count > 0
                and max(self.target_weights.values())
                > self.maximum_asset_weight + _WEIGHT_TOLERANCE
            ):
                raise ValueError("State target weights exceed the configured cap.")
        if self.method in {"initial_equal_hold", "periodic_equal"}:
            _validate_equal_weights(
                self.target_weights,
                allow_zero=self.rebalance_count == 0,
            )
        if self.method == "initial_equal_hold" and self.rebalance_count > 1:
            raise ValueError("Initial equal-hold portfolios can rebalance only once.")
        if self.pending_target is not None:
            _validate_target_compatibility(self, self.pending_target)
            if self.session is None:
                raise ValueError("A pending target requires a state session.")
            if self.pending_target.information_session != self.session:
                raise ValueError(
                    "Pending target information session must equal the state session."
                )
        _require_accounting_close(
            self.total_return,
            self.equity / self.initial_cash - 1,
            "Portfolio total return does not reconcile with equity.",
        )
        if self.peak_equity + _ACCOUNTING_ABSOLUTE_TOLERANCE < max(
            self.initial_cash,
            self.equity,
        ):
            raise ValueError("Portfolio peak equity is below its high-water mark.")
        _require_accounting_close(
            self.total_cost,
            self.fee_paid + self.slippage_paid,
            "Portfolio aggregate costs do not reconcile.",
        )
        if self.valuation_count == 0:
            _require_accounting_close(
                self.equity,
                self.cash,
                "Unvalued portfolio equity must equal cash.",
            )
            if any(
                value > _ACCOUNTING_ABSOLUTE_TOLERANCE
                for value in self.shares.values()
            ):
                raise ValueError("An unvalued portfolio cannot hold shares.")
            if any(
                value > _ACCOUNTING_ABSOLUTE_TOLERANCE
                for value in self.realized_weights.values()
            ):
                raise ValueError("An unvalued portfolio must have zero realized weights.")
        else:
            reconciled_equity = self.cash + sum(
                self.shares[symbol] * self.last_prices[symbol]
                for symbol in self.symbols
            )
            _require_accounting_close(
                self.equity,
                reconciled_equity,
                "Portfolio cash and holdings do not reconcile with equity.",
            )
            for symbol in self.symbols:
                expected_weight = (
                    self.shares[symbol] * self.last_prices[symbol] / self.equity
                )
                _require_accounting_close(
                    self.realized_weights[symbol],
                    expected_weight,
                    f"Realized weight for {symbol} does not reconcile.",
                )
        return self


class PortfolioForwardAdvance(BaseModel):
    """Result of advancing a portfolio ledger through one complete session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: PortfolioForwardState
    executions: tuple[PortfolioForwardExecution, ...]

    @model_validator(mode="after")
    def validate_advance(self) -> Self:
        if self.state.session is None:
            raise ValueError("Advanced portfolio state must have a session.")
        symbols = tuple(execution.symbol for execution in self.executions)
        if symbols != self.state.symbols:
            raise ValueError("Advance executions must cover every state symbol in order.")
        if any(
            execution.session != self.state.session for execution in self.executions
        ):
            raise ValueError("Advance executions must match the state session.")
        for execution in self.executions:
            _require_accounting_close(
                execution.fee,
                execution.traded_notional * self.state.fee_rate,
                f"Execution fee for {execution.symbol} does not match state fee rate.",
            )
            _require_accounting_close(
                execution.slippage,
                execution.traded_notional * self.state.slippage_rate,
                (
                    f"Execution slippage for {execution.symbol} does not match "
                    "state slippage rate."
                ),
            )
            expected_fill_price = _modeled_fill_price(
                execution.raw_open_price,
                execution.side,
                self.state.slippage_rate,
            )
            _require_accounting_close(
                execution.modeled_fill_price,
                expected_fill_price,
                (
                    f"Execution fill price for {execution.symbol} does not match "
                    "state slippage rate."
                ),
            )
        return self


def initialize_portfolio_forward_state(
    symbols: list[str] | tuple[str, ...],
    initial_cash: float,
    method: PortfolioMethod,
    *,
    fee_rate: float = 0.0003,
    slippage_rate: float = 0.0002,
    volatility_lookback: int = 60,
    maximum_asset_weight: float = 0.4,
    session: object | None = None,
    pending_target: PortfolioForwardTarget | None = None,
) -> PortfolioForwardState:
    """Create an all-cash, zero-return ledger without consulting external state."""

    safe_target = _validated_target_copy(pending_target)
    resolved_session = (
        safe_target.information_session
        if session is None and safe_target is not None
        else None
        if session is None
        else _normalize_session(session)
    )
    canonical_symbols = tuple(_canonical_symbol(symbol) for symbol in symbols)
    zero_map = dict.fromkeys(canonical_symbols, 0.0)
    return PortfolioForwardState(
        method=method,
        symbols=canonical_symbols,
        volatility_lookback=volatility_lookback,
        maximum_asset_weight=maximum_asset_weight,
        fee_rate=fee_rate,
        slippage_rate=slippage_rate,
        session=resolved_session,
        initial_cash=initial_cash,
        cash=initial_cash,
        shares=zero_map,
        equity=initial_cash,
        total_return=0,
        peak_equity=initial_cash,
        max_drawdown=0,
        last_prices={},
        target_weights=zero_map,
        realized_weights=zero_map,
        pending_target=safe_target,
    )


def compute_next_portfolio_target(
    close_history: pd.DataFrame,
    method: PortfolioMethod,
    *,
    information_session: object,
    volatility_lookback: int = 60,
    maximum_asset_weight: float = 0.4,
) -> PortfolioForwardTarget:
    """Compute the next-open target using finalized closes no later than one session."""

    if volatility_lookback < 2:
        raise ValueError("Volatility lookback must be at least 2 observations.")
    if not np.isfinite(maximum_asset_weight) or not 0 < maximum_asset_weight <= 1:
        raise ValueError("Maximum asset weight must be finite and between 0 and 1.")
    if method not in {
        "initial_equal_hold",
        "periodic_equal",
        "periodic_inverse_volatility",
    }:
        raise ValueError(f"Unknown portfolio method: {method}")
    if not isinstance(close_history, pd.DataFrame):
        raise ValueError("Close history must be a pandas DataFrame.")
    frame = close_history.copy(deep=True)
    if frame.empty or len(frame.columns) < 2 or len(frame.columns) > 8:
        raise ValueError("Close history requires between 2 and 8 assets.")
    symbols = tuple(_canonical_symbol(column) for column in frame.columns)
    if len(set(symbols)) != len(symbols):
        raise ValueError("Close-history symbols must be unique after normalization.")
    frame.columns = list(symbols)
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("Close history must use a DatetimeIndex.")
    if frame.index.hasnans:
        raise ValueError("Close-history index must not contain invalid timestamps.")
    if frame.index.has_duplicates:
        raise ValueError("Close-history index must not contain duplicate timestamps.")
    if not frame.index.is_monotonic_increasing:
        raise ValueError("Close-history index must be strictly increasing.")
    frame.index = (
        frame.index.tz_localize("UTC").normalize()
        if frame.index.tz is None
        else frame.index.tz_convert("UTC").normalize()
    )
    if frame.index.has_duplicates:
        raise ValueError(
            "Close history must contain at most one observation per UTC calendar date."
        )
    cutoff = pd.Timestamp(_normalize_session(information_session))
    if cutoff not in frame.index:
        raise ValueError("Information session is not present in close history.")
    frame = frame.loc[frame.index <= cutoff]
    if frame.empty:
        raise ValueError("Close history contains no finalized observations.")
    try:
        numeric = frame.apply(pd.to_numeric, errors="raise").astype(float)
    except (TypeError, ValueError) as error:
        raise ValueError("Finalized close prices must be numeric.") from error
    prices = numeric.to_numpy(dtype=float)
    if not np.isfinite(prices).all():
        raise ValueError("Finalized close prices must be finite.")
    if (prices <= 0).any():
        raise ValueError("Finalized close prices must be positive.")
    if method in {"initial_equal_hold", "periodic_equal"}:
        weights = pd.Series(1 / len(symbols), index=symbols, dtype=float)
    else:
        if maximum_asset_weight * len(symbols) < 1 - _WEIGHT_TOLERANCE:
            raise ValueError("Maximum asset weight is too small for the number of assets.")
        required_observations = volatility_lookback + 1
        if len(numeric) < required_observations:
            raise ValueError(
                "Inverse-volatility target requires at least "
                f"{required_observations} finalized closes."
            )
        returns = numeric.pct_change(fill_method=None).tail(volatility_lookback)
        volatility = returns.std(ddof=0)
        volatility_values = volatility.to_numpy(dtype=float)
        if (
            not np.isfinite(volatility_values).all()
            or (volatility_values <= 0).any()
        ):
            raise ValueError(
                "Inverse-volatility target requires finite, positive volatility "
                "for every asset."
            )
        weights = _cap_weights(1 / volatility, maximum_asset_weight)
    return PortfolioForwardTarget(
        method=method,
        information_session=numeric.index[-1].isoformat(),
        weights={symbol: float(weights[symbol]) for symbol in symbols},
    )


def queue_portfolio_target(
    state: PortfolioForwardState,
    target: PortfolioForwardTarget,
) -> PortfolioForwardState:
    """Return a validated copy of state with a next-session target attached."""

    safe_state = _validated_state_copy(state)
    safe_target = _validated_target_copy(target)
    if safe_target is None:
        raise ValueError("A portfolio target is required.")
    _validate_target_for_current_state(safe_state, safe_target)
    if safe_state.pending_target is not None:
        if safe_state.pending_target != safe_target:
            raise ValueError("Portfolio state already contains a different pending target.")
        return safe_state
    data = safe_state.model_dump(mode="python")
    data["pending_target"] = safe_target.model_dump(mode="python")
    return PortfolioForwardState.model_validate(data)


def advance_portfolio_session(
    state: PortfolioForwardState,
    *,
    session: object,
    open_prices: Mapping[str, float],
    close_prices: Mapping[str, float],
    pending_target: PortfolioForwardTarget | None = None,
) -> PortfolioForwardAdvance:
    """Execute one pending target at the open and value the ledger at the close."""

    safe_state = _validated_state_copy(state)
    session_label = _normalize_session(session)
    session_timestamp = pd.Timestamp(session_label)
    if safe_state.session is not None:
        previous_timestamp = pd.Timestamp(safe_state.session)
        if session_timestamp <= previous_timestamp:
            raise ValueError(
                "Portfolio sessions must be strictly increasing and cannot be replayed."
            )
    normalized_open = _exact_positive_price_map(
        open_prices,
        safe_state.symbols,
        "Open prices",
    )
    normalized_close = _exact_positive_price_map(
        close_prices,
        safe_state.symbols,
        "Close prices",
    )
    supplied_target = _validated_target_copy(pending_target)
    stored_target = safe_state.pending_target
    if (
        supplied_target is not None
        and stored_target is not None
        and supplied_target != stored_target
    ):
        raise ValueError(
            "Supplied target conflicts with the target already pending in state."
        )
    selected_target = supplied_target or stored_target
    if selected_target is not None:
        _validate_target_for_current_state(safe_state, selected_target)
        if pd.Timestamp(selected_target.information_session) >= session_timestamp:
            raise ValueError(
                "A pending target must be based on a close before its execution session."
            )

    symbols = safe_state.symbols
    open_row = pd.Series(
        {symbol: normalized_open[symbol] for symbol in symbols},
        dtype=float,
    )
    shares_before = pd.Series(
        {symbol: safe_state.shares[symbol] for symbol in symbols},
        dtype=float,
    )
    current_values = shares_before * open_row
    portfolio_value = float(safe_state.cash + current_values.sum())
    if not np.isfinite(portfolio_value) or portfolio_value <= 0:
        raise ValueError("Portfolio open value must be finite and positive.")

    target_values = current_values.copy()
    shares_after = shares_before.copy()
    cash_after = safe_state.cash
    traded_values = pd.Series(0.0, index=symbols, dtype=float)
    fee_values = pd.Series(0.0, index=symbols, dtype=float)
    slippage_values = pd.Series(0.0, index=symbols, dtype=float)
    target_weights = dict(safe_state.target_weights)
    rebalance_increment = 0
    if selected_target is not None:
        weights = pd.Series(
            {symbol: selected_target.weights[symbol] for symbol in symbols},
            dtype=float,
        )
        target_values, combined_cost = _target_values(
            portfolio_value,
            current_values,
            weights,
            safe_state.fee_rate + safe_state.slippage_rate,
        )
        traded_values = (target_values - current_values).abs()
        fee_values = traded_values * safe_state.fee_rate
        slippage_values = traded_values * safe_state.slippage_rate
        component_cost = float(fee_values.sum() + slippage_values.sum())
        _require_accounting_close(
            combined_cost,
            component_cost,
            "Portfolio target cost does not reconcile by asset.",
        )
        shares_after = target_values / open_row
        cash_after = portfolio_value - float(target_values.sum()) - combined_cost
        cash_tolerance = max(
            _ACCOUNTING_ABSOLUTE_TOLERANCE,
            abs(portfolio_value) * 1e-12,
        )
        if cash_after < -cash_tolerance:
            raise ValueError("Portfolio target would create a negative cash balance.")
        cash_after = max(cash_after, 0.0)
        target_weights = dict(selected_target.weights)
        rebalance_increment = 1

    executions: list[PortfolioForwardExecution] = []
    for symbol in symbols:
        shares_delta = float(shares_after[symbol] - shares_before[symbol])
        signed_value_change = float(
            target_values[symbol] - current_values[symbol]
        )
        side: Literal["buy", "sell", "hold"] = (
            "buy"
            if signed_value_change > 0
            else "sell"
            if signed_value_change < 0
            else "hold"
        )
        raw_price = normalized_open[symbol]
        modeled_fill_price = _modeled_fill_price(
            raw_price,
            side,
            safe_state.slippage_rate,
        )
        executions.append(
            PortfolioForwardExecution(
                symbol=symbol,
                session=session_label,
                side=side,
                raw_open_price=raw_price,
                modeled_fill_price=modeled_fill_price,
                shares_before=float(shares_before[symbol]),
                shares_after=float(shares_after[symbol]),
                shares_delta=shares_delta,
                current_value=float(current_values[symbol]),
                target_value=float(target_values[symbol]),
                traded_notional=float(traded_values[symbol]),
                fee=float(fee_values[symbol]),
                slippage=float(slippage_values[symbol]),
                total_cost=float(fee_values[symbol] + slippage_values[symbol]),
            )
        )

    session_turnover_notional = float(traded_values.sum())
    session_fee = float(fee_values.sum())
    session_slippage = float(slippage_values.sum())
    close_values = pd.Series(
        {
            symbol: float(shares_after[symbol] * normalized_close[symbol])
            for symbol in symbols
        },
        dtype=float,
    )
    equity = float(cash_after + close_values.sum())
    if not np.isfinite(equity) or equity <= 0:
        raise ValueError("Portfolio close equity must be finite and positive.")
    realized_weights = {
        symbol: float(close_values[symbol] / equity) for symbol in symbols
    }
    peak_equity = max(safe_state.peak_equity, equity)
    current_drawdown = equity / peak_equity - 1
    next_data = safe_state.model_dump(mode="python")
    next_data.update(
        {
            "session": session_label,
            "cash": cash_after,
            "shares": {
                symbol: float(shares_after[symbol]) for symbol in symbols
            },
            "equity": equity,
            "total_return": equity / safe_state.initial_cash - 1,
            "peak_equity": peak_equity,
            "max_drawdown": min(safe_state.max_drawdown, current_drawdown),
            "last_prices": dict(normalized_close),
            "target_weights": target_weights,
            "realized_weights": realized_weights,
            "pending_target": None,
            "valuation_count": safe_state.valuation_count + 1,
            "rebalance_count": safe_state.rebalance_count + rebalance_increment,
            "turnover_ratio": (
                safe_state.turnover_ratio
                + session_turnover_notional / portfolio_value
            ),
            "turnover_notional": (
                safe_state.turnover_notional + session_turnover_notional
            ),
            "fee_paid": safe_state.fee_paid + session_fee,
            "slippage_paid": safe_state.slippage_paid + session_slippage,
            "total_cost": (
                safe_state.total_cost + session_fee + session_slippage
            ),
        }
    )
    next_state = PortfolioForwardState.model_validate(next_data)
    return PortfolioForwardAdvance(state=next_state, executions=tuple(executions))


def _validate_target_for_current_state(
    state: PortfolioForwardState,
    target: PortfolioForwardTarget,
) -> None:
    _validate_target_compatibility(state, target)
    if state.session is None:
        raise ValueError(
            "Portfolio state needs a finalized information session before queuing a target."
        )
    if target.information_session != state.session:
        raise ValueError(
            "Target information session must equal the portfolio state session."
        )
    if state.method == "initial_equal_hold" and state.rebalance_count > 0:
        raise ValueError("Initial equal-hold target has already been executed.")


def _validate_target_compatibility(
    state: PortfolioForwardState,
    target: PortfolioForwardTarget,
) -> None:
    if target.method != state.method:
        raise ValueError("Target method does not match portfolio state.")
    if set(target.weights) != set(state.symbols):
        raise ValueError("Target weights must exactly match portfolio state symbols.")
    if state.method in {"initial_equal_hold", "periodic_equal"}:
        _validate_equal_weights(target.weights)
    elif (
        max(target.weights.values())
        > state.maximum_asset_weight + _WEIGHT_TOLERANCE
    ):
        raise ValueError("Target weights exceed the configured maximum asset weight.")


def _validate_equal_weights(
    weights: Mapping[str, float],
    *,
    allow_zero: bool = False,
) -> None:
    if allow_zero and all(
        isclose(value, 0.0, rel_tol=0, abs_tol=_WEIGHT_TOLERANCE)
        for value in weights.values()
    ):
        return
    expected = 1 / len(weights)
    if any(
        not isclose(value, expected, rel_tol=0, abs_tol=_WEIGHT_TOLERANCE)
        for value in weights.values()
    ):
        raise ValueError("Equal-weight methods require equal target weights.")


def _validated_state_copy(state: PortfolioForwardState) -> PortfolioForwardState:
    if not isinstance(state, PortfolioForwardState):
        raise ValueError("A PortfolioForwardState instance is required.")
    return PortfolioForwardState.model_validate(state.model_dump(mode="python"))


def _validated_target_copy(
    target: PortfolioForwardTarget | None,
) -> PortfolioForwardTarget | None:
    if target is None:
        return None
    if not isinstance(target, PortfolioForwardTarget):
        raise ValueError("A PortfolioForwardTarget instance is required.")
    return PortfolioForwardTarget.model_validate(target.model_dump(mode="python"))


def _exact_positive_price_map(
    values: Mapping[str, float],
    symbols: tuple[str, ...],
    label: str,
) -> dict[str, float]:
    normalized = _canonical_numeric_map(values, label)
    if set(normalized) != set(symbols):
        raise ValueError(f"{label} must exactly match portfolio state symbols.")
    if min(normalized.values()) <= 0:
        raise ValueError(f"{label} must be positive.")
    return {symbol: normalized[symbol] for symbol in symbols}


def _canonical_numeric_map(value: object, label: str) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a symbol-to-number mapping.")
    result: dict[str, float] = {}
    for raw_symbol, raw_value in value.items():
        symbol = _canonical_symbol(raw_symbol)
        if symbol in result:
            raise ValueError(f"{label} symbols must be unique after normalization.")
        if isinstance(raw_value, bool):
            raise ValueError(f"{label} values must be finite numbers.")
        try:
            numeric_value = float(raw_value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{label} values must be finite numbers.") from error
        if not np.isfinite(numeric_value):
            raise ValueError(f"{label} values must be finite numbers.")
        result[symbol] = numeric_value
    return result


def _canonical_symbol(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Portfolio symbols must be strings.")
    symbol = value.strip().upper()
    if not symbol:
        raise ValueError("Portfolio symbols must not be empty.")
    return symbol


def _normalize_session(value: object) -> str:
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Portfolio session must be a valid timestamp.") from error
    if pd.isna(timestamp):
        raise ValueError("Portfolio session must be a valid timestamp.")
    timestamp = (
        timestamp.tz_localize("UTC")
        if timestamp.tzinfo is None
        else timestamp.tz_convert("UTC")
    )
    return str(timestamp.normalize().isoformat())


def _modeled_fill_price(
    raw_open_price: float,
    side: Literal["buy", "sell", "hold"],
    slippage_rate: float,
) -> float:
    if side == "buy":
        return raw_open_price * (1 + slippage_rate)
    if side == "sell":
        return raw_open_price * (1 - slippage_rate)
    return raw_open_price


def _require_accounting_close(
    actual: float,
    expected: float,
    message: str,
) -> None:
    if not isclose(
        actual,
        expected,
        rel_tol=_ACCOUNTING_RELATIVE_TOLERANCE,
        abs_tol=_ACCOUNTING_ABSOLUTE_TOLERANCE,
    ):
        raise ValueError(message)
