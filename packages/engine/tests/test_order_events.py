from __future__ import annotations

from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError
from quantsieve_engine.order_events import (
    OrderAcknowledgedEvent,
    OrderCancelAcknowledgedEvent,
    OrderCancelRejectedEvent,
    OrderCancelRequestedEvent,
    OrderExpiredEvent,
    OrderFillEvent,
    OrderIdempotencyConflictError,
    OrderInvariantError,
    OrderRejectedEvent,
    OrderState,
    OrderTransitionError,
    apply_order_event,
    create_order_state,
    reduce_order,
    replay_order_events,
)


def acknowledged(key: str = "ack-1") -> OrderAcknowledgedEvent:
    return OrderAcknowledgedEvent(order_id="order-1", idempotency_key=key)


def fill(
    key: str,
    quantity: str,
    price: str,
    *,
    fill_id: str | None = None,
) -> OrderFillEvent:
    return OrderFillEvent(
        order_id="order-1",
        idempotency_key=key,
        fill_id=key if fill_id is None else fill_id,
        fill_quantity=quantity,
        fill_price=price,
    )


def working_order(quantity: str = "10") -> OrderState:
    return reduce_order(
        create_order_state(order_id="order-1", requested_quantity=quantity),
        acknowledged(),
    )


def partially_filled_order() -> OrderState:
    return reduce_order(working_order(), fill("fill-1", "2", "100"))


def test_contracts_are_strict_frozen_normalized_and_content_addressed() -> None:
    state = create_order_state(
        order_id=" order-1 ",
        requested_quantity=Decimal("10.0000"),
    )
    event = OrderFillEvent(
        order_id=" order-1 ",
        idempotency_key=" fill-1 ",
        fill_id=" execution-1 ",
        fill_quantity="2.000",
        fill_price=Decimal("100.5000"),
    )
    equivalent = OrderFillEvent(
        order_id="order-1",
        idempotency_key="different-delivery",
        fill_id="execution-1",
        fill_quantity=Decimal("2"),
        fill_price="100.5",
    )

    assert state.order_id == "order-1"
    assert state.requested_quantity == Decimal("10")
    assert event.fill_quantity.as_tuple() == Decimal("2").as_tuple()
    assert event.fill_price.as_tuple() == Decimal("100.5").as_tuple()
    assert event.payload_hash == equivalent.payload_hash
    with pytest.raises(ValidationError):
        state.status = "working"
    with pytest.raises(ValidationError, match="extra"):
        OrderAcknowledgedEvent.model_validate(
            {
                "order_id": "order-1",
                "idempotency_key": "ack-extra",
                "unknown": True,
            }
        )
    with pytest.raises(ValidationError, match="Decimal"):
        OrderFillEvent(
            order_id="order-1",
            idempotency_key="float-fill",
            fill_id="float-execution",
            fill_quantity=2.0,
            fill_price="100",
        )
    with pytest.raises(ValidationError, match="payload_hash"):
        OrderFillEvent(
            order_id="order-1",
            idempotency_key="bad-hash",
            fill_id="bad-hash-execution",
            fill_quantity="2",
            fill_price="100",
            payload_hash="0" * 64,
        )


def test_partial_fills_accumulate_exact_notional_and_weighted_average() -> None:
    state = working_order()
    state = reduce_order(state, fill("fill-1", "2", "100"))

    assert state.status == "partially_filled"
    assert state.filled_quantity == Decimal("2")
    assert state.filled_notional == Decimal("200")
    assert state.average_fill_price == Decimal("100")

    state = reduce_order(state, fill("fill-2", "3", "110"))
    assert state.status == "partially_filled"
    assert state.filled_quantity == Decimal("5")
    assert state.filled_notional == Decimal("530")
    assert state.average_fill_price == Decimal("106")

    state = apply_order_event(state, fill("fill-3", "5", "120"))
    assert state.status == "filled"
    assert state.filled_quantity == state.requested_quantity
    assert state.filled_notional == Decimal("1130")
    assert state.average_fill_price == Decimal("113")
    assert state.revision == 4
    assert [receipt.revision for receipt in state.event_receipts] == [1, 2, 3, 4]


