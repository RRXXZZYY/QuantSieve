import asyncio
import sqlite3
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Literal

import pandas as pd
import pytest
import quantsieve_api.routers.market as market_router
import quantsieve_engine.optimization as optimization_module
from fastapi.testclient import TestClient
from quantsieve_api.config import Settings
from quantsieve_api.main import create_app
from quantsieve_api.routers.market import (
    _annual_periods,
    _development_market_regime,
    _discovery_horizon,
    _discovery_shortlist_sort_key,
    _feasible_discovery_strategy_families,
    _history_capabilities,
    _history_for_backtest,
    _minimum_trade_events_for_window,
    _screening_constraint_summary,
    _screening_objective_score,
    _screening_sort_key,
    _select_diverse_discovery_shortlist,
    _strategy_regime_fit,
    _trade_quality,
    _validate_discovery_holdout,
    _with_warmup_metadata,
)
from quantsieve_api.routers.monitor import _curate_events
from quantsieve_api.schemas import DiscoverBacktestRequest
from quantsieve_engine import (
    PASSIVE_STRATEGY_IDS,
    STRATEGIES,
    BacktestConfig,
    BacktestMetrics,
    score_optimization_metrics,
)
from quantsieve_monitor import MonitorEvent
from quantsieve_monitor.models import EventKind
from quantsieve_providers import (
    BinanceSettlementDailyBarEvidence,
    BinanceSpotTradingRules,
    Citation,
    DataEnvelope,
    ExecutionQuote,
    Instrument,
)
from quantsieve_providers.base import DataProvider


class FakeProvider(DataProvider):
    name = "fake"

    def __init__(self) -> None:
        self.extra_periods = 0
        self.start_offset = 0
        self.history_error: Exception | None = None

    async def history(
        self, symbol: str, start: date | None = None, end: date | None = None
    ) -> DataEnvelope:
        if self.history_error is not None:
            raise self.history_error
        periods = (160 if symbol == "DISCOVER" else 100) + self.extra_periods
        dates = pd.date_range("2025-01-01", periods=periods, freq="B", tz=UTC)
        dates = dates + pd.offsets.BDay(self.start_offset)
        rows = [
            {
                "date": value.isoformat(),
                "open": 100 + index,
                "high": 102 + index,
                "low": 99 + index,
                "close": 101 + index,
                "volume": 1_000_000,
            }
            for index, value in enumerate(dates)
        ]
        return DataEnvelope(
            symbol=symbol,
            kind="history",
            rows=rows,
            citations=[
                Citation(
                    source="Test fixture",
                    url="https://example.com/data",
                    as_of=datetime.now(UTC),
                )
            ],
        )

    async def quote(self, symbol: str) -> DataEnvelope:
        return await self.history(symbol)

    async def fundamentals(self, symbol: str) -> DataEnvelope:
        return DataEnvelope(symbol=symbol, kind="fundamentals", rows=[], citations=[])

    async def capital_flow(self, symbol: str) -> DataEnvelope:
        return DataEnvelope(symbol=symbol, kind="capital_flow", rows=[], citations=[])

    async def news(self, symbol: str, limit: int = 20) -> DataEnvelope:
        return DataEnvelope(symbol=symbol, kind="news", rows=[], citations=[])

    async def search(self, query: str, limit: int = 10) -> list[Instrument]:
        symbol = query.strip().upper()
        if not symbol or limit < 1:
            return []
        market: Literal["CN", "CRYPTO", "US"]
        provider: Literal["akshare", "binance", "yfinance"]
        if symbol.isdigit():
            market = "CN"
            currency = "CNY"
            provider = "akshare"
        elif symbol.endswith("USDT"):
            market = "CRYPTO"
            currency = "USDT"
            provider = "binance"
        else:
            market = "US"
            currency = "USD"
            provider = "yfinance"
        return [
            Instrument(
                symbol=symbol,
                name=f"{symbol} fixture",
                market=market,
                exchange="Test exchange",
                currency=currency,
                provider=provider,
            )
        ]


class FakeRouter:
    def __init__(self) -> None:
        self.provider = FakeProvider()

    def resolve(self, symbol: str, requested: str = "auto") -> DataProvider:
        return self.provider

    async def search(
        self,
        query: str = "",
        *,
        market: Literal["all", "CN", "US"] = "all",
        limit: int = 10,
    ) -> list[Instrument]:
        del query, market
        return [
            Instrument(
                symbol="300750",
                name="宁德时代",
                market="CN",
                exchange="深圳证券交易所",
                currency="CNY",
                provider="akshare",
            )
        ][:limit]


class FakeBinanceProvider(FakeProvider):
    name = "binance"


class FakeBinanceRouter(FakeRouter):
    def __init__(self) -> None:
        self.provider = FakeBinanceProvider()


class OfflineOpeningQuoteProvider:
    async def execution_quote(
        self,
        symbol: str,
        *,
        rules: BinanceSpotTradingRules,
    ) -> ExecutionQuote:
        del symbol, rules
        raise AssertionError("Generic API tests must not request live opening quotes.")


class OfflineSettlementHistoryProvider:
    async def settlement_daily_bar(
        self,
        symbol: str,
        *,
        session: date,
        deadline: float,
    ) -> BinanceSettlementDailyBarEvidence:
        del symbol, session, deadline
        raise AssertionError(
            "Generic API tests must not request live settlement evidence."
        )


def client(tmp_path, *, build_version: str = "0.1.0") -> TestClient:
    app = create_app(
        Settings(
            build_version=build_version,
            database_path=str(tmp_path / "data.db"),
            cache_path=str(tmp_path / "cache.db"),
        ),
        portfolio_opening_provider=OfflineOpeningQuoteProvider(),
        portfolio_settlement_provider=OfflineSettlementHistoryProvider(),
    )
    fake_router = FakeRouter()
    app.state.providers = fake_router
    app.state.paper_track_scheduler.providers = fake_router
    return TestClient(app)


def test_market_history_distinguishes_request_limits_from_upstream_failures(
    tmp_path,
) -> None:
    api = client(tmp_path)
    provider = api.app.state.providers.provider

    provider.history_error = ValueError("Requested history window exceeds the provider limit.")
    invalid_window = api.get(
        "/api/v1/market/600519/history?provider=akshare&interval=1d"
    )

    assert invalid_window.status_code == 422
    assert invalid_window.json() == {
        "detail": "Requested history window exceeds the provider limit."
    }

    provider.history_error = RuntimeError("upstream minute source unavailable")
    upstream_failure = api.get(
        "/api/v1/market/600519/history?provider=akshare&interval=1d"
    )

    assert upstream_failure.status_code == 502
    assert upstream_failure.json() == {"detail": "upstream minute source unavailable"}


def test_discovery_distinguishes_upstream_history_failures(tmp_path) -> None:
    api = client(tmp_path)
    api.app.state.providers.provider.history_error = RuntimeError(
        "upstream minute source unavailable"
    )

    response = api.post(
        "/api/v1/backtests/discover",
        json={"symbol": "600519", "provider": "akshare"},
    )

    assert response.status_code == 502, response.json()
    assert response.json() == {"detail": "upstream minute source unavailable"}


@pytest.mark.parametrize(
    ("failed_scheduler", "expected_events"),
    [
        (
            None,
            [
                "start:monitor",
                "start:paper",
                "start:portfolio-opening",
                "start:portfolio-settlement",
                "stop:portfolio-settlement",
                "stop:portfolio-opening",
                "stop:paper",
                "stop:monitor",
            ],
        ),
        (
            "monitor",
            [
                "start:monitor",
                "stop:portfolio-settlement",
                "stop:portfolio-opening",
                "stop:paper",
                "stop:monitor",
            ],
        ),
        (
            "paper",
            [
                "start:monitor",
                "start:paper",
                "stop:portfolio-settlement",
                "stop:portfolio-opening",
                "stop:paper",
                "stop:monitor",
            ],
        ),
        (
            "portfolio-opening",
            [
                "start:monitor",
                "start:paper",
                "start:portfolio-opening",
                "stop:portfolio-settlement",
                "stop:portfolio-opening",
                "stop:paper",
                "stop:monitor",
            ],
        ),
        (
            "portfolio-settlement",
            [
                "start:monitor",
                "start:paper",
                "start:portfolio-opening",
                "start:portfolio-settlement",
                "stop:portfolio-settlement",
                "stop:portfolio-opening",
                "stop:paper",
                "stop:monitor",
            ],
        ),
    ],
)
def test_lifespan_stops_schedulers_in_reverse_order(
    tmp_path,
    failed_scheduler: str | None,
    expected_events: list[str],
) -> None:
    app = create_app(
        Settings(
            build_version="lifespan-test",
            database_path=str(tmp_path / "lifespan.db"),
            cache_path=str(tmp_path / "lifespan-cache.db"),
        )
    )
    events: list[str] = []

    class SchedulerProbe:
        def __init__(self, name: str, *, fail_start: bool = False) -> None:
            self.name = name
            self.fail_start = fail_start

        async def start(self) -> None:
            events.append(f"start:{self.name}")
            if self.fail_start:
                raise RuntimeError(f"{self.name} failed to start")

        async def stop(self) -> None:
            events.append(f"stop:{self.name}")

    app.state.monitor_scheduler = SchedulerProbe("monitor")
    app.state.paper_track_scheduler = SchedulerProbe(
        "paper",
        fail_start=failed_scheduler == "paper",
    )
    app.state.portfolio_opening_scheduler = SchedulerProbe(
        "portfolio-opening",
        fail_start=failed_scheduler == "portfolio-opening",
    )
    app.state.portfolio_settlement_scheduler = SchedulerProbe(
        "portfolio-settlement",
        fail_start=failed_scheduler == "portfolio-settlement",
    )
    app.state.monitor_scheduler.fail_start = failed_scheduler == "monitor"

    if failed_scheduler is not None:
        with (
            pytest.raises(
                RuntimeError,
                match=rf"{failed_scheduler} failed to start",
            ),
            TestClient(app),
        ):
            pass
    else:
        with TestClient(app) as api:
            assert api.get("/health").status_code == 200

    assert events == expected_events


def test_lifespan_preserves_start_error_when_cleanup_also_fails(tmp_path) -> None:
    app = create_app(
        Settings(
            build_version="lifespan-cleanup-test",
            database_path=str(tmp_path / "lifespan-cleanup.db"),
            cache_path=str(tmp_path / "lifespan-cleanup-cache.db"),
        )
    )
    events: list[str] = []

    class SchedulerProbe:
        def __init__(
            self,
            name: str,
            *,
            fail_start: bool = False,
            fail_stop: bool = False,
        ) -> None:
            self.name = name
            self.fail_start = fail_start
            self.fail_stop = fail_stop

        async def start(self) -> None:
            events.append(f"start:{self.name}")
            if self.fail_start:
                raise RuntimeError("primary startup failure")

        async def stop(self) -> None:
            events.append(f"stop:{self.name}")
            if self.fail_stop:
                raise LookupError(f"{self.name} cleanup failure")

    app.state.monitor_scheduler = SchedulerProbe("monitor", fail_stop=True)
    app.state.paper_track_scheduler = SchedulerProbe(
        "paper",
        fail_start=True,
        fail_stop=True,
    )
    app.state.portfolio_opening_scheduler = SchedulerProbe(
        "portfolio-opening",
        fail_stop=True,
    )
    app.state.portfolio_settlement_scheduler = SchedulerProbe(
        "portfolio-settlement",
        fail_stop=True,
    )

    with (
        pytest.raises(RuntimeError, match="primary startup failure") as captured,
        TestClient(app),
    ):
        pass

    assert events == [
        "start:monitor",
        "start:paper",
        "stop:portfolio-settlement",
        "stop:portfolio-opening",
        "stop:paper",
        "stop:monitor",
    ]
    assert captured.value.__notes__ == [
        "Scheduler cleanup also failed: portfolio-settlement (LookupError), "
        "portfolio-opening (LookupError), paper (LookupError), "
        "monitor (LookupError)."
    ]


def test_portfolio_opening_settings_are_disabled_and_validated_by_default(
    tmp_path,
) -> None:
    settings = Settings(
        build_version="opening-settings-test",
        database_path=str(tmp_path / "opening-settings.db"),
        cache_path=str(tmp_path / "opening-settings-cache.db"),
    )
    app = create_app(settings)

    assert settings.portfolio_opening_scheduler_enabled is False
    assert app.state.portfolio_opening_scheduler.status.enabled is False
    assert app.state.portfolio_opening_service.quote_deadline_seconds == 4.0
    assert app.state.portfolio_opening_service.lease_for == timedelta(seconds=15)
    with sqlite3.connect(settings.database_path) as connection:
        schema_versions = connection.execute(
            """
            SELECT version
            FROM app_schema_migrations
            WHERE component = 'portfolio_paper'
            ORDER BY version
            """
        ).fetchall()
    assert schema_versions == [(1,), (2,), (3,)]

    with pytest.raises(ValueError, match="at least five seconds"):
        Settings(
            portfolio_opening_quote_deadline_seconds=10.0,
            portfolio_opening_lease_seconds=14.0,
        )
    with pytest.raises(ValueError):
        Settings(portfolio_opening_poll_seconds=0.5)
    with pytest.raises(ValueError):
        Settings(portfolio_opening_scheduler_enabled=1)
    with pytest.raises(ValueError):
        Settings(portfolio_opening_poll_seconds=True)
    assert Settings(
        portfolio_opening_quote_deadline_seconds="4"
    ).portfolio_opening_quote_deadline_seconds == 4.0


def test_portfolio_opening_scheduler_can_be_enabled_from_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QUANTSIEVE_PORTFOLIO_OPENING_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("QUANTSIEVE_PORTFOLIO_OPENING_POLL_SECONDS", "7.5")
    monkeypatch.setenv(
        "QUANTSIEVE_PORTFOLIO_OPENING_QUOTE_DEADLINE_SECONDS",
        "3.5",
    )
    monkeypatch.setenv("QUANTSIEVE_PORTFOLIO_OPENING_LEASE_SECONDS", "9")

    settings = Settings()

    assert settings.portfolio_opening_scheduler_enabled is True
    assert settings.portfolio_opening_poll_seconds == 7.5
    assert settings.portfolio_opening_quote_deadline_seconds == 3.5
    assert settings.portfolio_opening_lease_seconds == 9.0


