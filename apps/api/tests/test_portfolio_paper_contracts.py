from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest
from pydantic import ValidationError
from quantsieve_api.portfolio_paper_contracts import (
    CertifiedPortfolioBar,
    PortfolioPaperBasketContract,
    PortfolioPaperDecisionCertificate,
    PortfolioPaperDecisionReceipt,
    _validate_execution_quotes_against_receipt,
    assess_portfolio_paper_eligibility,
    canonical_payload_hash,
    certify_binance_daily_bar,
    certify_portfolio_paper_decision,
)
from quantsieve_engine import (
    PortfolioForwardTarget,
    compute_next_portfolio_target,
)
from quantsieve_providers import (
    BinanceSpotTradingRules,
    ExecutionQuote,
    Instrument,
)

BASE_TIME = datetime(2026, 7, 25, 23, 55, tzinfo=UTC)


def canonical_exact(value: object) -> str:
    exact = Decimal(str(value))
    if exact == 0:
        return "0"
    text = format(exact, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def trading_rules(
    symbol: str,
    base_asset: str,
    *,
    quote_asset: str = "USDT",
    verified_at: datetime = BASE_TIME,
) -> BinanceSpotTradingRules:
    return BinanceSpotTradingRules(
        symbol=symbol,
        base_asset=base_asset,
        quote_asset=quote_asset,
        status="TRADING",
        spot_trading_allowed=True,
        order_types=("LIMIT", "MARKET"),
        lot_step_size="0.00001",
        lot_min_quantity="0.00001",
        lot_max_quantity="1000000",
        market_step_size="0.00001",
        market_min_quantity="0.00001",
        market_max_quantity="1000000",
        min_notional="5",
        min_notional_applies_to_market=True,
        max_notional=None,
        max_notional_applies_to_market=False,
        notional_average_price_minutes=5,
        verified_at=verified_at,
    )


def binance_instrument(
    symbol: str,
    base_asset: str,
    *,
    currency: str = "USDT",
) -> Instrument:
    return Instrument(
        symbol=symbol,
        name=f"{base_asset} / {currency}",
        market="CRYPTO",
        exchange="Binance Spot",
        currency=currency,
        provider="binance",
        asset_type="spot",
    )


def eligible_contract(symbol: str, base_asset: str):
    assessment = assess_portfolio_paper_eligibility(
        binance_instrument(symbol, base_asset),
        rules=trading_rules(symbol, base_asset),
        now=BASE_TIME + timedelta(minutes=1),
    )
    assert assessment.status == "eligible"
    assert assessment.contract is not None
    return assessment.contract


def finalized_row(
    *,
    symbol: str,
    open_price: float,
    close_price: float,
    observed_offset_seconds: int = 121,
    open_time: datetime | None = None,
    interval: str = "1d",
) -> dict[str, object]:
    open_time = open_time or datetime(2026, 7, 25, tzinfo=UTC)
    close_time = open_time + timedelta(days=1) - timedelta(milliseconds=1)
    observed_at = close_time + timedelta(seconds=observed_offset_seconds)
    exchange_time = observed_at - timedelta(milliseconds=500)
    high_price = max(open_price, close_price) * 1.01
    low_price = min(open_price, close_price) * 0.99
    return {
        "symbol": symbol,
        "interval": interval,
        "date": open_time.isoformat(),
        "open_time": open_time.isoformat(),
        "close_time": close_time.isoformat(),
        "observed_at": observed_at.isoformat(),
        "exchange_server_time": exchange_time.isoformat(),
        "clock_checked_at": exchange_time.isoformat(),
        "exchange_clock_verified": True,
        "finalized_at": (close_time + timedelta(minutes=2)).isoformat(),
        "finalized": True,
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
        "volume": 1000,
        "exact_open": canonical_exact(open_price),
        "exact_high": canonical_exact(high_price),
        "exact_low": canonical_exact(low_price),
        "exact_close": canonical_exact(close_price),
        "exact_volume": "1000",
    }


def certified_inputs():
    btc = eligible_contract("BTCUSDT", "BTC")
    eth = eligible_contract("ETHUSDT", "ETH")
    basket = PortfolioPaperBasketContract(
        instruments=(btc, eth),
        created_at=BASE_TIME + timedelta(minutes=2),
    )
    bars = (
        certify_binance_daily_bar(
            finalized_row(symbol="BTCUSDT", open_price=100, close_price=105),
            btc,
        ),
        certify_binance_daily_bar(
            finalized_row(symbol="ETHUSDT", open_price=50, close_price=48),
            eth,
        ),
    )
    target = PortfolioForwardTarget(
        method="periodic_equal",
        information_session=bars[0].session,
        weights={"BTCUSDT": 0.5, "ETHUSDT": 0.5},
    )
    decision = certify_portfolio_paper_decision(
        target=target,
        bars=bars,
        basket=basket,
        configuration_hash=canonical_payload_hash({"rebalance_bars": 21}),
        volatility_lookback=60,
        maximum_asset_weight=0.6,
        decided_at=max(bar.observed_at for bar in bars) + timedelta(seconds=1),
    )
    return basket, bars, decision


def persisted_receipt(
    decision: PortfolioPaperDecisionCertificate,
    *,
    persisted_at: datetime,
) -> PortfolioPaperDecisionReceipt:
    return PortfolioPaperDecisionReceipt(
        track_id="a" * 32,
        decision_id=decision.decision_id,
        certificate=decision,
        certificate_hash=canonical_payload_hash(decision.model_dump(mode="json")),
        persisted_at=persisted_at,
    )


def quote(
    symbol: str,
    observed_at: datetime,
    *,
    bid: float,
    ask: float,
    request_started_at: datetime | None = None,
    ask_quantity: str = "1000000",
) -> ExecutionQuote:
    return ExecutionQuote(
        symbol=symbol,
        provider="binance",
        venue="Binance Spot",
        bid_price=bid,
        ask_price=ask,
        bid_quantity="1000000",
        ask_quantity=ask_quantity,
        request_started_at=request_started_at
        or observed_at - timedelta(milliseconds=100),
        observed_at=observed_at,
        notional_reference_price=ask,
        notional_reference_kind="exchange_reference",
        notional_reference_window_minutes=None,
        notional_reference_at=observed_at,
        notional_reference_observed_at=observed_at + timedelta(milliseconds=25),
        exchange_reference_available=True,
        exchange_reference_at=observed_at,
        exchange_reference_observed_at=observed_at + timedelta(milliseconds=25),
        exchange_server_time=observed_at + timedelta(milliseconds=50),
        clock_checked_at=observed_at + timedelta(milliseconds=50),
        cache_used=False,
    )


def test_execution_eligibility_is_strictly_separate_from_research_coverage() -> None:
    btc = binance_instrument("BTCUSDT", "BTC")
    pending = assess_portfolio_paper_eligibility(btc, now=BASE_TIME)
    eligible = assess_portfolio_paper_eligibility(
        btc,
        rules=trading_rules("BTCUSDT", "BTC"),
        now=BASE_TIME + timedelta(minutes=1),
    )
    stale = assess_portfolio_paper_eligibility(
        btc,
        rules=trading_rules("BTCUSDT", "BTC"),
        now=BASE_TIME + timedelta(minutes=16),
    )
    usdc = assess_portfolio_paper_eligibility(
        binance_instrument("BTCUSDC", "BTC", currency="USDC"),
        rules=trading_rules("BTCUSDC", "BTC", quote_asset="USDC"),
        now=BASE_TIME + timedelta(minutes=1),
    )
    index = assess_portfolio_paper_eligibility(
        Instrument(
            symbol="NDX",
            name="NASDAQ-100",
            market="INDEX",
            exchange="Nasdaq",
            currency="USD",
            provider="macro",
            asset_type="index",
        )
    )
    future = assess_portfolio_paper_eligibility(
        Instrument(
            symbol="CL",
            name="WTI continuous",
            market="FUTURES",
            exchange="NYMEX proxy",
            currency="USD",
            provider="futures",
            asset_type="continuous_future",
        )
    )

    assert pending.status == "verification_required"
    assert eligible.status == "eligible"
    assert eligible.contract is not None
    assert (
        eligible.contract.execution_model
        == "single_level_top_of_book_after_persisted_decision"
    )
    assert stale.status == "rejected"
    assert usdc.status == "research_only"
    assert index.status == "research_only"
    assert future.status == "research_only"


def test_execution_identity_is_explicitly_single_level_and_all_or_nothing() -> None:
    btc = eligible_contract("BTCUSDT", "BTC")
    eth = eligible_contract("ETHUSDT", "ETH")
    basket = PortfolioPaperBasketContract(
        instruments=(btc, eth),
        created_at=BASE_TIME + timedelta(minutes=2),
    )

    assert btc.execution_model == "single_level_top_of_book_after_persisted_decision"
    assert btc.identity.execution_model == btc.execution_model
    assert basket.execution_model == "all_or_nothing_single_level_top_of_book"
    assert basket.identity.execution_model == basket.execution_model
    assert basket.partial_execution_allowed is False

    old_instrument_identity = btc.model_dump(mode="python")
    old_instrument_identity["execution_model"] = (
        "fresh_bid_ask_after_persisted_decision"
    )
    with pytest.raises(ValidationError, match="single_level_top_of_book"):
        type(btc).model_validate(old_instrument_identity)

    old_basket_identity = basket.model_dump(mode="python")
    old_basket_identity["execution_model"] = "all_or_nothing_fresh_bid_ask"
    with pytest.raises(ValidationError, match="all_or_nothing_single_level"):
        PortfolioPaperBasketContract.model_validate(old_basket_identity)


def test_finalized_bar_certificate_requires_lag_and_detects_revision_tampering() -> None:
    contract = eligible_contract("BTCUSDT", "BTC")
    row = finalized_row(symbol="BTCUSDT", open_price=100, close_price=105)
    certified = certify_binance_daily_bar(row, contract)

    assert certified.session == "2026-07-25T00:00:00+00:00"
    assert certified.observed_at >= certified.finalized_at
    assert len(certified.revision_hash) == 64

    early = finalized_row(
        open_price=100,
        close_price=105,
        symbol="BTCUSDT",
        observed_offset_seconds=119,
    )
    with pytest.raises(ValidationError, match="timing"):
        certify_binance_daily_bar(early, contract)

    unverified = dict(row)
    unverified["exchange_clock_verified"] = False
    with pytest.raises(ValueError, match="exchange-clock"):
        certify_binance_daily_bar(unverified, contract)

    wrong_symbol = dict(row)
    wrong_symbol["symbol"] = "ETHUSDT"
    with pytest.raises(ValueError, match="symbol"):
        certify_binance_daily_bar(wrong_symbol, contract)

    wrong_interval = dict(row)
    wrong_interval["interval"] = "1h"
    with pytest.raises(ValueError, match="1d"):
        certify_binance_daily_bar(wrong_interval, contract)

    wrong_geometry = dict(row)
    wrong_geometry["open_time"] = datetime(
        2026,
        7,
        25,
        12,
        tzinfo=UTC,
    ).isoformat()
    wrong_geometry["close_time"] = datetime(
        2026,
        7,
        25,
        12,
        59,
        59,
        999000,
        tzinfo=UTC,
    ).isoformat()
    wrong_geometry["finalized_at"] = datetime(
        2026,
        7,
        25,
        13,
        1,
        59,
        999000,
        tzinfo=UTC,
    ).isoformat()
    with pytest.raises(ValidationError, match="complete UTC midnight"):
        certify_binance_daily_bar(wrong_geometry, contract)

    tampered = certified.model_dump(mode="python")
    tampered["close"] = 106
    tampered["exact_close"] = "106"
    with pytest.raises(ValidationError, match="revision hash"):
        CertifiedPortfolioBar.model_validate(tampered)


def test_finalized_bar_numeric_evidence_is_strict_finite_and_roundtrips() -> None:
    contract = eligible_contract("BTCUSDT", "BTC")
    row = finalized_row(symbol="BTCUSDT", open_price=100, close_price=105)
    for field in ("open", "high", "low", "close", "volume"):
        row[field] = str(row[field])

    certified = certify_binance_daily_bar(row, contract)
    roundtripped = CertifiedPortfolioBar.model_validate_json(
        certified.model_dump_json()
    )

    assert roundtripped == certified
    assert certified.schema_version == 2
    assert certified.open == 100.0
    assert certified.volume == 1000.0
    assert certified.exact_open == "100"
    assert certified.exact_volume == "1000"

    for field in ("open", "high", "low", "close", "volume"):
        injected = certified.model_dump(mode="python")
        injected[field] = str(injected[field])
        with pytest.raises(ValidationError):
            CertifiedPortfolioBar.model_validate(injected)


def test_finalized_bar_rejects_boolean_prices_and_volume_at_both_boundaries() -> None:
    contract = eligible_contract("BTCUSDT", "BTC")
    row = finalized_row(symbol="BTCUSDT", open_price=1, close_price=1)
    row.update(
        {
            "high": 1,
            "low": 1,
            "volume": 0,
            "exact_high": "1",
            "exact_low": "1",
            "exact_volume": "0",
        }
    )
    certified = certify_binance_daily_bar(row, contract)
    attacks = {
        "open": True,
        "high": True,
        "low": True,
        "close": True,
        "volume": False,
    }

    for field, value in attacks.items():
        provider_attack = dict(row)
        provider_attack[field] = value
        with pytest.raises(ValueError, match="finite number"):
            certify_binance_daily_bar(provider_attack, contract)

        persisted_attack = certified.model_dump(mode="python")
        persisted_attack[field] = value
        with pytest.raises(ValidationError):
            CertifiedPortfolioBar.model_validate(persisted_attack)


def test_finalized_bar_requires_exact_v2_evidence_and_rejects_legacy_v1() -> None:
    contract = eligible_contract("BTCUSDT", "BTC")
    row = finalized_row(symbol="BTCUSDT", open_price=100, close_price=105)
    missing_exact = dict(row)
    missing_exact.pop("exact_close")

    with pytest.raises(ValueError, match="evidence is incomplete"):
        certify_binance_daily_bar(missing_exact, contract)

    certified = certify_binance_daily_bar(row, contract)
    legacy = certified.model_dump(mode="python")
    legacy["schema_version"] = 1
    with pytest.raises(ValidationError):
        CertifiedPortfolioBar.model_validate(legacy)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("exact_close", "+1"),
        ("exact_volume", "-0"),
        ("exact_close", "-1"),
        ("exact_close", "1e2"),
        ("exact_close", ".5"),
        ("exact_close", "1."),
        ("exact_close", "１２３.４５"),
        ("exact_close", "١٢٣.٤٥"),
        ("exact_close", "1_0"),
        ("exact_close", " 1"),
        ("exact_close", "1 "),
    ],
)
def test_exact_bar_rejects_non_ascii_fixed_point_syntax_at_both_boundaries(
    field: str,
    invalid: str,
) -> None:
    contract = eligible_contract("BTCUSDT", "BTC")
    row = finalized_row(symbol="BTCUSDT", open_price=1, close_price=1)
    provider_attack = dict(row)
    provider_attack[field] = invalid

    with pytest.raises(ValueError, match="finite decimal string"):
        certify_binance_daily_bar(provider_attack, contract)

    certified = certify_binance_daily_bar(row, contract)
    persisted_attack = certified.model_dump(mode="python")
    persisted_attack[field] = invalid
    with pytest.raises(ValidationError, match="finite decimal string"):
        CertifiedPortfolioBar.model_validate(persisted_attack)


