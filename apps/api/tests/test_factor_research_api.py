from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event, Lock
from time import monotonic, sleep
from typing import Literal, cast

import httpx
import pytest
import quantsieve_api.factor_research_service as factor_service_module
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from quantsieve_api.config import Settings
from quantsieve_api.main import create_app
from quantsieve_api.research_runs import (
    ResearchDatasetEvidence,
    ResearchRunReceipt,
    ResearchRunStore,
)
from quantsieve_engine import RunManifest
from quantsieve_providers import Citation, DataEnvelope
from quantsieve_providers.base import DataProvider

ProviderName = Literal[
    "akshare",
    "yfinance",
    "binance",
    "futures",
    "macro",
]

FIXED_RETRIEVED_AT = datetime(2026, 1, 1, tzinfo=UTC)
SYMBOLS: tuple[tuple[str, ProviderName], ...] = (
    ("ALPHA", "yfinance"),
    ("BETA", "yfinance"),
    ("GAMMAUSDT", "binance"),
    ("DELTA", "futures"),
)


def _business_days(first: date, count: int) -> list[date]:
    result: list[date] = []
    current = first
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _rows(asset_number: int, *, count: int = 120) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, day in enumerate(_business_days(date(2024, 1, 2), count)):
        base = (
            Decimal("40")
            + Decimal(asset_number * 9)
            + Decimal(index) * Decimal(asset_number + 2) / Decimal("20")
        )
        cycle = Decimal((index + asset_number) % 7 - 3) / Decimal("100")
        open_price = base + cycle
        close_price = open_price * (
            Decimal("1")
            + Decimal(asset_number + 1) / Decimal("1000")
            + Decimal((index + asset_number) % 5 - 2) / Decimal("10000")
        )
        high = max(open_price, close_price) + Decimal("0.5")
        low = min(open_price, close_price) - Decimal("0.5")
        volume = (
            Decimal("1000000")
            + Decimal((asset_number + 1) * index * 137)
            + Decimal((index + asset_number) % 4) * Decimal("1000")
        )
        rows.append(
            {
                "date": datetime.combine(
                    day,
                    datetime.min.time(),
                    tzinfo=UTC,
                ).isoformat(),
                "open": _decimal_text(open_price),
                "high": _decimal_text(high),
                "low": _decimal_text(low),
                "close": _decimal_text(close_price),
                "volume": _decimal_text(volume),
            }
        )
    return rows


@dataclass
class _ConcurrencyProbe:
    active: int = 0
    maximum: int = 0


class FixtureFactorProvider(DataProvider):
    def __init__(
        self,
        name: ProviderName,
        rows_by_symbol: dict[str, list[dict[str, object]]],
        canonical_symbols: dict[str, str],
        probe: _ConcurrencyProbe,
    ) -> None:
        self.name = name
        self.rows_by_symbol = rows_by_symbol
        self.canonical_symbols = canonical_symbols
        self.probe = probe
        self.finalized = True
        self.error: RuntimeError | None = None
        self.delay_seconds = 0.005
        self.retrieved_at = FIXED_RETRIEVED_AT

    async def history(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
    ) -> DataEnvelope:
        return await self.history_interval(
            symbol,
            start,
            end,
            interval="1d",
        )

    async def history_interval(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        *,
        interval: Literal["15m", "1h", "4h", "1d", "1wk"] = "1d",
    ) -> DataEnvelope:
        del start, end
        assert interval == "1d"
        if self.error is not None:
            raise self.error
        self.probe.active += 1
        self.probe.maximum = max(self.probe.maximum, self.probe.active)
        try:
            await asyncio.sleep(self.delay_seconds)
            return DataEnvelope(
                symbol=self.canonical_symbols[symbol],
                kind="history",
                rows=[dict(row) for row in self.rows_by_symbol[symbol]],
                citations=[
                    Citation(
                        source=f"{self.name} deterministic fixture",
                        url=f"https://example.test/{self.name}/{symbol}",
                        retrieved_at=self.retrieved_at,
                        as_of=self.retrieved_at,
                    )
                ],
                metadata={
                    "finalized_bars_only": self.finalized,
                    "bar_finalization_policy": "test_completed_daily_bars",
                },
            )
        finally:
            self.probe.active -= 1

    async def quote(self, symbol: str) -> DataEnvelope:
        return await self.history(symbol)

    async def fundamentals(self, symbol: str) -> DataEnvelope:
        raise AssertionError(f"Factor research must not request fundamentals for {symbol}.")

    async def capital_flow(self, symbol: str) -> DataEnvelope:
        raise AssertionError(f"Factor research must not request capital flow for {symbol}.")

    async def news(self, symbol: str, limit: int = 20) -> DataEnvelope:
        del limit
        raise AssertionError(f"Factor research must not request news for {symbol}.")


