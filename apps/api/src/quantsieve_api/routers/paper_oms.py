from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, TypeVar, cast

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from ..paper_oms import (
    CreatePaperAccountCommand,
    PaperAccountMutationResult,
    PaperAccountRecord,
    PaperEventMutationResult,
    PaperOmsBusyError,
    PaperOmsConflictError,
    PaperOmsExecutionUnavailableError,
    PaperOmsInsufficientCashError,
    PaperOmsInsufficientPositionError,
    PaperOmsIntegrityError,
    PaperOmsNotFoundError,
    PaperOmsRevisionError,
    PaperOmsRiskRejectedError,
    PaperOmsRiskUnavailableError,
    PaperOmsStore,
    PaperOmsTransitionError,
    PaperOrderMutationResult,
    PaperOrderRecord,
    PaperOrderRiskEvaluationRecord,
    RecordPaperOrderEventCommand,
    SubmitPaperOrderCommand,
)
from ..paper_oms_risk_service import (
    PaperOmsOrderSubmissionService,
    PaperOmsPriceCapabilityError,
    PaperOmsPriceUnavailableError,
)

router = APIRouter(prefix="/paper-oms", tags=["paper-oms"])

_COMMAND_NAMESPACE = "api.paper-oms.v1"
_ExactDecimal = Annotated[
    str,
    Field(
        min_length=1,
        max_length=120,
        pattern=r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$",
    ),
]
_Identifier = Annotated[str, Field(min_length=1, max_length=200)]
_T = TypeVar("_T")


class _PaperOmsIdentityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    idempotency_key: _Identifier


class _PaperOmsRequest(_PaperOmsIdentityRequest):
    occurred_at: datetime

    @field_validator("occurred_at", mode="before")
    @classmethod
    def parse_occurred_at(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return value
        return value


class PaperAccountCreateRequest(_PaperOmsRequest):
    account_id: _Identifier
    currency: Annotated[str, Field(min_length=1, max_length=16)]
    initial_cash: _ExactDecimal


class PaperOrderSubmitRequest(_PaperOmsRequest):
    order_id: _Identifier
    symbol: Annotated[str, Field(min_length=1, max_length=100)]
    side: Literal["buy", "sell"]
    quantity: _ExactDecimal


class PaperOrderEventRequest(_PaperOmsRequest):
    expected_order_revision: Annotated[int, Field(ge=0)]
    event_type: Literal[
        "acknowledged",
        "cancel_requested",
        "cancel_acknowledged",
        "cancel_rejected",
        "rejected",
        "expired",
    ]
    reason: Annotated[str, Field(min_length=1, max_length=200)] | None = None


class PaperFillRequest(_PaperOmsIdentityRequest):
    expected_order_revision: Annotated[int, Field(ge=0)]
    expected_account_revision: Annotated[int, Field(ge=1)]
    quantity: _ExactDecimal


def _store(request: Request) -> PaperOmsStore:
    return cast(PaperOmsStore, request.app.state.paper_oms_store)


def _submission_service(request: Request) -> PaperOmsOrderSubmissionService:
    return cast(
        PaperOmsOrderSubmissionService,
        request.app.state.paper_oms_submission_service,
    )


def _raise_http_error(error: Exception) -> None:
    if isinstance(error, PaperOmsNotFoundError):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, (PaperOmsConflictError, PaperOmsRevisionError)):
        raise HTTPException(status_code=409, detail=str(error)) from error
    if isinstance(error, PaperOmsRiskRejectedError):
        evaluation = error.evaluation
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ORDER_RISK_REJECTED",
                "request_hash": evaluation.request.request_hash,
                "decision_hash": evaluation.decision.decision_hash,
                "idempotent_replay": error.idempotent_replay,
                "findings": [
                    {
                        "code": finding.code,
                        "rule_id": finding.rule_id,
                        "message_key": finding.message_key,
                    }
                    for finding in evaluation.decision.findings
                ],
            },
        ) from error
    if isinstance(error, PaperOmsPriceCapabilityError):
        raise HTTPException(status_code=422, detail=str(error)) from error
    if isinstance(
        error,
        (
            PaperOmsRiskUnavailableError,
            PaperOmsExecutionUnavailableError,
            PaperOmsPriceUnavailableError,
        ),
    ):
        raise HTTPException(
            status_code=503,
            detail="Trusted paper-order risk evidence is temporarily unavailable.",
        ) from error
    if isinstance(
        error,
        (
            PaperOmsTransitionError,
            PaperOmsInsufficientCashError,
            PaperOmsInsufficientPositionError,
            ValidationError,
            ValueError,
        ),
    ):
        raise HTTPException(status_code=422, detail=str(error)) from error
    if isinstance(error, PaperOmsBusyError):
        raise HTTPException(
            status_code=503,
            detail="模拟交易账本暂时繁忙，请使用同一幂等键重试。",
        ) from error
    if isinstance(error, PaperOmsIntegrityError):
        raise HTTPException(
            status_code=500,
            detail="模拟交易账本完整性校验失败，操作已拒绝。",
        ) from error
    raise error


def _run_store_operation(operation: Callable[[], _T]) -> _T:
    try:
        return operation()
    except Exception as error:
        _raise_http_error(error)
        raise AssertionError("HTTP error translation returned unexpectedly.") from error