def test_exact_bar_allows_integer_and_leading_zero_fixed_point_sources() -> None:
    contract = eligible_contract("BTCUSDT", "BTC")
    row = finalized_row(symbol="BTCUSDT", open_price=1, close_price=1)
    row["exact_close"] = "0001.0000"
    row["exact_volume"] = "0001000"

    certified = certify_binance_daily_bar(row, contract)

    assert certified.exact_close == "1"
    assert certified.exact_volume == "1000"


def test_exact_bar_values_prevent_underflow_and_float_revision_aliases() -> None:
    contract = eligible_contract("BTCUSDT", "BTC")
    negative_underflow = finalized_row(
        symbol="BTCUSDT",
        open_price=1,
        close_price=1,
    )
    negative_underflow["volume"] = -0.0
    negative_underflow["exact_volume"] = "-1e-9999"
    with pytest.raises(ValueError, match="finite decimal string"):
        certify_binance_daily_bar(negative_underflow, contract)

    first = finalized_row(symbol="BTCUSDT", open_price=1, close_price=1)
    second = dict(first)
    first["exact_close"] = "1.00000000000000001"
    second["exact_close"] = "1.00000000000000002"
    first_bar = certify_binance_daily_bar(first, contract)
    second_bar = certify_binance_daily_bar(second, contract)

    assert first_bar.close == second_bar.close == 1.0
    assert first_bar.exact_close != second_bar.exact_close
    assert first_bar.revision_hash != second_bar.revision_hash


