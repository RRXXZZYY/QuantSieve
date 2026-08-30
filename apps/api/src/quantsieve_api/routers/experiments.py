from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError

from ..experiments import (
    CrossMarketBacktestRunExpiredError,
    ExperimentStore,
    PortfolioBacktestRunExpiredError,
)
from ..research_runs import (
    ResearchRunExpiredError,
    ResearchRunIntegrityError,
    ResearchRunNotFoundError,
    ResearchRunReceipt,
    ResearchRunStore,
)
from ..schemas import (
    CrossMarketExperimentCreate,
    CrossMarketExperimentFromRunCreate,
    CrossMarketExperimentRecord,
    CrossMarketExperimentSummary,
    ExperimentCreate,
    ExperimentFromRunCreate,
    ExperimentRecord,
    PortfolioBacktestRequest,
    PortfolioExperimentCreate,
    PortfolioExperimentFromRunCreate,
    PortfolioExperimentRecord,
    PortfolioExperimentSummary,
)

router = APIRouter(prefix="/experiments", tags=["experiments"])


def _store(request: Request) -> ExperimentStore:
    return cast(ExperimentStore, request.app.state.experiment_store)


def _research_runs(request: Request) -> ResearchRunStore:
    return cast(ResearchRunStore, request.app.state.research_run_store)


@router.get("")
async def list_experiments(
    request: Request,
    query: str = Query(default="", max_length=100),
    symbol: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ExperimentRecord]:
    return _store(request).list(query=query, symbol=symbol, limit=limit)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_experiment(
    request: Request,
    body: ExperimentCreate,
) -> ExperimentRecord:
    if body.start > body.end:
        raise HTTPException(status_code=422, detail="实验开始日期不能晚于结束日期。")
    return _store(request).create(body)


def _backtest_instrument(
    body: ExperimentFromRunCreate,
    run: ResearchRunReceipt,
) -> dict[str, str]:
    snapshot = run.datasets[0].snapshot
    request_payload = run.run_request if isinstance(run.run_request, dict) else {}
    requested_provider = str(request_payload.get("provider", "")).strip().lower()
    provider = snapshot.provider
    supported_providers = {
        "auto",
        "akshare",
        "yfinance",
        "binance",
        "futures",
        "macro",
    }
    display_provider = (
        provider
        if provider in supported_providers
        else requested_provider
        if requested_provider in supported_providers
        else "auto"
    )
    provider_defaults = {
        "akshare": ("CN", "CNY", "equity"),
        "binance": ("CRYPTO", "USDT", "crypto"),
        "futures": ("GLOBAL", "USD", "future"),
        "macro": ("GLOBAL", "", "macro"),
        "yfinance": ("GLOBAL", "USD", "market"),
    }
    market, currency, asset_type = provider_defaults.get(
        display_provider,
        ("GLOBAL", "", "market"),
    )
    if display_provider == "binance":
        for quote_currency in ("USDT", "USDC", "BTC", "ETH"):
            if snapshot.symbol.endswith(quote_currency):
                currency = quote_currency
                break
    return {
        "symbol": snapshot.symbol,
        "name": body.instrument_name or snapshot.symbol,
        "market": market,
        "exchange": "",
        "currency": currency,
        "provider": display_provider,
        "asset_type": asset_type,
    }


def _authoritative_experiment(
    body: ExperimentFromRunCreate,
    run: ResearchRunReceipt,
) -> ExperimentCreate:
    if run.manifest.run_kind not in {"single_backtest", "custom_backtest"}:
        raise HTTPException(status_code=422, detail="该运行回执不是单标的回测。")
    if len(run.datasets) != 1:
        raise HTTPException(status_code=422, detail="单标的回测必须且只能引用一个数据快照。")
    if not isinstance(run.result, dict):
        raise HTTPException(status_code=422, detail="运行回执缺少可归档的回测结果。")
    result = run.result
    strategy = result.get("strategy")
    backtest_result = result.get("result")
    benchmark = result.get("benchmark")
    exposure_matched = result.get("exposure_matched_benchmark")
    if (
        not isinstance(strategy, dict)
        or not isinstance(backtest_result, dict)
        or not isinstance(benchmark, dict)
        or not isinstance(benchmark.get("result"), dict)
    ):
        raise HTTPException(status_code=422, detail="运行回执的策略或基准证据不完整。")

    optimization = result.get("optimization")
    validation: dict[str, Any] | None = None
    if isinstance(optimization, dict):
        required_validation_fields = {
            "objective",
            "split_date",
            "validation_passed",
            "validation_code",
            "validation_reason",
            "forward_observation_eligible",
            "train_metrics",
            "validation_metrics",
            "validation_benchmark_metrics",
        }
        if not required_validation_fields.issubset(optimization):
            raise HTTPException(status_code=422, detail="运行回执的样本外验证证据不完整。")
        validation = {
            "objective": optimization["objective"],
            "split_date": optimization["split_date"],
            "validation_passed": optimization["validation_passed"],
            "validation_code": optimization["validation_code"],
            "validation_reason": optimization["validation_reason"],
            "forward_observation_eligible": optimization[
                "forward_observation_eligible"
            ],
            "development_metrics": optimization["train_metrics"],
            "validation_metrics": optimization["validation_metrics"],
            "validation_benchmark_metrics": optimization[
                "validation_benchmark_metrics"
            ],
            "validation_exposure_matched_benchmark_metrics": optimization.get(
                "validation_exposure_matched_benchmark_metrics"
            ),
        }

    snapshot = run.datasets[0].snapshot
    exposure_result = (
        exposure_matched.get("result")
        if isinstance(exposure_matched, dict)
        and isinstance(exposure_matched.get("result"), dict)
        else None
    )
    try:
        return ExperimentCreate.model_validate(
            {
                "name": body.name,
                "notes": body.notes,
                "instrument": _backtest_instrument(body, run),
                "strategy": strategy,
                "interval": snapshot.interval,
                "start": snapshot.first_observation_at.date(),
                "end": snapshot.last_observation_at.date(),
                "optimized": isinstance(optimization, dict),
                "run_request": run.run_request,
                "engine_config": run.engine_config,
                "data_metadata": run.datasets[0].metadata,
                "metrics": backtest_result.get("metrics"),
                "benchmark_metrics": benchmark["result"].get("metrics"),
                "exposure_matched_benchmark_metrics": (
                    exposure_result.get("metrics")
                    if exposure_result is not None
                    else None
                ),
                "comparison": result.get("comparison"),
                "timing_comparison": result.get("timing_comparison"),
                "diagnostics": result.get("diagnostics", {}),
                "validation": validation,
                "citations": list(run.datasets[0].citations),
            }
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=jsonable_encoder(exc.errors(include_url=False)),
        ) from exc


