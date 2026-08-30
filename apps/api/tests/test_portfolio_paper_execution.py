from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction

import pytest
from pydantic import ValidationError
from quantsieve_api.portfolio_paper_contracts import (
    PortfolioPaperBasketContract,
    PortfolioPaperDecisionReceipt,
    PortfolioPaperInstrumentContract,
    canonical_payload_hash,
    certify_binance_daily_bar,
    certify_portfolio_paper_decision,
)
from quantsieve_api.portfolio_paper_execution import (
    PortfolioPaperExecutionBatch,
    PortfolioPaperExecutionCommand,
    PortfolioPaperModeledFill,
    build_portfolio_paper_activation_opening_batch,
)
from quantsieve_engine import PortfolioForwardTarget
from quantsieve_providers import BinanceSpotTradingRules, ExecutionQuote

TRACK_ID = "b" * 32
CONFIGURATION_HASH = "c" * 64
RULES_VERIFIED_AT = datetime(2026, 7, 24, 23, 50, tzinfo=UTC)
BASKET_CREATED_AT = RULES_VERIFIED_AT + timedelta(minutes=2)
BAR_CLOSE_AT = datetime(2026, 7, 24, 23, 59, 59, 999000, tzinfo=UTC)
BAR_OBSERVED_AT = BAR_CLOSE_AT + timedelta(minutes=2, milliseconds=500)
DECIDED_AT = BAR_OBSERVED_AT + timedelta(milliseconds=500)
PERSISTED_AT = DECIDED_AT + timedelta(milliseconds=100)
ACCEPTED_AT = datetime(2026, 7, 25, 0, 3, tzinfo=UTC)


def basket(
    *,
    lot_step_size: str = "0.00001",
    market_step_size: str = "0.00001",
    lot_min_quantity: str = "0.00001",
    market_min_quantity: str = "0.00001",
    lot_max_quantity: str = "1000000",
    market_max_quantity: str = "1000000",
    min_notional: str = "5",
    min_notional_applies_to_market: bool = True,
    max_notional: str | None = None,
    max_notional_applies_to_market: bool = False,
) -> PortfolioPaperBasketContract:
    contracts = []
    for symbol, base_asset in (("BTCUSDT", "BTC"), ("ETHUSDT", "ETH")):
        rules = BinanceSpotTradingRules(
            symbol=symbol,
            base_asset=base_asset,
            quote_asset="USDT",
            status="TRADING",
            spot_trading_allowed=True,
            order_types=("LIMIT", "MARKET"),
            lot_step_size=Decimal(lot_step_size),
            lot_min_quantity=Decimal(lot_min_quantity),
            lot_max_quantity=Decimal(lot_max_quantity),
            market_step_size=Decimal(market_step_size),
            market_min_quantity=Decimal(market_min_quantity),
            market_max_quantity=Decimal(market_max_quantity),
            min_notional=Decimal(min_notional),
            min_notional_applies_to_market=min_notional_applies_to_market,
            max_notional=None if max_notional is None else Decimal(max_notional),
            max_notional_applies_to_market=max_notional_applies_to_market,
            notional_average_price_minutes=5,
            verified_at=RULES_VERIFIED_AT,
        )
        contracts.append(
            PortfolioPaperInstrumentContract(
                symbol=symbol,
                base_asset=base_asset,
                quote_currency="USDT",
                rules=rules,
                created_at=BASKET_CREATED_AT,
            )
        )
    return PortfolioPaperBasketContract(
        instruments=tuple(contracts),
        created_at=BASKET_CREATED_AT,
    )