class FixtureFactorRouter:
    def __init__(self) -> None:
        self.probe = _ConcurrencyProbe()
        self.rows_by_symbol = {symbol: _rows(index) for index, (symbol, _) in enumerate(SYMBOLS)}
        self.canonical_symbols = {symbol: symbol for symbol, _ in SYMBOLS}
        self.providers = {
            provider_name: FixtureFactorProvider(
                provider_name,
                self.rows_by_symbol,
                self.canonical_symbols,
                self.probe,
            )
            for provider_name in {provider for _, provider in SYMBOLS}
        }

    def resolve(
        self,
        symbol: str,
        requested: ProviderName,
    ) -> DataProvider:
        assert symbol in self.rows_by_symbol
        return self.providers[requested]

    def append_shared_future_days(self, count: int = 3) -> None:
        for asset_number, (symbol, _) in enumerate(SYMBOLS):
            existing = self.rows_by_symbol[symbol]
            final_day = datetime.fromisoformat(str(existing[-1]["date"])).date()
            new_days = _business_days(final_day + timedelta(days=1), count)
            for offset, day in enumerate(new_days, start=1):
                shock = Decimal("10000") * Decimal(asset_number + 1)
                open_price = shock + Decimal(offset)
                existing.append(
                    {
                        "date": datetime.combine(
                            day,
                            datetime.min.time(),
                            tzinfo=UTC,
                        ).isoformat(),
                        "open": _decimal_text(open_price),
                        "high": _decimal_text(open_price + Decimal("2")),
                        "low": _decimal_text(open_price - Decimal("2")),
                        "close": _decimal_text(open_price + Decimal("1")),
                        "volume": str(9_000_000 + asset_number * 100_000),
                    }
                )


@dataclass
class _BlockingThreadProbe:
    active: int = 0
    maximum: int = 0
    lock: Lock = field(default_factory=Lock)

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)

    def leave(self) -> None:
        with self.lock:
            self.active -= 1

    def snapshot(self) -> tuple[int, int]:
        with self.lock:
            return self.active, self.maximum


class BlockingThreadFactorProvider(FixtureFactorProvider):
    def __init__(
        self,
        name: ProviderName,
        rows_by_symbol: dict[str, list[dict[str, object]]],
        canonical_symbols: dict[str, str],
        probe: _BlockingThreadProbe,
        release: Event,
    ) -> None:
        super().__init__(
            name,
            rows_by_symbol,
            canonical_symbols,
            _ConcurrencyProbe(),
        )
        self.blocking_probe = probe
        self.release = release

    async def history_interval(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        *,
        interval: Literal["15m", "1h", "4h", "1d", "1wk"] = "1d",
    ) -> DataEnvelope:
        del start, end
        assert interval == "1d"
        return await asyncio.to_thread(self._blocking_history, symbol)

    def _blocking_history(self, symbol: str) -> DataEnvelope:
        self.blocking_probe.enter()
        try:
            if not self.release.wait(timeout=5):
                raise RuntimeError("blocking factor fixture was not released")
            return DataEnvelope(
                symbol=self.canonical_symbols[symbol],
                kind="history",
                rows=[dict(row) for row in self.rows_by_symbol[symbol]],
                citations=[
                    Citation(
                        source=f"{self.name} blocking fixture",
                        url=f"https://example.test/{self.name}/{symbol}",
                        retrieved_at=self.retrieved_at,
                        as_of=self.retrieved_at,
                    )
                ],
                metadata={
                    "finalized_bars_only": True,
                    "bar_finalization_policy": ("test_completed_daily_bars"),
                },
            )
        finally:
            self.blocking_probe.leave()


def _request_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "instruments": [{"symbol": symbol, "provider": provider} for symbol, provider in SYMBOLS],
        "factor_id": "momentum",
        "lookback": 10,
        "horizons": [1, 5],
        "quantiles": 3,
        "interval": "1d",
        "start": "2024-03-01",
        "end": "2024-06-28",
        "finalized_bars_only": True,
    }
    payload.update(changes)
    return payload


def _client(
    tmp_path: Path,
    *,
    factor_research_max_concurrency: int = 1,
    factor_research_provider_max_concurrency: int = 8,
    factor_research_deadline_seconds: float = 45.0,
) -> tuple[TestClient, FixtureFactorRouter]:
    app = create_app(
        Settings(
            database_path=str(tmp_path / "factor-api.db"),
            cache_path=str(tmp_path / "factor-cache.db"),
            factor_research_max_concurrency=factor_research_max_concurrency,
            factor_research_provider_max_concurrency=(factor_research_provider_max_concurrency),
            factor_research_deadline_seconds=factor_research_deadline_seconds,
            monitor_scheduler_enabled=False,
            paper_scheduler_enabled=False,
            portfolio_opening_scheduler_enabled=False,
            portfolio_settlement_scheduler_enabled=False,
        )
    )
    router = FixtureFactorRouter()
    app.state.providers = router
    return TestClient(app), router


