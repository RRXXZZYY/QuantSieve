from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .reproducibility import canonical_payload_hash

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_INSTRUMENT_UID_PATTERN = r"^[a-z0-9][a-z0-9._:-]{2,127}$"
_RESEARCH_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$"
_METRIC_QUANTUM = Decimal("0.000000000001")


def _utc_timestamp(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware.")
    return value.astimezone(UTC)


def _reject_surrounding_whitespace(value: str, *, label: str) -> str:
    if value != value.strip():
        raise ValueError(f"{label} cannot contain surrounding whitespace.")
    return value


def _finite_decimal(value: Decimal, *, label: str) -> Decimal:
    if not value.is_finite():
        raise ValueError(f"{label} must be finite.")
    return value


def _content_identity(
    value: BaseModel | Mapping[str, object],
    *,
    contract: str,
    id_field: str,
) -> dict[str, object]:
    data = value.model_dump(mode="python") if isinstance(value, BaseModel) else dict(value)
    data.pop(id_field, None)
    return {"contract": contract, **data}


class _PointInTimeBase(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )

    schema_version: Literal[1] = 1
    instrument_uid: str = Field(
        min_length=3,
        max_length=128,
        pattern=_INSTRUMENT_UID_PATTERN,
    )
    period_at: datetime
    effective_at: datetime
    observed_at: datetime
    available_at: datetime

    @field_validator("instrument_uid")
    @classmethod
    def validate_instrument_uid(cls, value: str) -> str:
        return _reject_surrounding_whitespace(value, label="instrument_uid")

    @field_validator(
        "period_at",
        "effective_at",
        "observed_at",
        "available_at",
    )
    @classmethod
    def normalize_timestamp(cls, value: datetime, info: Any) -> datetime:
        return _utc_timestamp(value, label=info.field_name)


class _AvailableAtDecision(_PointInTimeBase):
    @model_validator(mode="after")
    def validate_point_in_time_availability(self) -> Self:
        if self.effective_at > self.observed_at:
            raise ValueError("effective_at cannot be later than observed_at.")
        if self.observed_at > self.available_at:
            raise ValueError("observed_at cannot be later than available_at.")
        if self.available_at > self.period_at:
            raise ValueError(
                "available_at cannot be later than period_at; this would introduce lookahead."
            )
        return self


class _UniverseObservationContent(_AvailableAtDecision):
    membership_basis: Literal[
        "point_in_time_source",
        "fixed_user_selected_ex_post",
    ] = "point_in_time_source"
    included: bool


class UniverseObservation(_UniverseObservationContent):
    """Research-universe inclusion with an explicit historical-evidence basis."""

    observation_id: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_observation_id(self) -> Self:
        expected = canonical_payload_hash(
            _content_identity(
                self,
                contract="quantsieve.factor.universe-observation.v1",
                id_field="observation_id",
            )
        )
        if self.observation_id != expected:
            raise ValueError("observation_id does not match universe observation content.")
        return self


class _FeatureObservationContent(_AvailableAtDecision):
    feature_name: str = Field(
        min_length=1,
        max_length=120,
        pattern=_RESEARCH_NAME_PATTERN,
    )
    value: Decimal

    @field_validator("feature_name")
    @classmethod
    def validate_feature_name(cls, value: str) -> str:
        return _reject_surrounding_whitespace(value, label="feature_name")

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: Decimal) -> Decimal:
        return _finite_decimal(value, label="Feature value")


class FeatureObservation(_FeatureObservationContent):
    """Finite factor value whose availability is no later than the decision."""

    observation_id: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_observation_id(self) -> Self:
        expected = canonical_payload_hash(
            _content_identity(
                self,
                contract="quantsieve.factor.feature-observation.v1",
                id_field="observation_id",
            )
        )
        if self.observation_id != expected:
            raise ValueError("observation_id does not match feature observation content.")
        return self


