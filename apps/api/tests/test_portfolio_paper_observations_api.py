from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from threading import get_ident
from types import SimpleNamespace

import pytest
import quantsieve_api.routers.portfolio_paper as portfolio_paper_router_module
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from quantsieve_api.routers.portfolio_paper import (
    PortfolioPaperObservationResponse,
    PortfolioPaperStatusResponse,
    PortfolioPaperValuationSummary,
    portfolio_paper_observation,
    portfolio_paper_observations,
    router,
)

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
INFORMATION_SESSION = datetime(2026, 7, 25, tzinfo=UTC).isoformat()
EXECUTION_SESSION = datetime(2026, 7, 26, tzinfo=UTC).isoformat()
SYMBOLS = ("BTCUSDT", "ETHUSDT")
TARGET_WEIGHTS = {"BTCUSDT": 0.5, "ETHUSDT": 0.5}
SENSITIVE_HASH = "a" * 64
SENSITIVE_OWNER = "lease-owner-private-token"
SENSITIVE_ERROR = "upstream credential private-token"


class FakeObservationStore:
    def __init__(self, records: list[SimpleNamespace]) -> None:
        self.records = records
        self.list_limits: list[int] = []
        self.list_threads: list[int] = []
        self.get_ids: list[str] = []
        self.get_threads: list[int] = []

    def list(self, *, limit: int = 100) -> list[SimpleNamespace]:
        self.list_limits.append(limit)
        self.list_threads.append(get_ident())
        return self.records[:limit]

    def get(self, track_id: str) -> SimpleNamespace | None:
        self.get_ids.append(track_id)
        self.get_threads.append(get_ident())
        return next(
            (record for record in self.records if record.id == track_id),
            None,
        )


def _scheduler(*, enabled: bool, running: bool) -> SimpleNamespace:
    return SimpleNamespace(
        status=SimpleNamespace(
            enabled=enabled,
            running=running,
            poll_seconds=60.0,
            last_run_at=NOW,
            last_outcome="failed",
            last_track_id="f" * 32,
            last_error=SENSITIVE_ERROR,
            consecutive_failures=9,
        )
    )


def _record(
    index: int,
    *,
    deadline: datetime,
    opened: bool = False,
    valued: bool = False,
    last_error: str | None = None,
) -> SimpleNamespace:
    track_id = f"{index:x}" * 32
    created_at = NOW - timedelta(days=index)
    opening_at = created_at + timedelta(minutes=3) if opened or valued else None
    valuation_at = created_at + timedelta(days=1, minutes=3) if valued else None
    updated_at = valuation_at or opening_at or created_at
    certificate = SimpleNamespace(
        execution_session=EXECUTION_SESSION,
        execution_deadline=deadline,
        certificate_hash=SENSITIVE_HASH,
        bars=[{"raw_bar": "private"}],
        basket=SimpleNamespace(
            instruments=[
                SimpleNamespace(
                    rules={"api_rule_detail": "private"},
                )
            ]
        ),
    )
    target = SimpleNamespace(
        information_session=INFORMATION_SESSION,
        weights=dict(TARGET_WEIGHTS),
        target_hash=SENSITIVE_HASH,
    )
    pending_decision = SimpleNamespace(
        id=f"decision-private-{index}",
        target=target,
        certificate=certificate,
        target_hash=SENSITIVE_HASH,
        certificate_hash=SENSITIVE_HASH,
    )
    state = SimpleNamespace(
        cash=1000.0,
        equity=110000.0,
        total_return=0.1,
        peak_equity=112000.0,
        max_drawdown=-0.0178571428571429,
        realized_weights={"BTCUSDT": 0.55, "ETHUSDT": 0.4409090909090909},
        turnover_ratio=0.9995,
        turnover_notional=99950.0,
        fee_paid=29.985,
        slippage_paid=19.99,
        total_cost=49.975,
        valuation_count=1 if valued else 0,
        shares={"BTCUSDT": 0.4, "ETHUSDT": 13.5},
        last_prices={"BTCUSDT": 120000.0, "ETHUSDT": 4000.0},
    )
    return SimpleNamespace(
        id=track_id,
        config=SimpleNamespace(
            portfolio_experiment_id=f"experiment-{index}",
            symbols=SYMBOLS,
            method="periodic_equal",
            initial_cash=100000.0,
            fee_rate=0.0003,
            slippage_rate=0.0002,
            configuration_hash=SENSITIVE_HASH,
        ),
        pending_decision=pending_decision,
        state=state,
        created_at=created_at,
        updated_at=updated_at,
        opening_committed_at=opening_at,
        settlement_committed_at=valuation_at,
        opening_batch_id=SENSITIVE_HASH if opened or valued else None,
        settlement_id=("b" * 64) if valued else None,
        refresh_owner=SENSITIVE_OWNER,
        refresh_generation=47,
        refresh_lease_until=NOW + timedelta(hours=1),
        last_checked_at=NOW,
        last_error=last_error,
        configuration_hash=SENSITIVE_HASH,
        quote_payload={"private_quote": "must-not-escape"},
        idempotency_key="idempotency-private-token",
    )