def test_factor_research_returns_traceable_deterministic_evidence(
    tmp_path: Path,
) -> None:
    api, provider_router = _client(tmp_path)

    first_response = api.post(
        "/api/v1/factors/research",
        json=_request_payload(),
    )
    second_response = api.post(
        "/api/v1/factors/research",
        json=_request_payload(),
    )

    assert first_response.status_code == 200, first_response.json()
    assert second_response.status_code == 200, second_response.json()
    payload = first_response.json()
    repeated = second_response.json()
    assert provider_router.probe.maximum > 1
    assert payload["run_id"] != repeated["run_id"]
    assert payload["run_manifest"]["manifest_id"] != repeated["run_manifest"]["manifest_id"]
    assert payload["run_as_of_at"] == payload["recipe"]["run_as_of_at"]
    assert payload["run_as_of_at"] != repeated["run_as_of_at"]
    assert payload["time_series"] == repeated["time_series"]
    assert payload["diagnostics_by_horizon"] == repeated["diagnostics_by_horizon"]
    assert payload["recipe"]["feature_information_cutoff"] == "d-1"
    assert payload["recipe"]["label_formula"] == "open[d+1] to close[d+h]"
    assert payload["recipe"]["execution_delay"] == "one exact shared daily bar"
    assert payload["recipe"]["label_available_at"] == (
        "maximum modeled availability of all entry and exit rows"
    )
    assert payload["recipe"]["feature_availability"] == (
        "maximum modeled availability across every row in each "
        "declared lookback window and the full basket"
    )
    assert payload["recipe"]["fundamentals_enabled"] is False
    assert payload["coverage"]["forward_fill"] is False
    assert payload["coverage"]["alignment"] == ("exact_shared_utc_day_intersection")
    assert set(payload["diagnostics_by_horizon"]) == {"1", "5"}
    assert set(payload["time_series"]) == {"1", "5"}
    assert set(payload["latest_scores"]) == {"1", "5"}
    assert isinstance(
        payload["latest_scores"]["1"]["scores"][0]["factor_value"],
        str,
    )
    assert isinstance(
        payload["diagnostics_by_horizon"]["1"]["diagnostics"]["pearson_ic_mean"],
        str,
    )
    for series_row in payload["time_series"]["1"]:
        assert datetime.fromisoformat(series_row["feature_cutoff_at"]) < datetime.fromisoformat(
            series_row["entry_at"]
        )
        observation_label_times = []
        for observation in series_row["observations"]:
            entry_available_at = datetime.fromisoformat(observation["entry_available_at"])
            exit_available_at = datetime.fromisoformat(observation["exit_available_at"])
            label_available_at = datetime.fromisoformat(observation["label_available_at"])
            assert label_available_at == max(
                entry_available_at,
                exit_available_at,
            )
            observation_label_times.append(label_available_at)
        assert datetime.fromisoformat(series_row["label_available_at"]) == max(
            observation_label_times
        )

    limitations = payload["limitations"]
    assert limitations["universe_semantics"] == "fixed_user_selected_ex_post"
    assert limitations["point_in_time_universe"] is False
    assert limitations["source_availability"] == "provider_policy_estimate"
    assert limitations["point_in_time_validation_passed"] is False
    assert limitations["survivorship_bias_controlled"] is False
    assert limitations["tradable_conclusion"] is False

    snapshots = payload["dataset_snapshots"]
    assert len(snapshots) == len(SYMBOLS)
    assert [item["role"] for item in snapshots] == ["universe"] * len(SYMBOLS)
    assert [item["ordinal"] for item in snapshots] == list(range(len(SYMBOLS)))
    assert all(item["snapshot"]["quality_status"] == "passed" for item in snapshots)
    assert all(len(item["evidence_id"]) == 64 for item in snapshots)
    assert all(
        item["evidence_id"] == payload["coverage"]["per_asset"][item["ordinal"]]["evidence_id"]
        for item in snapshots
    )
    assert payload["run_manifest"]["run_kind"] == "factor_research"
    assert payload["run_manifest"]["cost_model_hash"]
    assert len(payload["run_manifest"]["datasets"]) == len(SYMBOLS)

    application = cast(FastAPI, api.app)
    receipt = application.state.research_run_store.get_run(payload["run_id"])
    assert receipt.run_id == payload["run_id"]
    assert receipt.manifest.manifest_id == payload["run_manifest"]["manifest_id"]
    assert [item.role for item in receipt.datasets] == ["universe"] * len(SYMBOLS)
    assert [item.ordinal for item in receipt.datasets] == list(range(len(SYMBOLS)))
    assert receipt.cost_model == {
        "kind": "none",
        "reason": "diagnostic_factor_study_not_execution_backtest",
    }
    assert receipt.manifest.execution_model == (
        "factor_available_d_minus_1__one_shared_bar_delay__label_open_d_plus_1_to_close_d_plus_h"
    )
    assert receipt.engine_config["dataset_evidence"] == [
        {
            "position": item["ordinal"],
            "role": item["role"],
            "ordinal": item["ordinal"],
            "snapshot_id": item["snapshot"]["snapshot_id"],
            "evidence_id": item["evidence_id"],
        }
        for item in snapshots
    ]
    assert receipt.result["diagnostics_by_horizon"] == payload["diagnostics_by_horizon"]
    assert receipt.result["run_as_of_at"] == payload["run_as_of_at"]
    assert receipt.parameters["run_as_of_at"] == payload["run_as_of_at"]
    assert receipt.engine_config["run_as_of_at"] == payload["run_as_of_at"]
    assert "citations" not in receipt.result
    assert "dataset_snapshots" not in receipt.result
    assert "run_id" not in receipt.result