@router.post(
    "/accounts",
    response_model=PaperAccountMutationResult,
    status_code=status.HTTP_201_CREATED,
    summary="创建仅模拟账户",
)
async def create_paper_account(
    request: Request,
    body: PaperAccountCreateRequest,
) -> PaperAccountMutationResult:
    try:
        command = CreatePaperAccountCommand(
            command_namespace=_COMMAND_NAMESPACE,
            idempotency_key=body.idempotency_key,
            account_id=body.account_id,
            occurred_at=body.occurred_at,
            currency=body.currency,
            initial_cash=Decimal(body.initial_cash),
        )
    except (ValidationError, ValueError) as error:
        _raise_http_error(error)
        raise AssertionError("HTTP error translation returned unexpectedly.") from error
    return _run_store_operation(lambda: _store(request).create_account(command))


@router.get(
    "/accounts/{account_id}",
    response_model=PaperAccountRecord,
    summary="读取仅模拟账户",
)
async def get_paper_account(
    request: Request,
    account_id: str,
) -> PaperAccountRecord:
    return _run_store_operation(lambda: _store(request).get_account(account_id))


@router.post(
    "/accounts/{account_id}/orders",
    response_model=PaperOrderMutationResult,
    status_code=status.HTTP_201_CREATED,
    summary="提交仅模拟订单意图",
)
async def submit_paper_order(
    request: Request,
    account_id: str,
    body: PaperOrderSubmitRequest,
) -> PaperOrderMutationResult:
    try:
        command = SubmitPaperOrderCommand(
            command_namespace=_COMMAND_NAMESPACE,
            idempotency_key=body.idempotency_key,
            account_id=account_id,
            occurred_at=body.occurred_at,
            order_id=body.order_id,
            symbol=body.symbol,
            side=body.side,
            execution_source="quantsieve.paper-oms.binance-simulator.v1",
            quantity=Decimal(body.quantity),
        )
        return await _submission_service(request).submit_order(command)
    except Exception as error:
        _raise_http_error(error)
        raise AssertionError("HTTP error translation returned unexpectedly.") from error


@router.get(
    "/accounts/{account_id}/order-risk/{idempotency_key}",
    response_model=PaperOrderRiskEvaluationRecord,
    summary="读取可重放的仅模拟订单风控评估",
)
async def get_paper_order_risk_evaluation(
    request: Request,
    account_id: str,
    idempotency_key: str,
) -> PaperOrderRiskEvaluationRecord:
    return _run_store_operation(
        lambda: _store(request).get_order_risk_evaluation(
            command_namespace=_COMMAND_NAMESPACE,
            idempotency_key=idempotency_key,
            account_id=account_id,
        )
    )


@router.get(
    "/accounts/{account_id}/orders",
    response_model=tuple[PaperOrderRecord, ...],
    summary="列出仅模拟订单",
)
async def list_paper_orders(
    request: Request,
    account_id: str,
) -> tuple[PaperOrderRecord, ...]:
    return _run_store_operation(lambda: _store(request).list_orders(account_id))


@router.get(
    "/accounts/{account_id}/orders/{order_id}",
    response_model=PaperOrderRecord,
    summary="读取仅模拟订单",
)
async def get_paper_order(
    request: Request,
    account_id: str,
    order_id: str,
) -> PaperOrderRecord:
    return _run_store_operation(lambda: _store(request).get_order(account_id, order_id))


@router.post(
    "/accounts/{account_id}/orders/{order_id}/events",
    response_model=PaperEventMutationResult,
    summary="记录仅模拟订单生命周期事件",
)
async def record_paper_order_event(
    request: Request,
    account_id: str,
    order_id: str,
    body: PaperOrderEventRequest,
) -> PaperEventMutationResult:
    try:
        command = RecordPaperOrderEventCommand(
            command_namespace=_COMMAND_NAMESPACE,
            idempotency_key=body.idempotency_key,
            account_id=account_id,
            occurred_at=body.occurred_at,
            order_id=order_id,
            expected_order_revision=body.expected_order_revision,
            event_type=body.event_type,
            reason=body.reason,
        )
    except (ValidationError, ValueError) as error:
        _raise_http_error(error)
        raise AssertionError("HTTP error translation returned unexpectedly.") from error
    return _run_store_operation(lambda: _store(request).record_order_event(command))


@router.post(
    "/accounts/{account_id}/orders/{order_id}/fills",
    response_model=PaperEventMutationResult,
    summary="原子记录仅模拟成交和复式账本",
)
async def record_paper_fill(
    request: Request,
    account_id: str,
    order_id: str,
    body: PaperFillRequest,
) -> PaperEventMutationResult:
    try:
        return await _submission_service(request).record_fill(
            command_namespace=_COMMAND_NAMESPACE,
            idempotency_key=body.idempotency_key,
            account_id=account_id,
            order_id=order_id,
            expected_order_revision=body.expected_order_revision,
            expected_account_revision=body.expected_account_revision,
            quantity=Decimal(body.quantity),
        )
    except Exception as error:
        _raise_http_error(error)
        raise AssertionError("HTTP error translation returned unexpectedly.") from error