def test_portfolio_settlement_settings_are_default_off_and_fail_closed(
    tmp_path,
) -> None:
    provider = OfflineSettlementHistoryProvider()
    settings = Settings(
        build_version="settlement-settings-test",
        database_path=str(tmp_path / "settlement-settings.db"),
        cache_path=str(tmp_path / "settlement-settings-cache.db"),
    )
    app = create_app(
        settings,
        portfolio_settlement_provider=provider,
    )

    assert settings.portfolio_settlement_scheduler_enabled is False
    assert settings.portfolio_settlement_poll_seconds == 60.0
    assert settings.portfolio_settlement_history_deadline_seconds == 20.0
    assert settings.portfolio_settlement_lease_seconds == 30.0
    assert app.state.portfolio_settlement_provider is provider
    assert app.state.portfolio_settlement_scheduler.status.enabled is False
    assert (
        app.state.portfolio_settlement_service.history_deadline_seconds
        == 20.0
    )
    assert app.state.portfolio_settlement_service.lease_for == timedelta(
        seconds=30
    )
    default_app = create_app(
        Settings(
            build_version="settlement-default-provider-test",
            database_path=str(tmp_path / "settlement-default-provider.db"),
            cache_path=str(tmp_path / "settlement-default-provider-cache.db"),
        )
    )
    assert (
        default_app.state.portfolio_settlement_provider
        is default_app.state.providers.binance_provider
    )

    with pytest.raises(ValueError, match="at least five seconds"):
        Settings(
            portfolio_settlement_history_deadline_seconds=20.0,
            portfolio_settlement_lease_seconds=24.999,
        )
    with pytest.raises(ValueError):
        Settings(portfolio_settlement_poll_seconds=0.999)
    with pytest.raises(ValueError):
        Settings(portfolio_settlement_poll_seconds=3600.001)
    with pytest.raises(ValueError):
        Settings(portfolio_settlement_history_deadline_seconds=True)
    with pytest.raises(ValueError):
        Settings(portfolio_settlement_lease_seconds=3600.001)
    with pytest.raises(ValueError):
        Settings(portfolio_settlement_scheduler_enabled=1)
    production = Settings(
        environment="production",
        build_version="settlement-production-test",
        portfolio_settlement_scheduler_enabled=True,
    )
    assert production.portfolio_settlement_scheduler_enabled is True

    parsed = Settings(
        environment="test",
        portfolio_settlement_scheduler_enabled=" TrUe ",
        portfolio_settlement_poll_seconds=" 75 ",
        portfolio_settlement_history_deadline_seconds=" 21.5 ",
        portfolio_settlement_lease_seconds=" 27 ",
    )
    assert parsed.portfolio_settlement_scheduler_enabled is True
    assert parsed.portfolio_settlement_poll_seconds == 75.0
    assert parsed.portfolio_settlement_history_deadline_seconds == 21.5
    assert parsed.portfolio_settlement_lease_seconds == 27.0


def test_portfolio_settlement_scheduler_can_be_enabled_from_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QUANTSIEVE_ENVIRONMENT", "test")
    monkeypatch.setenv(
        "QUANTSIEVE_PORTFOLIO_SETTLEMENT_SCHEDULER_ENABLED",
        "true",
    )
    monkeypatch.setenv("QUANTSIEVE_PORTFOLIO_SETTLEMENT_POLL_SECONDS", "90")
    monkeypatch.setenv(
        "QUANTSIEVE_PORTFOLIO_SETTLEMENT_HISTORY_DEADLINE_SECONDS",
        "22.5",
    )
    monkeypatch.setenv(
        "QUANTSIEVE_PORTFOLIO_SETTLEMENT_LEASE_SECONDS",
        "28",
    )

    settings = Settings()

    assert settings.portfolio_settlement_scheduler_enabled is True
    assert settings.portfolio_settlement_poll_seconds == 90.0
    assert settings.portfolio_settlement_history_deadline_seconds == 22.5
    assert settings.portfolio_settlement_lease_seconds == 28.0


def test_real_portfolio_opening_scheduler_follows_app_lifespan(tmp_path) -> None:
    app = create_app(
        Settings(
            build_version="opening-lifespan-test",
            database_path=str(tmp_path / "opening-lifespan.db"),
            cache_path=str(tmp_path / "opening-lifespan-cache.db"),
            portfolio_opening_scheduler_enabled=True,
            portfolio_opening_poll_seconds=3600.0,
        )
    )
    scheduler = app.state.portfolio_opening_scheduler

    assert scheduler.status.running is False
    with TestClient(app) as api:
        assert api.get("/health").status_code == 200
        assert scheduler.status.running is True
    assert scheduler.status.running is False


def test_portfolio_opening_status_is_exactly_redacted_by_default(tmp_path) -> None:
    api = client(tmp_path)

    response = api.get("/api/v1/portfolio-paper/opening-status")

    assert response.status_code == 200
    assert response.json() == {
        "availability": "internal_only",
        "enabled": False,
        "running": False,
    }
    serialized = response.text
    for sensitive_field in (
        "poll_seconds",
        "last_track_id",
        "last_error",
        "quote",
        "certificate",
        "config",
        "hash",
        "batch",
        "last_run_at",
        "last_outcome",
        "consecutive_failures",
    ):
        assert sensitive_field not in serialized


def test_portfolio_opening_status_reports_enabled_lifespan_task(tmp_path) -> None:
    app = create_app(
        Settings(
            build_version="opening-status-lifespan-test",
            database_path=str(tmp_path / "opening-status-lifespan.db"),
            cache_path=str(tmp_path / "opening-status-lifespan-cache.db"),
            portfolio_opening_scheduler_enabled=True,
            portfolio_opening_poll_seconds=3600.0,
        )
    )

    with TestClient(app) as api:
        response = api.get("/api/v1/portfolio-paper/opening-status")

        assert response.status_code == 200
        assert response.json()["enabled"] is True
        assert response.json()["running"] is True


def test_portfolio_opening_status_rejects_mutation(tmp_path) -> None:
    response = client(tmp_path).post(
        "/api/v1/portfolio-paper/opening-status",
        json={},
    )

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"


def test_portfolio_opening_status_openapi_is_get_only_with_response_schema(
    tmp_path,
) -> None:
    schema = client(tmp_path).get("/openapi.json").json()
    operation = schema["paths"]["/api/v1/portfolio-paper/opening-status"]

    assert set(operation) == {"get"}
    response_schema = operation["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert response_schema == {
        "$ref": (
            "#/components/schemas/"
            "PortfolioPaperOpeningStatusResponse"
        )
    }
    response_component = schema["components"]["schemas"][
        "PortfolioPaperOpeningStatusResponse"
    ]
    assert set(response_component["properties"]) == {
        "availability",
        "enabled",
        "running",
    }
    assert (
        response_component["properties"]["availability"]["description"]
        == "Rollout-stage label only; it is not an access-control boundary."
    )
    assert response_component["additionalProperties"] is False


class DateAwareProvider(FakeProvider):
    async def history(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
    ) -> DataEnvelope:
        start = start or date(2025, 1, 1)
        end = end or date(2025, 12, 31)
        dates = pd.date_range(start, end, freq="B", tz=UTC)
        rows = [
            {
                "date": value.isoformat(),
                "open": 100 + index,
                "high": 102 + index,
                "low": 99 + index,
                "close": 101 + index,
                "volume": 1_000_000,
            }
            for index, value in enumerate(dates)
        ]
        return DataEnvelope(
            symbol=symbol,
            kind="history",
            rows=rows,
            citations=[Citation(source="Date-aware fixture")],
        )


def portfolio_run_request() -> dict[str, object]:
    return {
        "assets": [
            {"symbol": "BTCUSDT", "provider": "binance", "currency": "USDT"},
            {"symbol": "NDX", "provider": "macro", "currency": "USD"},
            {"symbol": "CL", "provider": "futures", "currency": "USD"},
            {"symbol": "GC", "provider": "futures", "currency": "USD"},
        ],
        "start": "2025-03-01",
        "end": "2025-05-20",
        "volatility_lookback": 20,
        "rebalance_bars": 10,
        "maximum_asset_weight": 0.4,
    }


def portfolio_experiment_payload(api: TestClient) -> dict[str, object]:
    run_request = portfolio_run_request()
    response = api.post("/api/v1/backtests/portfolio", json=run_request)
    assert response.status_code == 200
    snapshot = response.json()
    metadata = {
        asset["symbol"]: asset["metadata"] for asset in snapshot["assets"]
    }
    assets = [
        {
            **asset,
            "name": asset["symbol"],
            "metadata": metadata[asset["symbol"]],
        }
        for asset in run_request["assets"]  # type: ignore[union-attr]
    ]
    return {
        "name": "全球多资产 · 逆波动研究",
        "notes": "验证组合实验档案。",
        "assets": assets,
        "interval": "1d",
        "start": run_request["start"],
        "end": run_request["end"],
        "focus_method": "periodic_inverse_volatility",
        "run_request": run_request,
        "data_quality": snapshot["data_quality"],
        "assumptions": snapshot["assumptions"],
        "common_bars": snapshot["common_bars"],
        "results": snapshot["results"],
        "segments": snapshot["segments"],
        "research_decision": snapshot["research_decision"],
        "citations": snapshot["citations"],
        "calculation_version": "test-build",
    }


def portfolio_experiment_from_run_payload(
    snapshot: dict[str, object],
) -> dict[str, object]:
    return {
        "name": "服务端回执 · 全球多资产",
        "notes": "仅提交用户可编辑的展示信息。",
        "focus_method": "periodic_inverse_volatility",
        "assets": [
            {
                "symbol": asset["symbol"],
                "requested_symbol": asset["requested_symbol"],
                "name": asset["symbol"],
                "market": "GLOBAL",
                "exchange": "研究数据源",
                "asset_type": "research_asset",
            }
            for asset in snapshot["assets"]  # type: ignore[union-attr]
        ],
        "run_id": snapshot["run_id"],
    }


def test_health_and_strategy_catalog(tmp_path) -> None:
    with pytest.raises(ValueError, match="QUANTSIEVE_BUILD_VERSION"):
        Settings(environment="production")

    api = client(tmp_path, build_version="test-commit")
    assert api.get("/health").json() == {
        "status": "ok",
        "version": "test-commit",
    }
    assert api.app.version == "test-commit"
    strategies = api.get("/api/v1/strategies").json()
    assert len(strategies) == len(STRATEGIES)
    core_trend = next(
        strategy
        for strategy in strategies
        if strategy["id"] == "core-trend-allocation"
    )
    assert core_trend["parameters"]["defensive_exposure"] == 0.35
    constant = next(
        strategy
        for strategy in strategies
        if strategy["id"] == "constant-allocation"
    )
    assert constant["parameters"]["allocation"] == 0.5
    assert any(item["id"] == "trend-filter" for item in strategies)
    assert any(item["id"] == "macd-regime" for item in strategies)
    atr_trend = next(item for item in strategies if item["id"] == "atr-trend")
    assert atr_trend["parameters"]["atr_multiplier"] == 3
    assert "下一根开盘" in atr_trend["risk_note"]
    rsi_regime_atr = next(item for item in strategies if item["id"] == "rsi-regime-atr")
    assert rsi_regime_atr["parameters"]["exit_rsi"] == 55
    assert rsi_regime_atr["warmup_bars"] == 150
    assert "下一根开盘" in rsi_regime_atr["risk_note"]
    assert next(item for item in strategies if item["id"] == "macd")["warmup_bars"] == 34


