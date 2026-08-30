from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from decimal import Decimal
from typing import Literal, Protocol

from quantsieve_engine.risk import OrderPriceEvidence, canonical_payload_hash
from quantsieve_providers import (
    BinanceSpotTradingRules,
    ExecutionQuote,
)

from .paper_oms import (
    PaperEventMutationResult,
    PaperOmsExecutionUnavailableError,
    PaperOmsFillExecutionEvidence,
    PaperOmsOrderMarketEvidence,
    PaperOmsStore,
    PaperOrderMutationResult,
    RecordPaperFillCommand,
    SubmitPaperOrderCommand,
)

_DEFAULT_PRICE_DEADLINE_SECONDS = 8.0
_PRICE_SOURCE = "binance.execution-quote.uncached-book-ticker"
_EXECUTION_SOURCE = "quantsieve.paper-oms.binance-simulator.v1"
_SIMULATED_FEE_RATE = Decimal("0.001")
_CANONICAL_BINANCE_SYMBOL = re.compile(r"^[A-Z0-9]{5,30}$", flags=re.ASCII)


class PaperOmsPriceCapabilityError(ValueError):
    """The public paper OMS cannot certify this instrument contract."""


class PaperOmsPriceUnavailableError(RuntimeError):
    """The trusted market-data boundary did not return usable evidence."""


class PaperOmsExecutionPriceProvider(Protocol):
    """Narrow trusted provider boundary used by the public order endpoint."""

    async def trading_rules(self, symbol: str) -> BinanceSpotTradingRules: ...

    async def execution_quote(
        self,
        symbol: str,
        *,
        rules: BinanceSpotTradingRules,
    ) -> ExecutionQuote: ...