def decision_receipt(
    execution_basket: PortfolioPaperBasketContract | None = None,
) -> PortfolioPaperDecisionReceipt:
    execution_basket = execution_basket or basket()
    target = PortfolioForwardTarget(
        method="periodic_equal",
        information_session="2026-07-24",
        weights={"BTCUSDT": 0.5, "ETHUSDT": 0.5},
    )
    bars = []
    for contract, close in zip(
        execution_basket.instruments,
        (100_000.0, 4_000.0),
        strict=True,
    ):
        bars.append(
            certify_binance_daily_bar(
                {
                    "symbol": contract.symbol,
                    "interval": "1d",
                    "open_time": datetime(2026, 7, 24, tzinfo=UTC).isoformat(),
                    "close_time": BAR_CLOSE_AT.isoformat(),
                    "finalized_at": (
                        BAR_CLOSE_AT + timedelta(minutes=2)
                    ).isoformat(),
                    "observed_at": BAR_OBSERVED_AT.isoformat(),
                    "exchange_server_time": (
                        BAR_OBSERVED_AT - timedelta(milliseconds=100)
                    ).isoformat(),
                    "clock_checked_at": (
                        BAR_OBSERVED_AT - timedelta(milliseconds=100)
                    ).isoformat(),
                    "exchange_clock_verified": True,
                    "open": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": 1_000,
                    "exact_open": str(close),
                    "exact_high": str(close * 1.01),
                    "exact_low": str(close * 0.99),
                    "exact_close": str(close),
                    "exact_volume": "1000",
                    "finalized": True,
                },
                contract,
            )
        )
    certificate = certify_portfolio_paper_decision(
        target=target,
        bars=bars,
        basket=execution_basket,
        configuration_hash=CONFIGURATION_HASH,
        volatility_lookback=60,
        maximum_asset_weight=0.6,
        decided_at=DECIDED_AT,
    )
    return PortfolioPaperDecisionReceipt(
        track_id=TRACK_ID,
        decision_id=certificate.decision_id,
        certificate=certificate,
        certificate_hash=canonical_payload_hash(
            certificate.model_dump(mode="json")
        ),
        persisted_at=PERSISTED_AT,
    )


def quotes(
    *,
    btc_ask: str = "100000",
    eth_ask: str = "4000",
    ask_quantity: str = "1000000",
    reference_price: str | None = None,
) -> dict[str, ExecutionQuote]:
    result: dict[str, ExecutionQuote] = {}
    for symbol, ask in (("BTCUSDT", btc_ask), ("ETHUSDT", eth_ask)):
        ask_decimal = Decimal(ask)
        reference = (
            ask_decimal if reference_price is None else Decimal(reference_price)
        )
        result[symbol] = ExecutionQuote(
            symbol=symbol,
            provider="binance",
            venue="Binance Spot",
            bid_price=ask_decimal - Decimal("1"),
            ask_price=ask_decimal,
            bid_quantity=Decimal(ask_quantity),
            ask_quantity=Decimal(ask_quantity),
            notional_reference_price=reference,
            notional_reference_kind="exchange_reference",
            notional_reference_window_minutes=None,
            notional_reference_at=ACCEPTED_AT - timedelta(milliseconds=1500),
            notional_reference_observed_at=ACCEPTED_AT
            - timedelta(milliseconds=1400),
            exchange_reference_available=True,
            exchange_reference_at=ACCEPTED_AT - timedelta(milliseconds=1500),
            exchange_reference_observed_at=ACCEPTED_AT
            - timedelta(milliseconds=1400),
            request_started_at=ACCEPTED_AT - timedelta(seconds=2),
            observed_at=ACCEPTED_AT - timedelta(seconds=1),
            exchange_server_time=ACCEPTED_AT - timedelta(milliseconds=500),
            clock_checked_at=ACCEPTED_AT - timedelta(milliseconds=500),
            cache_used=False,
        )
    return result


def build(
    *,
    execution_basket: PortfolioPaperBasketContract | None = None,
    receipt: PortfolioPaperDecisionReceipt | None = None,
    execution_quotes: dict[str, ExecutionQuote] | None = None,
    available_cash: Decimal | float | int = Decimal("100000"),
    fee_rate: Decimal | float | int = Decimal("0.0003"),
    slippage_rate: Decimal | float | int = Decimal("0.0002"),
) -> PortfolioPaperExecutionBatch:
    execution_basket = execution_basket or basket()
    receipt = receipt or decision_receipt(execution_basket)
    return build_portfolio_paper_activation_opening_batch(
        track_id=TRACK_ID,
        state_revision=0,
        available_cash=available_cash,
        configuration_hash=CONFIGURATION_HASH,
        decision_receipt=receipt,
        basket=execution_basket,
        execution_quotes=execution_quotes or quotes(),
        fee_rate=fee_rate,
        slippage_rate=slippage_rate,
        accepted_at=ACCEPTED_AT,
    )