def test_overfill_is_rejected_without_mutating_the_prior_state() -> None:
    state = partially_filled_order()

    with pytest.raises(OrderInvariantError, match="exceed"):
        reduce_order(state, fill("fill-too-large", "9", "101"))

    assert state.status == "partially_filled"
    assert state.filled_quantity == Decimal("2")
    assert state.revision == 2


def test_cancel_rejection_restores_each_prior_active_status() -> None:
    pending = create_order_state(order_id="order-1", requested_quantity="10")
    pending = reduce_order(
        pending,
        OrderCancelRequestedEvent(
            order_id="order-1",
            idempotency_key="cancel-pending",
        ),
    )
    assert pending.status == "pending_cancel"
    assert pending.status_before_cancel == "pending_submit"
    pending = reduce_order(
        pending,
        OrderCancelRejectedEvent(
            order_id="order-1",
            idempotency_key="cancel-pending-rejected",
            reason="still submitting",
        ),
    )
    assert pending.status == "pending_submit"

    working = working_order()
    working = reduce_order(
        working,
        OrderCancelRequestedEvent(
            order_id="order-1",
            idempotency_key="cancel-working",
        ),
    )
    working = reduce_order(
        working,
        OrderCancelRejectedEvent(
            order_id="order-1",
            idempotency_key="cancel-working-rejected",
            reason="already routed",
        ),
    )
    assert working.status == "working"

    partial = partially_filled_order()
    partial = reduce_order(
        partial,
        OrderCancelRequestedEvent(
            order_id="order-1",
            idempotency_key="cancel-partial",
        ),
    )
    partial = reduce_order(
        partial,
        OrderCancelRejectedEvent(
            order_id="order-1",
            idempotency_key="cancel-partial-rejected",
            reason="too late",
        ),
    )
    assert partial.status == "partially_filled"
    assert partial.status_before_cancel is None


def test_early_cancel_accepts_late_submit_ack_then_restores_working() -> None:
    state = create_order_state(order_id="order-1", requested_quantity="10")
    state = reduce_order(
        state,
        OrderCancelRequestedEvent(
            order_id="order-1",
            idempotency_key="early-cancel",
        ),
    )
    state = reduce_order(
        state,
        OrderAcknowledgedEvent(
            order_id="order-1",
            idempotency_key="late-submit-ack",
        ),
    )

    assert state.status == "pending_cancel"
    assert state.status_before_cancel == "working"

    state = reduce_order(
        state,
        OrderCancelRejectedEvent(
            order_id="order-1",
            idempotency_key="early-cancel-rejected",
            reason="already live",
        ),
    )
    assert state.status == "working"
    assert state.status_before_cancel is None


def test_early_cancel_accepts_late_submission_rejection() -> None:
    state = create_order_state(order_id="order-1", requested_quantity="10")
    state = reduce_order(
        state,
        OrderCancelRequestedEvent(
            order_id="order-1",
            idempotency_key="early-cancel",
        ),
    )
    state = reduce_order(
        state,
        OrderRejectedEvent(
            order_id="order-1",
            idempotency_key="late-submit-reject",
            reason="venue rejected order",
        ),
    )

    assert state.status == "rejected"
    assert state.status_before_cancel is None
    assert state.terminal_reason == "venue rejected order"


def test_fills_continue_while_cancel_is_pending_and_can_complete() -> None:
    state = working_order()
    state = reduce_order(
        state,
        OrderCancelRequestedEvent(
            order_id="order-1",
            idempotency_key="cancel-1",
        ),
    )
    state = reduce_order(state, fill("fill-during-cancel", "4", "100"))

    assert state.status == "pending_cancel"
    assert state.status_before_cancel == "partially_filled"
    assert state.filled_quantity == Decimal("4")

    state = reduce_order(state, fill("complete-during-cancel", "6", "101"))
    assert state.status == "filled"
    assert state.status_before_cancel is None


