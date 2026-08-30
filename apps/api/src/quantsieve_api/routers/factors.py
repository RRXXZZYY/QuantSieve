from __future__ import annotations

import asyncio
from time import monotonic
from typing import cast

from fastapi import APIRouter, HTTPException, Request
from quantsieve_providers import DataEnvelope

from ..config import Settings
from ..factor_research_service import (
    FactorResearchRequest,
    FactorResearchService,
    FactorResearchUpstreamError,
    _ProviderResolver,
    factor_catalog,
)
from ..research_runs import ResearchRunStore

router = APIRouter(tags=["factors"])


@router.get("/factors/catalog")
async def get_factor_catalog() -> dict[str, object]:
    return factor_catalog()


@router.post("/factors/research")
async def research_factor(
    request: Request,
    body: FactorResearchRequest,
) -> dict[str, object]:
    settings = cast(Settings, request.app.state.settings)
    service = FactorResearchService(
        providers=cast(_ProviderResolver, request.app.state.providers),
        provider_capacity=cast(
            asyncio.Semaphore,
            request.app.state.factor_research_provider_semaphore,
        ),
        provider_background_tasks=cast(
            set[asyncio.Task[DataEnvelope]],
            request.app.state.factor_research_provider_tasks,
        ),
        run_store=cast(
            ResearchRunStore,
            request.app.state.research_run_store,
        ),
        settings=settings,
    )
    capacity = cast(
        asyncio.Semaphore,
        request.app.state.factor_research_semaphore,
    )
    capacity_acquired = False
    execution_started = False
    deadline_monotonic = monotonic() + settings.factor_research_deadline_seconds
    try:
        async with asyncio.timeout(settings.factor_research_deadline_seconds):
            await capacity.acquire()
            capacity_acquired = True
            if monotonic() >= deadline_monotonic:
                raise TimeoutError
            execution_started = True
            return await service.research(
                body,
                deadline_monotonic=deadline_monotonic,
            )
    except TimeoutError as error:
        if not execution_started:
            raise HTTPException(
                status_code=503,
                detail="Factor research capacity is busy; retry later.",
                headers={"Retry-After": "1"},
            ) from error
        raise HTTPException(
            status_code=504,
            detail="Factor research request exceeded its server deadline.",
        ) from error
    except FactorResearchUpstreamError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        if capacity_acquired:
            capacity.release()
