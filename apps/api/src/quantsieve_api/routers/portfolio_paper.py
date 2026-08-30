from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol, cast

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from quantsieve_engine import PortfolioMethod

from ..portfolio_paper import PortfolioPaperTrackRecord
from ..portfolio_paper_preflight import (
    PortfolioPaperPreflightResponse,
    PortfolioPaperPreflightRulesProvider,
    review_portfolio_paper_readiness,
)
from ..portfolio_paper_scheduler import PortfolioPaperOpeningScheduler
from ..portfolio_paper_settlement_scheduler import (
    PortfolioPaperSettlementScheduler,
)
from ..schemas import PortfolioExperimentRecord

router = APIRouter(prefix="/portfolio-paper", tags=["portfolio-paper"])

_ObservationPhase = Literal[
    "awaiting_opening",
    "opening_window_expired",
    "awaiting_close_valuation",
    "valued",
]
_ObservationScope = Literal["one_session_modeled_observation"]
_Weight = Annotated[
    float,
    Field(ge=0, le=1, allow_inf_nan=False),
]
_TrackId = Annotated[
    str,
    Path(
        min_length=32,
        max_length=32,
        pattern=r"^[0-9a-f]{32}$",
    ),
]


class _PortfolioPaperObservationStore(Protocol):
    def list(self, *, limit: int = 100) -> list[PortfolioPaperTrackRecord]: ...

    def get(self, track_id: str) -> PortfolioPaperTrackRecord | None: ...


class _PortfolioExperimentStore(Protocol):
    def get_portfolio(self, experiment_id: str) -> PortfolioExperimentRecord | None: ...


class PortfolioPaperOpeningStatusResponse(BaseModel):
    """Public, read-only rollout status for the internal opening stage."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    availability: Literal["internal_only"] = Field(
        description=("Rollout-stage label only; it is not an access-control boundary.")
    )
    enabled: bool
    running: bool


class PortfolioPaperSchedulerPublicStatus(BaseModel):
    """Two-field scheduler health projection."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    enabled: bool
    running: bool


class PortfolioPaperStatusResponse(BaseModel):
    """Redacted health for the one-session modeled observation workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    availability: Literal["internal_only"]
    scope: _ObservationScope
    activation_available: Literal[False]
    opening: PortfolioPaperSchedulerPublicStatus
    settlement: PortfolioPaperSchedulerPublicStatus


class PortfolioPaperValuationSummary(BaseModel):
    """Public accounting totals; position quantities and prices stay internal."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    cash: float = Field(ge=0, allow_inf_nan=False)
    equity: float = Field(gt=0, allow_inf_nan=False)
    total_return: float = Field(gt=-1, allow_inf_nan=False)
    peak_equity: float = Field(gt=0, allow_inf_nan=False)
    max_drawdown: float = Field(ge=-1, le=0, allow_inf_nan=False)
    realized_weights: dict[str, _Weight]
    turnover_ratio: float = Field(ge=0, allow_inf_nan=False)
    turnover_notional: float = Field(ge=0, allow_inf_nan=False)
    fee_paid: float = Field(ge=0, allow_inf_nan=False)
    slippage_paid: float = Field(ge=0, allow_inf_nan=False)
    total_cost: float = Field(ge=0, allow_inf_nan=False)
    valuation_count: Literal[1]