class PaperOmsOrderSubmissionService:
    """Fetch trusted evidence, then let the store evaluate and commit atomically."""

    def __init__(
        self,
        *,
        store: PaperOmsStore,
        provider: PaperOmsExecutionPriceProvider,
        price_deadline_seconds: float = _DEFAULT_PRICE_DEADLINE_SECONDS,
        symbol_is_supported: Callable[[str], bool] | None = None,
    ) -> None:
        if (
            isinstance(price_deadline_seconds, bool)
            or not isinstance(price_deadline_seconds, (int, float))
            or not 0 < float(price_deadline_seconds) <= 60
        ):
            raise ValueError("Paper OMS price deadline must be between zero and 60 seconds.")
        self._store = store
        self._provider = provider
        self._price_deadline_seconds = float(price_deadline_seconds)
        self._symbol_is_supported = symbol_is_supported

    async def submit_order(
        self,
        command: SubmitPaperOrderCommand,
    ) -> PaperOrderMutationResult:
        """Preserve old allow/reject receipts before attempting market-data I/O."""

        replay = await asyncio.to_thread(
            self._store.replay_order_submission,
            command,
        )
        if replay is not None:
            return replay
        account = await asyncio.to_thread(
            self._store.get_account,
            command.account_id,
        )
        evidence = await self._price_evidence(
            command,
            quote_currency=account.currency,
        )
        return await asyncio.to_thread(
            self._store.submit_order,
            command,
            price_evidence=evidence,
        )

    async def record_fill(
        self,
        *,
        command_namespace: str,
        idempotency_key: str,
        account_id: str,
        order_id: str,
        expected_order_revision: int,
        expected_account_revision: int,
        quantity: Decimal,
    ) -> PaperEventMutationResult:
        replay = await asyncio.to_thread(
            self._store.replay_fill_identity,
            command_namespace=command_namespace,
            idempotency_key=idempotency_key,
            account_id=account_id,
            order_id=order_id,
            expected_order_revision=expected_order_revision,
            expected_account_revision=expected_account_revision,
            quantity=quantity,
        )
        if replay is not None:
            return replay
        order = await asyncio.to_thread(
            self._store.get_order,
            account_id,
            order_id,
        )
        account = await asyncio.to_thread(
            self._store.get_account,
            account_id,
        )
        approval = order.risk_evaluation
        if approval is None or approval.market_evidence is None:
            raise PaperOmsExecutionUnavailableError(
                "Paper order has no trusted schema-v3 approval evidence."
            )
        market = await self._market_evidence(
            symbol=order.symbol,
            side=order.side,
            quote_currency=account.currency,
        )
        fill_price = market.quote.ask_price if order.side == "buy" else market.quote.bid_price
        fee = quantity * fill_price * _SIMULATED_FEE_RATE
        external_fill_id = canonical_payload_hash(
            {
                "contract": "quantsieve.paper-oms.simulated-fill-id.v1",
                "command_namespace": command_namespace,
                "idempotency_key": idempotency_key,
                "account_id": account_id,
                "order_id": order_id,
            }
        )
        evidence_payload = {
            "schema_version": 1,
            "contract": "quantsieve.paper-oms.simulated-fill-evidence.v1",
            "account_id": account_id,
            "order_id": order_id,
            "symbol": order.symbol,
            "side": order.side,
            "quantity": quantity,
            "execution_source": _EXECUTION_SOURCE,
            "external_fill_id": external_fill_id,
            "reference_price": fill_price,
            "fill_price": fill_price,
            "fee_rate": _SIMULATED_FEE_RATE,
            "fee": fee,
            "observed_at": market.quote.observed_at,
            "available_at": market.quote.observed_at,
            "approval_evidence_hash": approval.market_evidence.evidence_hash,
            "trading_rules": market.trading_rules,
            "quote": market.quote,
            "rules_snapshot_hash": market.rules_snapshot_hash,
            "quote_snapshot_hash": market.quote_snapshot_hash,
        }
        execution_evidence = PaperOmsFillExecutionEvidence.model_validate(
            {
                **evidence_payload,
                "evidence_hash": canonical_payload_hash(evidence_payload),
            }
        )
        command = RecordPaperFillCommand(
            command_namespace=command_namespace,
            idempotency_key=idempotency_key,
            account_id=account_id,
            occurred_at=market.quote.observed_at,
            order_id=order_id,
            expected_order_revision=expected_order_revision,
            expected_account_revision=expected_account_revision,
            execution_source=_EXECUTION_SOURCE,
            external_fill_id=external_fill_id,
            quantity=quantity,
            reference_price=fill_price,
            fill_price=fill_price,
            fee=fee,
        )
        return await asyncio.to_thread(
            self._store.record_fill,
            command,
            execution_evidence=execution_evidence,
        )

    async def _price_evidence(
        self,
        command: SubmitPaperOrderCommand,
        *,
        quote_currency: str,
    ) -> PaperOmsOrderMarketEvidence:
        return await self._market_evidence(
            symbol=command.symbol,
            side=command.side,
            quote_currency=quote_currency,
        )

    async def _market_evidence(
        self,
        *,
        symbol: str,
        side: Literal["buy", "sell"],
        quote_currency: str,
    ) -> PaperOmsOrderMarketEvidence:
        try:
            if _CANONICAL_BINANCE_SYMBOL.fullmatch(symbol) is None:
                raise PaperOmsPriceCapabilityError(
                    "Paper OMS requires a compact canonical Binance symbol such "
                    "as BTCUSDT; aliases and separators are not accepted."
                )
            if self._symbol_is_supported is not None and not self._symbol_is_supported(symbol):
                raise PaperOmsPriceCapabilityError(
                    "The public paper OMS currently supports Binance Spot symbols only."
                )
            async with asyncio.timeout(self._price_deadline_seconds):
                rules = await self._provider.trading_rules(symbol)
                safe_rules = BinanceSpotTradingRules.model_validate(rules.model_dump(mode="python"))
                if safe_rules.symbol != symbol:
                    raise PaperOmsPriceCapabilityError(
                        "Paper OMS requires the canonical Binance symbol "
                        f"{safe_rules.symbol!r}; aliases are not accepted."
                    )
                if (
                    safe_rules.quote_asset != quote_currency
                    or safe_rules.status != "TRADING"
                    or not safe_rules.spot_trading_allowed
                    or "MARKET" not in safe_rules.order_types
                ):
                    raise PaperOmsPriceCapabilityError(
                        "Paper OMS supports only a currently tradable Binance Spot "
                        "market whose quote asset matches the paper account currency."
                    )
                quote = await self._provider.execution_quote(
                    safe_rules.symbol,
                    rules=safe_rules,
                )
                safe_quote = ExecutionQuote.model_validate(quote.model_dump(mode="python"))
        except PaperOmsPriceCapabilityError:
            raise
        except TimeoutError as error:
            raise PaperOmsPriceUnavailableError(
                "Trusted Binance execution-price evidence exceeded its deadline."
            ) from error
        except Exception as error:
            raise PaperOmsPriceUnavailableError(
                "Trusted Binance execution-price evidence is unavailable."
            ) from error

        if (
            safe_quote.symbol != safe_rules.symbol
            or safe_quote.provider != "binance"
            or safe_quote.cache_used is not False
            or safe_quote.observed_at < safe_quote.request_started_at
        ):
            raise PaperOmsPriceUnavailableError(
                "Trusted Binance execution-price evidence failed identity checks."
            )
        snapshot_hash = canonical_payload_hash(
            {
                "contract": "quantsieve.paper-oms.binance-market-snapshot.v1",
                "rules": safe_rules.model_dump(mode="python"),
                "quote": safe_quote.model_dump(mode="python"),
            }
        )
        reference_price = safe_quote.ask_price if side == "buy" else safe_quote.bid_price
        price_evidence = OrderPriceEvidence(
            symbol=safe_rules.symbol,
            quote_currency=safe_rules.quote_asset,
            reference_price=reference_price,
            observed_at=safe_quote.observed_at,
            available_at=safe_quote.observed_at,
            source=_PRICE_SOURCE,
            snapshot_hash=snapshot_hash,
        )
        evidence_payload = {
            "schema_version": 1,
            "contract": "quantsieve.paper-oms.order-market-evidence.v1",
            "side": side,
            "trading_rules": safe_rules,
            "quote": safe_quote,
            "price_evidence": price_evidence,
            "rules_snapshot_hash": canonical_payload_hash(safe_rules),
            "quote_snapshot_hash": canonical_payload_hash(safe_quote),
        }
        return PaperOmsOrderMarketEvidence.model_validate(
            {
                **evidence_payload,
                "evidence_hash": canonical_payload_hash(evidence_payload),
            }
        )