@pytest.fixture
def records() -> list[SimpleNamespace]:
    return [
        _record(1, deadline=NOW - timedelta(days=1), valued=True),
        _record(
            2,
            deadline=NOW - timedelta(days=1),
            opened=True,
            last_error=SENSITIVE_ERROR,
        ),
        _record(3, deadline=NOW),
        _record(4, deadline=NOW + timedelta(minutes=1)),
    ]


@pytest.fixture
def api(
    records: list[SimpleNamespace],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, FakeObservationStore]:
    monkeypatch.setattr(portfolio_paper_router_module, "_utc_now", lambda: NOW)
    store = FakeObservationStore(records)
    app = FastAPI()
    app.state.portfolio_paper_track_store = store
    app.state.portfolio_opening_scheduler = _scheduler(
        enabled=True,
        running=False,
    )
    app.state.portfolio_settlement_scheduler = _scheduler(
        enabled=False,
        running=True,
    )
    app.include_router(router, prefix="/api/v1")
    return TestClient(app), store


def test_combined_status_is_exactly_redacted_and_opening_status_remains(
    api: tuple[TestClient, FakeObservationStore],
) -> None:
    client, _store = api

    response = client.get("/api/v1/portfolio-paper/status")
    legacy = client.get("/api/v1/portfolio-paper/opening-status")

    assert response.status_code == 200
    assert response.json() == {
        "availability": "internal_only",
        "scope": "one_session_modeled_observation",
        "activation_available": False,
        "opening": {"enabled": True, "running": False},
        "settlement": {"enabled": False, "running": True},
    }
    assert legacy.status_code == 200
    assert legacy.json() == {
        "availability": "internal_only",
        "enabled": True,
        "running": False,
    }
    serialized = response.text
    for forbidden in (
        "poll_seconds",
        "last_run_at",
        "last_outcome",
        "last_track_id",
        "last_error",
        "consecutive_failures",
        SENSITIVE_ERROR,
    ):
        assert forbidden not in serialized


def test_list_preserves_newest_first_store_order_and_enforces_limit(
    api: tuple[TestClient, FakeObservationStore],
) -> None:
    client, store = api
    caller_thread = get_ident()

    response = client.get(
        "/api/v1/portfolio-paper/observations",
        params={"limit": 2},
    )

    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload] == ["1" * 32, "2" * 32]
    assert store.list_limits == [2]
    assert store.list_threads
    assert store.list_threads[0] != caller_thread
    assert inspect.iscoroutinefunction(portfolio_paper_observations) is False
    assert inspect.iscoroutinefunction(portfolio_paper_observation) is False


def test_four_phases_and_attention_are_derived_without_lease_processing(
    api: tuple[TestClient, FakeObservationStore],
) -> None:
    client, _store = api

    response = client.get(
        "/api/v1/portfolio-paper/observations",
        params={"limit": 4},
    )

    assert response.status_code == 200
    observations = {item["id"]: item for item in response.json()}
    assert observations["1" * 32]["phase"] == "valued"
    assert observations["1" * 32]["attention_required"] is False
    assert observations["2" * 32]["phase"] == "awaiting_close_valuation"
    assert observations["2" * 32]["attention_required"] is True
    assert observations["3" * 32]["phase"] == "opening_window_expired"
    assert observations["3" * 32]["attention_required"] is True
    assert observations["4" * 32]["phase"] == "awaiting_opening"
    assert observations["4" * 32]["attention_required"] is False
    assert observations["3" * 32]["opening_at"] is None
    assert observations["4" * 32]["opening_at"] is None
    assert observations["2" * 32]["opening_at"] is not None
    assert observations["2" * 32]["valuation_at"] is None