def test_cancel_acknowledgement_preserves_any_partial_fill() -> None:
    state = partially_filled_order()
    state = reduce_order(
        state,
        OrderCancelRequestedEvent(
            order_id="order-1",
            idempotency_key="cancel-1",
        ),
    )
    state = reduce_order(
        state,
        OrderCancelAcknowledgedEvent(
            order_id="order-1",
            idempotency_key="cancel-ack-1",
        ),
    )

    assert state.status == "cancelled"
    assert state.filled_quantity == Decimal("2")
    assert state.filled_notional == Decimal("200")


def test_reject_and_expire_have_distinct_legal_sources() -> None:
    rejected = reduce_order(
        create_order_state(order_id="order-1", requested_quantity="10"),
        OrderRejectedEvent(
            order_id="order-1",
            idempotency_key="reject-1",
            reason=" risk limit ",
        ),
    )
    assert rejected.status == "rejected"
    assert rejected.terminal_reason == "risk limit"

    expired = reduce_order(
        working_order(),
        OrderExpiredEvent(
            order_id="order-1",
            idempotency_key="expire-1",
            reason="day boundary",
        ),
    )
    assert expired.status == "expired"
    assert expired.terminal_reason == "day boundary"

    partially_expired = reduce_order(
        partially_filled_order(),
        OrderExpiredEvent(
            order_id="order-1",
            idempotency_key="expire-partial",
        ),
    )
    assert partially_expired.status == "expired"
    assert partially_expired.filled_quantity == Decimal("2")

    for state, key in (
        (
            create_order_state(order_id="order-1", requested_quantity="10"),
            "expire-early-cancel",
        ),
        (working_order(), "expire-working-cancel"),
        (partially_filled_order(), "expire-partial-cancel"),
    ):
        state = reduce_order(
            state,
            OrderCancelRequestedEvent(
                order_id="order-1",
                idempotency_key=f"cancel-before-{key}",
            ),
        )
        expired_during_cancel = reduce_order(
            state,
            OrderExpiredEvent(
                order_id="order-1",
                idempotency_key=key,
                reason="venue time-in-force elapsed",
            ),
        )
        assert expired_during_cancel.status == "expired"
        assert expired_during_cancel.status_before_cancel is None
        assert expired_during_cancel.filled_quantity == state.filled_quantity

    with pytest.raises(OrderTransitionError):
        reduce_order(
            working_order(),
            OrderRejectedEvent(
                order_id="order-1",
                idempotency_key="late-reject",
                reason="too late",
            ),
        )
    with pytest.raises(OrderTransitionError):
        reduce_order(
            create_order_state(order_id="order-1", requested_quantity="10"),
            OrderExpiredEvent(
                order_id="order-1",
                idempotency_key="early-expire",
            ),
        )


@pytest.mark.parametrize(
    ("state_factory", "event_factory"),
    [
        (working_order, lambda: acknowledged("second-ack")),
        (
            partially_filled_order,
            lambda: OrderAcknowledgedEvent(
                order_id="order-1",
                idempotency_key="late-ack",
            ),
        ),
        (
            working_order,
            lambda: OrderCancelAcknowledgedEvent(
                order_id="order-1",
                idempotency_key="unsolicited-cancel-ack",
            ),
        ),
        (
            working_order,
            lambda: OrderCancelRejectedEvent(
                order_id="order-1",
                idempotency_key="unsolicited-cancel-reject",
                reason="not requested",
            ),
        ),
    ],
)
def test_illegal_active_state_transitions_fail_closed(
    state_factory: object,
    event_factory: object,
) -> None:
    state = state_factory()  # type: ignore[operator]
    event = event_factory()  # type: ignore[operator]
    with pytest.raises(OrderTransitionError):
        reduce_order(state, event)