def test_exact_ohlc_geometry_cannot_hide_behind_equal_float_projections() -> None:
    contract = eligible_contract("BTCUSDT", "BTC")
    row = finalized_row(symbol="BTCUSDT", open_price=1, close_price=1)
    row.update(
        {
            "high": 1.0,
            "low": 1.0,
            "exact_open": "1.00000000000000002",
            "exact_high": "1.00000000000000001",
            "exact_low": "1",
            "exact_close": "1",
        }
    )

    with pytest.raises(ValidationError, match="Exact bar OHLC"):
        certify_binance_daily_bar(row, contract)


@pytest.mark.parametrize(
    "non_finite",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        "NaN",
        "Infinity",
        "-Infinity",
    ],
)
def test_finalized_bar_rejects_non_finite_provider_numbers(
    non_finite: float | str,
) -> None:
    contract = eligible_contract("BTCUSDT", "BTC")
    baseline = finalized_row(symbol="BTCUSDT", open_price=100, close_price=105)

    for field in ("open", "high", "low", "close", "volume"):
        attack = dict(baseline)
        attack[field] = non_finite
        with pytest.raises(ValueError, match="finite number"):
            certify_binance_daily_bar(attack, contract)


@pytest.mark.parametrize(
    "non_finite",
    [float("nan"), float("inf"), float("-inf")],
)
def test_certified_bar_model_rejects_non_finite_numbers(
    non_finite: float,
) -> None:
    contract = eligible_contract("BTCUSDT", "BTC")
    certified = certify_binance_daily_bar(
        finalized_row(symbol="BTCUSDT", open_price=100, close_price=105),
        contract,
    )

    for field in ("open", "high", "low", "close", "volume"):
        attack = certified.model_dump(mode="python")
        attack[field] = non_finite
        with pytest.raises(ValidationError, match="finite number"):
            CertifiedPortfolioBar.model_validate(attack)


