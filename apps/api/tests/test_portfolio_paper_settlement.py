from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction

import pytest
import quantsieve_api.portfolio_paper_settlement as settlement_module
from pydantic import ValidationError
from quantsieve_api.portfolio_paper_contracts import (
    CertifiedPortfolioBar,
    PortfolioPaperBasketContract,
    PortfolioPaperDecisionReceipt,
    PortfolioPaperInstrumentContract,
    canonical_payload_hash,
    certify_binance_daily_bar,
    certify_portfolio_paper_decision,
)
from quantsieve_api.portfolio_paper_execution import (
    PortfolioPaperExecutionBatch,
    build_portfolio_paper_activation_opening_batch,
)
from quantsieve_api.portfolio_paper_settlement import (
    PortfolioPaperCloseBarSet,
    PortfolioPaperCloseValuationCommand,
    PortfolioPaperModeledCloseAccount,
    PortfolioPaperModeledCloseSettlement,
    build_portfolio_paper_close_valuation,
)
from quantsieve_engine import (
    PortfolioForwardState,
    PortfolioForwardTarget,
    initialize_portfolio_forward_state,
)
from quantsieve_providers import BinanceSpotTradingRules, ExecutionQuote

TRACK_ID = "b" * 32
CONFIGURATION_HASH = "c" * 64
INFORMATION_OPEN = datetime(2026, 7, 24, tzinfo=UTC)
INFORMATION_CLOSE = INFORMATION_OPEN + timedelta(days=1) - timedelta(milliseconds=1)
RULES_VERIFIED_AT = datetime(2026, 7, 24, 23, 50, tzinfo=UTC)
BASKET_CREATED_AT = RULES_VERIFIED_AT + timedelta(minutes=2)
INFORMATION_OBSERVED_AT = INFORMATION_CLOSE + timedelta(minutes=2, seconds=1)
DECIDED_AT = INFORMATION_OBSERVED_AT + timedelta(seconds=1)
PERSISTED_AT = DECIDED_AT + timedelta(milliseconds=100)
OPENING_ACCEPTED_AT = datetime(2026, 7, 25, 0, 3, tzinfo=UTC)
EXECUTION_SESSION = datetime(2026, 7, 25, tzinfo=UTC)
SETTLEMENT_CLOSE = (
    EXECUTION_SESSION + timedelta(days=1) - timedelta(milliseconds=1)
)
SETTLEMENT_FINALIZED = SETTLEMENT_CLOSE + timedelta(minutes=2)
SETTLEMENT_ACCEPTED_AT = SETTLEMENT_FINALIZED + timedelta(seconds=10)
SETTLED_AT = SETTLEMENT_ACCEPTED_AT + timedelta(seconds=1)
SYMBOLS = (
    ("BTCUSDT", "BTC"),
    ("ETHUSDT", "ETH"),
    ("BNBUSDT", "BNB"),
    ("SOLUSDT", "SOL"),
    ("XRPUSDT", "XRP"),
    ("ADAUSDT", "ADA"),
)


