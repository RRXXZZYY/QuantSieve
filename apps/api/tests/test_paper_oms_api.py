from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from httpx import Response
from quantsieve_api.config import Settings
from quantsieve_api.main import create_app
from quantsieve_providers import BinanceSpotTradingRules, ExecutionQuote


class FakePaperOmsPriceProvider:
    def __init__(
        self,
        *,
        bid_price: Decimal = Decimal("99"),
        ask_price: Decimal = Decimal("100"),
        unavailable: bool = False,
        lot_step_size: Decimal = Decimal("0.00001"),
        lot_min_quantity: Decimal = Decimal("0.00001"),
        lot_max_quantity: Decimal = Decimal("1000000"),
        market_step_size: Decimal = Decimal("0.00001"),
        market_min_quantity: Decimal = Decimal("0.00001"),
        market_max_quantity: Decimal = Decimal("1000000"),
        min_notional: Decimal = Decimal("5"),
        max_notional: Decimal | None = None,
    ) -> None:
        self.bid_price = bid_price
        self.ask_price = ask_price
        self.unavailable = unavailable
        self.lot_step_size = lot_step_size
        self.lot_min_quantity = lot_min_quantity
        self.lot_max_quantity = lot_max_quantity
        self.market_step_size = market_step_size
        self.market_min_quantity = market_min_quantity
        self.market_max_quantity = market_max_quantity
        self.min_notional = min_notional
        self.max_notional = max_notional
        self.trading_rules_calls = 0
        self.execution_quote_calls = 0
        self.concurrent_quote_barrier: threading.Barrier | None = None
        self._quote_sequence = 0
        self._quote_sequence_lock = threading.Lock()

    async def trading_rules(self, symbol: str) -> BinanceSpotTradingRules:
        self.trading_rules_calls += 1
        if self.unavailable:
            raise RuntimeError("simulated provider outage")
        now = datetime.now(UTC)
        return BinanceSpotTradingRules(
            symbol=symbol,
            base_asset="BTC",
            quote_asset="USDT",
            status="TRADING",
            spot_trading_allowed=True,
            order_types=("LIMIT", "MARKET"),
            lot_step_size=self.lot_step_size,
            lot_min_quantity=self.lot_min_quantity,
            lot_max_quantity=self.lot_max_quantity,
            market_step_size=self.market_step_size,
            market_min_quantity=self.market_min_quantity,
            market_max_quantity=self.market_max_quantity,
            min_notional=self.min_notional,
            min_notional_applies_to_market=True,
            max_notional=self.max_notional,
            max_notional_applies_to_market=self.max_notional is not None,
            notional_average_price_minutes=5,
            verified_at=now,
        )

    async def execution_quote(
        self,
        symbol: str,
        *,
        rules: BinanceSpotTradingRules,
    ) -> ExecutionQuote:
        with self._quote_sequence_lock:
            self.execution_quote_calls += 1
            self._quote_sequence += 1
            quote_sequence = self._quote_sequence
        assert rules.symbol == symbol
        ask_price = self.ask_price
        if self.concurrent_quote_barrier is not None:
            ask_price = Decimal("100") + Decimal(quote_sequence % 2)
            self.concurrent_quote_barrier.wait(timeout=5)
        now = datetime.now(UTC)
        return ExecutionQuote(
            symbol=symbol,
            provider="binance",
            venue="Binance Spot",
            bid_price=self.bid_price,
            ask_price=ask_price,
            bid_quantity=Decimal("1000"),
            ask_quantity=Decimal("1000"),
            notional_reference_price=Decimal("99.5"),
            notional_reference_kind="last_price",
            notional_reference_window_minutes=0,
            notional_reference_at=now - timedelta(milliseconds=100),
            notional_reference_observed_at=now - timedelta(milliseconds=80),
            exchange_reference_available=False,
            exchange_reference_at=now - timedelta(milliseconds=100),
            exchange_reference_observed_at=now - timedelta(milliseconds=80),
            request_started_at=now - timedelta(milliseconds=200),
            observed_at=now,
            exchange_server_time=now,
            clock_checked_at=now,
            cache_used=False,
        )


