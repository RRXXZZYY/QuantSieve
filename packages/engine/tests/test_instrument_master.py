from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError
from quantsieve_engine.instrument_master import (
    FuturesMetadata,
    FuturesRollMetadata,
    InstrumentNotFoundError,
    InstrumentVersion,
    InstrumentVersionSet,
    build_instrument_version,
    build_instrument_version_set,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 6, 1, tzinfo=UTC)
T2 = datetime(2027, 1, 1, tzinfo=UTC)


def instrument(**changes: object) -> InstrumentVersion:
    values: dict[str, object] = {
        "instrument_uid": "security:xnas:aapl",
        "asset_class": "equity",
        "canonical_symbol": "AAPL",
        "venue": "XNAS",
        "currency": "USD",
        "timezone": "America/New_York",
        "calendar_id": "XNAS",
        "price_multiplier": Decimal("1"),
        "contract_multiplier": Decimal("1"),
        "tick_size": Decimal("0.01"),
        "lot_size": Decimal("1"),
        "valid_from": T0,
        "source": "exchange-security-master",
        "source_as_of": T0 - timedelta(seconds=1),
    }
    values.update(changes)
    return build_instrument_version(**values)  # type: ignore[arg-type]


def test_version_hash_is_stable_for_equivalent_decimal_and_timezone_values() -> None:
    first = instrument(
        tick_size=Decimal("0.0100"),
        valid_from=datetime(2025, 12, 31, 19, tzinfo=timezone(timedelta(hours=-5))),
        source_as_of=datetime(
            2025,
            12,
            31,
            18,
            59,
            59,
            tzinfo=timezone(timedelta(hours=-5)),
        ),
    )
    second = instrument(tick_size=Decimal("0.01"))

    assert first.version_id == second.version_id
    assert first.valid_from == T0
    assert first.tick_size == Decimal("0.01")


@pytest.mark.parametrize(
    "asset_class",
    ["equity", "etf", "index", "crypto", "commodity", "fx", "rate", "macro"],
)
def test_supported_non_future_asset_classes(asset_class: str) -> None:
    result = instrument(asset_class=asset_class)

    assert result.asset_class == asset_class


def test_version_is_frozen_and_rejects_tampering() -> None:
    original = instrument()
    with pytest.raises(ValidationError, match="frozen"):
        original.currency = "EUR"

    payload = original.model_dump(mode="python")
    payload["currency"] = "EUR"
    with pytest.raises(ValidationError, match="version_id"):
        InstrumentVersion.model_validate(payload)


def test_point_in_time_resolution_uses_closed_open_boundaries() -> None:
    first = instrument(valid_to=T1)
    second = instrument(
        canonical_symbol="AAPL",
        tick_size=Decimal("0.005"),
        valid_from=T1,
        source_as_of=T1,
        revision_of=first.version_id,
    )
    versions = build_instrument_version_set([first, second])

    assert versions.resolve(T0).version_id == first.version_id
    assert versions.resolve(T1 - timedelta(microseconds=1)).version_id == first.version_id
    assert versions.resolve(T1).version_id == second.version_id
    assert versions.resolve(T2).version_id == second.version_id


def test_resolution_fails_closed_before_history_and_inside_a_gap() -> None:
    first = instrument(valid_to=T1)
    second_start = T1 + timedelta(days=1)
    second = instrument(
        valid_from=second_start,
        source_as_of=second_start,
        revision_of=first.version_id,
    )
    versions = build_instrument_version_set([first, second])

    with pytest.raises(InstrumentNotFoundError, match="No instrument version"):
        versions.resolve(T0 - timedelta(microseconds=1))
    with pytest.raises(InstrumentNotFoundError, match="No instrument version"):
        versions.resolve(T1)
    with pytest.raises(ValueError, match="timezone-aware"):
        versions.resolve(datetime(2026, 1, 1))


def test_version_set_rejects_overlap_bad_order_and_broken_lineage() -> None:
    first = instrument(valid_to=T1 + timedelta(days=1))
    overlapping = instrument(
        valid_from=T1,
        source_as_of=T1,
        revision_of=first.version_id,
    )
    with pytest.raises(ValidationError, match="overlap"):
        build_instrument_version_set([first, overlapping])
    with pytest.raises(ValidationError, match="ordered"):
        build_instrument_version_set([overlapping, first])

    non_overlapping_first = instrument(valid_to=T1)
    broken = instrument(valid_from=T1, source_as_of=T1, revision_of="a" * 64)
    with pytest.raises(ValidationError, match="prior version"):
        build_instrument_version_set([non_overlapping_first, broken])


