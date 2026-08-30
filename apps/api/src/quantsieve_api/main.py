from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from typing import Protocol

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from quantsieve_engine.risk import (
    OrderRiskLimits,
    build_order_rule_set,
)
from quantsieve_monitor import EventStore
from quantsieve_providers import SQLiteCache

from .config import Settings, get_settings
from .experiments import ExperimentStore
from .monitoring import MonitorScheduler
from .news_translation import NewsTranslationService
from .paper import PaperTrackStore
from .paper_oms import PaperOmsStore, ServerOwnedPaperKillSwitch
from .paper_oms_risk_service import (
    PaperOmsExecutionPriceProvider,
    PaperOmsOrderSubmissionService,
)
from .portfolio_paper import PortfolioPaperTrackStore
from .portfolio_paper_scheduler import PortfolioPaperOpeningScheduler
from .portfolio_paper_service import (
    PortfolioPaperExecutionQuoteProvider,
    PortfolioPaperOpeningService,
)
from .portfolio_paper_settlement_scheduler import (
    PortfolioPaperSettlementScheduler,
)
from .portfolio_paper_settlement_service import (
    PortfolioPaperSettlementHistoryProvider,
    PortfolioPaperSettlementService,
)
from .providers import ProviderRouter
from .research_runs import ResearchRunStore
from .routers import (
    chat_router,
    experiments_router,
    factors_router,
    market_router,
    monitor_router,
    paper_oms_router,
    paper_router,
    portfolio_paper_router,
)
from .routers.monitor import ensure_demo_event
from .routers.paper import PaperTrackScheduler
from .schemas import HealthResponse

VERSION = "0.1.0"