def test_factor_catalog_exposes_server_owned_contract(tmp_path: Path) -> None:
    api, _ = _client(tmp_path)

    response = api.get("/api/v1/factors/catalog")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == 1
    assert [item["factor_id"] for item in payload["recipes"]] == [
        "momentum",
        "reversal",
        "low_volatility",
        "volume_surprise",
    ]
    assert all(item["formula"] and item["direction"] for item in payload["recipes"])
    assert payload["defaults"]["interval"] == "1d"
    assert payload["limits"]["intervals"] == ["1d"]
    assert payload["limits"]["horizons"]["maximum_items"] == 4
    assert payload["limits"]["horizons"]["maximum_value"] == 63
    assert payload["limits"]["maximum_candidate_observation_pairs"] == 120_000
    assert payload["limits"]["quantiles"]["requires_instruments_at_least_quantiles"] is True
    assert payload["limits"]["universe"] == "explicit_fixed_user_selection"
    assert payload["validity"]["point_in_time_universe"] is False
    assert payload["validity"]["survivorship_bias_controlled"] is False
    assert payload["validity"]["tradable_conclusion"] is False


def test_factor_research_waits_one_shared_bar_after_explicit_finalization(
    tmp_path: Path,
) -> None:
    api, provider_router = _client(tmp_path)
    for row in provider_router.rows_by_symbol["GAMMAUSDT"]:
        opened_at = datetime.fromisoformat(str(row["date"]))
        row["finalized_at"] = (opened_at + timedelta(days=1, minutes=2)).isoformat()

    response = api.post(
        "/api/v1/factors/research",
        json=_request_payload(horizons=[1]),
    )

    assert response.status_code == 200, response.json()
    payload = response.json()
    first = payload["time_series"]["1"][0]
    period_at = datetime.fromisoformat(first["period_at"])
    entry_at = datetime.fromisoformat(first["entry_at"])
    label_available_at = datetime.fromisoformat(first["label_available_at"])
    assert period_at.minute == 2
    assert period_at < entry_at < label_available_at
    binance_coverage = next(
        item for item in payload["coverage"]["per_asset"] if item["provider"] == "binance"
    )
    assert binance_coverage["availability_sources"] == ["row_finalized_at"]


def test_factor_research_uses_entire_lookback_window_availability(
    tmp_path: Path,
) -> None:
    api, provider_router = _client(tmp_path)
    request_payload = _request_payload(horizons=[1])
    baseline = api.post(
        "/api/v1/factors/research",
        json=request_payload,
    )
    assert baseline.status_code == 200, baseline.json()
    baseline_series = baseline.json()["time_series"]["1"]
    target = baseline_series[0]
    target_decision = datetime.fromisoformat(target["nominal_decision_at"]).date()
    shared_days = _business_days(date(2024, 1, 2), 120)
    decision_index = shared_days.index(target_decision)
    dependency_day = shared_days[decision_index - int(request_payload["lookback"]) - 1]
    dependency_row = next(
        row
        for row in provider_router.rows_by_symbol["ALPHA"]
        if datetime.fromisoformat(str(row["date"])).date() == dependency_day
    )
    dependency_row["available_at"] = "2099-01-01T00:00:00+00:00"

    response = api.post(
        "/api/v1/factors/research",
        json=request_payload,
    )

    assert response.status_code == 200, response.json()
    payload = response.json()
    returned_decisions = {
        datetime.fromisoformat(row["nominal_decision_at"]).date()
        for row in payload["time_series"]["1"]
    }
    assert target_decision not in returned_decisions
    assert (
        payload["coverage"]["dropped_reason_counts_by_horizon"]["1"][
            "feature_not_available_as_of_research_start"
        ]
        > 0
    )
    assert all(
        datetime.fromisoformat(observation["feature_available_at"])
        < datetime.fromisoformat(row["entry_at"])
        for row in payload["time_series"]["1"]
        for observation in row["observations"]
    )


