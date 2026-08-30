from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Literal

import pytest
from pydantic import ValidationError
from quantsieve_engine.paper_ledger import (
    InsufficientPaperCashError,
    InsufficientPaperPositionError,
    JournalTransaction,
    PaperFillEvent,
    PaperLedgerConflictError,
    PaperLedgerRevisionError,
    PaperLedgerState,
    apply_paper_fill,
    build_opening_balance_event,
    build_paper_fill_event,
    open_paper_ledger,
    reconcile_paper_ledger,
)

NOW = datetime(2026, 7, 29, 1, 2, 3, tzinfo=UTC)


def opened_ledger(initial_cash: str = "1000") -> PaperLedgerState:
    return open_paper_ledger(
        build_opening_balance_event(
            account_id="paper-account-one",
            idempotency_key="open-account-one",
            currency="usdt",
            initial_cash=initial_cash,
            occurred_at=NOW,
        )
    )


def fill(
    state: PaperLedgerState,
    *,
    key: str,
    fill_id: str,
    side: Literal["buy", "sell"],
    quantity: str,
    reference_price: str,
    fill_price: str,
    fee: str = "0",
    symbol: str = "BTCUSDT",
    occurred_at: datetime | None = None,
) -> PaperFillEvent:
    return build_paper_fill_event(
        account_id=state.account_id,
        expected_revision=state.revision,
        idempotency_key=key,
        fill_id=fill_id,
        order_id=f"order-{fill_id}",
        symbol=symbol,
        side=side,
        quantity=quantity,
        reference_price=reference_price,
        fill_price=fill_price,
        fee=fee,
        occurred_at=occurred_at or NOW + timedelta(minutes=state.revision),
    )


def assert_balanced(transaction: JournalTransaction) -> None:
    assert sum((posting.amount for posting in transaction.postings), Decimal(0)) == 0
    assert len({posting.account for posting in transaction.postings}) == len(transaction.postings)


def test_opening_contract_is_exact_frozen_and_balanced() -> None:
    event = build_opening_balance_event(
        account_id=" account-one ",
        idempotency_key=" opening-one ",
        currency="usdt",
        initial_cash="1000.00000000000000000000000000000",
        occurred_at=NOW.astimezone(timezone(timedelta(hours=8))),
    )
    state = open_paper_ledger(event)

    assert event.account_id == "account-one"
    assert event.currency == "USDT"
    assert event.initial_cash == Decimal("1000")
    assert event.occurred_at == NOW
    assert state.cash == Decimal("1000")
    assert state.revision == 1
    assert_balanced(state.transactions[0])
    assert reconcile_paper_ledger(state).cash == Decimal("1000")

    with pytest.raises(ValidationError):
        event.initial_cash = Decimal("2")
    with pytest.raises(ValidationError):
        event.model_validate({**event.model_dump(), "unexpected": True})
    with pytest.raises(ValueError, match="exact finite decimal"):
        build_opening_balance_event(
            account_id="float-account",
            idempotency_key="float-opening",
            currency="USDT",
            initial_cash=1000.0,  # type: ignore[arg-type]
            occurred_at=NOW,
        )


def test_buy_fill_creates_balanced_journal_and_fifo_lot() -> None:
    state = opened_ledger()
    event = fill(
        state,
        key="buy-one",
        fill_id="fill-buy-one",
        side="buy",
        quantity="2",
        reference_price="10",
        fill_price="10.1",
        fee="0.2",
    )

    result = apply_paper_fill(state, event)
    position = result.state.positions[0]

    assert result.idempotent_replay is False
    assert event.gross_notional == Decimal("20.2")
    assert event.slippage == Decimal("0.2")
    assert result.state.cash == Decimal("979.6")
    assert result.state.fees_paid == Decimal("0.2")
    assert position.quantity == Decimal("2")
    assert position.book_cost == Decimal("20.2")
    assert position.lots[0].unit_cost == Decimal("10.1")
    assert position.lots[0].remaining_quantity == Decimal("2")
    assert "ASSET:INVENTORY:BTCUSDT:USDT" in {
        posting.account for posting in result.transaction.postings
    }
    assert_balanced(result.transaction)
    assert reconcile_paper_ledger(result.state).inventory_costs == {"BTCUSDT": Decimal("20.2")}