def test_same_key_same_payload_is_noop_and_different_payload_conflicts() -> None:
    state = working_order()
    original = fill("fill-1", "10", "100")
    terminal = reduce_order(state, original)

    replayed = reduce_order(terminal, original)
    assert replayed == terminal
    assert replayed.revision == terminal.revision

    with pytest.raises(OrderIdempotencyConflictError, match="different payload"):
        reduce_order(terminal, fill("fill-1", "10", "101"))
    with pytest.raises(OrderTransitionError, match="exact idempotent"):
        reduce_order(
            terminal,
            OrderCancelRequestedEvent(
                order_id="order-1",
                idempotency_key="new-terminal-event",
            ),
        )


def test_fill_economic_identity_is_idempotent_across_delivery_keys() -> None:
    state = working_order()
    original = fill("delivery-1", "2", "100", fill_id="execution-1")
    partially_filled = reduce_order(state, original)

    replay = fill("delivery-2", "2", "100", fill_id="execution-1")
    assert replay.payload_hash == original.payload_hash
    assert reduce_order(partially_filled, replay) == partially_filled
    assert partially_filled.revision == 2
    assert partially_filled.event_receipts[-1].fill_id == "execution-1"

    with pytest.raises(OrderIdempotencyConflictError, match="different economics"):
        reduce_order(
            partially_filled,
            fill("delivery-3", "2", "101", fill_id="execution-1"),
        )


def test_idempotency_conflict_is_detected_before_an_active_transition() -> None:
    state = working_order()

    with pytest.raises(OrderIdempotencyConflictError):
        reduce_order(
            state,
            OrderCancelRequestedEvent(
                order_id="order-1",
                idempotency_key="ack-1",
            ),
        )


def test_reducer_revalidates_forged_models_and_order_identity() -> None:
    state = working_order()
    forged_state = state.model_copy(update={"filled_quantity": Decimal("11")})
    with pytest.raises(ValidationError, match="exceed"):
        reduce_order(forged_state, fill("fill-1", "1", "100"))

    original = fill("fill-1", "1", "100")
    forged_event = original.model_copy(update={"fill_quantity": Decimal("2")})
    with pytest.raises(ValidationError, match="payload_hash"):
        reduce_order(state, forged_event)

    with pytest.raises(OrderInvariantError, match="does not match"):
        reduce_order(
            state,
            OrderFillEvent(
                order_id="another-order",
                idempotency_key="wrong-order",
                fill_id="wrong-order-execution",
                fill_quantity="1",
                fill_price="100",
            ),
        )


def test_order_state_rejects_inconsistent_projection_fields() -> None:
    with pytest.raises(ValidationError, match="Average fill price"):
        OrderState(
            order_id="order-1",
            requested_quantity="10",
            status="partially_filled",
            filled_quantity="2",
            filled_notional="200",
            average_fill_price="99",
        )
    with pytest.raises(ValidationError, match="exactly"):
        OrderState(
            order_id="order-1",
            requested_quantity="10",
            status="filled",
            filled_quantity="9",
            filled_notional="900",
            average_fill_price="100",
        )


def test_decimal_economics_do_not_depend_on_process_decimal_context() -> None:
    quantity = "12345678901234567890123456789"
    price = "9.8765432101234567890123456789"

    with localcontext() as context:
        context.prec = 6
        low_precision = replay_order_events(
            create_order_state(order_id="order-1", requested_quantity=quantity),
            [acknowledged(), fill("fill-1", quantity, price)],
        )
    with localcontext() as context:
        context.prec = 80
        high_precision = replay_order_events(
            create_order_state(order_id="order-1", requested_quantity=quantity),
            [acknowledged(), fill("fill-1", quantity, price)],
        )

    assert low_precision == high_precision
    assert low_precision.status == "filled"
    assert low_precision.average_fill_price == Decimal(price)


def test_replay_helper_preserves_order_and_exact_duplicate_semantics() -> None:
    initial = create_order_state(order_id="order-1", requested_quantity="3")
    events = [
        acknowledged(),
        fill("fill-1", "1", "10"),
        fill("fill-1", "1", "10"),
        fill("fill-2", "2", "11"),
    ]

    state = replay_order_events(initial, events)

    assert state.status == "filled"
    assert state.revision == 3
    assert state.filled_quantity == Decimal("3")
    assert state.filled_notional == Decimal("32")