def test_factor_research_never_persists_a_future_finalized_label(
    tmp_path: Path,
) -> None:
    api, provider_router = _client(tmp_path)
    request_payload = _request_payload(horizons=[1])
    baseline = api.post(
        "/api/v1/factors/research",
        json=request_payload,
    )
    assert baseline.status_code == 200, baseline.json()
    target = baseline.json()["time_series"]["1"][-1]
    target_decision = target["nominal_decision_at"]
    target_exit_days = {
        observation["symbol"]: datetime.fromisoformat(observation["exit_effective_at"]).date()
        for observation in target["observations"]
    }
    for symbol, _ in SYMBOLS:
        target_row = next(
            row
            for row in provider_router.rows_by_symbol[symbol]
            if datetime.fromisoformat(str(row["date"])).date() == target_exit_days[symbol]
        )
        target_row["finalized_at"] = "2099-01-01T00:00:00+00:00"

    response = api.post(
        "/api/v1/factors/research",
        json=request_payload,
    )

    assert response.status_code == 200, response.json()
    payload = response.json()
    assert target_decision not in {
        row["nominal_decision_at"] for row in payload["time_series"]["1"]
    }
    assert (
        payload["coverage"]["dropped_reason_counts_by_horizon"]["1"][
            "label_not_available_as_of_research_start"
        ]
        > 0
    )
    run_as_of_at = datetime.fromisoformat(payload["run_as_of_at"])
    assert all(
        datetime.fromisoformat(row["label_available_at"]) <= run_as_of_at
        for row in payload["time_series"]["1"]
    )
    application = cast(FastAPI, api.app)
    receipt = application.state.research_run_store.get_run(payload["run_id"])
    assert receipt.result["time_series"] == payload["time_series"]
    assert receipt.result["run_as_of_at"] == payload["run_as_of_at"]
    assert receipt.parameters["run_as_of_at"] == payload["run_as_of_at"]
    assert receipt.engine_config["run_as_of_at"] == payload["run_as_of_at"]
    assert target_decision not in {
        row["nominal_decision_at"] for row in receipt.result["time_series"]["1"]
    }


def test_changed_source_citation_creates_matching_new_evidence_receipt(
    tmp_path: Path,
) -> None:
    api, provider_router = _client(tmp_path)
    first = api.post(
        "/api/v1/factors/research",
        json=_request_payload(horizons=[1]),
    )
    assert first.status_code == 200, first.json()

    provider_router.providers["binance"].retrieved_at += timedelta(seconds=1)
    second = api.post(
        "/api/v1/factors/research",
        json=_request_payload(horizons=[1]),
    )

    assert second.status_code == 200, second.json()
    first_payload = first.json()
    second_payload = second.json()
    assert second_payload["run_id"] != first_payload["run_id"]
    assert (
        second_payload["run_manifest"]["manifest_id"]
        != first_payload["run_manifest"]["manifest_id"]
    )
    first_binance = next(
        item for item in first_payload["dataset_snapshots"] if item["provider"] == "binance"
    )
    second_binance = next(
        item for item in second_payload["dataset_snapshots"] if item["provider"] == "binance"
    )
    assert second_binance["snapshot"]["snapshot_id"] != first_binance["snapshot"]["snapshot_id"]
    assert (
        second_binance["snapshot"]["source_rows_hash"]
        == first_binance["snapshot"]["source_rows_hash"]
    )
    assert second_binance["evidence_id"] != first_binance["evidence_id"]
    returned_citation = next(
        item for item in second_payload["citations"] if item["provider"] == "binance"
    )
    application = cast(FastAPI, api.app)
    receipt = application.state.research_run_store.get_run(second_payload["run_id"])
    receipt_citation = next(
        citation
        for evidence in receipt.datasets
        if evidence.snapshot.provider == "binance"
        for citation in evidence.citations
    )
    assert returned_citation["retrieved_at"] == receipt_citation["retrieved_at"]


def test_factor_research_uses_exact_shared_days_without_forward_fill(
    tmp_path: Path,
) -> None:
    api, provider_router = _client(tmp_path)
    missing_day = date(2024, 4, 15)
    provider_router.rows_by_symbol["ALPHA"] = [
        row
        for row in provider_router.rows_by_symbol["ALPHA"]
        if datetime.fromisoformat(str(row["date"])).date() != missing_day
    ]

    response = api.post(
        "/api/v1/factors/research",
        json=_request_payload(horizons=[1]),
    )

    assert response.status_code == 200, response.json()
    payload = response.json()
    returned_periods = {
        datetime.fromisoformat(item["period_at"]).date() for item in payload["time_series"]["1"]
    }
    assert missing_day not in returned_periods
    snapshots = {
        (item["provider"], item["symbol"]): item["snapshot"]
        for item in payload["dataset_snapshots"]
    }
    coverage = {
        (item["provider"], item["symbol"]): item for item in payload["coverage"]["per_asset"]
    }
    assert {key: snapshot["row_count"] for key, snapshot in snapshots.items()} == {
        key: item["source_rows"] for key, item in coverage.items()
    }
    assert (
        snapshots[("yfinance", "ALPHA")]["row_count"] + 1
        == snapshots[("yfinance", "BETA")]["row_count"]
    )
    assert {item["aligned_rows"] for item in payload["coverage"]["per_asset"]} == {
        payload["coverage"]["common_days"]
    }
    assert {item["requested_start"] for item in snapshots.values()} == {"2024-01-27"}
    assert {item["requested_end"] for item in snapshots.values()} == {"2024-07-14"}
    assert payload["coverage"]["forward_fill"] is False