def test_only_valued_observation_exposes_accounting_summary(
    api: tuple[TestClient, FakeObservationStore],
) -> None:
    client, _store = api

    valued = client.get(f"/api/v1/portfolio-paper/observations/{'1' * 32}")
    awaiting = client.get(f"/api/v1/portfolio-paper/observations/{'2' * 32}")

    assert valued.status_code == 200
    assert valued.json()["valuation"] == {
        "cash": 1000.0,
        "equity": 110000.0,
        "total_return": 0.1,
        "peak_equity": 112000.0,
        "max_drawdown": -0.0178571428571429,
        "realized_weights": {
            "BTCUSDT": 0.55,
            "ETHUSDT": 0.4409090909090909,
        },
        "turnover_ratio": 0.9995,
        "turnover_notional": 99950.0,
        "fee_paid": 29.985,
        "slippage_paid": 19.99,
        "total_cost": 49.975,
        "valuation_count": 1,
    }
    assert valued.json()["valuation_at"] is not None
    assert awaiting.status_code == 200
    assert awaiting.json()["valuation"] is None
    assert awaiting.json()["valuation_at"] is None


def test_public_projection_never_serializes_internal_evidence_or_live_claims(
    api: tuple[TestClient, FakeObservationStore],
) -> None:
    client, _store = api

    response = client.get(
        "/api/v1/portfolio-paper/observations",
        params={"limit": 4},
    )

    assert response.status_code == 200
    serialized = response.text.lower()
    forbidden_fields = (
        "refresh_owner",
        "refresh_generation",
        "refresh_lease_until",
        "last_checked_at",
        "last_error",
        "configuration_hash",
        "target_hash",
        "certificate_hash",
        "decision_id",
        "opening_batch_id",
        "settlement_id",
        "idempotency",
        "certificate",
        "raw_bar",
        "quote_payload",
        "private_quote",
        "rules",
        "shares",
        "last_prices",
    )
    for forbidden in forbidden_fields:
        assert forbidden not in serialized
    for sensitive_value in (
        SENSITIVE_HASH,
        SENSITIVE_OWNER,
        SENSITIVE_ERROR,
        "private-token",
    ):
        assert sensitive_value.lower() not in serialized
    for misleading_claim in (
        "continuous",
        "continuously",
        "ongoing",
        "live trading",
        "real-time tracking",
    ):
        assert misleading_claim not in serialized


def test_detail_404_and_request_validation_do_not_touch_store(
    api: tuple[TestClient, FakeObservationStore],
) -> None:
    client, store = api

    missing = client.get(f"/api/v1/portfolio-paper/observations/{'f' * 32}")
    invalid_id = client.get("/api/v1/portfolio-paper/observations/not-a-track-id")
    zero = client.get(
        "/api/v1/portfolio-paper/observations",
        params={"limit": 0},
    )
    excessive = client.get(
        "/api/v1/portfolio-paper/observations",
        params={"limit": 101},
    )
    fractional = client.get(
        "/api/v1/portfolio-paper/observations",
        params={"limit": "1.5"},
    )

    assert missing.status_code == 404
    assert missing.json() == {"detail": "Portfolio-paper observation not found."}
    assert invalid_id.status_code == 422
    assert zero.status_code == 422
    assert excessive.status_code == 422
    assert fractional.status_code == 422
    assert store.get_ids == ["f" * 32]
    assert store.list_limits == []