def test_builds_exact_all_or_nothing_modeled_opening_buys() -> None:
    batch = build()

    assert batch.command.execution_session == "2026-07-25T00:00:00+00:00"
    assert batch.command.state_revision == 0
    assert batch.command.symbols == ("BTCUSDT", "ETHUSDT")
    assert tuple(fill.symbol for fill in batch.fills) == batch.command.symbols
    assert all(fill.fill_kind == "modeled_paper_fill" for fill in batch.fills)
    assert all(fill.side == "buy" for fill in batch.fills)
    assert batch.fills[0].quantity == Decimal("0.49975")
    assert batch.fills[1].quantity == Decimal("12.49375")
    assert all(fill.raw_notional == Decimal("49975") for fill in batch.fills)
    assert all(fill.fee == Decimal("14.9925") for fill in batch.fills)
    assert all(fill.slippage == Decimal("9.995") for fill in batch.fills)
    assert all(fill.cash_debit == Decimal("49999.9875") for fill in batch.fills)
    assert batch.total_raw_notional == Decimal("99950")
    assert batch.total_fee == Decimal("29.985")
    assert batch.total_slippage == Decimal("19.99")
    assert batch.total_cash_debit == Decimal("99999.975")
    assert batch.ending_cash == Decimal("0.025")
    assert batch.total_cash_debit == (
        batch.total_raw_notional + batch.total_fee + batch.total_slippage
    )
    assert PortfolioPaperExecutionBatch.model_validate_json(
        batch.model_dump_json()
    ) == batch


def test_idempotency_excludes_quotes_but_evidence_hashes_do_not() -> None:
    execution_basket = basket()
    receipt = decision_receipt(execution_basket)
    first = build(execution_basket=execution_basket, receipt=receipt)
    changed_quotes = quotes(btc_ask="100001")
    second = build(
        execution_basket=execution_basket,
        receipt=receipt,
        execution_quotes=changed_quotes,
    )

    assert first.command.idempotency_key == second.command.idempotency_key
    assert first.command.command_hash == second.command.command_hash
    assert first.quote_set_hash != second.quote_set_hash
    assert first.fill_set_hash != second.fill_set_hash
    assert first.batch_hash != second.batch_hash


def test_exact_common_step_handles_twenty_nine_decimal_places() -> None:
    tiny_step_basket = basket(
        lot_step_size="0.00000000000000000000000000001",
        market_step_size="0.00000000000000000000000000002",
        lot_min_quantity="0.00000000000000000000000000002",
        market_min_quantity="0.00000000000000000000000000002",
    )
    batch_result = build(
        execution_basket=tiny_step_basket,
        receipt=decision_receipt(tiny_step_basket),
    )

    assert all(
        fill.common_step_size == Decimal("0.00000000000000000000000000002")
        for fill in batch_result.fills
    )
    assert all(
        (
            Fraction(fill.quantity)
            / Fraction("0.00000000000000000000000000002")
        ).denominator
        == 1
        for fill in batch_result.fills
    )


def test_economic_payload_and_hashes_ignore_ambient_decimal_precision() -> None:
    tiny_step_basket = basket(
        lot_step_size="0.00000000000000000000000000001",
        market_step_size="0.00000000000000000000000000002",
        lot_min_quantity="0.00000000000000000000000000002",
        market_min_quantity="0.00000000000000000000000000002",
    )
    fixed_receipt = decision_receipt(tiny_step_basket)
    fixed_quotes = quotes()
    batches: list[PortfolioPaperExecutionBatch] = []

    for precision in (6, 28, 60):
        with localcontext() as context:
            context.prec = precision
            batches.append(
                build(
                    execution_basket=tiny_step_basket,
                    receipt=fixed_receipt,
                    execution_quotes=fixed_quotes,
                )
            )

    expected = batches[0]
    expected_economic_fields = (
        expected.command.available_cash,
        expected.command.fee_rate,
        expected.command.slippage_rate,
        tuple(expected.command.target_weights.items()),
        tuple(
            (
                fill.quantity,
                fill.common_step_size,
                fill.target_weight,
                fill.target_cash_budget,
                fill.ask_price,
                fill.modeled_fill_price,
                fill.raw_notional,
                fill.notional_reference_price,
                fill.reference_notional,
                fill.fee_rate,
                fill.slippage_rate,
                fill.fee,
                fill.slippage,
                fill.cash_debit,
            )
            for fill in expected.fills
        ),
        expected.total_raw_notional,
        expected.total_fee,
        expected.total_slippage,
        expected.total_cash_debit,
        expected.ending_cash,
    )
    expected_hashes = (
        expected.command.idempotency_key,
        expected.command.command_hash,
        expected.quote_set_hash,
        expected.fill_set_hash,
        tuple(fill.quote_hash for fill in expected.fills),
        tuple(fill.fill_hash for fill in expected.fills),
        expected.batch_hash,
    )
    expected_payload = expected.model_dump(mode="json")

    assert any(abs(fill.quantity.as_tuple().exponent) == 29 for fill in expected.fills)
    for batch_result in batches[1:]:
        economic_fields = (
            batch_result.command.available_cash,
            batch_result.command.fee_rate,
            batch_result.command.slippage_rate,
            tuple(batch_result.command.target_weights.items()),
            tuple(
                (
                    fill.quantity,
                    fill.common_step_size,
                    fill.target_weight,
                    fill.target_cash_budget,
                    fill.ask_price,
                    fill.modeled_fill_price,
                    fill.raw_notional,
                    fill.notional_reference_price,
                    fill.reference_notional,
                    fill.fee_rate,
                    fill.slippage_rate,
                    fill.fee,
                    fill.slippage,
                    fill.cash_debit,
                )
                for fill in batch_result.fills
            ),
            batch_result.total_raw_notional,
            batch_result.total_fee,
            batch_result.total_slippage,
            batch_result.total_cash_debit,
            batch_result.ending_cash,
        )
        hashes = (
            batch_result.command.idempotency_key,
            batch_result.command.command_hash,
            batch_result.quote_set_hash,
            batch_result.fill_set_hash,
            tuple(fill.quote_hash for fill in batch_result.fills),
            tuple(fill.fill_hash for fill in batch_result.fills),
            batch_result.batch_hash,
        )

        assert economic_fields == expected_economic_fields
        assert batch_result.model_dump(mode="json") == expected_payload
        assert hashes == expected_hashes