def test_version_set_hash_is_deterministic_and_rejects_tampering() -> None:
    version = instrument()
    first = build_instrument_version_set([version])
    second = build_instrument_version_set((version,))

    assert first.set_id == second.set_id
    payload = first.model_dump(mode="python")
    payload["instrument_uid"] = "security:xnas:msft"
    with pytest.raises(ValidationError, match=r"instrument_uid|set_id"):
        InstrumentVersionSet.model_validate(payload)


def test_dated_future_requires_consistent_contract_events() -> None:
    dated = FuturesMetadata(
        contract_kind="dated",
        underlying_canonical_symbol="CL",
        expiry=date(2026, 12, 20),
        last_trade_at=datetime(2026, 12, 18, 19, 30, tzinfo=UTC),
        settlement_at=datetime(2026, 12, 21, tzinfo=UTC),
        settlement_type="physical",
    )
    future = instrument(
        instrument_uid="future:xnym:clz26",
        asset_class="future",
        canonical_symbol="CLZ26",
        venue="XNYM",
        currency="USD",
        timezone="America/New_York",
        calendar_id="XNYM",
        contract_multiplier=Decimal("1000"),
        tick_size=Decimal("0.01"),
        futures=dated,
    )

    assert future.futures == dated
    with pytest.raises(ValidationError, match="requires an expiry"):
        FuturesMetadata(
            contract_kind="dated",
            underlying_canonical_symbol="CL",
        )
    with pytest.raises(ValidationError, match="after its expiry"):
        FuturesMetadata(
            contract_kind="dated",
            underlying_canonical_symbol="CL",
            expiry=date(2026, 12, 20),
            last_trade_at=datetime(2026, 12, 21, tzinfo=UTC),
        )
    with pytest.raises(ValidationError, match="settlement_type"):
        FuturesMetadata(
            contract_kind="dated",
            underlying_canonical_symbol="CL",
            expiry=date(2026, 12, 20),
            settlement_at=datetime(2026, 12, 21, tzinfo=UTC),
        )


def test_continuous_future_requires_roll_rule_and_forbids_dated_fields() -> None:
    with pytest.raises(ValidationError, match="requires roll metadata"):
        FuturesMetadata(
            contract_kind="continuous",
            underlying_canonical_symbol="CL",
        )
    with pytest.raises(ValidationError, match="cannot declare an expiry"):
        FuturesMetadata(
            contract_kind="continuous",
            underlying_canonical_symbol="CL",
            expiry=date(2026, 12, 20),
            roll=FuturesRollMetadata(rule="volume"),
        )
    with pytest.raises(ValidationError, match="requires custom_rule_id"):
        FuturesRollMetadata(rule="custom")
    with pytest.raises(ValidationError, match="only valid"):
        FuturesRollMetadata(rule="volume", custom_rule_id="desk-rule")

    continuous = FuturesMetadata(
        contract_kind="continuous",
        underlying_canonical_symbol="CL",
        roll=FuturesRollMetadata(
            rule="volume",
            roll_offset_business_days=2,
            adjustment="difference",
        ),
    )
    assert continuous.roll is not None
    assert continuous.roll.adjustment == "difference"


def test_future_metadata_is_required_only_for_future_asset_class() -> None:
    with pytest.raises(ValidationError, match="requires futures metadata"):
        instrument(
            instrument_uid="future:xnym:clz26",
            asset_class="future",
            canonical_symbol="CLZ26",
            venue="XNYM",
            calendar_id="XNYM",
        )
    with pytest.raises(ValidationError, match="only valid for future"):
        instrument(
            futures=FuturesMetadata(
                contract_kind="continuous",
                underlying_canonical_symbol="AAPL",
                roll=FuturesRollMetadata(rule="calendar"),
            )
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("currency", "usd", "string_pattern_mismatch"),
        ("currency", "U$", "string_too_short|string_pattern_mismatch"),
        ("timezone", "Mars/Olympus", "valid IANA"),
        ("timezone", " America/New_York", "surrounding whitespace"),
        ("tick_size", Decimal("0"), "finite positive"),
        ("lot_size", Decimal("NaN"), "finite number|finite positive"),
        ("price_multiplier", Decimal("-1"), "finite positive"),
    ],
)
def test_invalid_currency_timezone_and_decimal_are_rejected(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        instrument(**{field: value})


def test_numeric_fields_require_decimal_and_source_cannot_be_future_known() -> None:
    with pytest.raises(ValidationError, match="Decimal"):
        instrument(tick_size=0.01)
    with pytest.raises(ValidationError, match="source_as_of"):
        instrument(source_as_of=T0 + timedelta(seconds=1))


def test_version_set_revalidates_nested_version_integrity() -> None:
    original = instrument()
    tampered_payload = original.model_dump(mode="python")
    tampered_payload["venue"] = "XNYS"

    with pytest.raises(ValidationError, match="version_id"):
        InstrumentVersion.model_construct(**tampered_payload)
        build_instrument_version_set([InstrumentVersion.model_construct(**tampered_payload)])