def canonical_exact(value: object) -> str:
    exact = Decimal(str(value))
    if exact == 0:
        return "0"
    text = format(exact, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def basket(
    size: int,
    *,
    step: str = "0.00001",
) -> PortfolioPaperBasketContract:
    instruments = []
    for symbol, base_asset in SYMBOLS[:size]:
        rules = BinanceSpotTradingRules(
            symbol=symbol,
            base_asset=base_asset,
            quote_asset="USDT",
            status="TRADING",
            spot_trading_allowed=True,
            order_types=("LIMIT", "MARKET"),
            lot_step_size=Decimal(step),
            lot_min_quantity=Decimal(step),
            lot_max_quantity=Decimal("1000000000000000000000000000000"),
            market_step_size=Decimal(step),
            market_min_quantity=Decimal(step),
            market_max_quantity=Decimal("1000000000000000000000000000000"),
            min_notional=Decimal("0.00000000000000000000000000001"),
            min_notional_applies_to_market=True,
            max_notional=None,
            max_notional_applies_to_market=False,
            notional_average_price_minutes=5,
            verified_at=RULES_VERIFIED_AT,
        )
        instruments.append(
            PortfolioPaperInstrumentContract(
                symbol=symbol,
                base_asset=base_asset,
                quote_currency="USDT",
                rules=rules,
                created_at=BASKET_CREATED_AT,
            )
        )
    return PortfolioPaperBasketContract(
        instruments=tuple(instruments),
        created_at=BASKET_CREATED_AT,
    )


def target(size: int) -> PortfolioForwardTarget:
    weight = 1 / size
    return PortfolioForwardTarget(
        method="periodic_equal",
        information_session=INFORMATION_OPEN.isoformat(),
        weights={
            symbol: weight
            for symbol, _base_asset in SYMBOLS[:size]
        },
    )


def certified_bar(
    contract: PortfolioPaperInstrumentContract,
    *,
    open_time: datetime,
    close_price: float | str,
    observed_at: datetime,
) -> CertifiedPortfolioBar:
    close_time = open_time + timedelta(days=1) - timedelta(milliseconds=1)
    finalized_at = close_time + timedelta(minutes=2)
    exchange_time = observed_at - timedelta(milliseconds=100)
    exact_close = canonical_exact(close_price)
    close_projection = float(Decimal(exact_close))
    high_projection = close_projection * 1.02
    low_projection = close_projection * 0.98
    return certify_binance_daily_bar(
        {
            "symbol": contract.symbol,
            "interval": "1d",
            "open_time": open_time.isoformat(),
            "close_time": close_time.isoformat(),
            "finalized_at": finalized_at.isoformat(),
            "observed_at": observed_at.isoformat(),
            "exchange_server_time": exchange_time.isoformat(),
            "clock_checked_at": exchange_time.isoformat(),
            "exchange_clock_verified": True,
            "open": close_projection,
            "high": high_projection,
            "low": low_projection,
            "close": close_projection,
            "volume": 10_000,
            "exact_open": exact_close,
            "exact_high": canonical_exact(high_projection),
            "exact_low": canonical_exact(low_projection),
            "exact_close": exact_close,
            "exact_volume": "10000",
            "finalized": True,
        },
        contract,
    )


def opening_batch_and_state(
    size: int,
    *,
    step: str = "0.00001",
    initial_cash: Decimal = Decimal("100000"),
) -> tuple[
    PortfolioPaperExecutionBatch,
    PortfolioForwardState,
    PortfolioPaperBasketContract,
]:
    execution_basket = basket(size, step=step)
    pending_target = target(size)
    information_bars = tuple(
        certified_bar(
            contract,
            open_time=INFORMATION_OPEN,
            close_price=100 + index * 10,
            observed_at=INFORMATION_OBSERVED_AT + timedelta(milliseconds=index),
        )
        for index, contract in enumerate(execution_basket.instruments)
    )
    certificate = certify_portfolio_paper_decision(
        target=pending_target,
        bars=information_bars,
        basket=execution_basket,
        configuration_hash=CONFIGURATION_HASH,
        volatility_lookback=60,
        maximum_asset_weight=0.6,
        decided_at=DECIDED_AT,
    )
    receipt = PortfolioPaperDecisionReceipt(
        track_id=TRACK_ID,
        decision_id=certificate.decision_id,
        certificate=certificate,
        certificate_hash=canonical_payload_hash(
            certificate.model_dump(mode="json")
        ),
        persisted_at=PERSISTED_AT,
    )
    quotes = {}
    for index, contract in enumerate(execution_basket.instruments):
        ask = Decimal(100 + index * 10)
        quotes[contract.symbol] = ExecutionQuote(
            symbol=contract.symbol,
            provider="binance",
            venue="Binance Spot",
            bid_price=ask - Decimal("0.01"),
            ask_price=ask,
            bid_quantity=Decimal(
                "1000000000000000000000000000000"
            ),
            ask_quantity=Decimal(
                "1000000000000000000000000000000"
            ),
            notional_reference_price=ask,
            notional_reference_kind="exchange_reference",
            notional_reference_window_minutes=None,
            notional_reference_at=OPENING_ACCEPTED_AT
            - timedelta(milliseconds=1500),
            notional_reference_observed_at=OPENING_ACCEPTED_AT
            - timedelta(milliseconds=1400),
            exchange_reference_available=True,
            exchange_reference_at=OPENING_ACCEPTED_AT
            - timedelta(milliseconds=1500),
            exchange_reference_observed_at=OPENING_ACCEPTED_AT
            - timedelta(milliseconds=1400),
            request_started_at=OPENING_ACCEPTED_AT - timedelta(seconds=2),
            observed_at=OPENING_ACCEPTED_AT - timedelta(seconds=1),
            exchange_server_time=OPENING_ACCEPTED_AT
            - timedelta(milliseconds=500),
            clock_checked_at=OPENING_ACCEPTED_AT
            - timedelta(milliseconds=500),
            cache_used=False,
        )
    opening = build_portfolio_paper_activation_opening_batch(
        track_id=TRACK_ID,
        state_revision=0,
        available_cash=initial_cash,
        configuration_hash=CONFIGURATION_HASH,
        decision_receipt=receipt,
        basket=execution_basket,
        execution_quotes=quotes,
        fee_rate=Decimal("0.0003"),
        slippage_rate=Decimal("0.0002"),
        accepted_at=OPENING_ACCEPTED_AT,
    )
    state = initialize_portfolio_forward_state(
        opening.command.symbols,
        float(initial_cash),
        "periodic_equal",
        fee_rate=0.0003,
        slippage_rate=0.0002,
        volatility_lookback=60,
        maximum_asset_weight=0.6,
        pending_target=pending_target,
    )
    return opening, state, execution_basket


def settlement_bars(
    execution_basket: PortfolioPaperBasketContract,
    *,
    close_prices: tuple[float | str, ...] | None = None,
) -> tuple[CertifiedPortfolioBar, ...]:
    prices = close_prices or tuple(
        110 + index * 8 for index in range(len(execution_basket.instruments))
    )
    return tuple(
        certified_bar(
            contract,
            open_time=EXECUTION_SESSION,
            close_price=prices[index],
            observed_at=SETTLEMENT_FINALIZED
            + timedelta(seconds=1, milliseconds=index),
        )
        for index, contract in enumerate(execution_basket.instruments)
    )


def settle(
    size: int = 2,
    *,
    step: str = "0.00001",
    initial_cash: Decimal = Decimal("100000"),
    close_prices: tuple[float | str, ...] | None = None,
) -> tuple[
    PortfolioPaperModeledCloseSettlement,
    PortfolioPaperExecutionBatch,
    PortfolioForwardState,
    tuple[CertifiedPortfolioBar, ...],
]:
    opening, state, execution_basket = opening_batch_and_state(
        size,
        step=step,
        initial_cash=initial_cash,
    )
    bars = settlement_bars(
        execution_basket,
        close_prices=close_prices,
    )
    result = build_portfolio_paper_close_valuation(
        opening_batch=opening,
        bars=bars,
        initial_state=state,
        accepted_at=SETTLEMENT_ACCEPTED_AT,
        settled_at=SETTLED_AT,
    )
    return result, opening, state, bars


@pytest.mark.parametrize("size", [2, 6])
def test_values_two_or_six_opening_fills_without_a_second_trade(size: int) -> None:
    result, opening, state, bars = settle(size)

    assert result.settlement_kind == "modeled_paper_close_valuation"
    assert result.command.source_state_revision == 0
    assert result.command.target_state_revision == 1
    assert result.command.symbols == opening.command.symbols
    assert result.bar_set.symbols == opening.command.symbols
    assert result.account.cash == opening.ending_cash
    assert result.account.opening_fee == opening.total_fee
    assert result.account.opening_slippage == opening.total_slippage
    assert result.account.opening_cash_debit == opening.total_cash_debit
    assert result.account.close_trade_count == 0
    assert result.account.close_fee == 0
    assert result.account.close_slippage == 0
    assert result.forward_state.valuation_count == state.valuation_count + 1
    assert result.forward_state.rebalance_count == 1
    assert result.forward_state.pending_target is None
    assert result.forward_state.session == EXECUTION_SESSION.isoformat()
    assert tuple(position.symbol for position in result.account.positions) == tuple(
        bar.symbol for bar in bars
    )
    assert tuple(position.quantity for position in result.account.positions) == tuple(
        fill.quantity for fill in opening.fills
    )


def test_exact_account_uses_certified_close_values_and_preserves_cash_remainder() -> None:
    result, opening, _state, _bars = settle(
        close_prices=(125.5, 80.25),
    )
    expected_values = tuple(
        fill.quantity * Decimal(str(close))
        for fill, close in zip(
            opening.fills,
            (125.5, 80.25),
            strict=True,
        )
    )
    expected_equity = opening.ending_cash + sum(expected_values, Decimal())

    assert tuple(
        position.market_value for position in result.account.positions
    ) == expected_values
    assert result.account.holdings_value == sum(expected_values, Decimal())
    assert result.account.equity == expected_equity
    assert result.account.total_return.fraction == (
        Fraction(expected_equity - opening.command.available_cash)
        / Fraction(opening.command.available_cash)
    )
    assert result.forward_state.cash == float(opening.ending_cash)
    assert result.forward_state.equity == float(expected_equity)
    assert result.account.turnover_notional == opening.total_raw_notional
    assert result.account.turnover_ratio.fraction == (
        Fraction(opening.total_raw_notional)
        / Fraction(opening.command.available_cash)
    )


def test_settlement_uses_exact_provider_close_not_collapsed_float_projection() -> None:
    exact_closes = (
        "1.00000000000000001",
        "0.99999999999999999",
    )
    result, opening, _state, bars = settle(close_prices=exact_closes)

    assert tuple(bar.close for bar in bars) == (1.0, 1.0)
    assert tuple(bar.exact_close for bar in bars) == exact_closes
    assert tuple(
        position.certified_close_price_decimal
        for position in result.account.positions
    ) == tuple(Decimal(value) for value in exact_closes)
    assert result.account.positions[0].market_value == (
        opening.fills[0].quantity * Decimal(exact_closes[0])
    )
    assert result.account.positions[1].market_value == (
        opening.fills[1].quantity * Decimal(exact_closes[1])
    )


def test_29_digit_quantity_is_exact_in_account_and_only_float_in_compat_state() -> None:
    result, opening, _state, _bars = settle(
        step="0.00000000000000000000000000001",
        initial_cash=Decimal("1.2345678901234567890123456789"),
        close_prices=(1.25, 0.75),
    )

    assert opening.fills[0].quantity.as_tuple().exponent == -29
    assert result.account.positions[0].quantity == opening.fills[0].quantity
    assert result.account.positions[0].market_value == (
        opening.fills[0].quantity * Decimal("1.25")
    )
    assert result.forward_state.shares["BTCUSDT"] == float(
        opening.fills[0].quantity
    )


def test_exact_settlement_is_independent_of_decimal_context_precision() -> None:
    opening, state, execution_basket = opening_batch_and_state(
        2,
        step="0.00000000000000000000000000001",
        initial_cash=Decimal("1.2345678901234567890123456789"),
    )
    bars = settlement_bars(
        execution_basket,
        close_prices=(1.25, 0.75),
    )
    with localcontext() as context:
        context.prec = 6
        constrained = build_portfolio_paper_close_valuation(
            opening_batch=opening,
            bars=bars,
            initial_state=state,
            accepted_at=SETTLEMENT_ACCEPTED_AT,
            settled_at=SETTLED_AT,
        )

    ordinary = build_portfolio_paper_close_valuation(
        opening_batch=opening,
        bars=bars,
        initial_state=state,
        accepted_at=SETTLEMENT_ACCEPTED_AT,
        settled_at=SETTLED_AT,
    )
    assert constrained.account == ordinary.account
    assert constrained.command == ordinary.command
    assert constrained.settlement_hash == ordinary.settlement_hash


def test_price_gains_and_losses_update_exact_return_peak_and_drawdown() -> None:
    gain, opening, _state, _bars = settle(close_prices=(200.0, 220.0))
    loss, _opening, _state, _bars = settle(close_prices=(40.0, 44.0))

    assert gain.account.equity > opening.command.available_cash
    assert gain.account.peak_equity == gain.account.equity
    assert gain.account.current_drawdown.fraction == 0
    assert gain.account.max_drawdown.fraction == 0
    assert loss.account.equity < opening.command.available_cash
    assert loss.account.peak_equity == opening.command.available_cash
    assert loss.account.current_drawdown.fraction < 0
    assert (
        loss.account.max_drawdown.fraction
        == loss.account.current_drawdown.fraction
    )


def test_json_roundtrip_and_every_top_level_hash_revalidate() -> None:
    result, _opening, _state, _bars = settle()

    assert (
        PortfolioPaperModeledCloseSettlement.model_validate_json(
            result.model_dump_json()
        )
        == result
    )
    with pytest.raises(ValidationError, match="command hash"):
        PortfolioPaperCloseValuationCommand.model_validate(
            result.command.model_copy(update={"command_hash": "0" * 64})
        )
    with pytest.raises(ValidationError, match="bar-set hash"):
        PortfolioPaperCloseBarSet.model_validate(
            result.bar_set.model_copy(update={"bar_set_hash": "0" * 64})
        )
    with pytest.raises(ValidationError, match="account hash"):
        PortfolioPaperModeledCloseAccount.model_validate(
            result.account.model_copy(update={"account_hash": "0" * 64})
        )
    with pytest.raises(ValidationError, match="state hash"):
        PortfolioPaperModeledCloseSettlement.model_validate(
            result.model_copy(update={"forward_state_hash": "0" * 64})
        )
    with pytest.raises(ValidationError, match="settlement hash"):
        PortfolioPaperModeledCloseSettlement.model_validate(
            result.model_copy(update={"settlement_hash": "0" * 64})
        )


def test_rejects_account_valued_from_different_certified_bar_revisions() -> None:
    opening, state, execution_basket = opening_batch_and_state(2)
    low_bars = settlement_bars(
        execution_basket,
        close_prices=(120.0, 118.0),
    )
    high_bars = settlement_bars(
        execution_basket,
        close_prices=(240.0, 236.0),
    )
    low = build_portfolio_paper_close_valuation(
        opening_batch=opening,
        bars=low_bars,
        initial_state=state,
        accepted_at=SETTLEMENT_ACCEPTED_AT,
        settled_at=SETTLED_AT,
    )
    high = build_portfolio_paper_close_valuation(
        opening_batch=opening,
        bars=high_bars,
        initial_state=state,
        accepted_at=SETTLEMENT_ACCEPTED_AT,
        settled_at=SETTLED_AT,
    )
    forged = high.model_dump(mode="python")
    forged.update(
        {
            "account": low.account,
            "forward_state": low.forward_state,
            "forward_state_hash": low.forward_state_hash,
        }
    )
    forged["settlement_hash"] = settlement_module._canonical_hash(
        {
            key: value
            for key, value in forged.items()
            if key != "settlement_hash"
        }
    )

    with pytest.raises(
        ValidationError,
        match="exact certified bar closes",
    ):
        PortfolioPaperModeledCloseSettlement.model_validate(forged)


def test_stable_idempotency_excludes_observation_timing_but_hashes_full_evidence() -> None:
    first, opening, state, bars = settle()
    delayed_bars = tuple(
        bar.model_copy(
            update={
                "observed_at": bar.observed_at + timedelta(seconds=1),
            }
        )
        for bar in bars
    )
    second = build_portfolio_paper_close_valuation(
        opening_batch=opening,
        bars=delayed_bars,
        initial_state=state,
        accepted_at=SETTLEMENT_ACCEPTED_AT + timedelta(seconds=1),
        settled_at=SETTLED_AT + timedelta(seconds=1),
    )

    assert second.command.idempotency_key == first.command.idempotency_key
    assert second.bar_set.revision_set_hash == first.bar_set.revision_set_hash
    assert second.bar_set.bar_set_hash != first.bar_set.bar_set_hash
    assert second.command.command_hash != first.command.command_hash
    assert second.settlement_hash != first.settlement_hash


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate", "reversed"])
def test_rejects_incomplete_extra_duplicate_or_reordered_bar_sets(
    mutation: str,
) -> None:
    _result, opening, state, bars = settle()
    if mutation == "missing":
        forged = bars[:-1]
    elif mutation == "extra":
        forged = (*bars, bars[0])
    elif mutation == "duplicate":
        forged = (bars[0], bars[0])
    else:
        forged = tuple(reversed(bars))

    with pytest.raises(ValueError):
        build_portfolio_paper_close_valuation(
            opening_batch=opening,
            bars=forged,
            initial_state=state,
            accepted_at=SETTLEMENT_ACCEPTED_AT,
            settled_at=SETTLED_AT,
        )


@pytest.mark.parametrize("mutation", ["wrong_day", "unfinished", "future"])
def test_rejects_wrong_day_unfinished_or_future_bar(mutation: str) -> None:
    _result, opening, state, bars = settle()
    first = bars[0]
    if mutation == "wrong_day":
        forged_first = first.model_copy(
            update={"session": INFORMATION_OPEN.isoformat()}
        )
    elif mutation == "unfinished":
        forged_first = first.model_copy(
            update={
                "finalized_at": first.close_time + timedelta(minutes=1)
            }
        )
    else:
        forged_first = first.model_copy(
            update={"observed_at": SETTLEMENT_ACCEPTED_AT + timedelta(seconds=1)}
        )

    with pytest.raises(ValueError):
        build_portfolio_paper_close_valuation(
            opening_batch=opening,
            bars=(forged_first, *bars[1:]),
            initial_state=state,
            accepted_at=SETTLEMENT_ACCEPTED_AT,
            settled_at=SETTLED_AT,
        )


def test_rejects_forged_batch_bar_revision_and_initial_state() -> None:
    _result, opening, state, bars = settle()
    forged_batch = opening.model_copy(update={"batch_hash": "0" * 64})
    forged_bar = bars[0].model_copy(update={"revision_hash": "0" * 64})
    forged_state = state.model_copy(update={"fee_rate": 0.01})

    with pytest.raises(ValueError, match="opening batch"):
        build_portfolio_paper_close_valuation(
            opening_batch=forged_batch,
            bars=bars,
            initial_state=state,
            accepted_at=SETTLEMENT_ACCEPTED_AT,
            settled_at=SETTLED_AT,
        )
    with pytest.raises(ValueError, match="bar"):
        build_portfolio_paper_close_valuation(
            opening_batch=opening,
            bars=(forged_bar, *bars[1:]),
            initial_state=state,
            accepted_at=SETTLEMENT_ACCEPTED_AT,
            settled_at=SETTLED_AT,
        )
    with pytest.raises(ValueError, match="configuration"):
        build_portfolio_paper_close_valuation(
            opening_batch=opening,
            bars=bars,
            initial_state=forged_state,
            accepted_at=SETTLEMENT_ACCEPTED_AT,
            settled_at=SETTLED_AT,
        )


def test_rejects_settlement_before_evidence_and_does_not_mutate_opening() -> None:
    _result, opening, state, bars = settle()
    before = opening.model_dump(mode="python")

    with pytest.raises(ValueError, match="before evidence acceptance"):
        build_portfolio_paper_close_valuation(
            opening_batch=opening,
            bars=bars,
            initial_state=state,
            accepted_at=SETTLEMENT_ACCEPTED_AT,
            settled_at=SETTLEMENT_ACCEPTED_AT - timedelta(microseconds=1),
        )
    assert opening.model_dump(mode="python") == before