@pytest.mark.parametrize(
    ("execution_basket", "execution_quotes"),
    [
        (
            basket(
                lot_min_quantity="100",
                market_min_quantity="100",
            ),
            quotes(),
        ),
        (
            basket(
                lot_max_quantity="0.1",
                market_max_quantity="0.1",
            ),
            quotes(),
        ),
        (
            basket(
                min_notional="100000",
                min_notional_applies_to_market=True,
            ),
            quotes(reference_price="1"),
        ),
        (
            basket(
                max_notional="10",
                max_notional_applies_to_market=True,
            ),
            quotes(reference_price="1000000"),
        ),
        (basket(), quotes(ask_quantity="0.01")),
    ],
)
def test_any_infeasible_symbol_rejects_the_entire_batch(
    execution_basket: PortfolioPaperBasketContract,
    execution_quotes: dict[str, ExecutionQuote],
) -> None:
    with pytest.raises(ValueError, match="all-or-nothing"):
        build(
            execution_basket=execution_basket,
            receipt=decision_receipt(execution_basket),
            execution_quotes=execution_quotes,
        )


def test_notional_rules_use_reference_price_but_costs_use_best_ask() -> None:
    execution_basket = basket(
        min_notional="40000",
        min_notional_applies_to_market=True,
    )
    low_reference = quotes(reference_price="1")

    with pytest.raises(ValueError, match="all-or-nothing"):
        build(
            execution_basket=execution_basket,
            receipt=decision_receipt(execution_basket),
            execution_quotes=low_reference,
        )

    accepted = build(
        execution_basket=execution_basket,
        receipt=decision_receipt(execution_basket),
        execution_quotes=quotes(reference_price="100000"),
    )
    assert accepted.fills[1].reference_notional != accepted.fills[1].raw_notional
    assert accepted.fills[1].fee == (
        accepted.fills[1].raw_notional * accepted.command.fee_rate
    )


def test_rejects_missing_duplicate_and_mismatched_quote_symbols() -> None:
    missing = quotes()
    missing.pop("ETHUSDT")
    with pytest.raises(ValueError, match="exactly match"):
        build(execution_quotes=missing)

    duplicate_case = quotes()
    duplicate_case["btcusdt"] = duplicate_case["BTCUSDT"]
    with pytest.raises(ValueError, match="duplicate"):
        build(execution_quotes=duplicate_case)

    mismatched = quotes()
    mismatched["BTCUSDT"] = mismatched["BTCUSDT"].model_copy(
        update={"symbol": "ETHUSDT"}
    )
    with pytest.raises(ValueError, match="key does not match"):
        build(execution_quotes=mismatched)