def test_decision_certificate_binds_finalized_inputs_before_quotes() -> None:
    basket, bars, decision = certified_inputs()

    assert decision.target.information_session == bars[0].session
    assert decision.basket_hash == basket.basket_hash
    assert decision.decided_at > max(bar.observed_at for bar in bars)

    future_bar = bars[0].model_dump(mode="python")
    future_bar["observed_at"] = decision.decided_at + timedelta(seconds=1)
    altered_bars = (
        CertifiedPortfolioBar.model_validate(future_bar),
        bars[1],
    )
    with pytest.raises(ValidationError, match="cannot predate"):
        decision.model_validate(
            {
                **decision.model_dump(mode="python"),
                "bars": altered_bars,
            }
        )

    with pytest.raises(ValidationError, match="repeat"):
        decision.model_validate(
            {
                **decision.model_dump(mode="python"),
                "bars": (bars[0], bars[0], bars[1]),
            }
        )

    future_contracts = tuple(
        contract.model_copy(
            update={"created_at": decision.decided_at + timedelta(seconds=30)}
        )
        for contract in basket.instruments
    )
    future_basket = PortfolioPaperBasketContract(
        instruments=future_contracts,
        created_at=decision.decided_at + timedelta(seconds=31),
    )
    with pytest.raises(ValueError, match="created after its use"):
        certify_portfolio_paper_decision(
            target=decision.target,
            bars=bars,
            basket=future_basket,
            configuration_hash=decision.configuration_hash,
            volatility_lookback=decision.volatility_lookback,
            maximum_asset_weight=decision.maximum_asset_weight,
            decided_at=decision.decided_at,
        )