@pytest.mark.parametrize(
    "factor_id",
    ["momentum", "reversal", "low_volatility", "volume_surprise"],
)
def test_all_supported_factor_recipes_execute(
    tmp_path: Path,
    factor_id: str,
) -> None:
    api, _ = _client(tmp_path)

    response = api.post(
        "/api/v1/factors/research",
        json=_request_payload(factor_id=factor_id, horizons=[1]),
    )

    assert response.status_code == 200, response.json()
    assert response.json()["recipe"]["factor_id"] == factor_id


def test_appending_future_bars_does_not_change_existing_factor_results(
    tmp_path: Path,
) -> None:
    api, provider_router = _client(tmp_path)
    request_payload = _request_payload(
        horizons=[1, 3],
        end="2024-05-31",
    )
    before = api.post(
        "/api/v1/factors/research",
        json=request_payload,
    )
    assert before.status_code == 200, before.json()

    provider_router.append_shared_future_days()
    after = api.post(
        "/api/v1/factors/research",
        json=request_payload,
    )
    assert after.status_code == 200, after.json()

    before_payload = before.json()
    after_payload = after.json()
    assert after_payload["time_series"] == before_payload["time_series"]
    assert after_payload["diagnostics_by_horizon"] == before_payload["diagnostics_by_horizon"]
    assert after_payload["latest_scores"] == before_payload["latest_scores"]


@pytest.mark.parametrize(
    ("change", "expected_fragment"),
    [
        (
            {
                "instruments": [
                    {"symbol": symbol, "provider": provider} for symbol, provider in SYMBOLS[:2]
                ]
            },
            "at least 3",
        ),
        ({"horizons": [1, 1]}, "unique"),
        ({"horizons": ["1"]}, "integer"),
        ({"horizons": [1, 2, 3, 4, 5]}, "at most 4 items"),
        ({"quantiles": 5}, "at least quantiles"),
        ({"interval": "1h"}, "Input should be '1d'"),
        ({"factor_id": "value"}, "momentum"),
        ({"start": "2024-06-01", "end": "2024-06-01"}, "earlier"),
        ({"finalized_bars_only": False}, "Input should be True"),
    ],
)
def test_factor_research_rejects_invalid_requests(
    tmp_path: Path,
    change: dict[str, object],
    expected_fragment: str,
) -> None:
    api, _ = _client(tmp_path)

    response = api.post(
        "/api/v1/factors/research",
        json=_request_payload(**change),
    )

    assert response.status_code == 422
    assert expected_fragment in response.text


@pytest.mark.parametrize(
    ("change", "expected_fragment"),
    [
        (
            {"start": "2010-01-01", "end": "2020-01-02"},
            "10 years",
        ),
        (
            {"start": "2099-01-01", "end": "2099-02-01"},
            "future",
        ),
        (
            {"start": "0001-01-01", "end": "0001-01-02"},
            "calendar range",
        ),
    ],
)
def test_factor_research_enforces_date_resource_boundaries(
    tmp_path: Path,
    change: dict[str, object],
    expected_fragment: str,
) -> None:
    api, provider_router = _client(tmp_path)

    response = api.post(
        "/api/v1/factors/research",
        json=_request_payload(**change),
    )

    assert response.status_code == 422
    assert expected_fragment in response.text
    assert provider_router.probe.maximum == 0


def test_factor_research_rejects_prefetch_resource_amplification(
    tmp_path: Path,
) -> None:
    api, provider_router = _client(tmp_path)
    instruments = [
        {
            "symbol": f"ASSET{index}",
            "provider": "yfinance",
        }
        for index in range(20)
    ]

    response = api.post(
        "/api/v1/factors/research",
        json=_request_payload(
            instruments=instruments,
            horizons=[1, 5, 20, 63],
            quantiles=5,
            start="2018-01-01",
            end="2025-12-31",
        ),
    )

    assert response.status_code == 422
    assert "before source data is fetched" in response.json()["detail"]
    assert provider_router.probe.maximum == 0


def test_factor_research_rejects_canonical_alias_duplicates(
    tmp_path: Path,
) -> None:
    api, provider_router = _client(tmp_path)
    provider_router.canonical_symbols["BETA"] = "ALPHA"

    response = api.post(
        "/api/v1/factors/research",
        json=_request_payload(),
    )

    assert response.status_code == 422
    assert "duplicate canonical instruments" in response.json()["detail"]