def test_portfolio_backtest_compares_three_allocation_rules(tmp_path) -> None:
    api = client(tmp_path)
    response = api.post(
        "/api/v1/backtests/portfolio",
        json={
            "assets": [
                {"symbol": "BTCUSDT", "provider": "binance", "currency": "USDT"},
                {"symbol": "NDX", "provider": "macro", "currency": "USD"},
                {"symbol": "CL", "provider": "futures", "currency": "USD"},
                {"symbol": "GC", "provider": "futures", "currency": "USD"},
            ],
            "start": "2025-03-01",
            "end": "2025-05-20",
            "volatility_lookback": 20,
            "rebalance_bars": 10,
            "maximum_asset_weight": 0.4,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["run_id"]) == 32
    assert datetime.fromisoformat(payload["run_expires_at"]) > datetime.now(UTC)
    stored_run = api.app.state.experiment_store.get_portfolio_run(payload["run_id"])
    assert stored_run is not None
    assert "run_id" not in stored_run.result_payload
    assert "run_expires_at" not in stored_run.result_payload
    assert set(payload["results"]) == {
        "initial_equal_hold",
        "periodic_equal",
        "periodic_inverse_volatility",
    }
    assert payload["common_bars"] > 0
    assert len(payload["segments"]) == 4
    assert len(payload["assets"]) == 4
    inverse = payload["results"]["periodic_inverse_volatility"]
    assert sum(inverse["latest_weights"].values()) == pytest.approx(1)
    assert len(inverse["equity"]) == payload["common_bars"]
    assert inverse["metrics"]["rebalances"] == payload["results"]["periodic_equal"][
        "metrics"
    ]["rebalances"]


def test_portfolio_backtest_rejects_duplicate_assets(tmp_path) -> None:
    response = client(tmp_path).post(
        "/api/v1/backtests/portfolio",
        json={
            "assets": [
                {"symbol": "NDX", "provider": "macro", "currency": "USD"},
                {"symbol": "ndx", "provider": "macro", "currency": "USD"},
            ],
            "start": "2025-01-01",
            "end": "2025-05-01",
            "maximum_asset_weight": 0.5,
        },
    )

    assert response.status_code == 422


def test_portfolio_backtest_rejects_mixed_settlement_currencies(tmp_path) -> None:
    response = client(tmp_path).post(
        "/api/v1/backtests/portfolio",
        json={
            "assets": [
                {"symbol": "NDX", "provider": "macro", "currency": "USD"},
                {"symbol": "600519", "provider": "akshare", "currency": "CNY"},
            ],
            "start": "2025-01-01",
            "end": "2025-05-01",
            "maximum_asset_weight": 0.5,
        },
    )

    assert response.status_code == 422
    assert "汇率换算" in response.json()["detail"][0]["msg"]


def test_portfolio_experiment_archive_crud_search_and_persistence(tmp_path) -> None:
    api = client(tmp_path)
    payload = portfolio_experiment_payload(api)

    created_response = api.post("/api/v1/experiments/portfolios", json=payload)

    assert created_response.status_code == 201
    created = created_response.json()
    assert created["kind"] == "portfolio"
    assert created["schema_version"] == 1
    assert created["source_run_id"] is None
    assert created["focus_method"] == "periodic_inverse_volatility"
    assert (
        created["assumptions"]["execution"]
        == "next_common_session_open_proxy"
    )
    assert len(created["results"]["periodic_inverse_volatility"]["equity"]) > 0

    listed = api.get(
        "/api/v1/experiments/portfolios",
        params={"query": "全球多资产"},
    ).json()
    assert [item["id"] for item in listed] == [created["id"]]
    assert listed[0]["symbols"] == ["BTCUSDT", "NDX", "CL", "GC"]
    assert listed[0]["asset_count"] == 4
    assert listed[0]["total_return"] == pytest.approx(
        created["results"]["periodic_inverse_volatility"]["metrics"]["total_return"]
    )
    assert "results" not in listed[0]
    assert "assets" not in listed[0]

    by_symbol = api.get(
        "/api/v1/experiments/portfolios",
        params={"symbol": "btcusdt"},
    ).json()
    assert [item["id"] for item in by_symbol] == [created["id"]]
    assert api.get(
        "/api/v1/experiments/portfolios",
        params={"symbol": "MISSING"},
    ).json() == []
    assert api.get("/api/v1/experiments").json() == []

    detail = api.get(
        f"/api/v1/experiments/portfolios/{created['id']}"
    ).json()
    assert detail == created
    assert detail["run_request"]["assets"][0]["symbol"] == "BTCUSDT"

    restarted_api = client(tmp_path)
    persisted = restarted_api.get(
        f"/api/v1/experiments/portfolios/{created['id']}"
    )
    assert persisted.status_code == 200
    assert persisted.json()["calculation_version"] == "test-build"

    deleted = restarted_api.delete(
        f"/api/v1/experiments/portfolios/{created['id']}"
    )
    assert deleted.status_code == 204
    assert restarted_api.get(
        f"/api/v1/experiments/portfolios/{created['id']}"
    ).status_code == 404
    assert restarted_api.delete(
        f"/api/v1/experiments/portfolios/{created['id']}"
    ).status_code == 404


def test_portfolio_experiment_from_run_is_trusted_persistent_and_idempotent(
    tmp_path,
) -> None:
    first_api = client(tmp_path, build_version="run-build")
    run_response = first_api.post(
        "/api/v1/backtests/portfolio",
        json=portfolio_run_request(),
    )
    assert run_response.status_code == 200
    snapshot = run_response.json()
    from_run = portfolio_experiment_from_run_payload(snapshot)
    assert set(from_run) == {
        "name",
        "notes",
        "focus_method",
        "assets",
        "run_id",
    }

    restarted_api = client(tmp_path, build_version="later-build")
    created_response = restarted_api.post(
        "/api/v1/experiments/portfolios/from-run",
        json=from_run,
    )

    assert created_response.status_code == 201
    created = created_response.json()
    assert created["source_run_id"] == snapshot["run_id"]
    assert created["results"] == snapshot["results"]
    assert created["research_decision"] == snapshot["research_decision"]
    assert created["calculation_version"] == "run-build"

    spoofed = deepcopy(from_run)
    spoofed["results"] = {
        "periodic_inverse_volatility": {
            "metrics": {"total_return": 99_999}
        }
    }
    rejected = restarted_api.post(
        "/api/v1/experiments/portfolios/from-run",
        json=spoofed,
    )
    assert rejected.status_code == 422
    assert "extra_forbidden" in rejected.text

    duplicate = deepcopy(from_run)
    duplicate["name"] = "同一回执不能生成第二份权威记录"
    duplicated_response = restarted_api.post(
        "/api/v1/experiments/portfolios/from-run",
        json=duplicate,
    )
    assert duplicated_response.status_code == 201
    assert duplicated_response.json()["id"] == created["id"]
    assert duplicated_response.json()["name"] == created["name"]

    with sqlite3.connect(tmp_path / "data.db") as connection:
        connection.execute(
            "UPDATE portfolio_backtest_runs SET expires_at = ? WHERE run_id = ?",
            (
                (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
                snapshot["run_id"],
            ),
        )
    retry_after_receipt_expiry = restarted_api.post(
        "/api/v1/experiments/portfolios/from-run",
        json=duplicate,
    )
    assert retry_after_receipt_expiry.status_code == 201
    assert retry_after_receipt_expiry.json()["id"] == created["id"]

    listed = restarted_api.get("/api/v1/experiments/portfolios").json()
    assert [item["id"] for item in listed] == [created["id"]]
    assert listed[0]["source_run_id"] == snapshot["run_id"]


def test_portfolio_experiment_from_run_reports_missing_and_expired_receipts(
    tmp_path,
) -> None:
    api = client(tmp_path)
    run_response = api.post(
        "/api/v1/backtests/portfolio",
        json=portfolio_run_request(),
    )
    assert run_response.status_code == 200
    snapshot = run_response.json()
    from_run = portfolio_experiment_from_run_payload(snapshot)

    missing = deepcopy(from_run)
    missing["run_id"] = "f" * 32
    missing_response = api.post(
        "/api/v1/experiments/portfolios/from-run",
        json=missing,
    )
    assert missing_response.status_code == 404
    assert "不存在" in missing_response.json()["detail"]

    expired_at = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    with sqlite3.connect(tmp_path / "data.db") as connection:
        connection.execute(
            "UPDATE portfolio_backtest_runs SET expires_at = ? WHERE run_id = ?",
            (expired_at, snapshot["run_id"]),
        )
    expired_response = api.post(
        "/api/v1/experiments/portfolios/from-run",
        json=from_run,
    )
    assert expired_response.status_code == 410
    assert "已过期" in expired_response.json()["detail"]

    cleanup_response = api.post(
        "/api/v1/backtests/portfolio",
        json=portfolio_run_request(),
    )
    assert cleanup_response.status_code == 200
    with sqlite3.connect(tmp_path / "data.db") as connection:
        expired_row = connection.execute(
            "SELECT run_id FROM portfolio_backtest_runs WHERE run_id = ?",
            (snapshot["run_id"],),
        ).fetchone()
    assert expired_row is None


def test_portfolio_from_run_preserves_auto_resolved_binance_annualization(
    tmp_path,
) -> None:
    api = client(tmp_path)
    fake_binance = FakeBinanceRouter()
    api.app.state.providers = fake_binance
    api.app.state.paper_track_scheduler.providers = fake_binance
    run_request = {
        "assets": [
            {"symbol": "BTCUSDT", "provider": "auto", "currency": "USDT"},
            {"symbol": "ETHUSDT", "provider": "auto", "currency": "USDT"},
        ],
        "start": "2025-03-01",
        "end": "2025-05-20",
        "volatility_lookback": 20,
        "rebalance_bars": 10,
        "maximum_asset_weight": 0.5,
    }
    run_response = api.post("/api/v1/backtests/portfolio", json=run_request)

    assert run_response.status_code == 200
    snapshot = run_response.json()
    assert snapshot["data_quality"]["annual_periods"] == 365
    assert all(asset["provider"] == "binance" for asset in snapshot["assets"])

    created_response = api.post(
        "/api/v1/experiments/portfolios/from-run",
        json=portfolio_experiment_from_run_payload(snapshot),
    )

    assert created_response.status_code == 201
    created = created_response.json()
    assert all(asset["provider"] == "binance" for asset in created["assets"])
    assert created["data_quality"]["annual_periods"] == 365
    assert all(
        result["config"]["annual_periods"] == 365
        for result in created["results"].values()
    )


@pytest.mark.parametrize("wrong_currency", ["USD", "USDC", "CNY"])
def test_portfolio_backtest_rejects_known_currency_spoof(
    tmp_path,
    wrong_currency: str,
) -> None:
    api = client(tmp_path)
    fake_binance = FakeBinanceRouter()
    api.app.state.providers = fake_binance
    api.app.state.paper_track_scheduler.providers = fake_binance

    response = api.post(
        "/api/v1/backtests/portfolio",
        json={
            "assets": [
                {
                    "symbol": "BTCUSDT",
                    "provider": "auto",
                    "currency": wrong_currency,
                },
                {
                    "symbol": "ETHUSDT",
                    "provider": "auto",
                    "currency": wrong_currency,
                },
            ],
            "start": "2025-03-01",
            "end": "2025-05-20",
            "maximum_asset_weight": 0.5,
        },
    )

    assert response.status_code == 422
    assert "服务端资料结算币种为 USDT" in response.json()["detail"]


def test_portfolio_backtest_rejects_unverifiable_dynamic_currency(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = client(tmp_path)

    async def no_instruments(query: str, limit: int = 10) -> list[Instrument]:
        del query, limit
        return []

    monkeypatch.setattr(api.app.state.providers.provider, "search", no_instruments)
    response = api.post(
        "/api/v1/backtests/portfolio",
        json={
            "assets": [
                {"symbol": "UNKNOWN-A", "provider": "yfinance", "currency": "USD"},
                {"symbol": "UNKNOWN-B", "provider": "yfinance", "currency": "USD"},
            ],
            "start": "2025-03-01",
            "end": "2025-05-20",
            "maximum_asset_weight": 0.5,
        },
    )

    assert response.status_code == 422
    assert "无法从服务端目录验证" in response.json()["detail"]


def test_portfolio_experiment_uses_365_periods_for_all_binance_assets(
    tmp_path,
) -> None:
    api = client(tmp_path)
    payload = portfolio_experiment_payload(api)
    for asset in payload["assets"]:  # type: ignore[union-attr]
        asset["provider"] = "binance"
    for asset in payload["run_request"]["assets"]:  # type: ignore[index]
        asset["provider"] = "binance"
    payload["data_quality"]["annual_periods"] = 365  # type: ignore[index]
    payload["assumptions"]["execution"] = "next_open"  # type: ignore[index]
    for result in payload["results"].values():  # type: ignore[union-attr]
        result["config"]["annual_periods"] = 365
    for segment in payload["segments"]:  # type: ignore[union-attr]
        for result in segment["results"].values():
            result["config"]["annual_periods"] = 365

    response = api.post("/api/v1/experiments/portfolios", json=payload)

    assert response.status_code == 201
    record = response.json()
    assert record["data_quality"]["annual_periods"] == 365
    assert record["assumptions"]["execution"] == "next_open"
    assert all(
        result["config"]["annual_periods"] == 365
        for result in record["results"].values()
    )


def test_portfolio_experiment_rejects_inconsistent_snapshots(tmp_path) -> None:
    api = client(tmp_path)
    payload = portfolio_experiment_payload(api)
    invalid_cases: list[tuple[dict[str, object], str]] = []

    duplicate_asset = deepcopy(payload)
    duplicate_asset["assets"][1]["symbol"] = "btcusdt"  # type: ignore[index]
    invalid_cases.append((duplicate_asset, "资产代码不能重复"))

    too_few_assets = deepcopy(payload)
    too_few_assets["assets"] = too_few_assets["assets"][:1]  # type: ignore[index]
    invalid_cases.append((too_few_assets, "2–6 个资产"))

    mixed_currency = deepcopy(payload)
    mixed_currency["assets"][1]["currency"] = "CNY"  # type: ignore[index]
    invalid_cases.append((mixed_currency, "汇率换算"))

    request_asset_mismatch = deepcopy(payload)
    request_asset_mismatch["run_request"]["assets"][0]["symbol"] = (  # type: ignore[index]
        "ETHUSDT"
    )
    invalid_cases.append((request_asset_mismatch, "资产集合"))

    request_date_mismatch = deepcopy(payload)
    request_date_mismatch["run_request"]["start"] = "2025-03-03"  # type: ignore[index]
    invalid_cases.append((request_date_mismatch, "开始和结束日期"))

    assumption_mismatch = deepcopy(payload)
    assumption_mismatch["assumptions"]["rebalance_bars"] = 11  # type: ignore[index]
    invalid_cases.append((assumption_mismatch, "配置参数"))

    missing_method = deepcopy(payload)
    del missing_method["results"]["periodic_equal"]  # type: ignore[index]
    invalid_cases.append((missing_method, "精确包含三种配置方法"))

    method_mismatch = deepcopy(payload)
    method_mismatch["results"]["periodic_equal"]["method"] = (  # type: ignore[index]
        "initial_equal_hold"
    )
    invalid_cases.append((method_mismatch, "method 必须与结果键一致"))

    bars_mismatch = deepcopy(payload)
    bars_mismatch["common_bars"] = int(payload["common_bars"]) + 1
    invalid_cases.append((bars_mismatch, "common_bars"))

    data_quality_mismatch = deepcopy(payload)
    data_quality_mismatch["data_quality"]["annual_periods"] = 365  # type: ignore[index]
    invalid_cases.append((data_quality_mismatch, "年化周期"))

    bad_weights = deepcopy(payload)
    weights = bad_weights["results"]["periodic_inverse_volatility"][  # type: ignore[index]
        "latest_weights"
    ]
    first_symbol = next(iter(weights))
    weights[first_symbol] += 0.1
    invalid_cases.append((bad_weights, "最新权重"))

    decision_mismatch = deepcopy(payload)
    decision = decision_mismatch["research_decision"]  # type: ignore[assignment]
    decision["drawdown_improved_segments"] = (  # type: ignore[index]
        0
        if decision["drawdown_improved_segments"]  # type: ignore[index]
        else 1
    )
    invalid_cases.append((decision_mismatch, "回撤改善分段数"))

    evidence_check_mismatch = deepcopy(payload)
    evidence_checks = evidence_check_mismatch["research_decision"][  # type: ignore[index]
        "evidence_checks"
    ]
    evidence_checks["full_sample_positive_return"] = not evidence_checks[
        "full_sample_positive_return"
    ]
    invalid_cases.append((evidence_check_mismatch, "证据检查明细"))

    discontinuous_segments = deepcopy(payload)
    discontinuous_segments["segments"][1]["index"] = 4  # type: ignore[index]
    invalid_cases.append((discontinuous_segments, "分段编号"))

    for invalid_payload, expected_message in invalid_cases:
        response = api.post(
            "/api/v1/experiments/portfolios",
            json=invalid_payload,
        )
        assert response.status_code == 422
        assert expected_message in response.text

    assert api.get("/api/v1/experiments/portfolios").json() == []


def test_backtest_history_fetches_pre_roll_outside_evaluation_window() -> None:
    history, data, signal_history, error = asyncio.run(
        _history_for_backtest(
            DateAwareProvider(),
            "TEST",
            date(2025, 3, 1),
            date(2025, 4, 1),
            "1d",
            20,
        )
    )
    annotated = _with_warmup_metadata(history, 20, signal_history, error)

    assert data.index.min().date() >= date(2025, 3, 1)
    assert signal_history.index.max() < data.index.min()
    assert len(signal_history) == 20
    assert annotated.metadata["indicator_warmup_complete"] is True
    assert annotated.metadata["indicator_warmup_available_bars"] == 20


def test_settings_accept_comma_separated_cors_origins(monkeypatch) -> None:
    monkeypatch.setenv(
        "QUANTSIEVE_CORS_ORIGINS",
        "https://app.example.test,http://localhost:3000",
    )

    assert Settings().cors_origins == [
        "https://app.example.test",
        "http://localhost:3000",
    ]


def test_symbol_search_accepts_company_names(tmp_path) -> None:
    response = client(tmp_path).get("/api/v1/symbols/search", params={"q": "宁德时代"})

    assert response.status_code == 200
    assert response.json()[0] == {
        "symbol": "300750",
        "name": "宁德时代",
        "market": "CN",
        "exchange": "深圳证券交易所",
        "currency": "CNY",
        "provider": "akshare",
        "asset_type": "equity",
    }


def test_demo_requires_no_api_key(tmp_path) -> None:
    response = client(tmp_path).post(
        "/api/v1/chat",
        json={
            "messages": [{"role": "user", "content": "show demo"}],
            "demo_id": "berkshire-13f",
        },
    )
    assert response.status_code == 200
    assert response.json()["grounded"] is True
    assert response.json()["citations"][0]["source"] == "SEC EDGAR"


def test_demo_stream_emits_status_tokens_and_done(tmp_path) -> None:
    with client(tmp_path).stream(
        "POST",
        "/api/v1/chat/stream",
        json={
            "messages": [{"role": "user", "content": "show demo"}],
            "demo_id": "berkshire-13f",
        },
    ) as response:
        payload = "".join(response.iter_text())

    assert response.status_code == 200
    assert "event: status" in payload
    assert "event: token" in payload
    assert "event: done" in payload


def test_no_key_research_builds_real_quant_snapshot_for_selected_instrument(tmp_path) -> None:
    response = client(tmp_path).post(
        "/api/v1/chat",
        json={
            "messages": [{"role": "user", "content": "开始研究"}],
            "symbol": "TEST",
            "provider": "yfinance",
            "instrument_name": "测试标的",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["grounded"] is True
    assert "测试标的（TEST）免 Key 研究报告" in payload["content"]
    assert "get_history" in payload["tool_calls"]
    assert payload["citations"][0]["source"] == "Test fixture"
    assert payload["artifacts"][0]["artifact_type"] == "research_snapshot"
    assert payload["artifacts"][0]["bars"] == 100
    assert "max_drawdown" in payload["artifacts"][0]["risk"]


def test_no_key_research_resolves_instrument_mentioned_in_question(tmp_path) -> None:
    response = client(tmp_path).post(
        "/api/v1/chat",
        json={"messages": [{"role": "user", "content": "帮我分析苹果最近的趋势和风险"}]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["artifacts"][0]["symbol"] == "AAPL"
    assert "Apple Inc.（AAPL）免 Key 研究报告" in payload["content"]


def test_no_key_research_requires_one_unambiguous_instrument(tmp_path) -> None:
    response = client(tmp_path).post(
        "/api/v1/chat",
        json={"messages": [{"role": "user", "content": "比较特斯拉和英伟达"}]},
    )

    assert response.status_code == 422
    assert "一次研究一个标的" in response.json()["detail"]


def test_backtest_uses_sourced_history(tmp_path) -> None:
    response = client(tmp_path).post(
        "/api/v1/backtests",
        json={"symbol": "TEST", "strategy_id": "sma-cross"},
    )
    payload = response.json()
    assert response.status_code == 200
    assert payload["citations"][0]["source"] == "Test fixture"
    assert len(payload["result"]["equity"]) == 100
    assert payload["interval"] == "1d"
    assert payload["benchmark"]["strategy"]["id"] == "buy-hold"
    assert payload["benchmark"]["result"]["config"]["signal_delay_bars"] == 0
    assert payload["strategy"]["warmup_bars"] == 60
    assert payload["data_metadata"]["indicator_warmup_required_bars"] == 60
    assert payload["data_metadata"]["indicator_warmup_available_bars"] == 0
    assert payload["data_metadata"]["indicator_warmup_complete"] is False
    assert isinstance(payload["comparison"]["excess_return"], float)
    assert payload["exposure_matched_benchmark"]["target_exposure"] == pytest.approx(
        payload["result"]["metrics"]["exposure_ratio"]
    )
    assert isinstance(payload["timing_comparison"]["excess_return"], float)
    assert (
        payload["timing_comparison"]["beats_exposure_matched"]
        == (payload["timing_comparison"]["excess_return"] > 0)
    )
    assert payload["result"]["metrics"]["trades_per_year"] >= 0
    assert "position_cycles" in payload["result"]
    assert payload["diagnostics"]["trade_quality"]["closed_trades"] == sum(
        bool(cycle["closed"]) for cycle in payload["result"]["position_cycles"]
    )
    assert payload["diagnostics"]["signal_state"]["status"] in {
        "pending_entry",
        "pending_exit",
        "holding",
        "cash",
    }
    assert len(payload["diagnostics"]["segments"]) == 4
    assert 0 <= payload["diagnostics"]["profitable_segment_ratio"] <= 1
    assert payload["diagnostics"]["trade_quality"]["closed_trades"] >= 0
    assert (
        0
        <= payload["diagnostics"]["trade_quality"]["win_rate_confidence_low"]
        <= payload["diagnostics"]["trade_quality"]["win_rate_confidence_high"]
        <= 1
    )
    assert all(
        "benchmark_return" in segment
        for segment in payload["diagnostics"]["segments"]
    )


def test_active_backtest_forces_next_open_execution_when_zero_delay_is_requested(
    tmp_path,
) -> None:
    """A close-derived signal cannot legally fill at that same bar's open."""
    response = client(tmp_path).post(
        "/api/v1/backtests",
        json={
            "symbol": "TEST",
            "strategy_id": "sma-cross",
            "config": {"signal_delay_bars": 0},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["result"]["config"]["signal_delay_bars"] == 1
    # The passive comparator is a known-at-start allocation and remains an
    # immediate benchmark rather than borrowing the active signal's delay.
    assert payload["benchmark"]["result"]["config"]["signal_delay_bars"] == 0


@pytest.mark.parametrize(
    ("strategy_id", "parameters", "message"),
    [
        ("macd", {"fast": 26, "slow": 12}, "fast must be shorter"),
        ("rsi", {"oversold": 70, "overbought": 30}, "oversold must be below"),
        ("buy-hold", {"unknown_parameter": 1}, "does not accept parameter"),
    ],
)
def test_backtest_rejects_invalid_manual_strategy_parameters(
    tmp_path,
    strategy_id: str,
    parameters: dict[str, float],
    message: str,
) -> None:
    response = client(tmp_path).post(
        "/api/v1/backtests",
        json={
            "symbol": "TEST",
            "provider": "yfinance",
            "strategy_id": strategy_id,
            "parameters": parameters,
        },
    )

    assert response.status_code == 422
    assert message in response.json()["detail"]


def test_backtest_optimizer_returns_selected_params_and_validation(tmp_path) -> None:
    api = client(tmp_path)
    response = api.post(
        "/api/v1/backtests/optimize",
        json={
            "symbol": "TEST",
            "provider": "yfinance",
            "strategy_id": "macd",
            "objective": "sharpe_ratio",
            "minimum_trades": 0,
            "minimum_annualized_return": -1,
            "maximum_drawdown": 1,
            "minimum_timing_positive_fold_ratio": 0,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["optimization"]["candidates_evaluated"] > 20
    assert payload["optimization"]["selected_parameters"] == payload["strategy"]["parameters"]
    assert payload["optimization"]["split_date"].startswith("2025-")
    assert payload["result"]["config"]["annual_periods"] == 252
    assert "validation_passed" in payload["optimization"]
    assert "validation_benchmark_metrics" in payload["optimization"]
    assert "validation_exposure_matched_benchmark_metrics" in payload["optimization"]
    assert "validation_timing_excess_return" in payload["optimization"]
    assert payload["optimization"]["minimum_timing_positive_fold_ratio"] == 0
    assert all(
        "timing_positive_fold_ratio" in candidate
        and "fold_timing_excess_returns" in candidate
        for candidate in payload["optimization"]["top_candidates"]
    )
    archived = api.post(
        "/api/v1/experiments/from-run",
        json={
            "name": "参数寻优可信档案",
            "run_id": payload["run_id"],
            "instrument_name": "测试标的",
        },
    )
    assert archived.status_code == 201
    experiment = archived.json()
    assert experiment["provenance_status"] == "server_verified"
    assert experiment["optimized"] is True
    assert (
        experiment["validation"]["validation_code"]
        == payload["optimization"]["validation_code"]
    )
    assert (
        experiment["validation"]["development_metrics"]
        == payload["optimization"]["train_metrics"]
    )
    assert all(
        "exposure_matched_benchmark_metrics" in fold
        and "timing_excess_return" in fold
        and "timing_value_added" in fold
        for fold in payload["optimization"]["walk_forward_folds"]
    )
    assert (
        payload["optimization"]["walk_forward_method"]
        == "expanding_window_reoptimization"
    )
    assert payload["optimization"]["walk_forward_execution_state_carried"] is True
    assert len(payload["optimization"]["cost_stress_tests"]) == 2
    assert payload["optimization"]["minimum_annualized_return"] == -1
    assert payload["optimization"]["maximum_drawdown"] == 1


def test_cross_market_robustness_keeps_each_market_validation_separate(tmp_path) -> None:
    response = client(tmp_path).post(
        "/api/v1/backtests/robustness",
        json={
            "strategy_id": "macd",
            "markets": [
                {"symbol": "TEST", "provider": "yfinance"},
                {"symbol": "BTCUSDT", "provider": "binance"},
            ],
            "objective": "sharpe_ratio",
            "minimum_trades": 0,
            "minimum_annualized_return": -1,
            "maximum_drawdown": 1,
            "minimum_timing_positive_fold_ratio": 0,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["parameter_policy"] == "independently_optimized_per_market"
    assert payload["final_holdout_min_closed_trades"] == 10
    assert payload["summary"]["requested"] == 2
    assert payload["summary"]["completed"] == 2
    assert payload["research_decision"]["status"] in {
        "validated_across_markets",
        "mixed_evidence",
        "provisional_only",
        "rejected_across_markets",
    }
    assert [market["symbol"] for market in payload["markets"]] == ["TEST", "BTCUSDT"]
    assert all(market["status"] == "completed" for market in payload["markets"])
    assert all(market["evidence"]["optimization"] for market in payload["markets"])
    assert all(
        market["evidence"]["strategy"]["parameters"]
        == market["evidence"]["optimization"]["selected_parameters"]
        for market in payload["markets"]
    )


def test_cross_market_robustness_rejects_duplicate_symbols(tmp_path) -> None:
    response = client(tmp_path).post(
        "/api/v1/backtests/robustness",
        json={
            "strategy_id": "macd",
            "markets": [
                {"symbol": "TEST", "provider": "yfinance"},
                {"symbol": "test", "provider": "binance"},
            ],
        },
    )

    assert response.status_code == 422
    assert "跨市场验证的标的不能重复" in response.text


def test_cross_market_robustness_distinguishes_constraint_rejection(
    tmp_path, monkeypatch
) -> None:
    async def no_eligible_parameters(*args, **kwargs):
        del args, kwargs
        raise ValueError("没有候选参数同时满足稳健性约束：测试约束。")

    monkeypatch.setattr(market_router, "_optimized_market_payload", no_eligible_parameters)
    response = client(tmp_path).post(
        "/api/v1/backtests/robustness",
        json={
            "strategy_id": "macd",
            "markets": [
                {"symbol": "TEST", "provider": "yfinance"},
                {"symbol": "BTCUSDT", "provider": "binance"},
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["research_decision"]["status"] == "rejected_across_markets"
    assert payload["summary"] == {
        "requested": 2,
        "completed": 2,
        "validated": 0,
        "provisional": 0,
        "rejected": 2,
        "unavailable": 0,
    }
    assert all(market["status"] == "rejected" for market in payload["markets"])
    assert all(
        market["validation_code"] == "constraints_too_strict"
        for market in payload["markets"]
    )


def test_cross_market_experiment_archives_server_receipt_idempotently(tmp_path) -> None:
    api = client(tmp_path)
    run_response = api.post(
        "/api/v1/backtests/robustness",
        json={
            "strategy_id": "macd",
            "markets": [
                {"symbol": "TEST", "provider": "yfinance"},
                {"symbol": "BTCUSDT", "provider": "binance"},
            ],
            "minimum_trades": 0,
            "minimum_annualized_return": -1,
            "maximum_drawdown": 1,
            "minimum_timing_positive_fold_ratio": 0,
        },
    )

    assert run_response.status_code == 200
    snapshot = run_response.json()
    run_id = snapshot["run_id"]
    stored_run = api.app.state.experiment_store.get_cross_market_run(run_id)
    assert stored_run is not None
    assert "run_id" not in stored_run.result_payload
    assert "run_expires_at" not in stored_run.result_payload

    request_payload = {
        "name": "MACD 跨市场反证",
        "notes": "服务端回执归档",
        "run_id": run_id,
    }
    created_response = api.post(
        "/api/v1/experiments/cross-market/from-run",
        json=request_payload,
    )

    assert created_response.status_code == 201
    created = created_response.json()
    assert created["source_run_id"] == run_id
    assert created["kind"] == "cross_market"
    assert created["strategy"]["id"] == "macd"
    assert [item["symbol"] for item in created["markets"]] == ["TEST", "BTCUSDT"]
    assert created["summary"] == snapshot["summary"]

    spoofed = {**request_payload, "markets": []}
    rejected = api.post("/api/v1/experiments/cross-market/from-run", json=spoofed)
    assert rejected.status_code == 422
    assert "extra_forbidden" in rejected.text

    duplicated = api.post(
        "/api/v1/experiments/cross-market/from-run",
        json={**request_payload, "name": "被忽略的重复归档"},
    )
    assert duplicated.status_code == 201
    assert duplicated.json()["id"] == created["id"]

    listed = api.get("/api/v1/experiments/cross-market?symbol=btcusdt")
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == created["id"]
    assert listed.json()[0]["symbols"] == ["TEST", "BTCUSDT"]

    fetched = api.get(f"/api/v1/experiments/cross-market/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created

    deleted = api.delete(f"/api/v1/experiments/cross-market/{created['id']}")
    assert deleted.status_code == 204
    assert api.get(f"/api/v1/experiments/cross-market/{created['id']}").status_code == 404


def test_passive_allocation_runs_directly_and_rejects_optimization(tmp_path) -> None:
    api = client(tmp_path)
    direct = api.post(
        "/api/v1/backtests",
        json={
            "symbol": "TEST",
            "provider": "yfinance",
            "strategy_id": "constant-allocation",
            "parameters": {"allocation": 0.37},
        },
    )

    assert direct.status_code == 200
    payload = direct.json()
    assert payload["strategy"]["parameters"]["allocation"] == 0.37
    assert payload["result"]["config"]["signal_delay_bars"] == 0
    assert payload["data_metadata"]["execution_model"] == "fixed_shares"
    assert payload["data_metadata"]["initial_allocation"] == pytest.approx(0.37)
    assert payload["data_metadata"]["rebalance_policy"] == "none"
    assert payload["result"] == payload["exposure_matched_benchmark"]["result"]
    assert payload["exposure_matched_benchmark"]["target_exposure"] == pytest.approx(
        0.37
    )
    positions = [row["position"] for row in payload["result"]["equity"]]
    # The initial allocation is applied at the first open, while reported
    # exposure is marked at the first close. A rising first bar therefore
    # already shows a small amount of natural exposure drift.
    assert positions[0] > 0.37
    assert positions[-1] > positions[0]
    assert positions[-1] > positions[0]
    assert payload["result"]["metrics"]["exposure_ratio"] > 0.37
    assert payload["result"]["metrics"]["trades"] == 1
    assert payload["result"]["metrics"]["closed_trades"] == 0
    assert payload["timing_comparison"]["excess_return"] == pytest.approx(0)
    assert payload["diagnostics"]["signal_state"]["status"] == "holding"
    assert payload["diagnostics"]["signal_state"]["pending_action"] == "hold"

    optimized = api.post(
        "/api/v1/backtests/optimize",
        json={
            "symbol": "TEST",
            "provider": "yfinance",
            "strategy_id": "constant-allocation",
        },
    )
    assert optimized.status_code == 422
    assert "被动配置不做参数寻优" in optimized.json()["detail"]


def test_direct_core_allocation_propagates_satellite_cycle_baseline(
    tmp_path,
    monkeypatch,
) -> None:
    original_run_backtest = market_router.run_backtest
    observed_baselines: list[pd.Series | None] = []

    def recording_run_backtest(*args, **kwargs):
        observed_baselines.append(kwargs.get("cycle_baseline_signals"))
        return original_run_backtest(*args, **kwargs)

    monkeypatch.setattr(market_router, "run_backtest", recording_run_backtest)
    response = client(tmp_path).post(
        "/api/v1/backtests",
        json={
            "symbol": "TEST",
            "provider": "yfinance",
            "strategy_id": "core-trend-allocation",
            "parameters": {"period": 3, "defensive_exposure": 0.35},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert any(baseline is not None for baseline in observed_baselines)
    cycles = payload["result"]["position_cycles"]
    assert cycles
    assert {cycle["cycle_kind"] for cycle in cycles} == {"satellite_over_core"}
    assert {cycle["return_semantics"] for cycle in cycles} == {
        "compounded_relative_to_core"
    }


def test_discovery_propagates_core_cycle_baseline_through_every_evaluation(
    tmp_path,
    monkeypatch,
) -> None:
    original_run_backtest = market_router.run_backtest
    original_run_cost_stress_tests = market_router.run_cost_stress_tests
    observed_backtests: list[tuple[int, int | None, int]] = []
    observed_cost_stress: list[tuple[int, int | None, int]] = []

    def recording_run_backtest(*args, **kwargs):
        frame = args[0]
        signals = args[1]
        baseline = kwargs.get("cycle_baseline_signals")
        if baseline is not None:
            pd.testing.assert_index_equal(baseline.index, signals.index)
            observed_backtests.append((len(frame), kwargs.get("evaluation_start"), len(baseline)))
        return original_run_backtest(*args, **kwargs)

    def recording_run_cost_stress_tests(*args, **kwargs):
        frame = args[0]
        signals = args[1]
        baseline = kwargs.get("cycle_baseline_signals")
        assert baseline is not None
        pd.testing.assert_index_equal(baseline.index, signals.index)
        observed_cost_stress.append((len(frame), kwargs.get("evaluation_start"), len(baseline)))
        return original_run_cost_stress_tests(*args, **kwargs)

    core_strategy = STRATEGIES["core-trend-allocation"]
    monkeypatch.setattr(
        market_router,
        "STRATEGIES",
        {"core-trend-allocation": core_strategy},
    )
    monkeypatch.setitem(
        optimization_module.PARAMETER_GRIDS,
        "core-trend-allocation",
        {"period": [3], "defensive_exposure": [0.35]},
    )
    # This test targets the API wiring after development selection. Force the
    # single real optimization result through that gate without altering any
    # backtest, signal, cost-stress, or optimizer implementation.
    monkeypatch.setattr(
        market_router,
        "is_development_selection_eligible",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(market_router, "run_backtest", recording_run_backtest)
    monkeypatch.setattr(
        market_router,
        "run_cost_stress_tests",
        recording_run_cost_stress_tests,
    )

    response = client(tmp_path).post(
        "/api/v1/backtests/discover",
        json={
            "symbol": "DISCOVER",
            "provider": "yfinance",
            "objective": "balanced",
            "shortlist_size": 1,
            "minimum_trades": 0,
            "minimum_trades_per_year": 1,
            "minimum_exposure": 0,
            "maximum_cash_streak_ratio": 1,
            "minimum_profitable_fold_ratio": 0,
            "minimum_timing_positive_fold_ratio": 0,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["selection_protocol"]["holdout_evaluated"] == 1
    assert payload["best_available"]["strategy"]["id"] == "core-trend-allocation"

    # 160 fixture bars split 80/20. Two development calls prove both screening
    # and the locked-candidate development result receive the sliced baseline.
    assert (
        sum(
            frame_length == 128 and evaluation_start is None and baseline_length == 128
            for frame_length, evaluation_start, baseline_length in observed_backtests
        )
        >= 2
    )
    assert (160, 128, 160) in observed_backtests  # untouched holdout
    assert (160, None, 160) in observed_backtests  # full-sample report
    assert observed_cost_stress == [(160, 128, 160)]


def test_discovery_requirements_are_published_from_the_server(tmp_path) -> None:
    response = client(tmp_path).get("/api/v1/backtests/discovery-requirements")

    assert response.status_code == 200
    assert response.json() == {
        "intervals": {
            "15m": {"full_sample_days": 90, "holdout_days": 18},
            "1h": {"full_sample_days": 180, "holdout_days": 36},
            "4h": {"full_sample_days": 365, "holdout_days": 73},
            "1d": {"full_sample_days": 365, "holdout_days": 73},
            "1wk": {"full_sample_days": 730, "holdout_days": 146},
        }
    }


def test_history_capabilities_publish_provider_windows_before_backtest(tmp_path) -> None:
    api = client(tmp_path)

    response = api.get("/api/v1/market/BTCUSDT/capabilities?provider=binance")
    crypto = _history_capabilities(SimpleNamespace(name="binance"), "BTCUSDT")
    futures = _history_capabilities(SimpleNamespace(name="futures"), "CL")

    assert response.status_code == 200
    assert response.json()["symbol"] == "BTCUSDT"
    assert crypto == {
        "symbol": "BTCUSDT",
        "provider": "binance",
        "intervals": {
            "15m": {"supported": True, "max_history_days": 180},
            "1h": {"supported": True, "max_history_days": 730},
            "4h": {"supported": True, "max_history_days": 1825},
            "1d": {"supported": True, "max_history_days": 7300},
            "1wk": {"supported": True, "max_history_days": 14600},
        },
    }
    assert futures["intervals"]["15m"] == {
        "supported": False,
        "max_history_days": None,
    }
    assert futures["intervals"]["1wk"] == {
        "supported": True,
        "max_history_days": None,
    }


@pytest.mark.parametrize(
    ("provider_name", "interval", "expected"),
    [
        ("akshare", "15m", 252 * 16),
        ("akshare", "1h", 252 * 4),
        ("akshare", "4h", 252),
        ("yfinance", "15m", 252 * 26),
        ("binance", "15m", 365 * 24 * 4),
    ],
)
def test_annual_periods_follow_the_provider_market_calendar(
    provider_name: str,
    interval: Literal["15m", "1h", "4h", "1d", "1wk"],
    expected: int,
) -> None:
    provider = FakeProvider()
    provider.name = provider_name

    assert _annual_periods(provider, interval) == expected


def test_discovery_minimum_trade_gate_uses_elapsed_calendar_time() -> None:
    dense = pd.to_datetime(["2025-01-01", "2025-01-02"])
    sparse = pd.to_datetime(["2025-01-01", "2026-01-01"])

    assert _minimum_trade_events_for_window(dense, 252, 12, None) == 3
    assert _minimum_trade_events_for_window(sparse, 252, 12, None) == 12
    assert _minimum_trade_events_for_window(sparse, 252, 12, 7) == 7


def test_development_market_regime_only_guides_template_ordering() -> None:
    trend_data = pd.DataFrame(
        {"close": [100 * 1.01**index for index in range(90)]},
        index=pd.date_range("2025-01-01", periods=90, freq="D", tz=UTC),
    )
    trend = _development_market_regime(trend_data)
    assert trend["classification"] == "trending"
    assert trend["direction"] == "rising"
    assert trend["path_efficiency"] > 0.35
    assert _strategy_regime_fit("macd", trend)[0] == "aligned"
    assert _strategy_regime_fit("atr-trend", trend)[0] == "aligned"
    assert _strategy_regime_fit("breakout-atr", trend)[0] == "aligned"
    assert _strategy_regime_fit("rsi", trend)[0] == "counter_regime"

    range_data = pd.DataFrame(
        {"close": [100 if index % 2 else 102 for index in range(90)]},
        index=pd.date_range("2025-01-01", periods=90, freq="D", tz=UTC),
    )
    range_bound = _development_market_regime(range_data)
    assert range_bound["classification"] == "range_bound"
    assert _strategy_regime_fit("rsi", range_bound)[0] == "aligned"
    assert _strategy_regime_fit("atr-trend", range_bound)[0] == "counter_regime"
    assert _strategy_regime_fit("breakout-atr", range_bound)[0] == "counter_regime"
    assert _strategy_regime_fit("macd", range_bound)[0] == "counter_regime"


def test_discovery_horizon_uses_calendar_time_for_intraday_validation() -> None:
    short_data = pd.DataFrame(
        {"close": range(2_881)},
        index=pd.date_range("2026-01-01", periods=2_881, freq="15min", tz=UTC),
    )
    short = _discovery_horizon(short_data, 2_304, "15m")
    assert short["status"] == "short_horizon"
    assert short["validation_eligible"] is False
    assert short["full_sample_days"] == 30
    assert short["holdout_days"] == 6

    mature_data = pd.DataFrame(
        {"close": range(8_641)},
        index=pd.date_range("2026-01-01", periods=8_641, freq="15min", tz=UTC),
    )
    mature = _discovery_horizon(mature_data, 6_912, "15m")
    assert mature["status"] == "adequate"
    assert mature["validation_eligible"] is True
    assert mature["full_sample_days"] == 90
    assert mature["holdout_days"] == 18


def test_strategy_discovery_shortlists_before_final_holdout(tmp_path) -> None:
    response = client(tmp_path).post(
        "/api/v1/backtests/discover",
        json={
            "symbol": "DISCOVER",
            "provider": "yfinance",
            "objective": "balanced",
            "shortlist_size": 1,
            "minimum_trades": 0,
            "minimum_trades_per_year": 1,
            "minimum_exposure": 0,
            "maximum_cash_streak_ratio": 1,
            "minimum_profitable_fold_ratio": 0,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["selection_protocol"]["shortlisted"] == 1
    assert (
        payload["selection_protocol"]["development_validated"]
        <= payload["selection_protocol"]["optimized"]
    )
    assert (
        payload["selection_protocol"]["development_selection_eligible"]
        >= payload["selection_protocol"]["development_validated"]
    )
    assert payload["selection_protocol"]["holdout_used_for_shortlisting"] is False
    assert payload["selection_protocol"]["holdout_execution_state_carried"] is True
    assert payload["selection_protocol"]["final_holdout_min_closed_trades"] == 10
    assert payload["selection_protocol"]["forward_observation_min_closed_trades"] == 5
    assert payload["selection_protocol"]["templates_screened"] == len(
        set(STRATEGIES) - PASSIVE_STRATEGY_IDS
    )
    assert (
        payload["selection_protocol"][
            "screening_initial_parameter_limit_per_template"
        ]
        == 12
    )
    assert payload["selection_protocol"]["screening_parameter_limit_per_template"] == 24
    assert payload["selection_protocol"]["screening_data_scope"] == "development_only"
    assert (
        payload["selection_protocol"]["screening_parameter_policy_symbol_agnostic"]
        is True
    )
    assert (
        payload["selection_protocol"]["screening_adaptation_scope"]
        == "requested_symbol_development_data"
    )
    assert payload["selection_protocol"]["holdout_used_for_screening"] is False
    assert set(payload["selection_protocol"]["feasible_strategy_families"]) <= {
        "mean_reversion",
        "risk_managed_allocation",
        "trend_or_breakout",
    }
    assert (
        payload["selection_protocol"]["screening_parameter_sets_evaluated"]
        == sum(item["screening_candidates_evaluated"] for item in payload["shortlist"])
    )
    assert (
        payload["selection_protocol"]["screening_parameter_grid_total"]
        == sum(item["screening_grid_total"] for item in payload["shortlist"])
    )
    assert payload["selection_protocol"][
        "screening_grid_coverage_ratio"
    ] == pytest.approx(
        payload["selection_protocol"]["screening_parameter_sets_evaluated"]
        / payload["selection_protocol"]["screening_parameter_grid_total"]
    )
    screening_constraints = payload["selection_protocol"]["screening_constraints"]
    assert screening_constraints["candidates_evaluated"] == payload[
        "selection_protocol"
    ]["screening_parameter_sets_evaluated"]
    assert (
        0
        <= screening_constraints["candidates_passing_base_constraints"]
        <= screening_constraints["candidates_evaluated"]
    )
    assert all(
        {
            "reason",
            "failed_candidates",
            "failure_ratio",
        }
        <= set(failure)
        and 0 < failure["failed_candidates"] <= screening_constraints["candidates_evaluated"]
        and 0 < failure["failure_ratio"] <= 1
        for failure in screening_constraints["constraint_failures"]
    )
    assert "indicator_warmup_required_bars" in payload["data_metadata"]
    assert payload["development_market_regime"]["classification"] in {
        "trending",
        "range_bound",
        "mixed",
        "insufficient",
    }
    horizon = payload["data_metadata"]["discovery_horizon"]
    assert horizon["status"] == "short_horizon"
    assert horizon["validation_eligible"] is False
    assert horizon["minimum_full_sample_days"] == 365
    assert payload["selection_protocol"]["holdout_evaluated"] <= 1
    assert len(payload["shortlist"]) == len(set(STRATEGIES) - PASSIVE_STRATEGY_IDS)
    assert all(
        1
        <= item["screening_candidates_evaluated"]
        <= payload["selection_protocol"]["screening_parameter_limit_per_template"]
        for item in payload["shortlist"]
    )
    assert all(
        0
        <= item["screening_candidates_passing_base_constraints"]
        <= item["screening_candidates_evaluated"]
        for item in payload["shortlist"]
    )
    assert all("screening_constraint_reasons" in item for item in payload["shortlist"])
    assert all("screening_objective_score" in item for item in payload["shortlist"])
    assert all(
        item["screening_selected_stage"] in {"coverage", "adaptive_refinement"}
        for item in payload["shortlist"]
    )
    assert all("screening_parameter_coverage" in item for item in payload["shortlist"])
    assert all(
        item["screening_candidates_evaluated"]
        == min(
            item["screening_grid_total"],
            payload["selection_protocol"]["screening_parameter_limit_per_template"],
        )
        and item["screening_grid_coverage_ratio"]
        == pytest.approx(
            item["screening_candidates_evaluated"] / item["screening_grid_total"]
        )
        and item["screening_budget"]["evaluated"]
        == item["screening_candidates_evaluated"]
        for item in payload["shortlist"]
    )
    assert all(
        item["screening_stages"][0]["stage"] == "coverage"
        and item["screening_stages"][0]["data_scope"] == "development_only"
        and sum(stage["evaluated"] for stage in item["screening_stages"])
        == item["screening_candidates_evaluated"]
        for item in payload["shortlist"]
    )
    assert all(
        (
            len(item["screening_stages"]) == 1
            and item["screening_budget"]["full_grid_evaluated"]
        )
        or (
            len(item["screening_stages"]) == 2
            and item["screening_stages"][1]["stage"] == "adaptive_refinement"
            and item["screening_stages"][1]["data_scope"] == "development_only"
            and (
                item["screening_stages"][1]["local_refinement_candidates"]
                + item["screening_stages"][1]["global_exploration_candidates"]
                == item["screening_stages"][1]["evaluated"]
            )
        )
        for item in payload["shortlist"]
    )
    assert all(
        all(
            1 <= level["sampled_levels"] <= level["available_levels"]
            and level["coverage_ratio"]
            == pytest.approx(level["sampled_levels"] / level["available_levels"])
            and level["extrema_covered"] is True
            for level in item["screening_parameter_coverage"].values()
        )
        for item in payload["shortlist"]
    )
    assert all(
        item["market_regime_fit"] in {"aligned", "neutral", "counter_regime"}
        and item["market_regime_fit_reason"]
        for item in payload["shortlist"]
    )
    assert len(payload["candidates"]) <= 1
    assert len(payload["selection_trials"]) <= 1
    assert all(
        {
            "development_validation_code",
            "development_validation_metrics",
        }
        <= set(item)
        for item in payload["selection_trials"]
    )
    assert payload["passive_baseline"]["backtest"]["strategy"]["id"] == "buy-hold"
    assert (
        payload["passive_baseline"]["backtest"]["result"]["config"][
            "signal_delay_bars"
        ]
        == 0
    )
    assert (
        payload["passive_baseline"]["backtest"]["result"]["metrics"]
        == payload["passive_baseline"]["backtest"]["benchmark"]["result"]["metrics"]
    )
    risk_budgeted = payload["passive_baseline"]["risk_budgeted"]
    assert risk_budgeted["backtest"]["strategy"]["id"] == "constant-allocation"
    assert risk_budgeted["budget_satisfied"] is True
    assert risk_budgeted["calibration_budget_satisfied"] is True
    assert risk_budgeted["target_exposure"] == pytest.approx(
        risk_budgeted["backtest"]["strategy"]["parameters"]["allocation"]
    )
    assert (
        risk_budgeted["full_sample_budget_satisfied"]
        == (
            abs(risk_budgeted["backtest"]["result"]["metrics"]["max_drawdown"])
            <= risk_budgeted["requested_max_drawdown"] + 1e-9
        )
    )
    if payload["best_available"]:
        split_date = payload["best_available"]["backtest"]["optimization"][
            "split_date"
        ]
        assert risk_budgeted["calibration_end"] < split_date
    assert payload["status"] in {
        "validated_candidate",
        "provisional_candidate",
        "no_validated_candidate",
        "development_rejected",
        "constraints_too_strict",
    }
    expected_decision = (
        "validated_active"
        if payload["champion"]
        else "freeze_for_evidence"
        if payload["provisional"]
        else "development_rejected"
        if (
            payload["selection_protocol"]["optimized"]
            and not payload["selection_protocol"]["development_selection_eligible"]
        )
        else "passive_baseline"
        if payload["passive_baseline"]["backtest"]["result"]["metrics"][
            "total_return"
        ]
        > 0
        else "no_actionable_strategy"
    )
    assert payload["research_decision"]["mode"] == expected_decision
    assert payload["passive_baseline"]["preferred"] is (
        expected_decision in {"passive_baseline", "development_rejected"}
    )
    if expected_decision == "development_rejected":
        assert payload["selection_protocol"]["holdout_evaluated"] == 0
        assert payload["best_available"] is None
    if payload["best_available"]:
        assert "validation_code" in payload["best_available"]
        assert "forward_observation_eligible" in payload["best_available"]
        optimization = payload["best_available"]["backtest"]["optimization"]
        quality = optimization["validation_trade_quality"]
        assert quality["closed_trades"] == (
            optimization["validation_metrics"]["closed_trades"]
        )
        assert (
            0
            <= quality["win_rate_confidence_low"]
            <= quality["win_rate_confidence_high"]
            <= 1
        )


def test_trade_quality_uses_closed_position_cycles_not_capital_lots() -> None:
    result = SimpleNamespace(
        trades=[
            {"return": 0.01, "closed": True},
            {"return": 0.02, "closed": True},
            {"return": 0.03, "closed": True},
        ],
        position_cycles=[
            {"return": -0.2, "closed": True},
            {"return": 0.5, "closed": False},
        ],
    )

    quality = _trade_quality(result)

    assert quality["closed_trades"] == 1
    assert quality["win_rate"] == 0
    assert quality["expectancy"] == pytest.approx(-0.2)
    assert quality["sample_quality"] == "insufficient"


def test_trade_quality_rejects_compact_results_without_cycle_details() -> None:
    result = SimpleNamespace(
        position_cycles=[],
        metrics=SimpleNamespace(closed_trades=1),
    )

    with pytest.raises(ValueError, match="details are incomplete"):
        _trade_quality(result)


def test_risk_budget_payload_audits_with_the_same_fixed_share_mechanism() -> None:
    index = pd.date_range("2025-01-01", periods=100, freq="D", tz=UTC)
    prices = pd.Series(
        [100 - (50 * offset / (len(index) - 1)) for offset in range(len(index))],
        index=index,
    )
    data = pd.DataFrame(
        {
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "volume": 1_000_000,
        },
        index=index,
    )
    config = BacktestConfig(
        fee_rate=0,
        slippage_rate=0,
        signal_delay_bars=1,
        annual_periods=365,
    )
    benchmark_definition, benchmark_result = market_router._benchmark(data, config)

    backtest, summary = market_router._risk_budgeted_backtest_payload(
        body=DiscoverBacktestRequest(symbol="TEST"),
        history=SimpleNamespace(metadata={}, rows=[], citations=[]),
        data=data,
        calibration_data=data,
        benchmark_definition=benchmark_definition,
        benchmark_result=benchmark_result,
        maximum_drawdown=0.2,
        config=config,
    )

    result = backtest["result"]
    target = summary["target_exposure"]
    assert 0 < target < 1
    assert abs(result["metrics"]["max_drawdown"]) <= 0.2 + 1e-9
    assert summary["full_sample_budget_satisfied"] is True
    assert result == backtest["exposure_matched_benchmark"]["result"]
    # Fixed shares naturally drift away from the initial allocation as price
    # changes; a constant-weight implementation would keep this flat.
    observed_exposure = [row["position"] for row in result["equity"]]
    assert observed_exposure[0] == pytest.approx(target, abs=0.001)
    assert observed_exposure[-1] < observed_exposure[0]
    signal_state = backtest["diagnostics"]["signal_state"]
    assert signal_state["status"] == "holding"
    assert signal_state["pending_action"] == "hold"
    assert signal_state["requested_signal"] == pytest.approx(target)
    assert signal_state["executed_position"] == pytest.approx(observed_exposure[-1])


def test_screening_constraint_summary_keeps_overlapping_failures_separate() -> None:
    summary = _screening_constraint_summary(
        [
            {"constraint_reasons": ("回撤超预算", "持仓率不足")},
            {"constraint_reasons": ("回撤超预算",)},
            {"constraint_reasons": ()},
        ]
    )

    assert summary == {
        "candidates_evaluated": 3,
        "candidates_passing_base_constraints": 1,
        "constraint_failures": [
            {
                "reason": "回撤超预算",
                "failed_candidates": 2,
                "failure_ratio": pytest.approx(2 / 3),
            },
            {
                "reason": "持仓率不足",
                "failed_candidates": 1,
                "failure_ratio": pytest.approx(1 / 3),
            },
        ],
    }


def test_discovery_screening_goal_changes_development_only_ranking() -> None:
    def result(total_return: float, drawdown: float, sharpe: float) -> SimpleNamespace:
        return SimpleNamespace(
            metrics=SimpleNamespace(
                total_return=total_return,
                max_drawdown=drawdown,
                sharpe_ratio=sharpe,
                exposure_ratio=0.5,
            )
        )

    benchmark = result(0.3, -0.3, 1.0)
    exposure = result(0.1, -0.1, 0.5)
    aggressive = result(0.4, -0.4, 0.5)
    defensive = result(0.2, -0.1, 2.0)
    scores = {
        objective: [
            _screening_objective_score(aggressive, benchmark, exposure, objective),
            _screening_objective_score(defensive, benchmark, exposure, objective),
        ]
        for objective in ("total_return", "sharpe_ratio", "drawdown_control")
    }

    assert scores["total_return"][0] > scores["total_return"][1]
    assert scores["sharpe_ratio"][1] > scores["sharpe_ratio"][0]
    assert scores["drawdown_control"][1] > scores["drawdown_control"][0]
    ranked = [
        {
            "constraint_reasons": (),
            "quality": "lagging",
            "score": 1.0,
            "screening_objective_score": score,
        }
        for score in scores["drawdown_control"]
    ]
    assert sorted(
        ranked,
        key=lambda item: _screening_sort_key(item, "drawdown_control"),
    )[0]["screening_objective_score"] == scores["drawdown_control"][1]


def test_discovery_screening_and_full_optimizer_share_objective_semantics() -> None:
    def result(
        total_return: float,
        *,
        drawdown: float = -0.1,
        sharpe: float = 1.0,
        exposure: float = 0.5,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            metrics=SimpleNamespace(
                total_return=total_return,
                max_drawdown=drawdown,
                sharpe_ratio=sharpe,
                exposure_ratio=exposure,
            )
        )

    benchmark = result(0.12)
    strategy_a = result(0.10)
    exposure_a = result(0.10)
    strategy_b = result(0.09)
    exposure_b = result(0.05)

    for objective in (
        "total_return",
        "sharpe_ratio",
        "drawdown_control",
        "balanced",
    ):
        screening_a = _screening_objective_score(
            strategy_a,
            benchmark,
            exposure_a,
            objective,
        )
        screening_b = _screening_objective_score(
            strategy_b,
            benchmark,
            exposure_b,
            objective,
        )
        assert screening_a == pytest.approx(
            score_optimization_metrics(
                strategy_a.metrics,
                objective,
                benchmark.metrics,
                exposure_a.metrics,
            )
        )
        assert screening_b == pytest.approx(
            score_optimization_metrics(
                strategy_b.metrics,
                objective,
                benchmark.metrics,
                exposure_b.metrics,
            )
        )

    assert _screening_objective_score(
        strategy_b,
        benchmark,
        exposure_b,
        "total_return",
    ) > _screening_objective_score(
        strategy_a,
        benchmark,
        exposure_a,
        "total_return",
    )


def test_template_score_uses_shared_window_return_not_capped_intraday_cagr() -> None:
    def result(total_return: float, annualized_return: float) -> SimpleNamespace:
        return SimpleNamespace(
            metrics=SimpleNamespace(
                total_return=total_return,
                annualized_return=annualized_return,
                max_drawdown=-0.1,
                sharpe_ratio=1.0,
            )
        )

    benchmark = result(0.25, 1_000_000)
    exposure = result(0.4, 1_000_000)
    capped = result(1.0, 1_000_000)
    same_realized_evidence = result(1.0, 0.15)
    lower_realized_return = result(0.8, 1_000_000)

    capped_score, capped_quality = market_router._template_score(
        capped,
        benchmark,
        exposure,
    )
    same_score, same_quality = market_router._template_score(
        same_realized_evidence,
        benchmark,
        exposure,
    )
    lower_score, _ = market_router._template_score(
        lower_realized_return,
        benchmark,
        exposure,
    )

    assert capped_score == pytest.approx(same_score)
    assert capped_quality == same_quality == "outperform"
    assert capped_score > lower_score


def test_discovery_shortlist_reserves_feasible_hypothesis_families() -> None:
    def item(strategy_id: str, *, feasible: bool = True) -> dict:
        return {
            "strategy": {"id": strategy_id},
            "screening_constraint_reasons": () if feasible else ("rejected",),
        }

    ranked = [
        item("sma-cross"),
        item("macd"),
        item("rsi"),
        item("volatility-target-trend"),
        item("bollinger", feasible=False),
    ]

    selected = _select_diverse_discovery_shortlist(ranked, 3)

    assert [entry["strategy"]["id"] for entry in selected] == [
        "sma-cross",
        "rsi",
        "volatility-target-trend",
    ]
    assert _feasible_discovery_strategy_families(ranked) == [
        "trend_or_breakout",
        "mean_reversion",
        "risk_managed_allocation",
    ]


def test_discovery_shortlist_prefers_feasible_recommended_interval() -> None:
    def item(*, recommended: bool, constraints: tuple[str, ...], score: float) -> dict:
        return {
            "interval_recommended": recommended,
            "screening_constraint_reasons": constraints,
            "quality": "outperform",
            "score": score,
            "screening_objective_score": score,
            "market_regime_fit": "aligned",
        }

    recommended = item(recommended=True, constraints=(), score=1)
    unsupported_but_higher_score = item(recommended=False, constraints=(), score=99)
    recommended_but_rejected = item(
        recommended=True,
        constraints=("开发样本持仓率低于下限",),
        score=100,
    )

    ranked = sorted(
        [unsupported_but_higher_score, recommended_but_rejected, recommended],
        key=lambda value: _discovery_shortlist_sort_key(value, "total_return"),
    )

    assert ranked == [recommended, unsupported_but_higher_score, recommended_but_rejected]


def test_strategy_discovery_rejects_tiny_holdout_trade_sample() -> None:
    strategy = BacktestMetrics(
        bars=100,
        duration_years=1,
        total_return=0.2,
        annualized_return=0.2,
        annualized_volatility=0.1,
        sharpe_ratio=2,
        max_drawdown=-0.05,
        win_rate=0.75,
        trades=8,
        closed_trades=8,
        trades_per_year=8,
        average_holding_bars=5,
        profit_factor=2,
        exposure_ratio=0.5,
        max_cash_streak=20,
        max_cash_streak_ratio=0.2,
    )
    benchmark = strategy.model_copy(
        update={"total_return": 0.1, "annualized_return": 0.1}
    )
    exposure_matched = strategy.model_copy(
        update={"total_return": 0.05, "annualized_return": 0.05}
    )

    passed, code, reason = _validate_discovery_holdout(
        strategy,
        benchmark,
        exposure_matched,
        DiscoverBacktestRequest(symbol="TEST"),
    )

    assert passed is False
    assert code == "sample_insufficient"
    assert "少于 10 个已闭合独立决策周期" in reason

    drawdown_failed, drawdown_code, _ = _validate_discovery_holdout(
        strategy.model_copy(update={"max_drawdown": -0.5}),
        benchmark,
        exposure_matched,
        DiscoverBacktestRequest(symbol="TEST", maximum_drawdown=0.3),
    )
    assert drawdown_failed is False
    assert drawdown_code == "drawdown_limit_failed"

    cost_failed, cost_code, _ = _validate_discovery_holdout(
        strategy,
        benchmark,
        exposure_matched,
        DiscoverBacktestRequest(symbol="TEST"),
        cost_stress_passed=False,
    )
    assert cost_failed is False
    assert cost_code == "cost_stress_failed"

    timing_failed, timing_code, _ = _validate_discovery_holdout(
        strategy,
        benchmark,
        strategy.model_copy(update={"total_return": 0.25}),
        DiscoverBacktestRequest(symbol="TEST"),
    )
    assert timing_failed is False
    assert timing_code == "exposure_matched_lag"


def test_strategy_comparison_uses_one_shared_benchmark(tmp_path) -> None:
    response = client(tmp_path).post(
        "/api/v1/backtests/compare",
        json={
            "symbol": "TEST",
            "provider": "yfinance",
            "interval": "1d",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["results"]) == len(STRATEGIES)
    assert payload["evaluation_mode"] == "template_default_snapshot"
    assert "不是参数寻优" in payload["evaluation_note"]
    assert payload["benchmark"]["strategy"]["id"] == "buy-hold"
    assert payload["bars"] == 100
    assert all("quality" in item for item in payload["results"])
    assert all("trades_per_year" in item["metrics"] for item in payload["results"])
    assert all(
        "exposure_matched_benchmark_metrics" in item
        and "timing_comparison" in item
        for item in payload["results"]
    )


def test_backtest_experiment_can_be_saved_listed_and_deleted(tmp_path) -> None:
    api = client(tmp_path)
    backtest = api.post(
        "/api/v1/backtests",
        json={"symbol": "TEST", "provider": "yfinance", "strategy_id": "sma-cross"},
    ).json()
    create_payload = {
        "name": "测试标的 · 双均线 · 日线",
        "notes": "用于验证可复现实验档案。",
        "instrument": {
            "symbol": "TEST",
            "name": "测试标的",
            "market": "US",
            "exchange": "测试交易所",
            "currency": "USD",
            "provider": "yfinance",
            "asset_type": "equity",
        },
        "strategy": {
            "id": backtest["strategy"]["id"],
            "name": backtest["strategy"]["name"],
            "category": backtest["strategy"]["category"],
            "parameters": backtest["strategy"]["parameters"],
        },
        "interval": "1d",
        "start": "2025-01-01",
        "end": "2025-06-01",
        "optimized": False,
        "run_request": {
            "symbol": "TEST",
            "provider": "yfinance",
            "strategy_id": "sma-cross",
            "parameters": backtest["strategy"]["parameters"],
        },
        "engine_config": backtest["result"]["config"],
        "data_metadata": backtest["data_metadata"],
        "metrics": backtest["result"]["metrics"],
        "benchmark_metrics": backtest["benchmark"]["result"]["metrics"],
        "comparison": backtest["comparison"],
        "diagnostics": backtest["diagnostics"],
        "citations": backtest["citations"],
    }

    created = api.post("/api/v1/experiments", json=create_payload)

    assert created.status_code == 201
    experiment = created.json()
    assert experiment["instrument"]["symbol"] == "TEST"
    assert experiment["strategy"]["id"] == "sma-cross"
    assert experiment["metrics"]["bars"] == 100

    listed = api.get("/api/v1/experiments", params={"query": "双均线"}).json()
    assert [item["id"] for item in listed] == [experiment["id"]]
    assert listed[0]["engine_config"]["signal_delay_bars"] == 1
    assert len(listed[0]["diagnostics"]["segments"]) == 4

    deleted = api.delete(f"/api/v1/experiments/{experiment['id']}")
    assert deleted.status_code == 204
    assert api.get("/api/v1/experiments").json() == []


def test_backtest_experiment_from_run_is_authoritative_and_idempotent(
    tmp_path,
) -> None:
    api = client(tmp_path, build_version="trusted-run-build")
    backtest_response = api.post(
        "/api/v1/backtests",
        json={"symbol": "TEST", "provider": "yfinance", "strategy_id": "sma-cross"},
    )
    assert backtest_response.status_code == 200
    backtest = backtest_response.json()
    assert len(backtest["run_id"]) == 32
    assert backtest["run_manifest"]["manifest_id"]
    assert backtest["dataset_snapshot"]["snapshot_id"]

    archive_request = {
        "name": "可信单标的实验",
        "notes": "只允许客户端补充标签。",
        "run_id": backtest["run_id"],
        "instrument_name": "测试标的",
    }
    created_response = api.post(
        "/api/v1/experiments/from-run",
        json=archive_request,
    )

    assert created_response.status_code == 201
    created = created_response.json()
    assert created["source_run_id"] == backtest["run_id"]
    assert created["provenance_status"] == "server_verified"
    assert (
        created["run_manifest"]["manifest_id"]
        == backtest["run_manifest"]["manifest_id"]
    )
    assert created["metrics"] == backtest["result"]["metrics"]
    assert created["strategy"]["parameters"] == backtest["strategy"]["parameters"]
    assert created["data_metadata"] == backtest["data_metadata"]
    assert created["instrument"]["name"] == "测试标的"

    spoofed = api.post(
        "/api/v1/experiments/from-run",
        json={**archive_request, "metrics": {"total_return": 999}},
    )
    assert spoofed.status_code == 422
    assert "extra_forbidden" in spoofed.text

    duplicate = api.post(
        "/api/v1/experiments/from-run",
        json={**archive_request, "name": "该名称不会产生第二份记录"},
    )
    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == created["id"]
    assert duplicate.json()["name"] == created["name"]

    listed = api.get("/api/v1/experiments").json()
    assert [item["id"] for item in listed] == [created["id"]]


def test_backtest_from_run_reports_missing_and_issues_fresh_evidenced_rerun(
    tmp_path,
) -> None:
    api = client(tmp_path)
    request_payload = {
        "symbol": "TEST",
        "provider": "yfinance",
        "strategy_id": "sma-cross",
    }
    first = api.post("/api/v1/backtests", json=request_payload).json()
    archive_request = {
        "name": "等待归档的回测",
        "run_id": first["run_id"],
    }

    missing = api.post(
        "/api/v1/experiments/from-run",
        json={**archive_request, "run_id": "f" * 32},
    )
    assert missing.status_code == 404

    with sqlite3.connect(tmp_path / "data.db") as connection:
        connection.execute(
            "UPDATE research_runs SET created_at = ?, expires_at = ? "
            "WHERE run_id = ?",
            (
                (datetime.now(UTC) - timedelta(days=2)).isoformat(),
                (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
                first["run_id"],
            ),
        )
    expired = api.post("/api/v1/experiments/from-run", json=archive_request)
    assert expired.status_code == 410

    rerun = api.post("/api/v1/backtests", json=request_payload)
    assert rerun.status_code == 200
    fresh = rerun.json()
    # A v2 dataset identity binds the complete acquisition evidence, including
    # citation timestamps. A fresh provider read is therefore a new snapshot
    # and run, not a silent renewal of the expired receipt.
    assert fresh["run_id"] != first["run_id"]
    assert (
        fresh["dataset_snapshot"]["snapshot_id"]
        != first["dataset_snapshot"]["snapshot_id"]
    )
    assert fresh["run_expires_at"] > first["run_expires_at"]

    still_expired = api.post("/api/v1/experiments/from-run", json=archive_request)
    assert still_expired.status_code == 410

    archived = api.post(
        "/api/v1/experiments/from-run",
        json={**archive_request, "run_id": fresh["run_id"]},
    )
    assert archived.status_code == 201
    assert archived.json()["source_run_id"] == fresh["run_id"]


def test_experiment_rejects_reversed_date_range(tmp_path) -> None:
    response = client(tmp_path).post(
        "/api/v1/experiments",
        json={
            "name": "错误区间",
            "instrument": {
                "symbol": "TEST",
                "name": "测试标的",
                "market": "US",
                "provider": "yfinance",
            },
            "strategy": {"id": "sma-cross", "name": "双均线"},
            "interval": "1d",
            "start": "2025-06-01",
            "end": "2025-01-01",
            "metrics": {
                "bars": 1,
                "duration_years": 1,
                "total_return": 0,
                "annualized_return": 0,
                "annualized_volatility": 0,
                "sharpe_ratio": 0,
                "max_drawdown": 0,
                "win_rate": 0,
                "trades": 0,
                "closed_trades": 0,
                "trades_per_year": 0,
                "average_holding_bars": 0,
                "profit_factor": 0,
                "exposure_ratio": 0,
                "max_cash_streak": 1,
                "max_cash_streak_ratio": 1,
            },
            "benchmark_metrics": {
                "bars": 1,
                "duration_years": 1,
                "total_return": 0,
                "annualized_return": 0,
                "annualized_volatility": 0,
                "sharpe_ratio": 0,
                "max_drawdown": 0,
                "win_rate": 0,
                "trades": 0,
                "closed_trades": 0,
                "trades_per_year": 0,
                "average_holding_bars": 0,
                "profit_factor": 0,
                "exposure_ratio": 0,
                "max_cash_streak": 1,
                "max_cash_streak_ratio": 1,
            },
            "comparison": {
                "excess_return": 0,
                "excess_annualized_return": 0,
                "drawdown_improvement": 0,
                "beats_benchmark": False,
                "positive_return": False,
            },
        },
    )

    assert response.status_code == 422
    assert "开始日期" in response.json()["detail"]


def test_provisional_experiment_can_be_forward_tracked_without_broker_actions(
    tmp_path,
    monkeypatch,
) -> None:
    api = client(tmp_path)
    backtest = api.post(
        "/api/v1/backtests",
        json={"symbol": "TEST", "provider": "yfinance", "strategy_id": "ema-cross"},
    ).json()
    experiment_payload = {
        "name": "测试纸面信号",
        "instrument": {
            "symbol": "TEST",
            "name": "测试标的",
            "market": "US",
            "exchange": "测试交易所",
            "currency": "USD",
            "provider": "yfinance",
            "asset_type": "equity",
        },
        "strategy": {
            "id": backtest["strategy"]["id"],
            "name": backtest["strategy"]["name"],
            "category": backtest["strategy"]["category"],
            "parameters": backtest["strategy"]["parameters"],
        },
        "interval": "1d",
        "start": "2025-01-01",
        "end": "2025-06-01",
        "run_request": {},
        "engine_config": backtest["result"]["config"],
        "data_metadata": backtest["data_metadata"],
        "metrics": backtest["result"]["metrics"],
        "benchmark_metrics": backtest["benchmark"]["result"]["metrics"],
        "comparison": backtest["comparison"],
        "diagnostics": backtest["diagnostics"],
        "citations": backtest["citations"],
    }
    unvalidated = api.post(
        "/api/v1/experiments",
        json=experiment_payload,
    ).json()
    blocked = api.post(
        "/api/v1/paper-tracks",
        json={"experiment_id": unvalidated["id"]},
    )
    assert blocked.status_code == 422
    assert "样本外验证" in blocked.json()["detail"]
    assert api.delete(f"/api/v1/experiments/{unvalidated['id']}").status_code == 204

    experiment_payload["validation"] = {
        "objective": "balanced",
        "split_date": "2025-05-01",
        "validation_passed": False,
        "validation_code": "sample_insufficient",
        "validation_reason": "最终留出交易样本不足。",
        "forward_observation_eligible": True,
        "development_metrics": backtest["result"]["metrics"],
        "validation_metrics": {
            **backtest["result"]["metrics"],
            "closed_trades": 4,
            "trades": 4,
        },
        "validation_benchmark_metrics": backtest["benchmark"]["result"]["metrics"],
        "validation_exposure_matched_benchmark_metrics": {
            **backtest["exposure_matched_benchmark"]["result"]["metrics"],
            "total_return": 0,
            "annualized_return": 0,
        },
    }
    legacy_validation = dict(experiment_payload["validation"])
    legacy_validation.pop("validation_exposure_matched_benchmark_metrics")
    legacy_validation["validation_metrics"] = {
        **legacy_validation["validation_metrics"],
        "closed_trades": 8,
        "trades": 8,
    }
    legacy_experiment = api.post(
        "/api/v1/experiments",
        json={**experiment_payload, "validation": legacy_validation},
    ).json()
    legacy_blocked = api.post(
        "/api/v1/paper-tracks",
        json={"experiment_id": legacy_experiment["id"]},
    )
    assert legacy_blocked.status_code == 422
    assert "不满足" in legacy_blocked.json()["detail"]
    assert (
        api.delete(f"/api/v1/experiments/{legacy_experiment['id']}").status_code
        == 204
    )
    invalid_provisional = api.post(
        "/api/v1/experiments",
        json=experiment_payload,
    )
    assert invalid_provisional.status_code == 422
    assert "5–9 笔" in invalid_provisional.text
    experiment_payload["validation"]["validation_metrics"].update(
        {"closed_trades": 8, "trades": 8}
    )
    experiment_payload["validation"].update(
        {
            "validation_passed": True,
            "validation_code": "passed",
            "forward_observation_eligible": False,
        }
    )
    invalid_validated = api.post(
        "/api/v1/experiments",
        json=experiment_payload,
    )
    assert invalid_validated.status_code == 422
    assert "少于 10 个已闭合独立决策周期" in invalid_validated.text
    experiment_payload["validation"].update(
        {
            "validation_passed": False,
            "validation_code": "sample_insufficient",
            "forward_observation_eligible": True,
        }
    )
    experiment_payload["validation"][
        "validation_exposure_matched_benchmark_metrics"
    ].update({"total_return": 1, "annualized_return": 1})
    invalid_timing = api.post(
        "/api/v1/experiments",
        json=experiment_payload,
    )
    assert invalid_timing.status_code == 422
    assert "相同起点投入比例的固定份额被动基准" in invalid_timing.text
    experiment_payload["validation"][
        "validation_exposure_matched_benchmark_metrics"
    ].update({"total_return": 0, "annualized_return": 0})
    experiment = api.post(
        "/api/v1/experiments",
        json=experiment_payload,
    ).json()

    # Exercise the paper router's explicit cycle-baseline plumbing. The real
    # helper returns this contract only for core-trend-allocation; a clipped
    # signal here keeps the broader API fixture small while proving every
    # rolling snapshot forwards the series to the engine and forward ledger.
    from quantsieve_api.routers import paper as paper_router

    captured_cycle_baselines = []
    actual_run_backtest = paper_router.run_backtest

    def cycle_baseline_for_test(strategy_id, signals, parameters):
        del strategy_id, parameters
        return signals.clip(upper=0.25)

    def capture_run_backtest(*args, **kwargs):
        captured_cycle_baselines.append(kwargs.get("cycle_baseline_signals"))
        return actual_run_backtest(*args, **kwargs)

    monkeypatch.setattr(
        paper_router,
        "strategy_cycle_baseline_signals",
        cycle_baseline_for_test,
    )
    monkeypatch.setattr(paper_router, "run_backtest", capture_run_backtest)

    created = api.post(
        "/api/v1/paper-tracks",
        json={"experiment_id": experiment["id"]},
    )

    assert created.status_code == 201
    track = created.json()
    assert track["status"] == "active"
    assert track["snapshot_count"] == 1
    assert track["observed_bar_count"] == 1
    assert len(track["snapshots"]) == 1
    snapshot = track["snapshots"][0]
    assert snapshot["latest_price"] == 200
    assert snapshot["signal_state"]["status"] in {
        "pending_entry",
        "pending_exit",
        "holding",
        "cash",
    }
    assert "order" not in snapshot
    assert "paper_return" not in snapshot
    assert snapshot["forward"]["bars"] == 0
    assert snapshot["forward"]["total_return"] == 0
    assert snapshot["forward"]["benchmark_total_return"] == 0
    assert snapshot["forward"]["equity"] == pytest.approx(
        backtest["result"]["config"]["initial_cash"]
    )
    assert snapshot["forward"]["position"] == 0
    assert snapshot["forward"]["orders"] == 0
    assert snapshot["forward"]["cycle_kind"] == "satellite_over_core"
    assert captured_cycle_baselines
    assert all(
        baseline is not None for baseline in captured_cycle_baselines
    )
    assert track["forward_health"]["status"] == "baseline"
    assert track["forward_health"]["assessment_ready"] is False
    assert track["forward_health"]["evidence_bars"] == 0

    duplicate = api.post(
        "/api/v1/paper-tracks",
        json={"experiment_id": experiment["id"]},
    )
    assert duplicate.status_code == 409

    scheduler_status = api.get("/api/v1/paper-tracks/scheduler").json()
    assert scheduler_status["enabled"] is True
    assert scheduler_status["poll_seconds"] == 60
    scheduler = api.app.state.paper_track_scheduler
    assert (
        asyncio.run(
            scheduler.refresh_due(now=datetime.now(UTC) + timedelta(hours=2))
        )
        == 1
    )
    refreshed = api.get("/api/v1/paper-tracks").json()[0]
    assert refreshed["snapshot_count"] == 1
    assert refreshed["observed_bar_count"] == 1
    assert len(refreshed["snapshots"]) == 1
    assert refreshed["last_checked_at"] is not None
    assert refreshed["snapshots"][0]["forward"]["bars"] == 0
    fake_router = api.app.state.providers
    fake_router.provider.extra_periods = 3
    assert (
        asyncio.run(
            scheduler.refresh_due(now=datetime.now(UTC) + timedelta(hours=4))
        )
        == 1
    )
    caught_up = api.get("/api/v1/paper-tracks").json()[0]
    assert caught_up["snapshot_count"] == 4
    assert caught_up["observed_bar_count"] == 4
    assert len(caught_up["snapshots"]) == 4
    assert caught_up["snapshots"][0]["latest_price"] == 203
    assert len({item["data_as_of"] for item in caught_up["snapshots"]}) == 4
    latest_forward = caught_up["snapshots"][0]["forward"]
    assert latest_forward["bars"] == 3
    assert latest_forward["total_return"] > 0
    assert latest_forward["benchmark_total_return"] > 0
    assert latest_forward["total_return"] == pytest.approx(
        latest_forward["benchmark_total_return"]
    )
    assert latest_forward["orders"] == 1
    assert latest_forward["round_trips"] == 0
    assert [item["forward"]["bars"] for item in caught_up["snapshots"]] == [
        3,
        2,
        1,
        0,
    ]
    executions = [
        item["forward"]["execution"]
        for item in caught_up["snapshots"]
        if item["forward"]["execution"]
    ]
    assert len(executions) == 1
    assert executions[0]["side"] == "buy"
    assert caught_up["forward_health"]["status"] == "collecting"
    assert caught_up["forward_health"]["evidence_bars"] == 3
    assert caught_up["forward_health"]["round_trips"] == 0
    assert caught_up["forward_health"]["assessment_ready"] is False
    store = api.app.state.paper_track_store
    persisted_track = store.get(track["id"])
    assert persisted_track is not None
    stale_snapshot = persisted_track.snapshots[0].model_copy(
        update={"id": "stale-snapshot"}
    )
    stale_result = store.add_snapshots(
        track["id"],
        [stale_snapshot],
        expected_snapshot_count=0,
    )
    assert stale_result.snapshot_count == 4
    fake_router.provider.start_offset = 500
    continuity_failure = api.post(f"/api/v1/paper-tracks/{track['id']}/refresh")
    assert continuity_failure.status_code == 422
    assert "无法无损补齐中间 K 线" in continuity_failure.json()["detail"]
    assert "无法无损补齐中间 K 线" in api.get(
        "/api/v1/paper-tracks"
    ).json()[0]["last_error"]
    paused = api.patch(
        f"/api/v1/paper-tracks/{track['id']}",
        json={"status": "paused"},
    ).json()
    assert paused["status"] == "paused"
    assert (
        asyncio.run(
            scheduler.refresh_due(now=datetime.now(UTC) + timedelta(hours=6))
        )
        == 0
    )
    assert len(api.get("/api/v1/paper-tracks").json()[0]["snapshots"]) == 4

    api.delete(f"/api/v1/experiments/{experiment['id']}")
    persisted = api.get("/api/v1/paper-tracks").json()
    assert persisted[0]["experiment"]["name"] == "测试纸面信号"
    assert len(persisted[0]["snapshots"]) == 4

    assert api.delete(f"/api/v1/paper-tracks/{track['id']}").status_code == 204
    assert api.get("/api/v1/paper-tracks").json() == []


def test_custom_backtest_runs_generated_code_in_sandbox(tmp_path) -> None:
    response = client(tmp_path).post(
        "/api/v1/backtests/custom",
        json={
            "symbol": "TEST",
            "name": "收盘价趋势",
            "code": (
                "def generate_signals(data):\n"
                "    average = data['close'].rolling(10).mean()\n"
                "    return (data['close'] > average).astype(float)\n"
            ),
        },
    )

    assert response.status_code == 200
    assert response.json()["strategy"]["id"] == "custom"
    assert len(response.json()["result"]["equity"]) == 100
    assert response.json()["citations"][0]["source"] == "Test fixture"


def test_custom_backtest_rejects_filesystem_access(tmp_path) -> None:
    response = client(tmp_path).post(
        "/api/v1/backtests/custom",
        json={
            "symbol": "TEST",
            "code": (
                "def generate_signals(data):\n"
                "    open('secret.txt').read()\n"
                "    return data['close'] * 0\n"
            ),
        },
    )

    assert response.status_code == 422
    assert "not allowed" in response.json()["detail"]


def test_monitor_hides_future_events(tmp_path) -> None:
    api = client(tmp_path)
    assert all(
        event["available_at"] <= (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
        for event in api.get("/api/v1/monitor/feed").json()
    )


def test_monitor_translation_only_accepts_persisted_visible_event_keys(
    tmp_path,
) -> None:
    api = client(tmp_path)
    now = datetime.now(UTC)
    visible = MonitorEvent(
        source="official-intel",
        source_id="translation-visible",
        profile_id="central-bank-watch",
        profile_name="全球央行与宏观政策",
        kind=EventKind.NEWS,
        title="Federal Reserve holds rates steady",
        content="Inflation risks remain elevated.",
        url="https://example.com/translation-visible",
        occurred_at=now,
        available_at=now,
    )
    future = visible.model_copy(
        update={
            "source_id": "translation-future",
            "available_at": now + timedelta(hours=1),
        }
    )
    api.app.state.event_store.upsert([visible, future])

    response = api.post(
        "/api/v1/monitor/translations",
        json={
            "items": [
                {
                    "source": visible.source,
                    "source_id": visible.source_id,
                },
                {
                    "source": future.source,
                    "source_id": future.source_id,
                },
                {
                    "source": "unknown",
                    "source_id": "missing",
                },
            ]
        },
    )

    assert response.status_code == 200, response.json()
    items = response.json()["items"]
    assert items[0]["status"] == "unavailable"
    assert items[0]["title_zh"] == visible.title
    assert items[1]["status"] == "not_found"
    assert items[2]["status"] == "not_found"
    assert response.json()["engine"] == "libretranslate-argos"
    assert response.json()["availability"] == "disabled"

    arbitrary_text = api.post(
        "/api/v1/monitor/translations",
        json={
            "items": [
                {
                    "source": visible.source,
                    "source_id": visible.source_id,
                    "text": "translate arbitrary input",
                }
            ]
        },
    )
    assert arbitrary_text.status_code == 422


def test_monitor_translation_enforces_unique_dynamic_batch_limit(tmp_path) -> None:
    api = client(tmp_path)
    duplicate = {
        "source": "official-intel",
        "source_id": "same",
    }

    duplicate_response = api.post(
        "/api/v1/monitor/translations",
        json={"items": [duplicate, duplicate]},
    )
    oversized_response = api.post(
        "/api/v1/monitor/translations",
        json={
            "items": [
                {
                    "source": "official-intel",
                    "source_id": f"item-{index}",
                }
                for index in range(19)
            ]
        },
    )

    assert duplicate_response.status_code == 422
    assert oversized_response.status_code == 422
    assert "最多翻译 18 条" in oversized_response.json()["detail"]


def test_monitor_feed_keeps_official_and_market_evidence_visible(tmp_path) -> None:
    api = client(tmp_path)
    now = datetime.now(UTC)
    noisy = [
        MonitorEvent(
            source="gdelt-headlines",
            source_id=f"headline-{index}",
            profile_id="geopolitical-watch",
            profile_name="全球地缘事件",
            kind=EventKind.GEOPOLITICAL,
            title=f"Headline {index}",
            content="Market headline",
            url=f"https://publisher.example/{index}",
            occurred_at=now - timedelta(seconds=index),
            available_at=now - timedelta(seconds=index),
            analysis="Scenario",
            market_relevance="high",
            analysis_method="rules",
        )
        # More rows than the API's bounded global recency window: source
        # sampling must still recover the older official and market anchors.
        for index in range(1_100)
    ]
    anchors = [
        MonitorEvent(
            source="official-intel",
            source_id="fed-1",
            profile_id="central-bank-watch",
            profile_name="全球央行",
            kind=EventKind.NEWS,
            title="Federal Reserve update",
            content="Official update",
            url="https://federalreserve.gov/update",
            occurred_at=now - timedelta(hours=12),
            available_at=now - timedelta(hours=12),
            analysis="Rates scenario",
            market_relevance="medium",
            analysis_method="rules",
        ),
        MonitorEvent(
            source="linked-market-data",
            source_id="btc-1",
            profile_id="market-bitcoin",
            profile_name="Bitcoin",
            kind=EventKind.MARKET,
            title="BTCUSDT 行情脉搏",
            content="Real market evidence",
            url="https://binance.com",
            occurred_at=now - timedelta(hours=10),
            available_at=now - timedelta(hours=10),
            analysis="Market evidence",
            market_relevance="unrated",
            analysis_method="rules",
        ),
    ]
    api.app.state.event_store.upsert([*noisy, *anchors])

    payload = api.get("/api/v1/monitor/feed", params={"limit": 20}).json()
    sources = [event["source"] for event in payload]

    assert 2 <= len(payload) <= 20
    assert len({(event["source"], event["source_id"]) for event in payload}) == len(
        payload
    )
    assert "official-intel" in sources
    assert "linked-market-data" in sources
    assert sources.count("gdelt-headlines") <= 7
    assert "official-intel" in sources[:3]
    assert "linked-market-data" in sources[:3]


def test_monitor_curation_uses_score_when_limit_is_smaller_than_anchor_set() -> None:
    now = datetime.now(UTC)
    low_social = MonitorEvent(
        source="public-rss",
        source_id="social-low",
        profile_id="person",
        profile_name="Person",
        kind=EventKind.SOCIAL,
        title="Low relevance social item",
        content="Low relevance item",
        url="https://example.com/social",
        occurred_at=now,
        available_at=now,
        market_relevance="low",
    )
    critical_market = MonitorEvent(
        source="linked-market-data",
        source_id="market-critical",
        profile_id="market-bitcoin",
        profile_name="Bitcoin",
        kind=EventKind.MARKET,
        title="Critical market evidence",
        content="Critical market evidence",
        url="https://example.com/market",
        occurred_at=now - timedelta(minutes=1),
        available_at=now - timedelta(minutes=1),
        market_relevance="critical",
    )

    payload = _curate_events(
        [low_social, critical_market],
        limit=1,
        profile_id=None,
    )

    assert [event.source_id for event in payload] == ["market-critical"]


def test_monitor_profile_curation_keeps_newest_event_first() -> None:
    now = datetime.now(UTC)
    recent = MonitorEvent(
        source="x-api",
        source_id="recent-low",
        profile_id="person",
        profile_name="Person",
        kind=EventKind.SOCIAL,
        title="Recent post",
        content="Recent post",
        url="https://example.com/recent",
        occurred_at=now,
        available_at=now,
        market_relevance="low",
    )
    old_critical = MonitorEvent(
        source="x-api",
        source_id="old-critical",
        profile_id="person",
        profile_name="Person",
        kind=EventKind.SOCIAL,
        title="Older critical post",
        content="Older critical post",
        url="https://example.com/old",
        occurred_at=now - timedelta(days=2),
        available_at=now - timedelta(days=2),
        market_relevance="critical",
    )

    payload = _curate_events(
        [old_critical, recent],
        limit=1,
        profile_id="person",
    )

    assert [event.source_id for event in payload] == ["recent-low"]


def test_monitor_curation_prefers_authority_and_keeps_distinct_market_cards() -> None:
    now = datetime.now(UTC)
    shared_article = "https://example.com/shared-article"
    aggregate = MonitorEvent(
        source="gdelt-headlines",
        source_id="aggregate-newer",
        profile_id="global-headline-watch",
        profile_name="全球新闻标题",
        kind=EventKind.NEWS,
        title="Shared report",
        content="Headline-level report",
        url=shared_article,
        occurred_at=now,
        available_at=now,
        market_relevance="low",
    )
    official = MonitorEvent(
        source="official-intel",
        source_id="official-authoritative",
        profile_id="central-bank-watch",
        profile_name="全球央行",
        kind=EventKind.NEWS,
        title="Shared report",
        content="Official release",
        url=shared_article,
        occurred_at=now - timedelta(minutes=2),
        available_at=now - timedelta(minutes=2),
        market_relevance="critical",
    )
    market_cards = [
        MonitorEvent(
            source="linked-market-data",
            source_id=f"{profile_id}-pulse",
            profile_id=profile_id,
            profile_name=profile_name,
            kind=EventKind.MARKET,
            title=f"{profile_name} market pulse",
            content="Independent related-market evidence",
            url="https://example.com/shared-market-provider",
            occurred_at=now - timedelta(minutes=3),
            available_at=now - timedelta(minutes=3),
            market_relevance="medium",
        )
        for profile_id, profile_name in (
            ("market-bitcoin", "Bitcoin"),
            ("market-ethereum", "Ethereum"),
        )
    ]

    payload = _curate_events(
        [aggregate, official, *market_cards],
        limit=10,
        profile_id=None,
    )
    source_ids = {event.source_id for event in payload}

    assert "official-authoritative" in source_ids
    assert "aggregate-newer" not in source_ids
    assert {"market-bitcoin-pulse", "market-ethereum-pulse"} <= source_ids


def test_monitor_feed_rejects_empty_profile_filter(tmp_path) -> None:
    response = client(tmp_path).get(
        "/api/v1/monitor/feed",
        params={"profile_id": ""},
    )

    assert response.status_code == 422