@router.post("/from-run", status_code=status.HTTP_201_CREATED)
async def create_experiment_from_run(
    request: Request,
    body: ExperimentFromRunCreate,
) -> ExperimentRecord:
    existing = _store(request).get_by_source_run_id(body.run_id)
    if existing is not None:
        return existing
    try:
        run = _research_runs(request).get_run(body.run_id)
    except ResearchRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="运行回执不存在，请重新运行回测。") from exc
    except ResearchRunExpiredError as exc:
        raise HTTPException(status_code=410, detail="运行回执已过期，请重新运行回测。") from exc
    except ResearchRunIntegrityError as exc:
        raise HTTPException(status_code=500, detail="运行回执完整性校验失败。") from exc
    experiment = _authoritative_experiment(body, run)
    return _store(request).create(
        experiment,
        source_run_id=run.run_id,
        run_manifest=run.manifest,
    )


@router.get("/portfolios")
async def list_portfolio_experiments(
    request: Request,
    query: str = Query(default="", max_length=100),
    symbol: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[PortfolioExperimentSummary]:
    return _store(request).list_portfolios(
        query=query,
        symbol=symbol,
        limit=limit,
    )


@router.post("/portfolios", status_code=status.HTTP_201_CREATED)
async def create_portfolio_experiment(
    request: Request,
    body: PortfolioExperimentCreate,
) -> PortfolioExperimentRecord:
    return _store(request).create_portfolio(body)


def _authoritative_portfolio_experiment(
    request: Request,
    body: PortfolioExperimentFromRunCreate,
    snapshot: dict[str, Any],
    run_request: PortfolioBacktestRequest,
) -> PortfolioExperimentCreate:
    raw_assets = snapshot.get("assets")
    if not isinstance(raw_assets, list):
        raise HTTPException(status_code=422, detail="组合运行回执缺少资产快照。")

    authoritative_assets: dict[str, dict[str, Any]] = {}
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict):
            raise HTTPException(status_code=422, detail="组合运行回执的资产快照无效。")
        requested_symbol = str(raw_asset.get("requested_symbol", "")).strip().upper()
        if not requested_symbol or requested_symbol in authoritative_assets:
            raise HTTPException(status_code=422, detail="组合运行回执的请求代码无效。")
        authoritative_assets[requested_symbol] = raw_asset

    requested_assets = {
        asset.symbol.strip().upper(): asset for asset in run_request.assets
    }
    display_assets = {
        (asset.requested_symbol or asset.symbol): asset for asset in body.assets
    }
    if (
        set(authoritative_assets) != set(requested_assets)
        or set(display_assets) != set(requested_assets)
    ):
        raise HTTPException(
            status_code=422,
            detail="展示资产必须与该组合运行回执的资产集合完全一致。",
        )

    assets: list[dict[str, Any]] = []
    for requested_symbol, requested_asset in requested_assets.items():
        authoritative = authoritative_assets[requested_symbol]
        display = display_assets[requested_symbol]
        resolved_provider = str(authoritative.get("provider", "")).strip().lower()
        assets.append(
            {
                "symbol": authoritative.get("symbol"),
                "requested_symbol": requested_symbol,
                "name": display.name,
                "market": display.market,
                "exchange": display.exchange,
                "currency": authoritative.get("currency"),
                "provider": (
                    resolved_provider
                    if requested_asset.provider == "auto"
                    else requested_asset.provider
                ),
                "asset_type": display.asset_type,
                "metadata": authoritative.get("metadata", {}),
            }
        )

    try:
        return PortfolioExperimentCreate.model_validate(
            {
                "name": body.name,
                "notes": body.notes,
                "assets": assets,
                "interval": "1d",
                "start": run_request.start,
                "end": run_request.end,
                "focus_method": body.focus_method,
                "run_request": run_request.model_dump(mode="json"),
                "data_quality": snapshot.get("data_quality"),
                "assumptions": snapshot.get("assumptions"),
                "common_bars": snapshot.get("common_bars"),
                "results": snapshot.get("results"),
                "segments": snapshot.get("segments"),
                "research_decision": snapshot.get("research_decision"),
                "citations": snapshot.get("citations", []),
                "calculation_version": str(
                    snapshot.get("calculation_version") or request.app.version
                ),
            }
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=jsonable_encoder(exc.errors(include_url=False)),
        ) from exc