def test_factor_research_fails_closed_on_internal_diagnostic_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _ = _client(tmp_path)

    def rejected_diagnostics(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ArithmeticError("diagnostic arithmetic invariant")

    monkeypatch.setattr(
        factor_service_module,
        "analyze_factor",
        rejected_diagnostics,
    )
    response = api.post(
        "/api/v1/factors/research",
        json=_request_payload(horizons=[1]),
    )

    assert response.status_code == 422
    assert "failed closed" in response.json()["detail"]
    assert "diagnostic arithmetic invariant" in response.json()["detail"]


def test_factor_research_rejects_unfinalized_and_insufficient_evidence(
    tmp_path: Path,
) -> None:
    api, provider_router = _client(tmp_path)
    provider_router.providers["yfinance"].finalized = False

    unfinalized = api.post(
        "/api/v1/factors/research",
        json=_request_payload(),
    )

    assert unfinalized.status_code == 422
    assert "finalized_bars_only=true" in unfinalized.json()["detail"]

    api, _ = _client(tmp_path / "narrow")
    insufficient = api.post(
        "/api/v1/factors/research",
        json=_request_payload(
            start="2024-03-01",
            end="2024-03-03",
            horizons=[5],
        ),
    )
    assert insufficient.status_code == 422
    assert "at least two" in insufficient.json()["detail"]


def test_factor_research_maps_provider_runtime_errors_to_bad_gateway(
    tmp_path: Path,
) -> None:
    api, provider_router = _client(tmp_path)
    provider_router.providers["binance"].error = RuntimeError("fixture upstream unavailable")

    response = api.post(
        "/api/v1/factors/research",
        json=_request_payload(),
    )

    assert response.status_code == 502
    assert "fixture upstream unavailable" in response.json()["detail"]


@pytest.mark.asyncio
async def test_factor_research_provider_failure_joins_siblings_before_releasing_capacity(
    tmp_path: Path,
) -> None:
    api, provider_router = _client(
        tmp_path,
        factor_research_max_concurrency=1,
        factor_research_deadline_seconds=2.0,
    )
    for provider in provider_router.providers.values():
        provider.delay_seconds = 0.2
    provider_router.providers["yfinance"].error = RuntimeError("fixture upstream unavailable")
    application = cast(FastAPI, api.app)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        first = await client.post(
            "/api/v1/factors/research",
            json=_request_payload(horizons=[1]),
        )
        assert provider_router.probe.active == 0
        first_peak = provider_router.probe.maximum

        second = await client.post(
            "/api/v1/factors/research",
            json=_request_payload(horizons=[1]),
        )
        assert provider_router.probe.active == 0

    assert first.status_code == 502
    assert second.status_code == 502
    assert 0 < first_peak <= len(SYMBOLS)
    assert provider_router.probe.maximum == first_peak
    capacity = cast(asyncio.Semaphore, application.state.factor_research_semaphore)
    assert not capacity.locked()
    with sqlite3.connect(tmp_path / "factor-api.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_factor_research_persistence_rechecks_deadline_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _ = _client(
        tmp_path,
        factor_research_max_concurrency=1,
        factor_research_deadline_seconds=0.3,
    )
    application = cast(FastAPI, api.app)
    store = cast(ResearchRunStore, application.state.research_run_store)
    original_create_run = store.create_run
    persistence_calls = 0

    def delayed_create_run(
        *,
        manifest: RunManifest,
        datasets: Sequence[ResearchDatasetEvidence],
        run_request: object,
        result: object,
        parameters: object,
        engine_config: object,
        cost_model: object,
        deadline_monotonic: float | None = None,
    ) -> ResearchRunReceipt:
        nonlocal persistence_calls
        persistence_calls += 1
        assert deadline_monotonic is not None
        remaining = deadline_monotonic - monotonic()
        if remaining > 0:
            sleep(remaining + 0.03)
        return original_create_run(
            manifest=manifest,
            datasets=datasets,
            run_request=run_request,
            result=result,
            parameters=parameters,
            engine_config=engine_config,
            cost_model=cost_model,
            deadline_monotonic=deadline_monotonic,
        )

    monkeypatch.setattr(store, "create_run", delayed_create_run)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/v1/factors/research",
            json=_request_payload(
                start="2024-05-01",
                end="2024-05-20",
                horizons=[1],
            ),
        )

    assert persistence_calls == 1
    assert response.status_code == 504
    assert response.json() == {"detail": "Factor research request exceeded its server deadline."}
    capacity = cast(asyncio.Semaphore, application.state.factor_research_semaphore)
    assert not capacity.locked()
    with sqlite3.connect(tmp_path / "factor-api.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 0


def test_factor_research_security_settings_are_bounded() -> None:
    defaults = Settings(_env_file=None)

    assert defaults.factor_research_max_concurrency == 1
    assert defaults.factor_research_provider_max_concurrency == 8
    assert defaults.factor_research_deadline_seconds == 45.0
    parsed = Settings(
        _env_file=None,
        factor_research_max_concurrency="2",  # type: ignore[arg-type]
        factor_research_provider_max_concurrency="4",  # type: ignore[arg-type]
        factor_research_deadline_seconds="12.5",  # type: ignore[arg-type]
    )
    assert parsed.factor_research_max_concurrency == 2
    assert parsed.factor_research_provider_max_concurrency == 4
    assert parsed.factor_research_deadline_seconds == 12.5
    with pytest.raises(ValidationError):
        Settings(_env_file=None, factor_research_max_concurrency=0)
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            factor_research_provider_max_concurrency=0,
        )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, factor_research_deadline_seconds=0.0)