class PortfolioPaperObservationResponse(BaseModel):
    """Strict public projection of one modeled opening and close valuation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(
        min_length=32,
        max_length=32,
        pattern=r"^[0-9a-f]{32}$",
    )
    portfolio_experiment_id: str = Field(min_length=1, max_length=128)
    symbols: tuple[str, ...] = Field(min_length=2, max_length=6)
    method: PortfolioMethod
    quote_currency: Literal["USDT"]
    venue: Literal["Binance Spot"]
    scope: _ObservationScope
    initial_cash: float = Field(gt=0, allow_inf_nan=False)
    fee_rate: float = Field(ge=0, le=0.1, allow_inf_nan=False)
    slippage_rate: float = Field(ge=0, le=0.1, allow_inf_nan=False)
    information_session: str = Field(min_length=1)
    execution_session: str = Field(min_length=1)
    target_weights: dict[str, _Weight]
    created_at: datetime
    updated_at: datetime
    opening_at: datetime | None
    valuation_at: datetime | None
    attention_required: bool
    phase: _ObservationPhase
    valuation: PortfolioPaperValuationSummary | None

    @field_validator(
        "created_at",
        "updated_at",
        "opening_at",
        "valuation_at",
    )
    @classmethod
    def normalize_timestamps(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Observation timestamps must be timezone-aware.")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_public_shape(self) -> PortfolioPaperObservationResponse:
        if self.updated_at < self.created_at:
            raise ValueError("Observation update time cannot precede its creation time.")
        if self.opening_at is not None and not (
            self.created_at <= self.opening_at <= self.updated_at
        ):
            raise ValueError("Observation opening time must fall within its public lifetime.")
        if self.valuation_at is not None and (
            self.opening_at is None or not self.opening_at <= self.valuation_at <= self.updated_at
        ):
            raise ValueError("Observation valuation time must follow its opening time.")
        if set(self.target_weights) != set(self.symbols):
            raise ValueError("Observation target weights must exactly match its symbols.")
        if self.valuation is not None and set(self.valuation.realized_weights) != set(self.symbols):
            raise ValueError("Observation realized weights must exactly match its symbols.")
        if self.phase == "valued":
            if self.opening_at is None or self.valuation_at is None or self.valuation is None:
                raise ValueError("A valued observation requires opening and valuation summaries.")
        elif self.valuation_at is not None or self.valuation is not None:
            raise ValueError("Only a valued observation may expose valuation results.")
        if self.phase == "awaiting_close_valuation":
            if self.opening_at is None:
                raise ValueError("A close-valuation observation requires its opening time.")
        elif self.phase != "valued" and self.opening_at is not None:
            raise ValueError("An unopened observation cannot expose an opening time.")
        return self


@router.get(
    "/status",
    response_model=PortfolioPaperStatusResponse,
    summary="Read redacted one-session observation workflow health",
)
async def portfolio_paper_status(
    request: Request,
) -> PortfolioPaperStatusResponse:
    """Return only rollout scope and the two scheduler liveness flags."""

    opening_scheduler = cast(
        PortfolioPaperOpeningScheduler,
        request.app.state.portfolio_opening_scheduler,
    )
    settlement_scheduler = cast(
        PortfolioPaperSettlementScheduler,
        request.app.state.portfolio_settlement_scheduler,
    )
    opening_status = opening_scheduler.status
    settlement_status = settlement_scheduler.status
    return PortfolioPaperStatusResponse(
        availability="internal_only",
        scope="one_session_modeled_observation",
        activation_available=False,
        opening=PortfolioPaperSchedulerPublicStatus(
            enabled=opening_status.enabled,
            running=opening_status.running,
        ),
        settlement=PortfolioPaperSchedulerPublicStatus(
            enabled=settlement_status.enabled,
            running=settlement_status.running,
        ),
    )


@router.get(
    "/opening-status",
    response_model=PortfolioPaperOpeningStatusResponse,
    summary="Read the redacted portfolio-opening scheduler status",
)
async def portfolio_paper_opening_status(
    request: Request,
) -> PortfolioPaperOpeningStatusResponse:
    """Return a public health view without execution evidence or identifiers."""

    scheduler = cast(
        PortfolioPaperOpeningScheduler,
        request.app.state.portfolio_opening_scheduler,
    )
    status = scheduler.status
    return PortfolioPaperOpeningStatusResponse(
        availability="internal_only",
        enabled=status.enabled,
        running=status.running,
    )


@router.get(
    "/preflight/{experiment_id}",
    response_model=PortfolioPaperPreflightResponse,
    summary="Review saved portfolio readiness without creating an observation",
)
async def portfolio_paper_preflight(
    request: Request,
    experiment_id: Annotated[str, Path(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")],
) -> PortfolioPaperPreflightResponse:
    """Check narrow execution-paper scope using public rules only.

    This endpoint is intentionally a review endpoint: it has no write path, no
    scheduler control, no account access, and no activation capability.
    """

    experiment_store = cast(
        _PortfolioExperimentStore,
        request.app.state.experiment_store,
    )
    experiment = experiment_store.get_portfolio(experiment_id)
    if experiment is None:
        raise HTTPException(
            status_code=404,
            detail="Portfolio experiment not found.",
        )
    provider = cast(
        PortfolioPaperPreflightRulesProvider,
        request.app.state.providers.binance_provider,
    )
    return await review_portfolio_paper_readiness(
        experiment,
        provider=provider,
        now=_utc_now(),
    )


@router.get(
    "/observations",
    response_model=list[PortfolioPaperObservationResponse],
    summary="List one-session modeled portfolio observations",
)
def portfolio_paper_observations(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[PortfolioPaperObservationResponse]:
    """Project newest-first internal records without execution evidence."""

    store = cast(
        _PortfolioPaperObservationStore,
        request.app.state.portfolio_paper_track_store,
    )
    observed_at = _utc_now()
    return [
        _project_observation(record, observed_at=observed_at) for record in store.list(limit=limit)
    ]


@router.get(
    "/observations/{track_id}",
    response_model=PortfolioPaperObservationResponse,
    summary="Read one modeled portfolio observation",
)
def portfolio_paper_observation(
    request: Request,
    track_id: _TrackId,
) -> PortfolioPaperObservationResponse:
    """Read one strict public projection or return a non-revealing 404."""

    store = cast(
        _PortfolioPaperObservationStore,
        request.app.state.portfolio_paper_track_store,
    )
    record = store.get(track_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="Portfolio-paper observation not found.",
        )
    return _project_observation(record, observed_at=_utc_now())


def _project_observation(
    record: PortfolioPaperTrackRecord,
    *,
    observed_at: datetime,
) -> PortfolioPaperObservationResponse:
    decision = record.pending_decision
    if decision is None:  # pragma: no cover - validated store invariant
        raise ValueError("Portfolio-paper observation lost its activation decision.")
    phase = _observation_phase(record, observed_at=observed_at)
    valuation = _valuation_summary(record) if phase == "valued" else None
    return PortfolioPaperObservationResponse(
        id=record.id,
        portfolio_experiment_id=record.config.portfolio_experiment_id,
        symbols=record.config.symbols,
        method=record.config.method,
        quote_currency="USDT",
        venue="Binance Spot",
        scope="one_session_modeled_observation",
        initial_cash=record.config.initial_cash,
        fee_rate=record.config.fee_rate,
        slippage_rate=record.config.slippage_rate,
        information_session=decision.target.information_session,
        execution_session=decision.certificate.execution_session,
        target_weights={
            symbol: decision.target.weights[symbol] for symbol in record.config.symbols
        },
        created_at=record.created_at,
        updated_at=record.updated_at,
        opening_at=record.opening_committed_at,
        valuation_at=record.settlement_committed_at,
        attention_required=(phase == "opening_window_expired" or record.last_error is not None),
        phase=phase,
        valuation=valuation,
    )


def _observation_phase(
    record: PortfolioPaperTrackRecord,
    *,
    observed_at: datetime,
) -> _ObservationPhase:
    if record.settlement_id is not None:
        return "valued"
    if record.opening_batch_id is not None:
        return "awaiting_close_valuation"
    decision = record.pending_decision
    if decision is None:  # pragma: no cover - validated store invariant
        raise ValueError("Portfolio-paper observation lost its activation decision.")
    if observed_at >= decision.certificate.execution_deadline:
        return "opening_window_expired"
    return "awaiting_opening"


def _valuation_summary(
    record: PortfolioPaperTrackRecord,
) -> PortfolioPaperValuationSummary:
    state = record.state
    if state.valuation_count != 1:  # pragma: no cover - validated store invariant
        raise ValueError("A completed one-session observation requires one valuation.")
    return PortfolioPaperValuationSummary(
        cash=state.cash,
        equity=state.equity,
        total_return=state.total_return,
        peak_equity=state.peak_equity,
        max_drawdown=state.max_drawdown,
        realized_weights={
            symbol: state.realized_weights[symbol] for symbol in record.config.symbols
        },
        turnover_ratio=state.turnover_ratio,
        turnover_notional=state.turnover_notional,
        fee_paid=state.fee_paid,
        slippage_paid=state.slippage_paid,
        total_cost=state.total_cost,
        valuation_count=1,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)