def test_observation_routes_are_get_only(
    api: tuple[TestClient, FakeObservationStore],
) -> None:
    client, _store = api

    status = client.post("/api/v1/portfolio-paper/status", json={})
    observations = client.post(
        "/api/v1/portfolio-paper/observations",
        json={},
    )
    detail = client.post(
        f"/api/v1/portfolio-paper/observations/{'1' * 32}",
        json={},
    )
    schema = client.get("/openapi.json").json()

    assert status.status_code == 405
    assert observations.status_code == 405
    assert detail.status_code == 405
    assert set(schema["paths"]["/api/v1/portfolio-paper/status"]) == {"get"}
    assert set(schema["paths"]["/api/v1/portfolio-paper/observations"]) == {"get"}
    assert set(schema["paths"]["/api/v1/portfolio-paper/observations/{track_id}"]) == {"get"}


def test_public_models_are_frozen_strict_and_forbid_extra_fields() -> None:
    valuation = PortfolioPaperValuationSummary(
        cash=1000.0,
        equity=110000.0,
        total_return=0.1,
        peak_equity=112000.0,
        max_drawdown=-0.01,
        realized_weights=dict(TARGET_WEIGHTS),
        turnover_ratio=1.0,
        turnover_notional=100000.0,
        fee_paid=30.0,
        slippage_paid=20.0,
        total_cost=50.0,
        valuation_count=1,
    )
    observation = PortfolioPaperObservationResponse(
        id="1" * 32,
        portfolio_experiment_id="experiment",
        symbols=SYMBOLS,
        method="periodic_equal",
        quote_currency="USDT",
        venue="Binance Spot",
        scope="one_session_modeled_observation",
        initial_cash=100000.0,
        fee_rate=0.0003,
        slippage_rate=0.0002,
        information_session=INFORMATION_SESSION,
        execution_session=EXECUTION_SESSION,
        target_weights=dict(TARGET_WEIGHTS),
        created_at=NOW - timedelta(days=2),
        updated_at=NOW,
        opening_at=NOW - timedelta(days=1),
        valuation_at=NOW,
        attention_required=False,
        phase="valued",
        valuation=valuation,
    )

    with pytest.raises(ValidationError):
        observation.phase = "awaiting_opening"
    with pytest.raises(ValidationError):
        PortfolioPaperObservationResponse.model_validate(
            {
                **observation.model_dump(mode="python"),
                "last_error": SENSITIVE_ERROR,
            }
        )
    with pytest.raises(ValidationError):
        PortfolioPaperValuationSummary.model_validate(
            {
                **valuation.model_dump(mode="python"),
                "cash": "1000",
            }
        )
    with pytest.raises(ValidationError):
        PortfolioPaperStatusResponse.model_validate(
            {
                "availability": "internal_only",
                "scope": "one_session_modeled_observation",
                "activation_available": True,
                "opening": {"enabled": False, "running": False},
                "settlement": {"enabled": False, "running": False},
            }
        )


def test_openapi_projection_contains_only_public_whitelist(
    api: tuple[TestClient, FakeObservationStore],
) -> None:
    client, _store = api

    schema = client.get("/openapi.json").json()
    observation = schema["components"]["schemas"]["PortfolioPaperObservationResponse"]
    valuation = schema["components"]["schemas"]["PortfolioPaperValuationSummary"]
    status = schema["components"]["schemas"]["PortfolioPaperStatusResponse"]

    assert set(observation["properties"]) == {
        "id",
        "portfolio_experiment_id",
        "symbols",
        "method",
        "quote_currency",
        "venue",
        "scope",
        "initial_cash",
        "fee_rate",
        "slippage_rate",
        "information_session",
        "execution_session",
        "target_weights",
        "created_at",
        "updated_at",
        "opening_at",
        "valuation_at",
        "attention_required",
        "phase",
        "valuation",
    }
    assert set(valuation["properties"]) == {
        "cash",
        "equity",
        "total_return",
        "peak_equity",
        "max_drawdown",
        "realized_weights",
        "turnover_ratio",
        "turnover_notional",
        "fee_paid",
        "slippage_paid",
        "total_cost",
        "valuation_count",
    }
    assert set(status["properties"]) == {
        "availability",
        "scope",
        "activation_available",
        "opening",
        "settlement",
    }
    assert status["properties"]["activation_available"]["const"] is False
    assert observation["additionalProperties"] is False
    assert valuation["additionalProperties"] is False
    assert status["additionalProperties"] is False