@pytest.mark.asyncio
async def test_factor_research_uses_one_shared_application_gate(
    tmp_path: Path,
) -> None:
    api, provider_router = _client(
        tmp_path,
        factor_research_max_concurrency=1,
        factor_research_deadline_seconds=2.0,
    )
    for provider in provider_router.providers.values():
        provider.delay_seconds = 0.05
    application = cast(FastAPI, api.app)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        first, second = await asyncio.gather(
            client.post(
                "/api/v1/factors/research",
                json=_request_payload(horizons=[1]),
            ),
            client.post(
                "/api/v1/factors/research",
                json=_request_payload(horizons=[1]),
            ),
        )

    assert first.status_code == 200, first.json()
    assert second.status_code == 200, second.json()
    assert first.json()["run_id"] != second.json()["run_id"]
    assert datetime.fromisoformat(first.json()["run_as_of_at"]) < datetime.fromisoformat(
        second.json()["run_as_of_at"]
    )
    assert (
        first.json()["run_manifest"]["manifest_id"] != second.json()["run_manifest"]["manifest_id"]
    )
    assert provider_router.probe.maximum == len(SYMBOLS)


@pytest.mark.asyncio
async def test_factor_research_busy_capacity_returns_stable_503(
    tmp_path: Path,
) -> None:
    api, provider_router = _client(
        tmp_path,
        factor_research_max_concurrency=1,
        factor_research_deadline_seconds=0.02,
    )
    application = cast(FastAPI, api.app)
    capacity = cast(asyncio.Semaphore, application.state.factor_research_semaphore)
    await capacity.acquire()
    transport = httpx.ASGITransport(app=application)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/factors/research",
                json=_request_payload(horizons=[1]),
            )
    finally:
        capacity.release()

    assert response.status_code == 503
    assert response.json() == {"detail": "Factor research capacity is busy; retry later."}
    assert response.headers["retry-after"] == "1"
    assert provider_router.probe.maximum == 0
    with sqlite3.connect(tmp_path / "factor-api.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_factor_research_fetch_timeout_returns_504_without_receipt(
    tmp_path: Path,
) -> None:
    api, provider_router = _client(
        tmp_path,
        factor_research_max_concurrency=1,
        factor_research_deadline_seconds=0.02,
    )
    for provider in provider_router.providers.values():
        provider.delay_seconds = 0.2
    application = cast(FastAPI, api.app)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        first = await client.post(
            "/api/v1/factors/research",
            json=_request_payload(horizons=[1]),
        )
        assert provider_router.probe.active == len(SYMBOLS)
        first_peak = provider_router.probe.maximum

        second = await client.post(
            "/api/v1/factors/research",
            json=_request_payload(horizons=[1]),
        )
        assert provider_router.probe.active <= 8

    assert first.status_code == 504
    assert second.status_code == 504
    assert first.json() == {"detail": "Factor research request exceeded its server deadline."}
    assert second.json() == {"detail": "Factor research request exceeded its server deadline."}
    assert first_peak == len(SYMBOLS)
    assert first_peak <= provider_router.probe.maximum <= 8
    capacity = cast(asyncio.Semaphore, application.state.factor_research_semaphore)
    assert not capacity.locked()
    provider_capacity = cast(
        asyncio.Semaphore,
        application.state.factor_research_provider_semaphore,
    )
    assert provider_capacity.locked()
    await asyncio.sleep(0.25)
    assert provider_router.probe.active == 0
    assert not provider_capacity.locked()
    with sqlite3.connect(tmp_path / "factor-api.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_factor_research_timeout_keeps_blocking_threads_globally_bounded(
    tmp_path: Path,
) -> None:
    api, provider_router = _client(
        tmp_path,
        factor_research_max_concurrency=1,
        factor_research_provider_max_concurrency=2,
        factor_research_deadline_seconds=0.04,
    )
    blocking_probe = _BlockingThreadProbe()
    release = Event()
    provider_router.providers = {
        provider_name: BlockingThreadFactorProvider(
            provider_name,
            provider_router.rows_by_symbol,
            provider_router.canonical_symbols,
            blocking_probe,
            release,
        )
        for provider_name in {provider for _, provider in SYMBOLS}
    }
    application = cast(FastAPI, api.app)
    transport = httpx.ASGITransport(app=application)

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            responses = []
            for _ in range(4):
                responses.append(
                    await client.post(
                        "/api/v1/factors/research",
                        json=_request_payload(horizons=[1]),
                    )
                )

        assert {response.status_code for response in responses} == {504}
        active, maximum = blocking_probe.snapshot()
        assert active == 2
        assert maximum == 2
        provider_capacity = cast(
            asyncio.Semaphore,
            application.state.factor_research_provider_semaphore,
        )
        assert provider_capacity.locked()
        provider_tasks = cast(
            set[asyncio.Task[DataEnvelope]],
            application.state.factor_research_provider_tasks,
        )
        assert len(provider_tasks) == 2
        with sqlite3.connect(tmp_path / "factor-api.db") as connection:
            assert connection.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 0
    finally:
        release.set()

    for _ in range(100):
        if blocking_probe.snapshot()[0] == 0 and not provider_tasks:
            break
        await asyncio.sleep(0.01)
    assert blocking_probe.snapshot() == (0, 2)
    assert provider_tasks == set()
    assert not provider_capacity.locked()