class _ApplicationScheduler(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


async def _stop_application_schedulers(
    schedulers: tuple[tuple[str, _ApplicationScheduler], ...],
    *,
    primary_error: BaseException | None,
) -> None:
    cleanup_errors: list[tuple[str, BaseException]] = []
    for name, scheduler in reversed(schedulers):
        try:
            await scheduler.stop()
        except BaseException as error:
            cleanup_errors.append((name, error))

    if not cleanup_errors:
        return
    if primary_error is not None:
        failures = ", ".join(
            f"{name} ({type(error).__name__})"
            for name, error in cleanup_errors
        )
        primary_error.add_note(f"Scheduler cleanup also failed: {failures}.")
        return
    if len(cleanup_errors) == 1:
        raise cleanup_errors[0][1]
    raise BaseExceptionGroup(
        "Multiple scheduler cleanup operations failed.",
        [error for _, error in cleanup_errors],
    )


def create_app(
    settings: Settings | None = None,
    *,
    portfolio_opening_provider: PortfolioPaperExecutionQuoteProvider | None = None,
    portfolio_settlement_provider: (
        PortfolioPaperSettlementHistoryProvider | None
    ) = None,
    paper_oms_price_provider: PaperOmsExecutionPriceProvider | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    build_version = settings.build_version.strip() or VERSION

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        schedulers: tuple[tuple[str, _ApplicationScheduler], ...] = (
            ("monitor", app.state.monitor_scheduler),
            ("paper", app.state.paper_track_scheduler),
            ("portfolio-opening", app.state.portfolio_opening_scheduler),
            ("portfolio-settlement", app.state.portfolio_settlement_scheduler),
        )
        primary_error: BaseException | None = None
        try:
            for _, scheduler in schedulers:
                await scheduler.start()
            yield
        except BaseException as error:
            primary_error = error
            raise
        finally:
            await _stop_application_schedulers(
                schedulers,
                primary_error=primary_error,
            )

    app = FastAPI(
        title=settings.app_name,
        version=build_version,
        description="Traceable AI investment research and backtesting API.",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.factor_research_semaphore = asyncio.Semaphore(
        settings.factor_research_max_concurrency
    )
    app.state.factor_research_provider_semaphore = asyncio.Semaphore(
        settings.factor_research_provider_max_concurrency
    )
    app.state.factor_research_provider_tasks = set()
    app.state.providers = ProviderRouter(settings.cache_path)
    app.state.event_store = EventStore(settings.database_path)
    app.state.news_translation_service = NewsTranslationService(
        enabled=settings.translation_enabled,
        base_url=settings.translation_base_url,
        cache=SQLiteCache(settings.cache_path),
        timeout_seconds=settings.translation_timeout_seconds,
        cache_ttl_days=settings.translation_cache_ttl_days,
        contract_version=settings.translation_contract_version,
    )
    app.state.experiment_store = ExperimentStore(settings.database_path)
    app.state.research_run_store = ResearchRunStore(settings.database_path)
    app.state.paper_track_store = PaperTrackStore(settings.database_path)
    app.state.paper_oms_kill_switch = ServerOwnedPaperKillSwitch(
        settings.database_path
    )
    app.state.paper_oms_order_risk_rule_set = build_order_rule_set(
        OrderRiskLimits(
            maximum_order_notional=Decimal("10000"),
            maximum_resulting_position=Decimal("1000000"),
            maximum_active_orders=20,
            minimum_cash_reserve=Decimal(0),
            maximum_price_age_seconds=5,
            maximum_kill_switch_age_seconds=5,
            market_order_price_buffer_ratio=Decimal("0.02"),
            order_fee_buffer_ratio=Decimal("0.002"),
        ),
        rule_set_id="quantsieve.paper-oms.server-policy",
        rule_set_version="1.0.0",
        compatibility_profile="paper-oms-market-v1",
    )
    app.state.paper_oms_store = PaperOmsStore(
        settings.database_path,
        order_risk_rule_set=app.state.paper_oms_order_risk_rule_set,
        kill_switch_reader=app.state.paper_oms_kill_switch.read,
    )
    app.state.paper_oms_price_provider = (
        paper_oms_price_provider
        if paper_oms_price_provider is not None
        else app.state.providers.binance_provider
    )
    app.state.paper_oms_submission_service = PaperOmsOrderSubmissionService(
        store=app.state.paper_oms_store,
        provider=app.state.paper_oms_price_provider,
        symbol_is_supported=lambda symbol: (
            app.state.providers.resolve(symbol)
            is app.state.providers.binance_provider
        ),
    )
    app.state.portfolio_paper_track_store = PortfolioPaperTrackStore(
        settings.database_path
    )
    app.state.portfolio_opening_provider = (
        portfolio_opening_provider
        if portfolio_opening_provider is not None
        else app.state.providers.binance_provider
    )
    app.state.portfolio_opening_service = PortfolioPaperOpeningService(
        store=app.state.portfolio_paper_track_store,
        provider=app.state.portfolio_opening_provider,
        quote_deadline_seconds=settings.portfolio_opening_quote_deadline_seconds,
        lease_for=timedelta(seconds=settings.portfolio_opening_lease_seconds),
    )
    app.state.portfolio_opening_scheduler = PortfolioPaperOpeningScheduler(
        service=app.state.portfolio_opening_service,
        enabled=settings.portfolio_opening_scheduler_enabled,
        poll_seconds=settings.portfolio_opening_poll_seconds,
    )
    app.state.portfolio_settlement_provider = (
        portfolio_settlement_provider
        if portfolio_settlement_provider is not None
        else app.state.providers.binance_provider
    )
    app.state.portfolio_settlement_service = PortfolioPaperSettlementService(
        store=app.state.portfolio_paper_track_store,
        provider=app.state.portfolio_settlement_provider,
        history_deadline_seconds=(
            settings.portfolio_settlement_history_deadline_seconds
        ),
        lease_for=timedelta(
            seconds=settings.portfolio_settlement_lease_seconds
        ),
    )
    app.state.portfolio_settlement_scheduler = (
        PortfolioPaperSettlementScheduler(
            service=app.state.portfolio_settlement_service,
            enabled=settings.portfolio_settlement_scheduler_enabled,
            poll_seconds=settings.portfolio_settlement_poll_seconds,
        )
    )
    app.state.paper_track_scheduler = PaperTrackScheduler(
        app.state.paper_track_store,
        app.state.providers,
        enabled=settings.paper_scheduler_enabled,
        poll_seconds=settings.paper_poll_seconds,
    )
    app.state.monitor_scheduler = MonitorScheduler(
        settings,
        app.state.event_store,
        app.state.providers,
    )
    ensure_demo_event(app.state.event_store)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", version=build_version)

    app.include_router(chat_router, prefix="/api/v1")
    app.include_router(experiments_router, prefix="/api/v1")
    app.include_router(factors_router, prefix="/api/v1")
    app.include_router(market_router, prefix="/api/v1")
    app.include_router(monitor_router, prefix="/api/v1")
    app.include_router(paper_router, prefix="/api/v1")
    app.include_router(paper_oms_router, prefix="/api/v1")
    app.include_router(portfolio_paper_router, prefix="/api/v1")
    return app


app = create_app()


def run() -> None:
    uvicorn.run("quantsieve_api.main:app", host="0.0.0.0", port=8000, reload=False)