class _LabelObservationContent(_PointInTimeBase):
    label_name: str = Field(
        min_length=1,
        max_length=120,
        pattern=_RESEARCH_NAME_PATTERN,
    )
    value: Decimal

    @field_validator("label_name")
    @classmethod
    def validate_label_name(cls, value: str) -> str:
        return _reject_surrounding_whitespace(value, label="label_name")

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: Decimal) -> Decimal:
        return _finite_decimal(value, label="Label value")

    @model_validator(mode="after")
    def validate_forward_label(self) -> Self:
        if self.effective_at <= self.period_at:
            raise ValueError("A forward label effective_at must be later than period_at.")
        if self.effective_at > self.observed_at:
            raise ValueError("Label effective_at cannot be later than observed_at.")
        if self.observed_at > self.available_at:
            raise ValueError("Label observed_at cannot be later than available_at.")
        return self


class LabelObservation(_LabelObservationContent):
    """Forward outcome evidence that is deliberately unavailable at the decision."""

    observation_id: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_observation_id(self) -> Self:
        expected = canonical_payload_hash(
            _content_identity(
                self,
                contract="quantsieve.factor.label-observation.v1",
                id_field="observation_id",
            )
        )
        if self.observation_id != expected:
            raise ValueError("observation_id does not match label observation content.")
        return self


def build_universe_observation(
    *,
    instrument_uid: str,
    period_at: datetime,
    effective_at: datetime,
    observed_at: datetime,
    available_at: datetime,
    membership_basis: Literal[
        "point_in_time_source",
        "fixed_user_selected_ex_post",
    ] = "point_in_time_source",
    included: bool,
) -> UniverseObservation:
    """Build immutable point-in-time universe membership evidence."""

    content = _UniverseObservationContent.model_validate(
        {
            "schema_version": 1,
            "instrument_uid": instrument_uid,
            "period_at": period_at,
            "effective_at": effective_at,
            "observed_at": observed_at,
            "available_at": available_at,
            "membership_basis": membership_basis,
            "included": included,
        }
    )
    identity = content.model_dump(mode="python")
    observation_id = canonical_payload_hash(
        _content_identity(
            identity,
            contract="quantsieve.factor.universe-observation.v1",
            id_field="observation_id",
        )
    )
    return UniverseObservation.model_validate({**identity, "observation_id": observation_id})


def build_feature_observation(
    *,
    instrument_uid: str,
    period_at: datetime,
    effective_at: datetime,
    observed_at: datetime,
    available_at: datetime,
    feature_name: str,
    value: Decimal,
) -> FeatureObservation:
    """Build immutable feature evidence without coercing approximate floats."""

    content = _FeatureObservationContent.model_validate(
        {
            "schema_version": 1,
            "instrument_uid": instrument_uid,
            "period_at": period_at,
            "effective_at": effective_at,
            "observed_at": observed_at,
            "available_at": available_at,
            "feature_name": feature_name,
            "value": value,
        }
    )
    identity = content.model_dump(mode="python")
    observation_id = canonical_payload_hash(
        _content_identity(
            identity,
            contract="quantsieve.factor.feature-observation.v1",
            id_field="observation_id",
        )
    )
    return FeatureObservation.model_validate({**identity, "observation_id": observation_id})


def build_label_observation(
    *,
    instrument_uid: str,
    period_at: datetime,
    effective_at: datetime,
    observed_at: datetime,
    available_at: datetime,
    label_name: str,
    value: Decimal,
) -> LabelObservation:
    """Build immutable forward-label evidence with explicit realization timing."""

    content = _LabelObservationContent.model_validate(
        {
            "schema_version": 1,
            "instrument_uid": instrument_uid,
            "period_at": period_at,
            "effective_at": effective_at,
            "observed_at": observed_at,
            "available_at": available_at,
            "label_name": label_name,
            "value": value,
        }
    )
    identity = content.model_dump(mode="python")
    observation_id = canonical_payload_hash(
        _content_identity(
            identity,
            contract="quantsieve.factor.label-observation.v1",
            id_field="observation_id",
        )
    )
    return LabelObservation.model_validate({**identity, "observation_id": observation_id})


def _observation_key(
    observation: _PointInTimeBase,
) -> tuple[datetime, str]:
    return observation.period_at, observation.instrument_uid