def _client(
    tmp_path,
    *,
    provider: FakePaperOmsPriceProvider | None = None,
) -> TestClient:
    app = create_app(
        Settings(
            build_version="paper-oms-api-test",
            database_path=str(tmp_path / "paper-oms.db"),
            cache_path=str(tmp_path / "paper-oms-cache.db"),
            monitor_scheduler_enabled=False,
            paper_scheduler_enabled=False,
        ),
        paper_oms_price_provider=provider or FakePaperOmsPriceProvider(),
    )
    app.state.paper_oms_kill_switch.clear()
    return TestClient(app)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _create_account(api: TestClient) -> dict[str, object]:
    response = api.post(
        "/api/v1/paper-oms/accounts",
        json={
            "idempotency_key": "create-account-1",
            "occurred_at": "2026-07-29T00:00:00Z",
            "account_id": "paper-main",
            "currency": "USDT",
            "initial_cash": "1000",
        },
    )
    assert response.status_code == 201
    return response.json()


def _submit_order(api: TestClient) -> dict[str, object]:
    response = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders",
        json=_submit_payload(),
    )
    assert response.status_code == 201
    return response.json()


def _submit_payload(
    *,
    idempotency_key: str = "submit-order-1",
    order_id: str = "order-1",
    symbol: str = "BTCUSDT",
    quantity: str = "2",
    side: str = "buy",
) -> dict[str, str]:
    return {
        "idempotency_key": idempotency_key,
        "occurred_at": "2026-07-29T00:01:00Z",
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
    }


def test_paper_oms_api_runs_idempotent_atomic_fill_cycle(tmp_path) -> None:
    provider = FakePaperOmsPriceProvider()
    api = _client(tmp_path, provider=provider)

    created = _create_account(api)
    assert created["idempotent_replay"] is False
    assert Decimal(created["account"]["ledger"]["cash"]) == Decimal("1000")

    replay = _create_account(api)
    assert replay["idempotent_replay"] is True

    submitted = _submit_order(api)
    assert submitted["idempotent_replay"] is False
    assert submitted["order"]["state"]["revision"] == 0

    acknowledged = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders/order-1/events",
        json={
            "idempotency_key": "ack-order-1",
            "occurred_at": _now_iso(),
            "expected_order_revision": 0,
            "event_type": "acknowledged",
        },
    )
    assert acknowledged.status_code == 200
    assert acknowledged.json()["order"]["state"]["revision"] == 1

    fill_payload = {
        "idempotency_key": "fill-order-1",
        "expected_order_revision": 1,
        "expected_account_revision": 1,
        "quantity": "2",
    }
    filled = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders/order-1/fills",
        json=fill_payload,
    )
    assert filled.status_code == 200, filled.text
    body = filled.json()
    assert body["idempotent_replay"] is False
    assert Decimal(body["account"]["ledger"]["cash"]) == Decimal("799.8")
    positions = body["account"]["ledger"]["positions"]
    assert len(positions) == 1
    assert positions[0]["symbol"] == "BTCUSDT"
    assert Decimal(positions[0]["quantity"]) == Decimal("2")
    assert body["order"]["state"]["revision"] == 2

    quote_calls_after_fill = (
        provider.trading_rules_calls,
        provider.execution_quote_calls,
    )
    provider.ask_price = Decimal("123.45")
    fill_replay = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders/order-1/fills",
        json=fill_payload,
    )
    assert fill_replay.status_code == 200
    assert fill_replay.json()["idempotent_replay"] is True
    assert fill_replay.json()["account"]["ledger"]["revision"] == 2
    assert (
        provider.trading_rules_calls,
        provider.execution_quote_calls,
    ) == quote_calls_after_fill
    assert Decimal(fill_replay.json()["event"]["order_event"]["fill_price"]) == Decimal("100")

    changed_revision = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders/order-1/fills",
        json={
            **fill_payload,
            "expected_order_revision": 2,
        },
    )
    assert changed_revision.status_code == 409
    assert (
        provider.trading_rules_calls,
        provider.execution_quote_calls,
    ) == quote_calls_after_fill

    listed = api.get("/api/v1/paper-oms/accounts/paper-main/orders")
    assert listed.status_code == 200
    assert [item["order_id"] for item in listed.json()] == ["order-1"]