def test_inverse_volatility_decision_binds_and_recomputes_full_window() -> None:
    btc = eligible_contract("BTCUSDT", "BTC")
    eth = eligible_contract("ETHUSDT", "ETH")
    basket = PortfolioPaperBasketContract(
        instruments=(btc, eth),
        created_at=BASE_TIME + timedelta(minutes=2),
    )
    sessions = tuple(
        datetime(2026, 7, 23, tzinfo=UTC) + timedelta(days=offset)
        for offset in range(3)
    )
    closes = {
        "BTCUSDT": (100.0, 110.0, 103.0),
        "ETHUSDT": (50.0, 52.0, 56.0),
    }
    contracts = {"BTCUSDT": btc, "ETHUSDT": eth}
    bars = tuple(
        certify_binance_daily_bar(
            finalized_row(
                symbol=symbol,
                open_time=session,
                open_price=close * 0.99,
                close_price=close,
            ),
            contracts[symbol],
        )
        for session in sessions
        for symbol in ("BTCUSDT", "ETHUSDT")
        for close in (closes[symbol][sessions.index(session)],)
    )
    close_history = {
        symbol: closes[symbol] for symbol in ("BTCUSDT", "ETHUSDT")
    }
    target = compute_next_portfolio_target(
        pd.DataFrame(close_history, index=sessions),
        "periodic_inverse_volatility",
        information_session=sessions[-1],
        volatility_lookback=2,
        maximum_asset_weight=0.8,
    )
    decision = certify_portfolio_paper_decision(
        target=target,
        bars=bars,
        basket=basket,
        configuration_hash=canonical_payload_hash({"lookback": 2}),
        volatility_lookback=2,
        maximum_asset_weight=0.8,
        decided_at=max(bar.observed_at for bar in bars) + timedelta(seconds=1),
    )

    assert len(decision.bars) == 6
    assert decision.target == target

    tampered_target = PortfolioForwardTarget(
        method="periodic_inverse_volatility",
        information_session=target.information_session,
        weights={"BTCUSDT": 0.5, "ETHUSDT": 0.5},
    )
    with pytest.raises(ValidationError, match="cannot be reproduced"):
        decision.model_validate(
            {
                **decision.model_dump(mode="python"),
                "target": tampered_target,
            }
        )

    gapped_sessions = (
        datetime(2026, 7, 21, tzinfo=UTC),
        datetime(2026, 7, 23, tzinfo=UTC),
        datetime(2026, 7, 25, tzinfo=UTC),
    )
    gapped_bars = tuple(
        certify_binance_daily_bar(
            finalized_row(
                symbol=symbol,
                open_time=session,
                open_price=close * 0.99,
                close_price=close,
            ),
            contracts[symbol],
        )
        for session in gapped_sessions
        for symbol in ("BTCUSDT", "ETHUSDT")
        for close in (closes[symbol][gapped_sessions.index(session)],)
    )
    gapped_target = compute_next_portfolio_target(
        pd.DataFrame(close_history, index=gapped_sessions),
        "periodic_inverse_volatility",
        information_session=gapped_sessions[-1],
        volatility_lookback=2,
        maximum_asset_weight=0.8,
    )
    with pytest.raises(ValidationError, match="consecutive UTC"):
        certify_portfolio_paper_decision(
            target=gapped_target,
            bars=gapped_bars,
            basket=basket,
            configuration_hash=canonical_payload_hash({"lookback": 2}),
            volatility_lookback=2,
            maximum_asset_weight=0.8,
            decided_at=max(bar.observed_at for bar in gapped_bars)
            + timedelta(seconds=1),
        )


