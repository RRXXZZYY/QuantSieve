from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from time import monotonic
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from quantsieve_engine import (
    DatasetSnapshot,
    FactorDiagnostics,
    analyze_factor,
    build_dataset_snapshot,
    build_factor_research_panel,
    build_feature_observation,
    build_label_observation,
    build_run_manifest,
    build_universe_observation,
    canonical_payload_hash,
)
from quantsieve_providers import DataEnvelope
from quantsieve_providers.base import DataProvider

from .config import Settings
from .research_runs import (
    ResearchDatasetEvidence,
    ResearchRunReceipt,
    ResearchRunStore,
)

FactorId = Literal[
    "momentum",
    "reversal",
    "low_volatility",
    "volume_surprise",
]
FactorProviderId = Literal[
    "akshare",
    "yfinance",
    "binance",
    "futures",
    "macro",
]

_FACTOR_RECIPES: dict[FactorId, dict[str, str]] = {
    "momentum": {
        "formula": "close[d-1] / close[d-lookback-1] - 1",
        "direction": "higher_is_stronger",
    },
    "reversal": {
        "formula": "-(close[d-1] / close[d-lookback-1] - 1)",
        "direction": "higher_is_stronger_reversal",
    },
    "low_volatility": {
        "formula": "-sample_std(close_to_close_returns[d-lookback:d-1])",
        "direction": "higher_is_lower_realized_volatility",
    },
    "volume_surprise": {
        "formula": "volume[d-1] / mean(volume[d-lookback-1:d-2]) - 1",
        "direction": "higher_is_larger_volume_surprise",
    },
}
_EXECUTION_MODEL = (
    "factor_available_d_minus_1__one_shared_bar_delay__label_open_d_plus_1_to_close_d_plus_h"
)
_MAXIMUM_CANDIDATE_OBSERVATION_PAIRS = 120_000


def _check_research_deadline(deadline_monotonic: float | None) -> None:
    if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
        raise TimeoutError("Factor research exceeded its server deadline.")


def factor_catalog() -> dict[str, object]:
    """Return the server-owned discovery contract for factor research."""

    return {
        "schema_version": 1,
        "recipes": [
            {
                "factor_id": factor_id,
                **_FACTOR_RECIPES[factor_id],
            }
            for factor_id in (
                "momentum",
                "reversal",
                "low_volatility",
                "volume_surprise",
            )
        ],
        "defaults": {
            "factor_id": "momentum",
            "lookback": 20,
            "horizons": [1, 5, 20],
            "quantiles": 5,
            "interval": "1d",
            "finalized_bars_only": True,
        },
        "limits": {
            "instruments": {"minimum": 3, "maximum": 20},
            "lookback": {"minimum": 5, "maximum": 252},
            "horizons": {
                "minimum_items": 1,
                "maximum_items": 4,
                "minimum_value": 1,
                "maximum_value": 63,
                "unique": True,
            },
            "quantiles": {
                "minimum": 2,
                "maximum": 10,
                "requires_instruments_at_least_quantiles": True,
            },
            "intervals": ["1d"],
            "maximum_date_span_years": 10,
            "maximum_candidate_observation_pairs": (_MAXIMUM_CANDIDATE_OBSERVATION_PAIRS),
            "mixed_providers": True,
            "providers": [
                "akshare",
                "yfinance",
                "binance",
                "futures",
                "macro",
            ],
            "universe": "explicit_fixed_user_selection",
        },
        "validity": {
            "finalized_bars_only_required": True,
            "universe_semantics": "fixed_user_selected_ex_post",
            "point_in_time_universe": False,
            "source_availability": "provider_policy_estimate",
            "point_in_time_validation_passed": False,
            "survivorship_bias_controlled": False,
            "fundamentals_enabled": False,
            "research_only": True,
            "tradable_conclusion": False,
        },
    }


class FactorResearchInputError(ValueError):
    """The request or available evidence cannot support the requested study."""


class FactorResearchUpstreamError(RuntimeError):
    """A provider failed while the factor service fetched source evidence."""


class FactorInstrumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(min_length=1, max_length=80)
    provider: FactorProviderId

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper()
        return value


class FactorResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instruments: tuple[FactorInstrumentRequest, ...] = Field(
        min_length=3,
        max_length=20,
    )
    factor_id: FactorId
    lookback: int = Field(ge=5, le=252, strict=True)
    horizons: tuple[int, ...] = Field(min_length=1, max_length=4)
    quantiles: int = Field(ge=2, le=10, strict=True)
    interval: Literal["1d"] = "1d"
    start: date
    end: date
    finalized_bars_only: Literal[True] = True

    @field_validator("horizons", mode="before")
    @classmethod
    def validate_horizons(cls, value: object) -> tuple[int, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("horizons must be an array of integers.")
        if any(
            isinstance(horizon, bool) or not isinstance(horizon, int) or not 1 <= horizon <= 63
            for horizon in value
        ):
            raise ValueError("Every horizon must be an integer between 1 and 63.")
        if len(set(value)) != len(value):
            raise ValueError("horizons must be unique.")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.start >= self.end:
            raise ValueError("start must be earlier than end.")
        if self.start.year <= date.max.year - 10:
            try:
                ten_year_limit = self.start.replace(year=self.start.year + 10)
            except ValueError:
                ten_year_limit = self.start.replace(
                    year=self.start.year + 10,
                    day=28,
                )
            if self.end > ten_year_limit:
                raise ValueError("The requested date span cannot exceed 10 years.")
        keys = [(instrument.provider, instrument.symbol) for instrument in self.instruments]
        if len(set(keys)) != len(keys):
            raise ValueError("instruments must be unique by provider and symbol.")
        if len(self.instruments) < self.quantiles:
            raise ValueError("The number of instruments must be at least quantiles.")
        return self


class _ProviderResolver(Protocol):
    def resolve(
        self,
        symbol: str,
        requested: FactorProviderId,
    ) -> DataProvider: ...


@dataclass(frozen=True, slots=True)
class _Bar:
    day: date
    timestamp: datetime
    available_at: datetime
    availability_source: str
    exact_numeric_source: bool
    raw: dict[str, object]
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class _AssetHistory:
    requested: FactorInstrumentRequest
    provider_name: str
    canonical_symbol: str
    bars: dict[date, _Bar]
    metadata: dict[str, object]
    citations: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class _AssetPeriodValue:
    asset: _AssetHistory
    factor_value: Decimal
    label_value: Decimal
    feature_effective_at: datetime
    feature_available_at: datetime
    entry_effective_at: datetime
    entry_available_at: datetime
    exit_effective_at: datetime
    exit_available_at: datetime

    @property
    def label_available_at(self) -> datetime:
        return max(self.entry_available_at, self.exit_available_at)


@dataclass(frozen=True, slots=True)
class _PeriodSample:
    nominal_decision_at: datetime
    feature_cutoff_at: datetime
    period_at: datetime
    entry_at: datetime
    label_available_at: datetime
    values: tuple[_AssetPeriodValue, ...]


def _utc_timestamp(value: object, *, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min, tzinfo=UTC)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise FactorResearchInputError(f"{label} cannot be empty.")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed_date = date.fromisoformat(text)
            except ValueError as error:
                raise FactorResearchInputError(f"{label} must use ISO-8601.") from error
            parsed = datetime.combine(parsed_date, time.min, tzinfo=UTC)
    else:
        raise FactorResearchInputError(f"{label} must be a date or datetime.")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _day_timestamp(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)


def _shift_date(value: date, *, days: int, label: str) -> date:
    try:
        return value + timedelta(days=days)
    except OverflowError as error:
        raise FactorResearchInputError(f"{label} exceeds the supported calendar range.") from error


def _decimal(
    value: object,
    *,
    label: str,
    allow_zero: bool,
) -> Decimal:
    if isinstance(value, bool):
        raise FactorResearchInputError(f"{label} must be numeric.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise FactorResearchInputError(f"{label} must be numeric.") from error
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        qualifier = "non-negative" if allow_zero else "positive"
        raise FactorResearchInputError(f"{label} must be finite and {qualifier}.")
    return result


def _row_decimal(
    raw_row: Mapping[str, object],
    field_name: str,
    *,
    label: str,
    allow_zero: bool,
) -> tuple[Decimal, bool]:
    exact_key = f"exact_{field_name}"
    exact_value = raw_row.get(exact_key)
    if exact_value is not None:
        return (
            _decimal(
                exact_value,
                label=f"{label} exact value",
                allow_zero=allow_zero,
            ),
            True,
        )
    return (
        _decimal(
            raw_row[field_name],
            label=label,
            allow_zero=allow_zero,
        ),
        False,
    )


def _row_available_at(
    raw_row: Mapping[str, object],
    *,
    day: date,
    requested: FactorInstrumentRequest,
) -> tuple[datetime, str]:
    for field_name in ("available_at", "finalized_at"):
        value = raw_row.get(field_name)
        if value is None:
            continue
        return (
            _utc_timestamp(
                value,
                label=(f"{requested.provider}:{requested.symbol} row {field_name}"),
            ),
            f"row_{field_name}",
        )
    # Public equity, futures, and macro providers currently expose a
    # finalization policy rather than a row-level publication timestamp.
    # Model those rows conservatively as available from the next UTC day.
    # The result continues to disclose that this is a policy estimate.
    return (
        datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC),
        "provider_policy_next_utc_day_estimate",
    )


def _parse_bars(
    envelope: DataEnvelope,
    *,
    requested: FactorInstrumentRequest,
    fetch_start: date,
    fetch_end: date,
) -> dict[date, _Bar]:
    bars: dict[date, _Bar] = {}
    for position, raw_row in enumerate(envelope.rows):
        if not isinstance(raw_row, Mapping):
            raise FactorResearchInputError(
                f"{requested.provider}:{requested.symbol} row {position} is not an object."
            )
        missing = {"date", "open", "high", "low", "close", "volume"} - set(raw_row)
        if missing:
            raise FactorResearchInputError(
                f"{requested.provider}:{requested.symbol} row {position} "
                "is missing: " + ", ".join(sorted(missing))
            )
        timestamp = _utc_timestamp(
            raw_row["date"],
            label=f"{requested.provider}:{requested.symbol} row date",
        )
        day = timestamp.date()
        if day < fetch_start or day > fetch_end:
            continue
        if day in bars:
            raise FactorResearchInputError(
                f"{requested.provider}:{requested.symbol} repeats daily key {day.isoformat()}."
            )
        available_at, availability_source = _row_available_at(
            raw_row,
            day=day,
            requested=requested,
        )
        if available_at < timestamp:
            raise FactorResearchInputError(
                f"{requested.provider}:{requested.symbol} availability precedes its row date."
            )
        open_price, open_exact = _row_decimal(
            raw_row,
            "open",
            label=f"{requested.provider}:{requested.symbol} open",
            allow_zero=False,
        )
        high_price, high_exact = _row_decimal(
            raw_row,
            "high",
            label=f"{requested.provider}:{requested.symbol} high",
            allow_zero=False,
        )
        low_price, low_exact = _row_decimal(
            raw_row,
            "low",
            label=f"{requested.provider}:{requested.symbol} low",
            allow_zero=False,
        )
        close_price, close_exact = _row_decimal(
            raw_row,
            "close",
            label=f"{requested.provider}:{requested.symbol} close",
            allow_zero=False,
        )
        volume, volume_exact = _row_decimal(
            raw_row,
            "volume",
            label=f"{requested.provider}:{requested.symbol} volume",
            allow_zero=True,
        )
        if high_price < max(open_price, low_price, close_price):
            raise FactorResearchInputError(
                f"{requested.provider}:{requested.symbol} high is inconsistent."
            )
        if low_price > min(open_price, high_price, close_price):
            raise FactorResearchInputError(
                f"{requested.provider}:{requested.symbol} low is inconsistent."
            )
        bars[day] = _Bar(
            day=day,
            timestamp=timestamp,
            available_at=available_at,
            availability_source=availability_source,
            exact_numeric_source=all(
                (open_exact, high_exact, low_exact, close_exact, volume_exact)
            ),
            raw=dict(raw_row),
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=volume,
        )
    return dict(sorted(bars.items()))


def _instrument_uid(provider: str, symbol: str) -> str:
    digest = hashlib.sha256(
        f"{provider.strip().lower()}:{symbol.strip().upper()}".encode()
    ).hexdigest()[:24]
    return f"factor:{provider.strip().lower()}:{digest}"


def _factor_value(
    *,
    factor_id: FactorId,
    asset: _AssetHistory,
    common_days: Sequence[date],
    decision_index: int,
    lookback: int,
) -> Decimal:
    latest_index = decision_index - 1
    first_index = decision_index - lookback - 1
    if first_index < 0:
        raise FactorResearchInputError("Insufficient factor lookback.")
    latest = asset.bars[common_days[latest_index]]
    first = asset.bars[common_days[first_index]]
    momentum = latest.close / first.close - Decimal("1")
    if factor_id == "momentum":
        return momentum
    if factor_id == "reversal":
        return -momentum
    if factor_id == "low_volatility":
        returns = [
            (
                asset.bars[common_days[position]].close
                / asset.bars[common_days[position - 1]].close
                - Decimal("1")
            )
            for position in range(first_index + 1, decision_index)
        ]
        mean = sum(returns, start=Decimal("0")) / Decimal(len(returns))
        variance = sum(
            ((value - mean) ** 2 for value in returns),
            start=Decimal("0"),
        ) / Decimal(len(returns) - 1)
        with localcontext() as context:
            context.prec = 50
            return -variance.sqrt()

    baseline_volumes = [
        asset.bars[common_days[position]].volume for position in range(first_index, latest_index)
    ]
    baseline = sum(
        baseline_volumes,
        start=Decimal("0"),
    ) / Decimal(len(baseline_volumes))
    if baseline == 0:
        raise FactorResearchInputError("Volume surprise has a zero historical volume baseline.")
    return latest.volume / baseline - Decimal("1")


def _feature_dependency_bars(
    *,
    asset: _AssetHistory,
    common_days: Sequence[date],
    decision_index: int,
    lookback: int,
) -> tuple[_Bar, ...]:
    """Return every source row in the declared feature lookback window."""

    first_index = decision_index - lookback - 1
    latest_index = decision_index - 1
    if first_index < 0:
        raise FactorResearchInputError("Insufficient factor lookback.")
    return tuple(
        asset.bars[common_days[position]] for position in range(first_index, latest_index + 1)
    )


def _label_value(
    *,
    asset: _AssetHistory,
    common_days: Sequence[date],
    decision_index: int,
    horizon: int,
) -> Decimal:
    # Daily source evidence can become available after the next UTC day has
    # already opened (Binance intentionally waits two minutes after close).
    # A full shared-bar delay keeps the diagnostic label strictly after the
    # modeled feature-availability time without inventing an intraday price.
    entry = asset.bars[common_days[decision_index + 1]].open
    exit_price = asset.bars[common_days[decision_index + horizon]].close
    return exit_price / entry - Decimal("1")


def _constant(values: Sequence[Decimal]) -> bool:
    return len(set(values)) < 2


def _citation_payload(envelope: DataEnvelope) -> tuple[dict[str, object], ...]:
    return tuple(dict(citation.model_dump(mode="json")) for citation in envelope.citations)


def _dataset_evidence_id(
    snapshot: DatasetSnapshot,
    *,
    role: str,
    ordinal: int,
) -> str:
    """Bind source rows and acquisition evidence without changing legacy snapshot ids."""

    return canonical_payload_hash(
        {
            "contract": "quantsieve.factor.dataset-evidence.v1",
            "role": role,
            "ordinal": ordinal,
            "snapshot_id": snapshot.snapshot_id,
            "requested_start": snapshot.requested_start,
            "requested_end": snapshot.requested_end,
            "source_rows_hash": snapshot.source_rows_hash,
            "metadata_hash": snapshot.metadata_hash,
            "citations_hash": snapshot.citations_hash,
        }
    )


def _capability_payload(metadata: Mapping[str, object]) -> dict[str, object]:
    allowed_keys = (
        "finalized_bars_only",
        "bar_finalization_policy",
        "bar_finalization_verified",
        "exchange_clock_verified",
        "reference_series",
        "tradable_quote",
        "execution_ready",
        "ohlc_derived_from_close",
        "fallback",
        "price_basis",
        "repair_applied",
        "repaired_rows",
        "execution_note",
    )
    return {key: metadata[key] for key in allowed_keys if key in metadata}


def _metadata_payload(
    source_metadata: Mapping[str, object],
    *,
    requested: FactorInstrumentRequest,
    fetch_start: date,
    fetch_end: date,
    common_day_count: int,
) -> dict[str, object]:
    metadata = dict(source_metadata)
    metadata.update(
        {
            "interval": "1d",
            "requested_provider": requested.provider,
            "requested_symbol": requested.symbol,
            "fetch_start": fetch_start.isoformat(),
            "fetch_end": fetch_end.isoformat(),
            "alignment": "exact_shared_utc_day_intersection",
            "forward_fill": False,
            "common_day_count": common_day_count,
            "source_availability": "provider_policy_estimate",
            "universe_semantics": "fixed_user_selected_ex_post",
            "point_in_time_universe": False,
        }
    )
    return metadata


def _result_payload(
    *,
    run_as_of_at: datetime,
    recipe: Mapping[str, object],
    coverage: Mapping[str, object],
    diagnostics_by_horizon: Mapping[str, object],
    time_series: Mapping[str, object],
    latest_scores: Mapping[str, object],
    limitations: Mapping[str, object],
) -> dict[str, object]:
    """Keep statistics in the run hash while excluding rows/citations/receipts."""

    return {
        "run_as_of_at": run_as_of_at.isoformat(),
        "recipe": dict(recipe),
        "coverage": dict(coverage),
        "diagnostics_by_horizon": dict(diagnostics_by_horizon),
        "time_series": dict(time_series),
        "latest_scores": dict(latest_scores),
        "limitations": dict(limitations),
    }


class FactorResearchService:
    """Provider-backed, research-only cross-sectional factor analysis."""

    def __init__(
        self,
        *,
        providers: _ProviderResolver,
        provider_capacity: asyncio.Semaphore,
        provider_background_tasks: set[asyncio.Task[DataEnvelope]],
        run_store: ResearchRunStore,
        settings: Settings,
    ) -> None:
        self.providers = providers
        self.provider_capacity = provider_capacity
        self.provider_background_tasks = provider_background_tasks
        self.run_store = run_store
        self.settings = settings

    def _observe_detached_provider_task(
        self,
        task: asyncio.Task[DataEnvelope],
    ) -> None:
        # Retrieve a detached task's exception so a timed-out request cannot
        # create an unobserved-task warning.  The provider task itself owns the
        # capacity lease, so a task cannot become done before returning it.
        self.provider_background_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _run_provider_history(
        self,
        provider: DataProvider,
        instrument: FactorInstrumentRequest,
        *,
        fetch_start: date,
        fetch_end: date,
    ) -> DataEnvelope:
        try:
            return await provider.history_interval(
                instrument.symbol,
                fetch_start,
                fetch_end,
                interval="1d",
            )
        finally:
            # Keep the lease coupled to the real provider coroutine.  In
            # particular, a request timeout must not free a slot while a
            # shielded asyncio.to_thread provider is still running.
            self.provider_capacity.release()

    async def _provider_history(
        self,
        provider: DataProvider,
        instrument: FactorInstrumentRequest,
        *,
        fetch_start: date,
        fetch_end: date,
    ) -> DataEnvelope:
        await self.provider_capacity.acquire()
        try:
            provider_task = asyncio.create_task(
                self._run_provider_history(
                    provider,
                    instrument,
                    fetch_start=fetch_start,
                    fetch_end=fetch_end,
                )
            )
        except BaseException:
            self.provider_capacity.release()
            raise
        try:
            return await asyncio.shield(provider_task)
        except asyncio.CancelledError:
            # asyncio.to_thread cannot stop a callable that is already
            # running.  Keep the app-owned provider slot leased to that real
            # work while allowing the HTTP deadline to return.
            self.provider_background_tasks.add(provider_task)
            provider_task.add_done_callback(self._observe_detached_provider_task)
            raise

    async def _fetch_asset(
        self,
        instrument: FactorInstrumentRequest,
        *,
        fetch_start: date,
        fetch_end: date,
        deadline_monotonic: float | None = None,
    ) -> _AssetHistory:
        _check_research_deadline(deadline_monotonic)
        try:
            provider = self.providers.resolve(
                instrument.symbol,
                instrument.provider,
            )
            envelope = await self._provider_history(
                provider,
                instrument,
                fetch_start=fetch_start,
                fetch_end=fetch_end,
            )
        except RuntimeError as error:
            raise FactorResearchUpstreamError(
                f"{instrument.provider}:{instrument.symbol}: {error}"
            ) from error
        except (KeyError, LookupError, ValueError) as error:
            raise FactorResearchInputError(
                f"{instrument.provider}:{instrument.symbol}: {error}"
            ) from error
        _check_research_deadline(deadline_monotonic)
        if envelope.metadata.get("finalized_bars_only") is not True:
            raise FactorResearchInputError(
                f"{instrument.provider}:{instrument.symbol} does not certify "
                "finalized_bars_only=true; degraded reference data cannot "
                "produce this research result."
            )
        if provider.name.strip().lower() != instrument.provider:
            raise FactorResearchInputError(
                f"{instrument.provider}:{instrument.symbol} resolved to an unexpected provider."
            )
        if not envelope.symbol.strip():
            raise FactorResearchInputError(
                f"{instrument.provider}:{instrument.symbol} returned an empty canonical symbol."
            )
        if not envelope.citations:
            raise FactorResearchInputError(
                f"{instrument.provider}:{instrument.symbol} returned no source citation."
            )
        bars = _parse_bars(
            envelope,
            requested=instrument,
            fetch_start=fetch_start,
            fetch_end=fetch_end,
        )
        _check_research_deadline(deadline_monotonic)
        if not bars:
            raise FactorResearchInputError(
                f"{instrument.provider}:{instrument.symbol} returned no daily bars."
            )
        return _AssetHistory(
            requested=instrument,
            provider_name=provider.name.strip().lower(),
            canonical_symbol=envelope.symbol.strip().upper(),
            bars=bars,
            metadata=dict(envelope.metadata),
            citations=_citation_payload(envelope),
        )

    async def _fetch_assets(
        self,
        instruments: Sequence[FactorInstrumentRequest],
        *,
        fetch_start: date,
        fetch_end: date,
        deadline_monotonic: float | None = None,
    ) -> tuple[_AssetHistory, ...]:
        tasks = tuple(
            asyncio.create_task(
                self._fetch_asset(
                    instrument,
                    fetch_start=fetch_start,
                    fetch_end=fetch_end,
                    deadline_monotonic=deadline_monotonic,
                )
            )
            for instrument in instruments
        )
        try:
            return tuple(await asyncio.gather(*tasks))
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        except BaseException:
            # A provider failure is not an HTTP-deadline cancellation.  Join
            # the remaining providers so their evidence work and capacity
            # leases have settled before surfacing the upstream error.
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def research(
        self,
        request: FactorResearchRequest,
        *,
        deadline_monotonic: float | None = None,
    ) -> dict[str, object]:
        _check_research_deadline(deadline_monotonic)
        research_started_at = datetime.now(UTC)
        today = research_started_at.date()
        if request.end > today:
            raise FactorResearchInputError(
                "end cannot be in the future for finalized daily-bar research."
            )
        instruments = tuple(
            sorted(
                request.instruments,
                key=lambda item: (item.provider, item.symbol),
            )
        )
        calendar_day_upper_bound = (request.end - request.start).days + 1
        prefetch_observation_upper_bound = (
            calendar_day_upper_bound * len(instruments) * len(request.horizons)
        )
        if prefetch_observation_upper_bound > _MAXIMUM_CANDIDATE_OBSERVATION_PAIRS:
            raise FactorResearchInputError(
                "The requested calendar span can exceed the 120000 candidate "
                "observation-pair resource limit; reduce instruments, horizons, "
                "or date span before source data is fetched."
            )
        maximum_horizon = max(request.horizons)
        fetch_start = _shift_date(
            request.start,
            days=-(request.lookback * 2 + 14),
            label="Factor warmup start",
        )
        uncapped_fetch_end = _shift_date(
            request.end,
            days=maximum_horizon * 2 + 14,
            label="Factor label fetch end",
        )
        # A UTC calendar day that is still in progress cannot be treated as a
        # finalized daily bar, even if an upstream metadata flag is wrong.
        fetch_end = min(uncapped_fetch_end, today - timedelta(days=1))
        assets = await self._fetch_assets(
            instruments,
            fetch_start=fetch_start,
            fetch_end=fetch_end,
            deadline_monotonic=deadline_monotonic,
        )
        _check_research_deadline(deadline_monotonic)
        canonical_keys = [(asset.provider_name, asset.canonical_symbol) for asset in assets]
        if len(set(canonical_keys)) != len(canonical_keys):
            raise FactorResearchInputError(
                "Requested aliases resolved to duplicate canonical instruments."
            )
        common_days = sorted(set.intersection(*(set(asset.bars) for asset in assets)))
        if not common_days:
            raise FactorResearchInputError(
                "The selected instruments have no exact shared daily labels."
            )
        candidate_indices = [
            index for index, day in enumerate(common_days) if request.start <= day <= request.end
        ]
        if not candidate_indices:
            raise FactorResearchInputError(
                "No exact shared decision dates fall inside start and end."
            )
        candidate_observation_pairs = len(candidate_indices) * len(assets) * len(request.horizons)
        if candidate_observation_pairs > _MAXIMUM_CANDIDATE_OBSERVATION_PAIRS:
            raise FactorResearchInputError(
                "The requested factor panel exceeds the 120000 candidate "
                "observation-pair resource limit; reduce instruments, horizons, "
                "or date span."
            )

        diagnostics_by_horizon: dict[str, object] = {}
        time_series: dict[str, object] = {}
        latest_scores: dict[str, object] = {}
        period_counts: dict[str, int] = {}
        dropped_reason_counts: dict[str, dict[str, int]] = {}
        factor_cache: dict[tuple[int, str, str], Decimal] = {}
        for horizon in request.horizons:
            _check_research_deadline(deadline_monotonic)
            dropped_dates: list[dict[str, str]] = []
            samples: list[_PeriodSample] = []
            for decision_index in candidate_indices:
                _check_research_deadline(deadline_monotonic)
                day = common_days[decision_index]
                if decision_index < request.lookback + 1:
                    dropped_dates.append(
                        {
                            "period_at": _day_timestamp(day).isoformat(),
                            "reason": "insufficient_lookback",
                        }
                    )
                    continue
                entry_index = decision_index + 1
                exit_index = decision_index + horizon
                if exit_index >= len(common_days):
                    dropped_dates.append(
                        {
                            "period_at": _day_timestamp(day).isoformat(),
                            "reason": "label_not_available",
                        }
                    )
                    continue
                try:
                    period_values: list[_AssetPeriodValue] = []
                    for asset in assets:
                        _check_research_deadline(deadline_monotonic)
                        feature_bars = _feature_dependency_bars(
                            asset=asset,
                            common_days=common_days,
                            decision_index=decision_index,
                            lookback=request.lookback,
                        )
                        feature_effective_at = max(bar.timestamp for bar in feature_bars)
                        feature_available_at = max(bar.available_at for bar in feature_bars)
                        entry_bar = asset.bars[common_days[entry_index]]
                        exit_bar = asset.bars[common_days[exit_index]]
                        factor_key = (
                            decision_index,
                            asset.provider_name,
                            asset.canonical_symbol,
                        )
                        factor_value = factor_cache.get(factor_key)
                        if factor_value is None:
                            factor_value = _factor_value(
                                factor_id=request.factor_id,
                                asset=asset,
                                common_days=common_days,
                                decision_index=decision_index,
                                lookback=request.lookback,
                            )
                            factor_cache[factor_key] = factor_value
                        period_values.append(
                            _AssetPeriodValue(
                                asset=asset,
                                factor_value=factor_value,
                                label_value=_label_value(
                                    asset=asset,
                                    common_days=common_days,
                                    decision_index=decision_index,
                                    horizon=horizon,
                                ),
                                feature_effective_at=feature_effective_at,
                                feature_available_at=feature_available_at,
                                entry_effective_at=entry_bar.timestamp,
                                entry_available_at=entry_bar.available_at,
                                exit_effective_at=exit_bar.timestamp,
                                exit_available_at=exit_bar.available_at,
                            )
                        )
                    values = tuple(period_values)
                except (
                    FactorResearchInputError,
                    ArithmeticError,
                    ValueError,
                ) as error:
                    dropped_dates.append(
                        {
                            "period_at": _day_timestamp(day).isoformat(),
                            "reason": str(error),
                        }
                    )
                    continue
                period_at = max(item.feature_available_at for item in values)
                feature_cutoff_at = max(item.feature_effective_at for item in values)
                entry_at = max(item.entry_effective_at for item in values)
                if period_at > research_started_at:
                    dropped_dates.append(
                        {
                            "period_at": _day_timestamp(day).isoformat(),
                            "reason": "feature_not_available_as_of_research_start",
                        }
                    )
                    continue
                if period_at >= entry_at:
                    dropped_dates.append(
                        {
                            "period_at": period_at.isoformat(),
                            "reason": "feature_not_available_before_delayed_entry",
                        }
                    )
                    continue
                label_available_at = max(item.label_available_at for item in values)
                if label_available_at > research_started_at:
                    dropped_dates.append(
                        {
                            "period_at": _day_timestamp(day).isoformat(),
                            "reason": "label_not_available_as_of_research_start",
                        }
                    )
                    continue
                if _constant([item.factor_value for item in values]):
                    dropped_dates.append(
                        {
                            "period_at": _day_timestamp(day).isoformat(),
                            "reason": "constant_factor_cross_section",
                        }
                    )
                    continue
                if _constant([item.label_value for item in values]):
                    dropped_dates.append(
                        {
                            "period_at": _day_timestamp(day).isoformat(),
                            "reason": "constant_label_cross_section",
                        }
                    )
                    continue
                samples.append(
                    _PeriodSample(
                        nominal_decision_at=_day_timestamp(day),
                        feature_cutoff_at=feature_cutoff_at,
                        period_at=period_at,
                        entry_at=entry_at,
                        label_available_at=label_available_at,
                        values=values,
                    )
                )
            if len(samples) < 2:
                raise FactorResearchInputError(
                    f"Horizon {horizon} has only {len(samples)} usable periods; "
                    "at least two are required after lookback, availability, "
                    "and constant-cross-section filters."
                )

            universe_observations = []
            feature_observations = []
            label_observations = []
            series_rows: list[dict[str, object]] = []
            for sample in samples:
                _check_research_deadline(deadline_monotonic)
                observations: list[dict[str, str]] = []
                for value in sample.values:
                    asset = value.asset
                    instrument_uid = _instrument_uid(
                        asset.provider_name,
                        asset.canonical_symbol,
                    )
                    try:
                        universe_observation = build_universe_observation(
                            instrument_uid=instrument_uid,
                            period_at=sample.period_at,
                            effective_at=sample.period_at,
                            observed_at=sample.period_at,
                            available_at=sample.period_at,
                            membership_basis="fixed_user_selected_ex_post",
                            included=True,
                        )
                        feature_observation = build_feature_observation(
                            instrument_uid=instrument_uid,
                            period_at=sample.period_at,
                            effective_at=value.feature_effective_at,
                            observed_at=value.feature_available_at,
                            available_at=value.feature_available_at,
                            feature_name=request.factor_id,
                            value=value.factor_value,
                        )
                        label_observation = build_label_observation(
                            instrument_uid=instrument_uid,
                            period_at=sample.period_at,
                            effective_at=value.exit_effective_at,
                            observed_at=value.label_available_at,
                            available_at=value.label_available_at,
                            label_name=f"forward_return_{horizon}d",
                            value=value.label_value,
                        )
                    except (ArithmeticError, ValueError) as error:
                        raise FactorResearchInputError(
                            f"Horizon {horizon} factor evidence failed closed: {error}"
                        ) from error
                    universe_observations.append(universe_observation)
                    feature_observations.append(feature_observation)
                    label_observations.append(label_observation)
                    observations.append(
                        {
                            "symbol": asset.canonical_symbol,
                            "provider": asset.provider_name,
                            "factor_value": str(value.factor_value),
                            "forward_return": str(value.label_value),
                            "feature_effective_at": (value.feature_effective_at.isoformat()),
                            "feature_available_at": (value.feature_available_at.isoformat()),
                            "entry_effective_at": (value.entry_effective_at.isoformat()),
                            "entry_available_at": (value.entry_available_at.isoformat()),
                            "exit_effective_at": (value.exit_effective_at.isoformat()),
                            "exit_available_at": (value.exit_available_at.isoformat()),
                            "label_available_at": (value.label_available_at.isoformat()),
                        }
                    )
                _check_research_deadline(deadline_monotonic)
                series_rows.append(
                    {
                        "nominal_decision_at": (sample.nominal_decision_at.isoformat()),
                        "feature_cutoff_at": (sample.feature_cutoff_at.isoformat()),
                        "period_at": sample.period_at.isoformat(),
                        "entry_at": sample.entry_at.isoformat(),
                        "label_available_at": sample.label_available_at.isoformat(),
                        "observations": observations,
                    }
                )
            try:
                _check_research_deadline(deadline_monotonic)
                panel = build_factor_research_panel(
                    feature_name=request.factor_id,
                    label_name=f"forward_return_{horizon}d",
                    universe=universe_observations,
                    features=feature_observations,
                    labels=label_observations,
                )
                diagnostics: FactorDiagnostics = analyze_factor(
                    panel,
                    quantile_count=request.quantiles,
                )
                _check_research_deadline(deadline_monotonic)
            except (ArithmeticError, ValueError) as error:
                raise FactorResearchInputError(
                    f"Horizon {horizon} diagnostics failed closed: {error}"
                ) from error
            horizon_key = str(horizon)
            diagnostics_by_horizon[horizon_key] = {
                "panel_id": panel.panel_id,
                "diagnostics": diagnostics.model_dump(mode="json"),
                "dropped_dates": dropped_dates,
            }
            time_series[horizon_key] = series_rows
            last_sample = samples[-1]
            latest_score_rows: list[dict[str, str]] = [
                {
                    "symbol": value.asset.canonical_symbol,
                    "provider": value.asset.provider_name,
                    "factor_value": str(value.factor_value),
                }
                for value in last_sample.values
            ]
            latest_score_rows.sort(
                key=lambda item: (
                    -Decimal(item["factor_value"]),
                    item["provider"],
                    item["symbol"],
                )
            )
            latest_scores[horizon_key] = {
                "period_at": last_sample.period_at.isoformat(),
                "entry_at": last_sample.entry_at.isoformat(),
                "scores": latest_score_rows,
            }
            period_counts[horizon_key] = len(samples)
            reason_counts: dict[str, int] = {}
            for dropped in dropped_dates:
                reason = dropped["reason"]
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            dropped_reason_counts[horizon_key] = dict(sorted(reason_counts.items()))

        now = datetime.now(UTC)
        snapshot_records: list[tuple[_AssetHistory, DatasetSnapshot]] = []
        dataset_evidence: list[ResearchDatasetEvidence] = []
        dataset_evidence_bindings: list[dict[str, object]] = []
        per_asset_rows: list[dict[str, object]] = []
        for ordinal, asset in enumerate(assets):
            _check_research_deadline(deadline_monotonic)
            # Preserve the complete fetched input, not only the aligned
            # intersection.  Reproduction must be able to independently
            # derive which source dates were excluded by cross-market
            # alignment.
            source_rows = tuple(asset.bars[day].raw for day in sorted(asset.bars))
            metadata = _metadata_payload(
                asset.metadata,
                requested=asset.requested,
                fetch_start=fetch_start,
                fetch_end=fetch_end,
                common_day_count=len(common_days),
            )
            try:
                snapshot = build_dataset_snapshot(
                    provider=asset.provider_name,
                    symbol=asset.canonical_symbol,
                    interval="1d",
                    rows=source_rows,
                    metadata=metadata,
                    citations=asset.citations,
                    requested_start=fetch_start,
                    requested_end=fetch_end,
                    finalized_only=True,
                    quality_status="passed",
                    observed_at=now,
                )
            except (ArithmeticError, ValueError) as error:
                raise FactorResearchInputError(
                    f"{asset.provider_name}:{asset.canonical_symbol} dataset "
                    f"snapshot failed closed: {error}"
                ) from error
            evidence = ResearchDatasetEvidence(
                snapshot=snapshot,
                rows=source_rows,
                metadata=metadata,
                citations=asset.citations,
                role="universe",
                ordinal=ordinal,
            )
            snapshot_records.append((asset, snapshot))
            dataset_evidence.append(evidence)
            evidence_id = _dataset_evidence_id(
                snapshot,
                role=evidence.role,
                ordinal=evidence.ordinal,
            )
            dataset_evidence_bindings.append(
                {
                    "position": ordinal,
                    "role": evidence.role,
                    "ordinal": evidence.ordinal,
                    "snapshot_id": snapshot.snapshot_id,
                    "evidence_id": evidence_id,
                }
            )
            per_asset_rows.append(
                {
                    "symbol": asset.canonical_symbol,
                    "provider": asset.provider_name,
                    "source_rows": len(asset.bars),
                    "aligned_rows": len(common_days),
                    "aligned_row_ratio": str(Decimal(len(common_days)) / Decimal(len(asset.bars))),
                    "numeric_input_basis": (
                        "exact_provider_decimal"
                        if all(bar.exact_numeric_source for bar in asset.bars.values())
                        else "provider_numeric_projection"
                    ),
                    "availability_sources": sorted(
                        {bar.availability_source for bar in asset.bars.values()}
                    ),
                    "capabilities": _capability_payload(asset.metadata),
                    "snapshot_id": snapshot.snapshot_id,
                    "evidence_id": evidence_id,
                }
            )

        source_distinct_days = len(set.union(*(set(asset.bars) for asset in assets)))
        recipe: dict[str, object] = {
            "run_as_of_at": research_started_at.isoformat(),
            "factor_id": request.factor_id,
            "lookback": request.lookback,
            "horizons": list(request.horizons),
            "quantiles": request.quantiles,
            "interval": request.interval,
            **_FACTOR_RECIPES[request.factor_id],
            "feature_information_cutoff": "d-1",
            "feature_availability": (
                "maximum modeled availability across every row in each "
                "declared lookback window and the full basket"
            ),
            "decision_time": "maximum modeled feature-window availability",
            "execution_delay": "one exact shared daily bar",
            "label_formula": "open[d+1] to close[d+h]",
            "label_evidence": ("entry open and exit close rows are bound separately"),
            "label_available_at": ("maximum modeled availability of all entry and exit rows"),
            "label_persistence_cutoff": ("not later than the research-start UTC instant"),
            "fundamentals_enabled": False,
            "cost_model": "none",
        }
        decision_date_coverage = {
            horizon: str(Decimal(count) / Decimal(len(candidate_indices)))
            for horizon, count in period_counts.items()
        }
        coverage: dict[str, object] = {
            "requested_assets": len(assets),
            "common_days": len(common_days),
            "source_distinct_days": source_distinct_days,
            "alignment_day_retention_rate": str(
                Decimal(len(common_days)) / Decimal(source_distinct_days)
            ),
            "requested_decision_days": len(candidate_indices),
            "candidate_observation_pairs": candidate_observation_pairs,
            "evaluated_periods_by_horizon": period_counts,
            "decision_date_coverage_by_horizon": decision_date_coverage,
            "dropped_reason_counts_by_horizon": dropped_reason_counts,
            "per_asset": per_asset_rows,
            "alignment": "exact_shared_utc_day_intersection",
            "forward_fill": False,
        }
        limitations: dict[str, object] = {
            "universe_semantics": "fixed_user_selected_ex_post",
            "point_in_time_universe": False,
            "source_availability": "provider_policy_estimate",
            "point_in_time_validation_passed": False,
            "survivorship_bias_controlled": False,
            "survivorship_bias_status": "not_controlled",
            "fundamentals_enabled": False,
            "research_only": True,
            "tradable_conclusion": False,
            "note": (
                "Row-level finalization time is consumed when supplied. Other "
                "daily rows use a conservative next-UTC-day provider-policy "
                "estimate. Feature availability covers every row in the "
                "declared lookback window. Labels bind both entry-open and "
                "exit-close rows and are excluded until both are available "
                "as of research start. Labels wait one additional shared bar, but the "
                "fixed user-selected universe may still contain survivorship bias. "
                "IC, IR, and quantile returns are descriptive diagnostics only: "
                "multi-day labels overlap and currently have no HAC/serial-correlation "
                "or multiple-testing adjustment. Exact factor ties that cross a "
                "quantile boundary fail closed instead of being split by symbol."
            ),
        }
        result_evidence = _result_payload(
            run_as_of_at=research_started_at,
            recipe=recipe,
            coverage=coverage,
            diagnostics_by_horizon=diagnostics_by_horizon,
            time_series=time_series,
            latest_scores=latest_scores,
            limitations=limitations,
        )
        run_request: dict[str, object] = {
            **request.model_dump(mode="json"),
            "instruments": [instrument.model_dump(mode="json") for instrument in instruments],
        }
        engine_config: dict[str, object] = {
            "run_as_of_at": research_started_at.isoformat(),
            "alignment": "exact_shared_utc_day_intersection",
            "forward_fill": False,
            "feature_information_cutoff": "d-1",
            "feature_availability": (
                "maximum modeled availability across every row in each "
                "declared lookback window and the full basket"
            ),
            "decision_time": "maximum modeled feature-window availability",
            "execution_delay": "one exact shared daily bar",
            "label_evidence": "entry_open_and_exit_close_rows",
            "label_available_at": ("maximum modeled availability of all entry and exit rows"),
            "label_persistence_cutoff": "research_start_utc",
            "execution_model": _EXECUTION_MODEL,
            "dataset_evidence": dataset_evidence_bindings,
            "universe_semantics": "fixed_user_selected_ex_post",
            "point_in_time_universe": False,
            "source_availability": "provider_policy_estimate",
        }
        cost_model: dict[str, object] = {
            "kind": "none",
            "reason": "diagnostic_factor_study_not_execution_backtest",
        }
        try:
            _check_research_deadline(deadline_monotonic)
            manifest = build_run_manifest(
                run_kind="factor_research",
                application_version=self.settings.build_version.strip() or "0.1.0",
                engine_version="quantsieve-factor-research-v1",
                source_revision=self.settings.source_revision,
                dependency_lock_hash=self.settings.dependency_lock_sha256,
                datasets=[snapshot for _, snapshot in snapshot_records],
                run_request=run_request,
                parameters=recipe,
                engine_config=engine_config,
                cost_model=cost_model,
                execution_model=_EXECUTION_MODEL,
                result=result_evidence,
                recorded_at=now,
            )
            _check_research_deadline(deadline_monotonic)
        except (ArithmeticError, ValueError) as error:
            raise FactorResearchInputError(f"Factor run manifest failed closed: {error}") from error
        receipt: ResearchRunReceipt = self.run_store.create_run(
            manifest=manifest,
            datasets=tuple(dataset_evidence),
            run_request=run_request,
            result=result_evidence,
            parameters=recipe,
            engine_config=engine_config,
            cost_model=cost_model,
            deadline_monotonic=deadline_monotonic,
        )
        receipt_datasets = tuple(
            sorted(receipt.datasets, key=lambda item: (item.ordinal, item.role))
        )
        response_snapshots = [
            {
                "symbol": evidence.snapshot.symbol,
                "provider": evidence.snapshot.provider,
                "role": evidence.role,
                "ordinal": evidence.ordinal,
                "evidence_id": _dataset_evidence_id(
                    evidence.snapshot,
                    role=evidence.role,
                    ordinal=evidence.ordinal,
                ),
                "snapshot": evidence.snapshot.model_dump(mode="json"),
            }
            for evidence in receipt_datasets
        ]
        citations = [
            {
                "symbol": evidence.snapshot.symbol,
                "provider": evidence.snapshot.provider,
                **citation,
            }
            for evidence in receipt_datasets
            for citation in evidence.citations
        ]
        return {
            **result_evidence,
            "citations": citations,
            "dataset_snapshots": response_snapshots,
            "run_manifest": receipt.manifest.model_dump(mode="json"),
            "run_id": receipt.run_id,
            "run_expires_at": receipt.expires_at.isoformat(),
        }