def test_partial_sell_consumes_first_lot_and_books_realized_pnl() -> None:
    state = opened_ledger()
    first_buy = apply_paper_fill(
        state,
        fill(
            state,
            key="buy-one",
            fill_id="fill-buy-one",
            side="buy",
            quantity="2",
            reference_price="10",
            fill_price="10.1",
            fee="0.2",
        ),
    ).state
    second_buy = apply_paper_fill(
        first_buy,
        fill(
            first_buy,
            key="buy-two",
            fill_id="fill-buy-two",
            side="buy",
            quantity="3",
            reference_price="12",
            fill_price="12",
            occurred_at=NOW + timedelta(minutes=2),
        ),
    ).state

    sold = apply_paper_fill(
        second_buy,
        fill(
            second_buy,
            key="sell-one",
            fill_id="fill-sell-one",
            side="sell",
            quantity="1",
            reference_price="15",
            fill_price="15",
            fee="0.1",
            occurred_at=NOW + timedelta(minutes=3),
        ),
    )
    position = sold.state.positions[0]

    assert len(sold.consumptions) == 1
    assert sold.consumptions[0].lot_id == position.lots[0].id
    assert sold.consumptions[0].quantity == Decimal("1")
    assert sold.consumptions[0].cost_basis == Decimal("10.1")
    assert position.lots[0].remaining_quantity == Decimal("1")
    assert position.lots[1].remaining_quantity == Decimal("3")
    assert position.quantity == Decimal("4")
    assert position.book_cost == Decimal("46.1")
    assert sold.state.cash == Decimal("958.5")
    assert sold.state.realized_pnl == Decimal("4.9")
    assert sold.state.fees_paid == Decimal("0.3")
    assert_balanced(sold.transaction)


def test_cross_lot_sell_is_fifo_and_reconciles_every_lot() -> None:
    state = opened_ledger()
    for index, (quantity, price) in enumerate((("2", "10.1"), ("3", "12")), start=1):
        event = fill(
            state,
            key=f"buy-{index}",
            fill_id=f"fill-buy-{index}",
            side="buy",
            quantity=quantity,
            reference_price=price,
            fill_price=price,
            occurred_at=NOW + timedelta(minutes=index),
        )
        state = apply_paper_fill(state, event).state

    first_sell = fill(
        state,
        key="sell-partial",
        fill_id="fill-sell-partial",
        side="sell",
        quantity="1",
        reference_price="15",
        fill_price="15",
        fee="0.1",
        occurred_at=NOW + timedelta(minutes=3),
    )
    state = apply_paper_fill(state, first_sell).state
    cross_sell = fill(
        state,
        key="sell-cross",
        fill_id="fill-sell-cross",
        side="sell",
        quantity="3",
        reference_price="14",
        fill_price="14",
        fee="0.2",
        occurred_at=NOW + timedelta(minutes=4),
    )

    result = apply_paper_fill(state, cross_sell)
    position = result.state.positions[0]

    assert [item.quantity for item in result.consumptions] == [
        Decimal("1"),
        Decimal("2"),
    ]
    assert [item.cost_basis for item in result.consumptions] == [
        Decimal("10.1"),
        Decimal("24"),
    ]
    assert [lot.remaining_quantity for lot in position.lots] == [
        Decimal("0"),
        Decimal("1"),
    ]
    assert position.quantity == Decimal("1")
    assert position.book_cost == Decimal("12")
    assert result.state.cash == Decimal("1000.5")
    assert result.state.realized_pnl == Decimal("12.8")
    assert result.state.fees_paid == Decimal("0.3")
    reconciliation = reconcile_paper_ledger(result.state)
    assert reconciliation.inventory_costs == {"BTCUSDT": Decimal("12")}
    assert reconciliation.realized_pnl == Decimal("12.8")
    assert_balanced(result.transaction)