def test_quote_set_must_be_fresh_complete_synchronized_and_post_decision() -> None:
    basket, _, decision = certified_inputs()
    receipt = persisted_receipt(
        decision,
        persisted_at=decision.decided_at + timedelta(milliseconds=500),
    )
    first_observed = decision.decided_at + timedelta(seconds=1)
    quotes = (
        quote("BTCUSDT", first_observed, bid=105, ask=105.1),
        quote(
            "ETHUSDT",
            first_observed + timedelta(milliseconds=500),
            bid=48,
            ask=48.05,
        ),
    )
    accepted_at = first_observed + timedelta(seconds=1)

    accepted = _validate_execution_quotes_against_receipt(
        basket=basket,
        decision=receipt,
        quotes=quotes,
        accepted_at=accepted_at,
    )

    assert tuple(accepted) == ("BTCUSDT", "ETHUSDT")

    future_quote_contracts = tuple(
        contract.model_copy(update={"created_at": accepted_at + timedelta(seconds=1)})
        for contract in basket.instruments
    )
    future_quote_basket = PortfolioPaperBasketContract(
        instruments=future_quote_contracts,
        created_at=accepted_at + timedelta(seconds=2),
    )
    with pytest.raises(ValueError, match="created after its use"):
        _validate_execution_quotes_against_receipt(
            basket=future_quote_basket,
            decision=receipt,
            quotes=quotes,
            accepted_at=accepted_at,
        )

    predecision = quote(
        "BTCUSDT",
        receipt.persisted_at - timedelta(milliseconds=1),
        bid=105,
        ask=105.1,
    )
    with pytest.raises(ValueError, match="predates decision persistence"):
        _validate_execution_quotes_against_receipt(
            basket=basket,
            decision=receipt,
            quotes=(predecision, quotes[1]),
            accepted_at=accepted_at,
        )

    with pytest.raises(ValueError, match="too far apart"):
        _validate_execution_quotes_against_receipt(
            basket=basket,
            decision=receipt,
            quotes=(
                quotes[0],
                quote(
                    "ETHUSDT",
                    first_observed + timedelta(seconds=3),
                    bid=48,
                    ask=48.05,
                ),
            ),
            accepted_at=first_observed + timedelta(seconds=4),
        )

    slow_observed = receipt.persisted_at + timedelta(seconds=6)
    slow_request = quote(
        "BTCUSDT",
        slow_observed,
        bid=105,
        ask=105.1,
        request_started_at=receipt.persisted_at + timedelta(milliseconds=100),
    )
    synchronized_slow_peer = quote(
        "ETHUSDT",
        slow_observed + timedelta(milliseconds=100),
        bid=48,
        ask=48.05,
        request_started_at=receipt.persisted_at + timedelta(milliseconds=200),
    )
    with pytest.raises(ValueError, match="freshness budget"):
        _validate_execution_quotes_against_receipt(
            basket=basket,
            decision=receipt,
            quotes=(slow_request, synchronized_slow_peer),
            accepted_at=slow_observed + timedelta(milliseconds=200),
        )

    forged_negative_depth = quotes[0].model_copy(
        update={"ask_quantity": Decimal("-1")}
    )
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        _validate_execution_quotes_against_receipt(
            basket=basket,
            decision=receipt,
            quotes=(forged_negative_depth, quotes[1]),
            accepted_at=accepted_at,
        )

    wrong_average_window = quotes[0].model_copy(
        update={
            "exchange_reference_available": False,
            "notional_reference_kind": "average_price",
            "notional_reference_window_minutes": 1,
        }
    )
    with pytest.raises(ValueError, match="trading-rule window"):
        _validate_execution_quotes_against_receipt(
            basket=basket,
            decision=receipt,
            quotes=(wrong_average_window, quotes[1]),
            accepted_at=accepted_at,
        )

    wrong_last_price_window = quotes[0].model_copy(
        update={
            "exchange_reference_available": False,
            "notional_reference_kind": "last_price",
            "notional_reference_window_minutes": 0,
        }
    )
    with pytest.raises(ValueError, match="zero-minute trading-rule window"):
        _validate_execution_quotes_against_receipt(
            basket=basket,
            decision=receipt,
            quotes=(wrong_last_price_window, quotes[1]),
            accepted_at=accepted_at,
        )

    stale_exchange_event = quotes[0].model_dump(mode="python")
    stale_exchange_event.update(
        {
            "exchange_reference_available": False,
            "exchange_reference_at": accepted_at - timedelta(seconds=11),
            "exchange_reference_observed_at": first_observed
            + timedelta(milliseconds=25),
            "notional_reference_kind": "average_price",
            "notional_reference_window_minutes": 5,
        }
    )
    with pytest.raises(ValueError, match="Exchange-reference event is too old"):
        _validate_execution_quotes_against_receipt(
            basket=basket,
            decision=receipt,
            quotes=(ExecutionQuote.model_validate(stale_exchange_event), quotes[1]),
            accepted_at=accepted_at,
        )

    future_exchange_event = stale_exchange_event | {
        "exchange_reference_at": quotes[0].exchange_server_time
        + timedelta(milliseconds=1)
    }
    with pytest.raises(ValidationError, match="cannot postdate exchange server time"):
        _validate_execution_quotes_against_receipt(
            basket=basket,
            decision=receipt,
            quotes=(ExecutionQuote.model_construct(**future_exchange_event), quotes[1]),
            accepted_at=accepted_at,
        )

    with pytest.raises(ValueError, match="exactly match"):
        _validate_execution_quotes_against_receipt(
            basket=basket,
            decision=receipt,
            quotes=(quotes[0],),
            accepted_at=accepted_at,
        )

    forged_rules = basket.instruments[0].rules.model_copy(
        update={"min_notional": Decimal("500")}
    )
    forged_contract = basket.instruments[0].model_copy(
        update={"rules": forged_rules}
    )
    forged_rule_basket = basket.model_copy(
        update={"instruments": (forged_contract, basket.instruments[1])}
    )
    assert (
        forged_rule_basket.identity.identity_hash
        == basket.identity.identity_hash
    )
    assert forged_rule_basket.basket_hash != basket.basket_hash
    with pytest.raises(ValueError, match="certified trading-rule snapshot"):
        _validate_execution_quotes_against_receipt(
            basket=forged_rule_basket,
            decision=receipt,
            quotes=quotes,
            accepted_at=accepted_at,
        )

    clock_skewed = quotes[0].model_dump(mode="python")
    clock_skewed["exchange_server_time"] = (
        quotes[0].clock_checked_at + timedelta(seconds=6)
    )
    with pytest.raises(ValueError, match="clocks differ"):
        _validate_execution_quotes_against_receipt(
            basket=basket,
            decision=receipt,
            quotes=(ExecutionQuote.model_validate(clock_skewed), quotes[1]),
            accepted_at=accepted_at,
        )

    different_basket = PortfolioPaperBasketContract(
        instruments=basket.instruments,
        max_quote_age_seconds=6,
        created_at=basket.created_at,
    )
    with pytest.raises(ValueError, match="does not belong"):
        _validate_execution_quotes_against_receipt(
            basket=different_basket,
            decision=receipt,
            quotes=quotes,
            accepted_at=accepted_at,
        )

    stale_at = basket.instruments[0].rules.verified_at + timedelta(minutes=16)
    stale_quotes = (
        quote("BTCUSDT", stale_at - timedelta(seconds=1), bid=105, ask=105.1),
        quote("ETHUSDT", stale_at - timedelta(milliseconds=500), bid=48, ask=48.05),
    )
    with pytest.raises(ValueError, match="stale"):
        _validate_execution_quotes_against_receipt(
            basket=basket,
            decision=receipt,
            quotes=stale_quotes,
            accepted_at=stale_at,
        )

    late_at = decision.execution_deadline + timedelta(days=1)
    late_quotes = (
        quote("BTCUSDT", late_at - timedelta(seconds=1), bid=105, ask=105.1),
        quote("ETHUSDT", late_at - timedelta(milliseconds=500), bid=48, ask=48.05),
    )
    with pytest.raises(ValueError, match="outside the certified execution window"):
        _validate_execution_quotes_against_receipt(
            basket=basket,
            decision=receipt,
            quotes=late_quotes,
            accepted_at=late_at,
        )


def test_execution_quote_rejects_non_finite_prices() -> None:
    observed_at = BASE_TIME + timedelta(seconds=1)
    with pytest.raises(ValidationError, match="finite"):
        quote("BTCUSDT", observed_at, bid=float("inf"), ask=float("inf"))