def test_concurrent_same_fill_request_replays_first_server_quote(
    tmp_path,
) -> None:
    provider = FakePaperOmsPriceProvider()
    api = _client(tmp_path, provider=provider)
    _create_account(api)
    _submit_order(api)
    acknowledged = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders/order-1/events",
        json={
            "idempotency_key": "concurrent-ack",
            "occurred_at": _now_iso(),
            "expected_order_revision": 0,
            "event_type": "acknowledged",
        },
    )
    assert acknowledged.status_code == 200, acknowledged.text
    provider.concurrent_quote_barrier = threading.Barrier(2)
    fill_payload = {
        "idempotency_key": "concurrent-fill",
        "expected_order_revision": 1,
        "expected_account_revision": 1,
        "quantity": "1",
    }

    def post_fill() -> Response:
        return api.post(
            "/api/v1/paper-oms/accounts/paper-main/orders/order-1/fills",
            json=fill_payload,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(post_fill), executor.submit(post_fill))
        responses = tuple(future.result() for future in futures)
    assert all(response.status_code == 200 for response in responses)
    bodies = tuple(response.json() for response in responses)
    assert {body["idempotent_replay"] for body in bodies} == {False, True}
    assert len({body["event"]["event_id"] for body in bodies}) == 1
    assert len({body["event"]["fill_execution_evidence"]["fill_price"] for body in bodies}) == 1
    with sqlite3.connect(tmp_path / "paper-oms.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_oms_order_events WHERE event_type = 'fill'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_oms_commands WHERE command_kind = 'fill'"
        ).fetchone() == (1,)


def test_paper_oms_api_fails_closed_on_stale_or_invalid_commands(tmp_path) -> None:
    api = _client(tmp_path)
    _create_account(api)
    _submit_order(api)

    stale = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders/order-1/events",
        json={
            "idempotency_key": "stale-event",
            "occurred_at": _now_iso(),
            "expected_order_revision": 9,
            "event_type": "acknowledged",
        },
    )
    assert stale.status_code == 409

    injected_namespace = api.post(
        "/api/v1/paper-oms/accounts",
        json={
            "command_namespace": "forged",
            "idempotency_key": "create-account-2",
            "occurred_at": "2026-07-29T00:00:00Z",
            "account_id": "paper-other",
            "currency": "USDT",
            "initial_cash": "1000",
        },
    )
    assert injected_namespace.status_code == 422

    float_amount = api.post(
        "/api/v1/paper-oms/accounts",
        json={
            "idempotency_key": "create-account-3",
            "occurred_at": "2026-07-29T00:00:00Z",
            "account_id": "paper-float",
            "currency": "USDT",
            "initial_cash": 1000.0,
        },
    )
    assert float_amount.status_code == 422

    missing = api.get("/api/v1/paper-oms/accounts/unknown")
    assert missing.status_code == 404