def _panel_identity(
    panel: FactorResearchPanel | Mapping[str, object],
) -> dict[str, object]:
    data = (
        panel.model_dump(mode="python") if isinstance(panel, FactorResearchPanel) else dict(panel)
    )
    data.pop("panel_id", None)
    for field_name in ("universe", "features", "labels"):
        observations = data[field_name]
        if not isinstance(observations, Sequence):
            raise TypeError(f"{field_name} must be a sequence.")
        data[field_name] = [
            (
                observation.observation_id
                if isinstance(
                    observation,
                    (UniverseObservation, FeatureObservation, LabelObservation),
                )
                else observation["observation_id"]
                if isinstance(observation, Mapping)
                else observation
            )
            for observation in observations
        ]
    return {"contract": "quantsieve.factor.research-panel.v1", **data}


class FactorResearchPanel(BaseModel):
    """Aligned multi-asset, multi-period factor panel with no silent imputation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )

    schema_version: Literal[1] = 1
    panel_id: str = Field(pattern=_SHA256_PATTERN)
    feature_name: str = Field(
        min_length=1,
        max_length=120,
        pattern=_RESEARCH_NAME_PATTERN,
    )
    label_name: str = Field(
        min_length=1,
        max_length=120,
        pattern=_RESEARCH_NAME_PATTERN,
    )
    universe: tuple[UniverseObservation, ...] = Field(min_length=1)
    features: tuple[FeatureObservation, ...] = Field(min_length=1)
    labels: tuple[LabelObservation, ...] = Field(min_length=1)

    @field_validator("feature_name", "label_name")
    @classmethod
    def validate_research_name(cls, value: str, info: Any) -> str:
        return _reject_surrounding_whitespace(value, label=info.field_name)

    @model_validator(mode="after")
    def validate_panel(self) -> Self:
        if any(item.feature_name != self.feature_name for item in self.features):
            raise ValueError("Every feature observation must match feature_name.")
        if any(item.label_name != self.label_name for item in self.labels):
            raise ValueError("Every label observation must match label_name.")

        for label, values in (
            ("Universe", self.universe),
            ("Feature", self.features),
            ("Label", self.labels),
        ):
            keys = [_observation_key(item) for item in values]
            if len(set(keys)) != len(keys):
                raise ValueError(f"{label} observation keys cannot be repeated.")
            if tuple(sorted(values, key=_observation_key)) != values:
                raise ValueError(f"{label} observations must use canonical key order.")

        universe_by_key = {_observation_key(item): item for item in self.universe}
        active_keys = {key for key, membership in universe_by_key.items() if membership.included}
        feature_keys = {_observation_key(item) for item in self.features}
        label_keys = {_observation_key(item) for item in self.labels}
        if feature_keys != label_keys:
            raise ValueError(
                "Feature and label observations must be paired; missing samples "
                "cannot be silently imputed."
            )
        unknown_keys = feature_keys - active_keys
        if unknown_keys:
            raise ValueError("Feature and label samples require included universe membership.")

        active_instruments = {instrument for _, instrument in active_keys}
        active_periods = {period for period, _ in active_keys}
        if len(active_instruments) < 2 or len(active_periods) < 2:
            raise ValueError(
                "A factor research panel requires multiple assets and multiple periods."
            )
        for period in active_periods:
            period_samples = sum(sample_period == period for sample_period, _ in feature_keys)
            if period_samples < 2:
                raise ValueError("Every period requires at least two paired factor samples.")

        expected_id = canonical_payload_hash(_panel_identity(self))
        if self.panel_id != expected_id:
            raise ValueError("panel_id does not match immutable panel content.")
        return self


def build_factor_research_panel(
    *,
    feature_name: str,
    label_name: str,
    universe: Sequence[UniverseObservation],
    features: Sequence[FeatureObservation],
    labels: Sequence[LabelObservation],
) -> FactorResearchPanel:
    """Build a canonical panel; input order never changes its content address."""

    safe_universe = tuple(
        sorted(
            (
                UniverseObservation.model_validate(item.model_dump(mode="python"))
                for item in universe
            ),
            key=_observation_key,
        )
    )
    safe_features = tuple(
        sorted(
            (
                FeatureObservation.model_validate(item.model_dump(mode="python"))
                for item in features
            ),
            key=_observation_key,
        )
    )
    safe_labels = tuple(
        sorted(
            (LabelObservation.model_validate(item.model_dump(mode="python")) for item in labels),
            key=_observation_key,
        )
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "feature_name": feature_name,
        "label_name": label_name,
        "universe": safe_universe,
        "features": safe_features,
        "labels": safe_labels,
    }
    panel_id = canonical_payload_hash(_panel_identity(payload))
    return FactorResearchPanel.model_validate({**payload, "panel_id": panel_id})


def _metric(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("Factor diagnostics cannot contain non-finite metrics.")
    if value == 0:
        return Decimal("0")
    return value.quantize(_METRIC_QUANTUM)


def _mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("Cannot calculate a mean without samples.")
    return sum(values, start=Decimal("0")) / Decimal(len(values))


def _pearson(
    left: Sequence[Decimal],
    right: Sequence[Decimal],
    *,
    label: str,
) -> Decimal:
    if len(left) != len(right) or len(left) < 2:
        raise ValueError(f"{label} requires at least two paired samples.")
    left_mean = _mean(left)
    right_mean = _mean(right)
    left_deviations = [value - left_mean for value in left]
    right_deviations = [value - right_mean for value in right]
    numerator = sum(
        (x_value * y_value)
        for x_value, y_value in zip(
            left_deviations,
            right_deviations,
            strict=True,
        )
    )
    left_squares = sum(
        (value * value for value in left_deviations),
        start=Decimal("0"),
    )
    right_squares = sum(
        (value * value for value in right_deviations),
        start=Decimal("0"),
    )
    if left_squares == 0:
        raise ValueError("Constant factor values cannot produce factor diagnostics.")
    if right_squares == 0:
        raise ValueError("Constant label values cannot produce factor diagnostics.")
    with localcontext() as context:
        context.prec = 50
        denominator = (left_squares * right_squares).sqrt()
        return _metric(numerator / denominator)


def _average_ranks(values: Sequence[Decimal]) -> list[Decimal]:
    indexed = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [Decimal("0")] * len(values)
    start = 0
    while start < len(indexed):
        end = start + 1
        while end < len(indexed) and indexed[end][1] == indexed[start][1]:
            end += 1
        average_rank = (Decimal(start + 1) + Decimal(end)) / Decimal("2")
        for position in range(start, end):
            ranks[indexed[position][0]] = average_rank
        start = end
    return ranks


def _sample_statistics(
    values: Sequence[Decimal],
) -> tuple[Decimal, Decimal, Decimal | None]:
    if len(values) < 2:
        raise ValueError("Sample statistics require at least two periods.")
    mean = _mean(values)
    variance = sum(
        ((value - mean) ** 2 for value in values),
        start=Decimal("0"),
    ) / Decimal(len(values) - 1)
    with localcontext() as context:
        context.prec = 50
        volatility = variance.sqrt()
        information_ratio = None if volatility == 0 else mean / volatility
    return (
        _metric(mean),
        _metric(volatility),
        None if information_ratio is None else _metric(information_ratio),
    )


def _quantile_portfolios(
    samples: Sequence[tuple[str, Decimal, Decimal]],
    *,
    quantile_count: int,
) -> tuple[tuple[Decimal, ...], dict[str, Decimal]]:
    ordered = sorted(samples, key=lambda item: (item[1], item[0]))
    for index in range(1, len(ordered)):
        prior_group = (index - 1) * quantile_count // len(ordered)
        current_group = index * quantile_count // len(ordered)
        if (
            prior_group != current_group
            and ordered[index - 1][1] == ordered[index][1]
        ):
            raise ValueError(
                "Identical factor values cannot cross a quantile boundary."
            )
    groups: list[list[tuple[str, Decimal, Decimal]]] = [[] for _ in range(quantile_count)]
    for index, sample in enumerate(ordered):
        group_index = index * quantile_count // len(ordered)
        groups[group_index].append(sample)
    if any(not group for group in groups):
        raise ValueError("Every quantile portfolio requires at least one sample.")

    returns = tuple(_metric(_mean([sample[2] for sample in group])) for group in groups)
    weights: dict[str, Decimal] = {}
    bottom_weight = Decimal("-1") / Decimal(len(groups[0]))
    top_weight = Decimal("1") / Decimal(len(groups[-1]))
    for instrument_uid, _, _ in groups[0]:
        weights[instrument_uid] = bottom_weight
    for instrument_uid, _, _ in groups[-1]:
        weights[instrument_uid] = top_weight
    return returns, weights


def _turnover(
    prior_weights: Mapping[str, Decimal],
    current_weights: Mapping[str, Decimal],
) -> Decimal:
    instruments = set(prior_weights) | set(current_weights)
    gross_change = sum(
        (
            abs(
                current_weights.get(instrument_uid, Decimal("0"))
                - prior_weights.get(instrument_uid, Decimal("0"))
            )
            for instrument_uid in instruments
        ),
        start=Decimal("0"),
    )
    return _metric(gross_change / Decimal("2"))


class FactorPeriodDiagnostics(BaseModel):
    """One cross-section of diagnostics, ordered by decision period."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )

    period_at: datetime
    eligible_count: int = Field(ge=1)
    sample_count: int = Field(ge=2)
    coverage_rate: Decimal
    pearson_ic: Decimal
    rank_ic: Decimal
    quantile_returns: tuple[Decimal, ...] = Field(min_length=2)
    long_short_return: Decimal
    long_short_turnover: Decimal | None = None

    @field_validator("period_at")
    @classmethod
    def normalize_period_at(cls, value: datetime) -> datetime:
        return _utc_timestamp(value, label="period_at")

    @field_validator(
        "coverage_rate",
        "pearson_ic",
        "rank_ic",
        "long_short_return",
        "long_short_turnover",
    )
    @classmethod
    def validate_metric(
        cls,
        value: Decimal | None,
        info: Any,
    ) -> Decimal | None:
        if value is None:
            return None
        return _finite_decimal(value, label=info.field_name)

    @field_validator("quantile_returns")
    @classmethod
    def validate_quantile_returns(
        cls,
        value: tuple[Decimal, ...],
    ) -> tuple[Decimal, ...]:
        for item in value:
            _finite_decimal(item, label="quantile_returns")
        return value

    @model_validator(mode="after")
    def validate_counts_and_ranges(self) -> Self:
        if self.sample_count > self.eligible_count:
            raise ValueError("sample_count cannot exceed eligible_count.")
        if not Decimal("0") <= self.coverage_rate <= Decimal("1"):
            raise ValueError("coverage_rate must be between zero and one.")
        for name, value in (
            ("pearson_ic", self.pearson_ic),
            ("rank_ic", self.rank_ic),
        ):
            if not Decimal("-1") <= value <= Decimal("1"):
                raise ValueError(f"{name} must be between minus one and one.")
        return self