@pytest.mark.parametrize(
    "symbol",
    [
        "BTC/USDT",
        "BRK.B",
        "CME:CL",
        "黄金",
        "X" * 180,
    ],
)
def test_complex_symbols_use_deterministic_length_safe_account_components(
    symbol: str,
) -> None:
    state = opened_ledger("1000")
    bought = apply_paper_fill(
        state,
        fill(
            state,
            key=f"buy-{hashlib.sha256(symbol.encode()).hexdigest()}",
            fill_id=f"fill-buy-{hashlib.sha256(symbol.encode()).hexdigest()}",
            side="buy",
            quantity="1",
            reference_price="10",
            fill_price="10",
            symbol=symbol,
        ),
    )
    component = f"H{hashlib.sha256(symbol.upper().encode('utf-8')).hexdigest().upper()}"

    assert bought.state.positions[0].symbol == symbol.upper()
    assert f"ASSET:INVENTORY:{component}:USDT" in {
        posting.account for posting in bought.transaction.postings
    }
    assert all(len(posting.account) <= 200 for posting in bought.transaction.postings)

    sold = apply_paper_fill(
        bought.state,
        fill(
            bought.state,
            key=f"sell-{hashlib.sha256(symbol.encode()).hexdigest()}",
            fill_id=f"fill-sell-{hashlib.sha256(symbol.encode()).hexdigest()}",
            side="sell",
            quantity="1",
            reference_price="11",
            fill_price="11",
            symbol=symbol,
            occurred_at=NOW + timedelta(minutes=3),
        ),
    )
    assert f"PNL:REALIZED:{component}:USDT" in {
        posting.account for posting in sold.transaction.postings
    }
    assert reconcile_paper_ledger(sold.state).inventory_costs == {symbol.upper(): Decimal(0)}


def test_fifo_uses_journal_revision_when_events_share_a_timestamp() -> None:
    state = opened_ledger()
    shared_time = NOW + timedelta(minutes=1)
    state = apply_paper_fill(
        state,
        fill(
            state,
            key="same-time-buy-one",
            fill_id="same-time-fill-buy-one",
            side="buy",
            quantity="1",
            reference_price="10",
            fill_price="10",
            occurred_at=shared_time,
        ),
    ).state
    state = apply_paper_fill(
        state,
        fill(
            state,
            key="same-time-sell-one",
            fill_id="same-time-fill-sell-one",
            side="sell",
            quantity="0.5",
            reference_price="12",
            fill_price="12",
            occurred_at=shared_time,
        ),
    ).state
    state = apply_paper_fill(
        state,
        fill(
            state,
            key="same-time-buy-two",
            fill_id="same-time-fill-buy-two",
            side="buy",
            quantity="1",
            reference_price="20",
            fill_price="20",
            occurred_at=shared_time,
        ),
    ).state

    result = apply_paper_fill(
        state,
        fill(
            state,
            key="same-time-sell-cross",
            fill_id="same-time-fill-sell-cross",
            side="sell",
            quantity="0.75",
            reference_price="30",
            fill_price="30",
            occurred_at=shared_time,
        ),
    )

    assert [lot.opened_revision for lot in result.state.positions[0].lots] == [2, 4]
    assert [item.quantity for item in result.consumptions] == [
        Decimal("0.5"),
        Decimal("0.25"),
    ]
    assert [item.cost_basis for item in result.consumptions] == [
        Decimal("5"),
        Decimal("5"),
    ]
    assert reconcile_paper_ledger(result.state).realized_pnl == Decimal("13.5")


def test_loss_sale_posts_realized_pnl_debit_and_remains_balanced() -> None:
    state = opened_ledger()
    state = apply_paper_fill(
        state,
        fill(
            state,
            key="buy-before-loss",
            fill_id="fill-buy-before-loss",
            side="buy",
            quantity="2",
            reference_price="10",
            fill_price="10",
        ),
    ).state

    result = apply_paper_fill(
        state,
        fill(
            state,
            key="sell-at-loss",
            fill_id="fill-sell-at-loss",
            side="sell",
            quantity="1",
            reference_price="8",
            fill_price="8",
            fee="0.1",
        ),
    )
    postings = {posting.account: posting.amount for posting in result.transaction.postings}

    assert result.state.cash == Decimal("987.9")
    assert result.state.realized_pnl == Decimal("-2")
    assert result.state.fees_paid == Decimal("0.1")
    assert result.state.positions[0].book_cost == Decimal("10")
    assert postings["PNL:REALIZED:BTCUSDT:USDT"] == Decimal("2")
    assert reconcile_paper_ledger(result.state).realized_pnl == Decimal("-2")
    assert_balanced(result.transaction)


def test_fill_idempotency_replays_exact_result_and_rejects_conflicts() -> None:
    state = opened_ledger()
    event = fill(
        state,
        key="buy-idempotent",
        fill_id="fill-idempotent",
        side="buy",
        quantity="2",
        reference_price="10",
        fill_price="10",
    )
    committed = apply_paper_fill(state, event)
    replayed = apply_paper_fill(committed.state, event)

    assert replayed.idempotent_replay is True
    assert replayed.state == committed.state
    assert replayed.transaction == committed.transaction

    conflicting_key = build_paper_fill_event(
        **{
            **event.model_dump(
                mode="python",
                exclude={
                    "schema_version",
                    "gross_notional",
                    "slippage",
                    "event_hash",
                },
            ),
            "fill_price": "11",
        }
    )
    with pytest.raises(PaperLedgerConflictError, match="different economics"):
        apply_paper_fill(committed.state, conflicting_key)

    reused_fill = fill(
        committed.state,
        key="another-key",
        fill_id=event.fill_id,
        side="buy",
        quantity="1",
        reference_price="10",
        fill_price="10",
    )
    with pytest.raises(PaperLedgerConflictError, match="Fill id"):
        apply_paper_fill(committed.state, reused_fill)