def test_paper_oms_api_replays_allowed_risk_evidence_without_provider_io(
    tmp_path,
) -> None:
    provider = FakePaperOmsPriceProvider()
    api = _client(tmp_path, provider=provider)
    _create_account(api)

    first = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders",
        json=_submit_payload(),
    )
    assert first.status_code == 201
    first_body = first.json()
    evaluation = first_body["order"]["risk_evaluation"]
    assert evaluation["outcome"] == "allow"
    assert provider.trading_rules_calls == 1
    assert provider.execution_quote_calls == 1

    replay = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders",
        json=_submit_payload(),
    )
    assert replay.status_code == 201
    replay_body = replay.json()
    assert replay_body["idempotent_replay"] is True
    assert (
        replay_body["order"]["risk_evaluation"]["request"]["request_hash"]
        == evaluation["request"]["request_hash"]
    )
    assert (
        replay_body["order"]["risk_evaluation"]["decision"]["decision_hash"]
        == evaluation["decision"]["decision_hash"]
    )
    assert provider.trading_rules_calls == 1
    assert provider.execution_quote_calls == 1

    readable = api.get("/api/v1/paper-oms/accounts/paper-main/order-risk/submit-order-1")
    assert readable.status_code == 200
    assert readable.json() == evaluation


def test_paper_oms_api_replays_rejection_and_conflicts_without_provider_io(
    tmp_path,
) -> None:
    provider = FakePaperOmsPriceProvider()
    api = _client(tmp_path, provider=provider)
    _create_account(api)
    rejected_payload = _submit_payload(quantity="200")

    first = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders",
        json=rejected_payload,
    )
    assert first.status_code == 422
    first_detail = first.json()["detail"]
    assert first_detail["code"] == "ORDER_RISK_REJECTED"
    assert first_detail["idempotent_replay"] is False
    assert first_detail["findings"]
    assert provider.trading_rules_calls == 1
    assert provider.execution_quote_calls == 1

    replay = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders",
        json=rejected_payload,
    )
    assert replay.status_code == 422
    replay_detail = replay.json()["detail"]
    assert replay_detail["idempotent_replay"] is True
    assert replay_detail["request_hash"] == first_detail["request_hash"]
    assert replay_detail["decision_hash"] == first_detail["decision_hash"]
    assert provider.trading_rules_calls == 1
    assert provider.execution_quote_calls == 1

    conflict = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders",
        json=_submit_payload(quantity="201"),
    )
    assert conflict.status_code == 409
    assert provider.trading_rules_calls == 1
    assert provider.execution_quote_calls == 1

    readable = api.get("/api/v1/paper-oms/accounts/paper-main/order-risk/submit-order-1")
    assert readable.status_code == 200
    evidence = readable.json()
    assert evidence["outcome"] == "reject"
    assert evidence["request"]["request_hash"] == first_detail["request_hash"]
    assert evidence["decision"]["decision_hash"] == first_detail["decision_hash"]

    listed = api.get("/api/v1/paper-oms/accounts/paper-main/orders")
    assert listed.status_code == 200
    assert listed.json() == []


def test_paper_oms_api_rejects_non_binance_and_aliases_before_provider_io(
    tmp_path,
) -> None:
    provider = FakePaperOmsPriceProvider()
    api = _client(tmp_path, provider=provider)
    _create_account(api)

    for index, symbol in enumerate(("AAPL", "BTC/USDT"), start=1):
        response = api.post(
            "/api/v1/paper-oms/accounts/paper-main/orders",
            json=_submit_payload(
                idempotency_key=f"unsupported-{index}",
                order_id=f"unsupported-order-{index}",
                symbol=symbol,
            ),
        )
        assert response.status_code == 422

    assert provider.trading_rules_calls == 0
    assert provider.execution_quote_calls == 0
    listed = api.get("/api/v1/paper-oms/accounts/paper-main/orders")
    assert listed.status_code == 200
    assert listed.json() == []


def test_paper_oms_api_fails_closed_when_trusted_price_is_unavailable(tmp_path) -> None:
    provider = FakePaperOmsPriceProvider(unavailable=True)
    api = _client(tmp_path, provider=provider)
    _create_account(api)

    response = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders",
        json=_submit_payload(),
    )
    assert response.status_code == 503
    assert provider.trading_rules_calls == 1
    assert provider.execution_quote_calls == 0

    listed = api.get("/api/v1/paper-oms/accounts/paper-main/orders")
    assert listed.status_code == 200
    assert listed.json() == []