def _diagnostics_identity(
    diagnostics: FactorDiagnostics | Mapping[str, object],
) -> dict[str, object]:
    data = (
        diagnostics.model_dump(mode="python")
        if isinstance(diagnostics, FactorDiagnostics)
        else dict(diagnostics)
    )
    data.pop("diagnostics_id", None)
    return {"contract": "quantsieve.factor.diagnostics.v1", **data}


class FactorDiagnostics(BaseModel):
    """Content-addressed IC, portfolio, turnover, and monotonicity evidence."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )

    schema_version: Literal[1] = 1
    diagnostics_id: str = Field(pattern=_SHA256_PATTERN)
    panel_id: str = Field(pattern=_SHA256_PATTERN)
    feature_name: str = Field(
        min_length=1,
        max_length=120,
        pattern=_RESEARCH_NAME_PATTERN,
    )
    label_name: str = Field(
        min_length=1,
        max_length=120,
        pattern=_RESEARCH_NAME_PATTERN,
    )
    quantile_count: int = Field(ge=2, le=20)
    period_count: int = Field(ge=2)
    observation_count: int = Field(ge=1)
    coverage_rate: Decimal
    pearson_ic_mean: Decimal
    pearson_ic_volatility: Decimal
    pearson_ic_information_ratio: Decimal | None
    rank_ic_mean: Decimal
    rank_ic_volatility: Decimal
    rank_ic_information_ratio: Decimal | None
    quantile_mean_returns: tuple[Decimal, ...] = Field(min_length=2)
    long_short_mean_return: Decimal
    long_short_volatility: Decimal
    long_short_information_ratio: Decimal | None
    average_turnover: Decimal
    quantile_monotonicity: Decimal
    periods: tuple[FactorPeriodDiagnostics, ...] = Field(min_length=2)

    @field_validator("feature_name", "label_name")
    @classmethod
    def validate_research_name(cls, value: str, info: Any) -> str:
        return _reject_surrounding_whitespace(value, label=info.field_name)

    @field_validator(
        "coverage_rate",
        "pearson_ic_mean",
        "pearson_ic_volatility",
        "pearson_ic_information_ratio",
        "rank_ic_mean",
        "rank_ic_volatility",
        "rank_ic_information_ratio",
        "long_short_mean_return",
        "long_short_volatility",
        "long_short_information_ratio",
        "average_turnover",
        "quantile_monotonicity",
    )
    @classmethod
    def validate_metric(
        cls,
        value: Decimal | None,
        info: Any,
    ) -> Decimal | None:
        if value is None:
            return None
        return _finite_decimal(value, label=info.field_name)

    @field_validator("quantile_mean_returns")
    @classmethod
    def validate_quantile_mean_returns(
        cls,
        value: tuple[Decimal, ...],
    ) -> tuple[Decimal, ...]:
        for item in value:
            _finite_decimal(item, label="quantile_mean_returns")
        return value

    @model_validator(mode="after")
    def validate_diagnostics(self) -> Self:
        if self.period_count != len(self.periods):
            raise ValueError("period_count must match periods.")
        if self.quantile_count != len(self.quantile_mean_returns):
            raise ValueError("quantile_count must match quantile_mean_returns.")
        if any(len(period.quantile_returns) != self.quantile_count for period in self.periods):
            raise ValueError("Every period must contain quantile_count returns.")
        if tuple(sorted(self.periods, key=lambda item: item.period_at)) != self.periods:
            raise ValueError("Diagnostic periods must be ordered.")
        if len({item.period_at for item in self.periods}) != len(self.periods):
            raise ValueError("Diagnostic periods cannot be repeated.")
        if self.observation_count != sum(item.sample_count for item in self.periods):
            raise ValueError("observation_count must match period sample counts.")
        if not Decimal("0") <= self.coverage_rate <= Decimal("1"):
            raise ValueError("coverage_rate must be between zero and one.")
        if self.average_turnover < 0:
            raise ValueError("average_turnover cannot be negative.")
        if not Decimal("-1") <= self.quantile_monotonicity <= Decimal("1"):
            raise ValueError("quantile_monotonicity must be between minus one and one.")
        expected = canonical_payload_hash(_diagnostics_identity(self))
        if self.diagnostics_id != expected:
            raise ValueError("diagnostics_id does not match immutable diagnostic content.")
        return self


def analyze_factor(
    panel: FactorResearchPanel,
    *,
    quantile_count: int = 5,
) -> FactorDiagnostics:
    """Analyze one factor without network access, imputation, or random choices.

    IC volatility is the sample standard deviation across periods and IC IR is
    mean divided by that volatility. A zero-volatility stream reports a null IR.
    Turnover is half the absolute change in equal-weighted top-minus-bottom
    holdings; the first period has no predecessor and therefore reports null.
    """

    if isinstance(quantile_count, bool) or not isinstance(quantile_count, int):
        raise TypeError("quantile_count must be an integer.")
    if not 2 <= quantile_count <= 20:
        raise ValueError("quantile_count must be between 2 and 20.")
    safe_panel = FactorResearchPanel.model_validate(panel.model_dump(mode="python"))
    universe_by_period: dict[datetime, set[str]] = {}
    for membership in safe_panel.universe:
        if membership.included:
            universe_by_period.setdefault(membership.period_at, set()).add(
                membership.instrument_uid
            )
    feature_by_key = {_observation_key(item): item.value for item in safe_panel.features}
    label_by_key = {_observation_key(item): item.value for item in safe_panel.labels}

    period_diagnostics: list[FactorPeriodDiagnostics] = []
    prior_weights: dict[str, Decimal] | None = None
    for period_at in sorted(universe_by_period):
        eligible = universe_by_period[period_at]
        samples = [
            (
                instrument_uid,
                feature_by_key[(period_at, instrument_uid)],
                label_by_key[(period_at, instrument_uid)],
            )
            for instrument_uid in sorted(eligible)
            if (period_at, instrument_uid) in feature_by_key
        ]
        if len(samples) < max(3, quantile_count):
            raise ValueError(
                "Insufficient samples: every period needs at least three "
                "observations and at least one per quantile."
            )
        feature_values = [sample[1] for sample in samples]
        label_values = [sample[2] for sample in samples]
        pearson_ic = _pearson(
            feature_values,
            label_values,
            label="Pearson IC",
        )
        rank_ic = _pearson(
            _average_ranks(feature_values),
            _average_ranks(label_values),
            label="Rank IC",
        )
        quantile_returns, weights = _quantile_portfolios(
            samples,
            quantile_count=quantile_count,
        )
        current_turnover = None if prior_weights is None else _turnover(prior_weights, weights)
        period_diagnostics.append(
            FactorPeriodDiagnostics(
                period_at=period_at,
                eligible_count=len(eligible),
                sample_count=len(samples),
                coverage_rate=_metric(Decimal(len(samples)) / Decimal(len(eligible))),
                pearson_ic=pearson_ic,
                rank_ic=rank_ic,
                quantile_returns=quantile_returns,
                long_short_return=_metric(quantile_returns[-1] - quantile_returns[0]),
                long_short_turnover=current_turnover,
            )
        )
        prior_weights = weights

    if len(period_diagnostics) < 2:
        raise ValueError("Factor diagnostics require at least two periods.")
    pearson_mean, pearson_volatility, pearson_ir = _sample_statistics(
        [item.pearson_ic for item in period_diagnostics]
    )
    rank_mean, rank_volatility, rank_ir = _sample_statistics(
        [item.rank_ic for item in period_diagnostics]
    )
    long_short_mean, long_short_volatility, long_short_ir = _sample_statistics(
        [item.long_short_return for item in period_diagnostics]
    )
    mean_quantile_returns = tuple(
        _metric(_mean([period.quantile_returns[index] for period in period_diagnostics]))
        for index in range(quantile_count)
    )
    if len(set(mean_quantile_returns)) == 1:
        monotonicity = Decimal("0")
    else:
        monotonicity = _pearson(
            [Decimal(index) for index in range(1, quantile_count + 1)],
            list(mean_quantile_returns),
            label="Quantile monotonicity",
        )
    turnovers = [
        item.long_short_turnover
        for item in period_diagnostics
        if item.long_short_turnover is not None
    ]
    if not turnovers:
        raise ValueError("Turnover requires at least two periods.")
    total_samples = sum(item.sample_count for item in period_diagnostics)
    total_eligible = sum(item.eligible_count for item in period_diagnostics)
    payload: dict[str, object] = {
        "schema_version": 1,
        "panel_id": safe_panel.panel_id,
        "feature_name": safe_panel.feature_name,
        "label_name": safe_panel.label_name,
        "quantile_count": quantile_count,
        "period_count": len(period_diagnostics),
        "observation_count": total_samples,
        "coverage_rate": _metric(Decimal(total_samples) / Decimal(total_eligible)),
        "pearson_ic_mean": pearson_mean,
        "pearson_ic_volatility": pearson_volatility,
        "pearson_ic_information_ratio": pearson_ir,
        "rank_ic_mean": rank_mean,
        "rank_ic_volatility": rank_volatility,
        "rank_ic_information_ratio": rank_ir,
        "quantile_mean_returns": mean_quantile_returns,
        "long_short_mean_return": long_short_mean,
        "long_short_volatility": long_short_volatility,
        "long_short_information_ratio": long_short_ir,
        "average_turnover": _metric(_mean(turnovers)),
        "quantile_monotonicity": _metric(monotonicity),
        "periods": tuple(period_diagnostics),
    }
    diagnostics_id = canonical_payload_hash(_diagnostics_identity(payload))
    return FactorDiagnostics.model_validate({**payload, "diagnostics_id": diagnostics_id})