@router.post("/portfolios/from-run", status_code=status.HTTP_201_CREATED)
async def create_portfolio_experiment_from_run(
    request: Request,
    body: PortfolioExperimentFromRunCreate,
) -> PortfolioExperimentRecord:
    existing = _store(request).get_portfolio_by_source_run_id(body.run_id)
    if existing is not None:
        return existing
    try:
        run = _store(request).get_portfolio_run(body.run_id)
    except PortfolioBacktestRunExpiredError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    if run is None:
        raise HTTPException(
            status_code=404,
            detail="组合运行回执不存在，请重新运行组合回测。",
        )
    experiment = _authoritative_portfolio_experiment(
        request,
        body,
        run.result_payload,
        run.run_request,
    )
    return _store(request).create_portfolio(
        experiment,
        source_run_id=run.run_id,
    )


def _authoritative_cross_market_experiment(
    body: CrossMarketExperimentFromRunCreate,
    snapshot: dict[str, Any],
    run_request: Any,
) -> CrossMarketExperimentCreate:
    required = {
        "strategy",
        "interval",
        "objective",
        "parameter_policy",
        "parameter_policy_note",
        "markets",
        "summary",
        "research_decision",
    }
    if not required.issubset(snapshot):
        raise HTTPException(status_code=422, detail="跨市场运行回执缺少可归档的证据快照。")
    return CrossMarketExperimentCreate.model_validate(
        {
            "name": body.name,
            "notes": body.notes,
            "strategy": snapshot["strategy"],
            "interval": snapshot["interval"],
            "objective": snapshot["objective"],
            "parameter_policy": snapshot["parameter_policy"],
            "parameter_policy_note": snapshot["parameter_policy_note"],
            "run_request": run_request.model_dump(mode="json"),
            "markets": snapshot["markets"],
            "summary": snapshot["summary"],
            "research_decision": snapshot["research_decision"],
        }
    )


@router.get("/cross-market")
async def list_cross_market_experiments(
    request: Request,
    query: str = Query(default="", max_length=100),
    symbol: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[CrossMarketExperimentSummary]:
    return _store(request).list_cross_market(query=query, symbol=symbol, limit=limit)


@router.post("/cross-market/from-run", status_code=status.HTTP_201_CREATED)
async def create_cross_market_experiment_from_run(
    request: Request,
    body: CrossMarketExperimentFromRunCreate,
) -> CrossMarketExperimentRecord:
    existing = _store(request).get_cross_market_by_source_run_id(body.run_id)
    if existing is not None:
        return existing
    try:
        run = _store(request).get_cross_market_run(body.run_id)
    except CrossMarketBacktestRunExpiredError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    if run is None:
        raise HTTPException(status_code=404, detail="跨市场运行回执不存在。")
    try:
        experiment = _authoritative_cross_market_experiment(
            body,
            run.result_payload,
            run.run_request,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    return _store(request).create_cross_market(experiment, source_run_id=run.run_id)


@router.get("/cross-market/{experiment_id}")
async def get_cross_market_experiment(
    request: Request,
    experiment_id: str,
) -> CrossMarketExperimentRecord:
    experiment = _store(request).get_cross_market(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="跨市场实验记录不存在。")
    return experiment


@router.delete(
    "/cross-market/{experiment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_cross_market_experiment(
    request: Request,
    experiment_id: str,
) -> Response:
    if not _store(request).delete_cross_market(experiment_id):
        raise HTTPException(status_code=404, detail="跨市场实验记录不存在。")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/portfolios/{experiment_id}")
async def get_portfolio_experiment(
    request: Request,
    experiment_id: str,
) -> PortfolioExperimentRecord:
    experiment = _store(request).get_portfolio(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="组合实验记录不存在。")
    return experiment


@router.delete(
    "/portfolios/{experiment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_portfolio_experiment(
    request: Request,
    experiment_id: str,
) -> Response:
    if not _store(request).delete_portfolio(experiment_id):
        raise HTTPException(status_code=404, detail="组合实验记录不存在。")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{experiment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_experiment(request: Request, experiment_id: str) -> Response:
    if not _store(request).delete(experiment_id):
        raise HTTPException(status_code=404, detail="实验记录不存在。")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