def test_paper_oms_api_owns_routing_fill_economics_and_server_timeline(
    tmp_path,
) -> None:
    api = _client(tmp_path)
    _create_account(api)

    client_routed = _submit_payload()
    client_routed["execution_source"] = "client-forged-venue"
    rejected_route = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders",
        json=client_routed,
    )
    assert rejected_route.status_code == 422

    _submit_order(api)
    backfilled = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders/order-1/events",
        json={
            "idempotency_key": "backfilled-ack",
            "occurred_at": "2020-01-01T00:00:00Z",
            "expected_order_revision": 0,
            "event_type": "acknowledged",
        },
    )
    assert backfilled.status_code == 409
    future = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders/order-1/events",
        json={
            "idempotency_key": "future-ack",
            "occurred_at": (datetime.now(UTC) + timedelta(seconds=10)).isoformat(),
            "expected_order_revision": 0,
            "event_type": "acknowledged",
        },
    )
    assert future.status_code == 409

    acknowledged = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders/order-1/events",
        json={
            "idempotency_key": "server-time-ack",
            "occurred_at": _now_iso(),
            "expected_order_revision": 0,
            "event_type": "acknowledged",
        },
    )
    assert acknowledged.status_code == 200
    forged_fill = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders/order-1/fills",
        json={
            "idempotency_key": "forged-fill",
            "occurred_at": _now_iso(),
            "expected_order_revision": 1,
            "expected_account_revision": 1,
            "execution_source": "forged",
            "external_fill_id": "forged",
            "quantity": "1",
            "reference_price": "0.01",
            "fill_price": "0.01",
            "fee": "0",
        },
    )
    assert forged_fill.status_code == 422

    filled = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders/order-1/fills",
        json={
            "idempotency_key": "server-owned-fill",
            "expected_order_revision": 1,
            "expected_account_revision": 1,
            "quantity": "1",
        },
    )
    assert filled.status_code == 200, filled.text
    event = filled.json()["event"]
    assert event["execution_source"] == "quantsieve.paper-oms.binance-simulator.v1"
    assert Decimal(event["fill_execution_evidence"]["fill_price"]) == Decimal("100")
    assert Decimal(event["fill_execution_evidence"]["fee"]) == Decimal("0.1")
    assert event["committed_at"] >= event["received_at"]
    with sqlite3.connect(tmp_path / "paper-oms.db") as connection:
        evidence = connection.execute(
            """
            SELECT fill_execution_evidence_payload,
                   fill_execution_evidence_hash,
                   occurred_at, received_at, committed_at
            FROM paper_oms_order_events
            WHERE event_type = 'fill'
            """
        ).fetchone()
        assert evidence is not None
        assert all(value is not None for value in evidence)
        market = connection.execute(
            """
            SELECT market_evidence_payload, market_evidence_hash,
                   reservation_state_payload, reservation_state_hash
            FROM paper_oms_order_risk_evaluations
            WHERE outcome = 'allow'
            """
        ).fetchone()
        assert market is not None
        assert all(value is not None for value in market)


def test_paper_oms_sell_approval_uses_bid_not_ask(tmp_path) -> None:
    api = _client(
        tmp_path,
        provider=FakePaperOmsPriceProvider(
            bid_price=Decimal("97"),
            ask_price=Decimal("103"),
        ),
    )
    _create_account(api)
    response = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders",
        json=_submit_payload(
            idempotency_key="sell-without-position",
            order_id="sell-without-position",
            quantity="1",
            side="sell",
        ),
    )
    assert response.status_code == 422
    evidence = api.get("/api/v1/paper-oms/accounts/paper-main/order-risk/sell-without-position")
    assert evidence.status_code == 200
    body = evidence.json()
    assert body["request"]["price_evidence"]["reference_price"] == "97"
    assert body["market_evidence"]["side"] == "sell"