def test_rejects_same_identity_with_modified_certified_rules() -> None:
    certified_basket = basket()
    receipt = decision_receipt(certified_basket)
    payload = certified_basket.model_dump(mode="python")
    payload["instruments"][0]["rules"]["lot_max_quantity"] = Decimal("999999")
    modified_rules = PortfolioPaperBasketContract.model_validate(payload)
    assert modified_rules.identity == certified_basket.identity
    assert modified_rules.basket_hash != certified_basket.basket_hash

    with pytest.raises(ValueError, match="exactly match"):
        build(execution_basket=modified_rules, receipt=receipt)

    batch = build(execution_basket=certified_basket, receipt=receipt)
    command_payload = batch.command.model_dump(mode="python")
    command_payload["certified_basket_hash"] = "d" * 64
    with pytest.raises(ValidationError, match="exactly match"):
        PortfolioPaperExecutionCommand.model_validate(command_payload)


def test_revalidates_model_copy_forgery_and_non_finite_values() -> None:
    forged_quote = quotes()
    forged_quote["BTCUSDT"] = forged_quote["BTCUSDT"].model_copy(
        update={"ask_price": Decimal("NaN")}
    )
    with pytest.raises(ValueError, match="revalidation"):
        build(execution_quotes=forged_quote)

    valid = build()
    forged_fill = valid.fills[0].model_copy(
        update={"cash_debit": Decimal("Infinity")}
    )
    with pytest.raises(ValidationError):
        PortfolioPaperModeledFill.model_validate(
            forged_fill.model_dump(mode="python")
        )

    batch_payload = valid.model_dump(mode="python")
    batch_payload["ending_cash"] = Decimal("-1")
    with pytest.raises(ValidationError):
        PortfolioPaperExecutionBatch.model_validate(batch_payload)


def test_rejects_reordered_missing_and_repeated_modeled_fills() -> None:
    batch_result = build()
    payload = batch_result.model_dump(mode="python")
    payload["fills"] = tuple(reversed(batch_result.fills))
    with pytest.raises(ValidationError, match="canonical command symbol order"):
        PortfolioPaperExecutionBatch.model_validate(payload)

    payload = batch_result.model_dump(mode="python")
    payload["fills"] = (batch_result.fills[0],)
    with pytest.raises(ValidationError):
        PortfolioPaperExecutionBatch.model_validate(payload)

    payload = batch_result.model_dump(mode="python")
    payload["fills"] = (batch_result.fills[0], batch_result.fills[0])
    with pytest.raises(ValidationError):
        PortfolioPaperExecutionBatch.model_validate(payload)


def test_hashes_reject_tampering_even_when_accounting_still_looks_valid() -> None:
    batch_result = build()
    quote_payload = batch_result.fills[0].model_dump(mode="python")
    quote_payload["quote_hash"] = "e" * 64
    with pytest.raises(ValidationError, match="quote hash"):
        PortfolioPaperModeledFill.model_validate(quote_payload)

    command_payload = batch_result.command.model_dump(mode="python")
    command_payload["command_hash"] = "e" * 64
    with pytest.raises(ValidationError, match="command hash"):
        PortfolioPaperExecutionCommand.model_validate(command_payload)

    batch_payload = batch_result.model_dump(mode="python")
    batch_payload["batch_hash"] = "e" * 64
    with pytest.raises(ValidationError, match="batch hash"):
        PortfolioPaperExecutionBatch.model_validate(batch_payload)


def test_weight_tolerance_is_compared_beyond_decimal_context_precision() -> None:
    batch_result = build()
    payload = batch_result.command.model_dump(mode="python")
    payload["target_weights"] = {
        "BTCUSDT": Decimal("0.5"),
        "ETHUSDT": Decimal(
            "0.5000000001000000000000000000000000000001"
        ),
    }

    with pytest.raises(ValidationError, match="sum to one"):
        PortfolioPaperExecutionCommand.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("available_cash", Decimal("NaN")),
        ("available_cash", Decimal("Infinity")),
        ("available_cash", Decimal("-1")),
        ("fee_rate", Decimal("-0.1")),
        ("slippage_rate", Decimal("Infinity")),
    ],
)
def test_rejects_non_finite_or_negative_command_amounts(
    field: str,
    value: Decimal,
) -> None:
    batch_result = build()
    payload = batch_result.command.model_dump(mode="python")
    payload[field] = value
    with pytest.raises(ValidationError):
        PortfolioPaperExecutionCommand.model_validate(payload)