def test_stale_revision_and_non_monotonic_time_fail_closed() -> None:
    state = opened_ledger()
    stale = build_paper_fill_event(
        account_id=state.account_id,
        expected_revision=state.revision + 1,
        idempotency_key="stale",
        fill_id="stale-fill",
        order_id="stale-order",
        symbol="BTCUSDT",
        side="buy",
        quantity="1",
        reference_price="10",
        fill_price="10",
        fee="0",
        occurred_at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(PaperLedgerRevisionError):
        apply_paper_fill(state, stale)

    backdated = fill(
        state,
        key="backdated",
        fill_id="backdated-fill",
        side="buy",
        quantity="1",
        reference_price="10",
        fill_price="10",
        occurred_at=NOW - timedelta(seconds=1),
    )
    with pytest.raises(PaperLedgerConflictError, match="precede"):
        apply_paper_fill(state, backdated)


def test_cash_and_long_only_position_limits_are_hard_invariants() -> None:
    state = opened_ledger("10")
    too_large = fill(
        state,
        key="too-large-buy",
        fill_id="too-large-buy",
        side="buy",
        quantity="2",
        reference_price="6",
        fill_price="6",
    )
    with pytest.raises(InsufficientPaperCashError):
        apply_paper_fill(state, too_large)

    no_position = fill(
        state,
        key="naked-sell",
        fill_id="naked-sell",
        side="sell",
        quantity="1",
        reference_price="6",
        fill_price="6",
    )
    with pytest.raises(InsufficientPaperPositionError):
        apply_paper_fill(state, no_position)


def test_exact_arithmetic_ignores_ambient_decimal_precision() -> None:
    def calculate(precision: int) -> PaperLedgerState:
        with localcontext() as context:
            context.prec = precision
            state = opened_ledger("999999999999999999999999999999999999999")
            event = fill(
                state,
                key="long-buy",
                fill_id="long-buy",
                side="buy",
                quantity="12345678901234567890.123456789",
                reference_price="0.0000000001234567890123456789",
                fill_price="0.0000000001234567890123456791",
                fee="0.0000000000000000000000000001",
            )
            return apply_paper_fill(state, event).state

    assert calculate(6) == calculate(80)


def test_model_copy_forgery_is_revalidated_at_transition_boundary() -> None:
    state = opened_ledger()
    forged = state.model_copy(update={"cash": Decimal("999")})
    event = fill(
        state,
        key="forged-state-buy",
        fill_id="forged-state-buy",
        side="buy",
        quantity="1",
        reference_price="10",
        fill_price="10",
    )

    with pytest.raises(ValueError, match="failed strict model revalidation"):
        apply_paper_fill(forged, event)

    forged_event = event.model_copy(update={"gross_notional": Decimal("1")})
    with pytest.raises(ValueError, match="failed strict model revalidation"):
        apply_paper_fill(state, forged_event)


def test_journal_and_materialized_projection_tampering_fail_closed() -> None:
    state = opened_ledger()
    bought = apply_paper_fill(
        state,
        fill(
            state,
            key="tamper-buy",
            fill_id="tamper-buy",
            side="buy",
            quantity="1",
            reference_price="10",
            fill_price="10",
        ),
    ).state

    transaction = bought.transactions[-1]
    bad_posting = transaction.postings[0].model_copy(update={"amount": Decimal("-1")})
    forged_transaction = transaction.model_copy(
        update={"postings": (bad_posting, *transaction.postings[1:])}
    )
    forged_journal = bought.model_copy(
        update={"transactions": (*bought.transactions[:-1], forged_transaction)}
    )
    with pytest.raises(ValueError, match="failed strict model revalidation"):
        reconcile_paper_ledger(forged_journal)

    forged_cash = bought.model_copy(update={"cash": Decimal("999")})
    with pytest.raises(ValueError, match="failed strict model revalidation"):
        reconcile_paper_ledger(forged_cash)