def test_paper_oms_market_filters_use_exact_quantity_and_notional_rules(
    tmp_path,
) -> None:
    default_api = _client(tmp_path / "default")
    _create_account(default_api)
    bad_step = default_api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders",
        json=_submit_payload(
            idempotency_key="bad-step",
            order_id="bad-step",
            quantity="0.000011",
        ),
    )
    assert bad_step.status_code == 422
    assert "steps" in bad_step.json()["detail"]
    below_notional = default_api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders",
        json=_submit_payload(
            idempotency_key="below-notional",
            order_id="below-notional",
            quantity="0.00001",
        ),
    )
    assert below_notional.status_code == 422
    assert "minimum" in below_notional.json()["detail"]

    constrained_api = _client(
        tmp_path / "constrained",
        provider=FakePaperOmsPriceProvider(
            lot_step_size=Decimal("0.01"),
            lot_min_quantity=Decimal("0.1"),
            lot_max_quantity=Decimal("2"),
            market_step_size=Decimal("0.01"),
            market_min_quantity=Decimal("0.1"),
            market_max_quantity=Decimal("2"),
            min_notional=Decimal("1"),
            max_notional=Decimal("150"),
        ),
    )
    _create_account(constrained_api)
    below_quantity = constrained_api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders",
        json=_submit_payload(
            idempotency_key="below-quantity",
            order_id="below-quantity",
            quantity="0.05",
        ),
    )
    assert below_quantity.status_code == 422
    assert "quantity filters" in below_quantity.json()["detail"]
    above_quantity = constrained_api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders",
        json=_submit_payload(
            idempotency_key="above-quantity",
            order_id="above-quantity",
            quantity="3",
        ),
    )
    assert above_quantity.status_code == 422
    assert "quantity filters" in above_quantity.json()["detail"]
    above_notional = constrained_api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders",
        json=_submit_payload(
            idempotency_key="above-notional",
            order_id="above-notional",
            quantity="2",
        ),
    )
    assert above_notional.status_code == 422
    assert "maximum" in above_notional.json()["detail"]


def test_partial_fill_fragment_does_not_reapply_order_minimum_filters(
    tmp_path,
) -> None:
    api = _client(
        tmp_path,
        provider=FakePaperOmsPriceProvider(
            lot_step_size=Decimal("0.1"),
            lot_min_quantity=Decimal("1"),
            market_step_size=Decimal("0.1"),
            market_min_quantity=Decimal("1"),
            min_notional=Decimal("50"),
        ),
    )
    _create_account(api)
    submitted = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders",
        json=_submit_payload(
            idempotency_key="partial-order",
            order_id="partial-order",
            quantity="2",
        ),
    )
    assert submitted.status_code == 201, submitted.text
    acknowledged = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders/partial-order/events",
        json={
            "idempotency_key": "partial-ack",
            "occurred_at": _now_iso(),
            "expected_order_revision": 0,
            "event_type": "acknowledged",
        },
    )
    assert acknowledged.status_code == 200, acknowledged.text
    partial = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders/partial-order/fills",
        json={
            "idempotency_key": "partial-small-fragment",
            "expected_order_revision": 1,
            "expected_account_revision": 1,
            "quantity": "0.1",
        },
    )
    assert partial.status_code == 200, partial.text
    assert partial.json()["order"]["state"]["status"] == "partially_filled"
    assert Decimal(partial.json()["order"]["state"]["filled_quantity"]) == Decimal("0.1")


def test_paper_oms_command_construction_errors_are_422(tmp_path) -> None:
    api = _client(tmp_path)
    _create_account(api)
    invalid_symbol = api.post(
        "/api/v1/paper-oms/accounts/paper-main/orders",
        json=_submit_payload(symbol="BTC USDT"),
    )
    assert invalid_symbol.status_code == 422
