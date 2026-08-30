from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from math import gcd, isfinite
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self, TypeVar
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from quantsieve_engine import (
    PortfolioForwardState,
    PortfolioForwardTarget,
    PortfolioMethod,
    initialize_portfolio_forward_state,
)
from quantsieve_providers import ExecutionQuote

from .portfolio_paper_contracts import (
    PORTFOLIO_TARGET_CALCULATION_VERSION,
    CertifiedPortfolioBar,
    PortfolioPaperBasketContract,
    PortfolioPaperBasketIdentity,
    PortfolioPaperDecisionCertificate,
    PortfolioPaperDecisionReceipt,
    _validate_execution_quotes_against_receipt,
)

_COMPONENT = "portfolio_paper"
_DATABASE_SCHEMA_VERSION = 3
_HASH_LENGTH = 64
_OPENING_WINDOW_TERMINAL_ERROR = (
    "Portfolio opening could not start within its certified execution window."
)

if TYPE_CHECKING:
    from .portfolio_paper_execution import PortfolioPaperExecutionBatch
    from .portfolio_paper_settlement import PortfolioPaperModeledCloseSettlement


class PortfolioPaperStoreError(RuntimeError):
    """Base error for the unavailable internal portfolio-paper store."""


class PortfolioPaperSchemaError(PortfolioPaperStoreError):
    """Raised when the component database schema cannot be trusted."""


class PortfolioPaperConflictError(PortfolioPaperStoreError):
    """Raised when a unique portfolio-paper identity already exists."""


class PortfolioPaperCorruptionError(PortfolioPaperStoreError):
    """Raised when persisted JSON, hashes, or denormalized columns disagree."""


class PortfolioPaperConfig(BaseModel):
    """Frozen configuration for a not-yet-available execution-paper track."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    tracking_mode: Literal["execution_paper"] = "execution_paper"
    portfolio_experiment_id: str = Field(min_length=1, max_length=128)
    symbols: tuple[str, ...]
    basket_identity: PortfolioPaperBasketIdentity
    method: PortfolioMethod
    calculation_version: Literal["portfolio-forward-target-v1"] = (
        PORTFOLIO_TARGET_CALCULATION_VERSION
    )
    initial_cash: float = Field(gt=0)
    fee_rate: float = Field(default=0.0003, ge=0, le=0.1)
    slippage_rate: float = Field(default=0.0002, ge=0, le=0.1)
    volatility_lookback: int = Field(default=60, ge=2, le=252)
    rebalance_bars: int = Field(default=21, ge=1)
    maximum_asset_weight: float = Field(default=0.4, gt=0, le=1)

    @field_validator("portfolio_experiment_id")
    @classmethod
    def normalize_experiment_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Portfolio experiment id must not be empty.")
        return normalized

    @field_validator("symbols", mode="before")
    @classmethod
    def normalize_symbols(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError("Portfolio-paper symbols must be a sequence.")
        symbols: list[str] = []
        for raw_symbol in value:
            if not isinstance(raw_symbol, str):
                raise ValueError("Portfolio-paper symbols must be strings.")
            symbol = raw_symbol.strip().upper()
            if not symbol:
                raise ValueError("Portfolio-paper symbols must not be empty.")
            symbols.append(symbol)
        if not 2 <= len(symbols) <= 6:
            raise ValueError("Portfolio paper requires between 2 and 6 symbols.")
        if len(set(symbols)) != len(symbols):
            raise ValueError(
                "Portfolio-paper symbols must be unique after normalization."
            )
        return tuple(symbols)

    @model_validator(mode="after")
    def validate_config(self) -> Self:
        numeric = (
            self.initial_cash,
            self.fee_rate,
            self.slippage_rate,
            self.maximum_asset_weight,
        )
        if not all(isfinite(value) for value in numeric):
            raise ValueError("Portfolio-paper configuration must be finite.")
        if (
            self.method == "periodic_inverse_volatility"
            and self.maximum_asset_weight * len(self.symbols) < 1 - 1e-10
        ):
            raise ValueError(
                "Maximum asset weight is too small for the portfolio-paper basket."
            )
        basket_symbols = tuple(
            instrument.symbol for instrument in self.basket_identity.instruments
        )
        if basket_symbols != self.symbols:
            raise ValueError(
                "Paper configuration symbols must exactly match its eligible basket."
            )
        return self


class PortfolioPaperDecision(BaseModel):
    """Durable target evidence stored before any future execution is permitted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    track_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    target: PortfolioForwardTarget
    target_hash: str = Field(
        min_length=_HASH_LENGTH,
        max_length=_HASH_LENGTH,
        pattern=r"^[0-9a-f]{64}$",
    )
    certificate: PortfolioPaperDecisionCertificate
    certificate_hash: str = Field(
        min_length=_HASH_LENGTH,
        max_length=_HASH_LENGTH,
        pattern=r"^[0-9a-f]{64}$",
    )
    persisted_at: datetime

    @field_validator("persisted_at")
    @classmethod
    def normalize_persisted_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Decision persistence time must be timezone-aware.")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if self.id != self.certificate.decision_id:
            raise ValueError("Decision id must match its certified evidence.")
        if self.target != self.certificate.target:
            raise ValueError("Decision target must match its certified evidence.")
        if self.target_hash != _canonical_hash(self.target):
            raise ValueError("Portfolio-paper target hash does not match its payload.")
        if self.certificate_hash != _canonical_hash(self.certificate):
            raise ValueError(
                "Portfolio-paper certificate hash does not match its payload."
            )
        if self.persisted_at < self.certificate.decided_at:
            raise ValueError("Decision cannot be persisted before it is calculated.")
        if self.persisted_at >= self.certificate.execution_deadline:
            raise ValueError("Decision persistence must precede its execution deadline.")
        return self

    @property
    def receipt(self) -> PortfolioPaperDecisionReceipt:
        return PortfolioPaperDecisionReceipt(
            track_id=self.track_id,
            decision_id=self.id,
            certificate=self.certificate,
            certificate_hash=self.certificate_hash,
            persisted_at=self.persisted_at,
        )


class PortfolioPaperLease(BaseModel):
    """Persistent fenced lease returned to an internal worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    track_id: str = Field(
        min_length=32,
        max_length=32,
        pattern=r"^[0-9a-f]{32}$",
    )
    owner: str = Field(min_length=1, max_length=200)
    generation: int = Field(ge=1)
    state_revision: int = Field(ge=0)
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def normalize_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Lease expiry must be timezone-aware.")
        return value.astimezone(UTC)


class PortfolioPaperTrackRecord(BaseModel):
    """Validated read model; deliberately not exposed by any application route."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    tracking_mode: Literal["execution_paper"]
    status: Literal["internal_only"]
    config: PortfolioPaperConfig
    configuration_hash: str = Field(
        min_length=_HASH_LENGTH,
        max_length=_HASH_LENGTH,
        pattern=r"^[0-9a-f]{64}$",
    )
    state_revision: int = Field(ge=0)
    state: PortfolioForwardState
    pending_decision: PortfolioPaperDecision | None
    opening_batch_id: str | None = Field(
        default=None,
        min_length=_HASH_LENGTH,
        max_length=_HASH_LENGTH,
        pattern=r"^[0-9a-f]{64}$",
    )
    opening_session: str | None = None
    opening_committed_at: datetime | None = None
    settlement_id: str | None = Field(
        default=None,
        min_length=_HASH_LENGTH,
        max_length=_HASH_LENGTH,
        pattern=r"^[0-9a-f]{64}$",
    )
    settlement_session: str | None = None
    settlement_committed_at: datetime | None = None
    refresh_generation: int = Field(ge=0)
    refresh_owner: str | None = None
    refresh_lease_until: datetime | None = None
    created_at: datetime
    updated_at: datetime
    last_checked_at: datetime | None = None
    last_error: str | None = Field(default=None, max_length=1_000)

    @field_validator(
        "refresh_lease_until",
        "created_at",
        "updated_at",
        "last_checked_at",
        "opening_committed_at",
        "settlement_committed_at",
    )
    @classmethod
    def normalize_optional_timestamp(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Portfolio-paper timestamps must be timezone-aware.")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if self.created_at > self.updated_at:
            raise ValueError("Track creation time cannot follow its update time.")
        if (
            self.last_checked_at is not None
            and self.last_checked_at > self.updated_at
        ):
            raise ValueError("Track check time cannot follow its update time.")
        if (
            self.refresh_lease_until is not None
            and self.refresh_lease_until <= self.updated_at
        ):
            raise ValueError("Track lease expiry must follow its persisted update time.")
        if self.config.tracking_mode != self.tracking_mode:
            raise ValueError("Track mode must match its frozen configuration.")
        if self.configuration_hash != _canonical_hash(self.config):
            raise ValueError("Track configuration hash does not match its payload.")
        if self.state_revision != self.state.valuation_count:
            raise ValueError(
                "Portfolio-paper state revision must equal its valuation count."
            )
        _validate_state_against_config(self.state, self.config)
        if self.pending_decision is None:
            raise ValueError(
                "Every internal paper track requires its durable activation decision."
            )
        if self.pending_decision.track_id != self.id:
            raise ValueError("Activation decision belongs to a different track.")
        if self.pending_decision.persisted_at != self.created_at:
            raise ValueError(
                "Internal-only activation receipt must equal track creation time."
            )
        certificate = self.pending_decision.certificate
        if certificate.configuration_hash != self.configuration_hash:
            raise ValueError(
                "Activation decision certificate does not match track configuration."
            )
        if (
            certificate.basket_identity_hash
            != self.config.basket_identity.identity_hash
        ):
            raise ValueError(
                "Activation decision certificate does not match track basket identity."
            )
        if (
            certificate.calculation_version != self.config.calculation_version
            or certificate.volatility_lookback != self.config.volatility_lookback
            or certificate.maximum_asset_weight
            != self.config.maximum_asset_weight
        ):
            raise ValueError(
                "Activation decision calculation parameters do not match track config."
            )
        if self.state.pending_target is not None:
            if self.pending_decision.target != self.state.pending_target:
                raise ValueError(
                    "Pending decision target must equal the state pending target."
                )
        elif self.settlement_id is None or self.state_revision != 1:
            raise ValueError(
                "Only a committed revision-one close valuation may clear the "
                "activation target."
            )
        if (self.opening_batch_id is None) != (self.opening_session is None):
            raise ValueError(
                "Opening batch id and execution session must either both exist or be absent."
            )
        if (self.opening_batch_id is None) != (self.opening_committed_at is None):
            raise ValueError(
                "Opening batch id and commit time must either both exist or be absent."
            )
        if self.opening_committed_at is not None:
            if (
                self.opening_session
                != self.pending_decision.certificate.execution_session
            ):
                raise ValueError(
                    "Opening batch session must equal the certified execution session."
                )
            if self.opening_committed_at < self.pending_decision.persisted_at:
                raise ValueError(
                    "Opening batch cannot commit before its durable decision."
                )
            if self.opening_committed_at > self.updated_at:
                raise ValueError(
                    "Opening batch commit time cannot follow the track update time."
                )
        settlement_parts = (
            self.settlement_id,
            self.settlement_session,
            self.settlement_committed_at,
        )
        if any(part is None for part in settlement_parts) != all(
            part is None for part in settlement_parts
        ):
            raise ValueError(
                "Settlement id, session, and commit time must all exist or all be absent."
            )
        if self.settlement_id is not None:
            if self.opening_batch_id is None:
                raise ValueError(
                    "A committed close valuation requires its opening batch."
                )
            if (
                self.settlement_session != self.opening_session
                or self.state.session != self.settlement_session
                or self.state_revision != 1
                or self.state.pending_target is not None
            ):
                raise ValueError(
                    "Committed close valuation identity must match revision-one state."
                )
            if (
                self.settlement_committed_at is None
                or self.opening_committed_at is None
                or self.settlement_committed_at < self.opening_committed_at
                or self.settlement_committed_at > self.updated_at
            ):
                raise ValueError(
                    "Settlement commit time must follow opening and precede update."
                )
        elif self.state_revision != 0 or self.state.pending_target is None:
            raise ValueError(
                "An unsettled internal paper track must retain revision-zero activation."
            )
        if (self.refresh_owner is None) != (self.refresh_lease_until is None):
            raise ValueError("Lease owner and expiry must either both exist or be absent.")
        if (
            self.refresh_owner is not None
            and _normalize_owner(self.refresh_owner) != self.refresh_owner
        ):
            raise ValueError("Persisted lease owner must use its canonical form.")
        return self


ModelT = TypeVar("ModelT", bound=BaseModel)


class PortfolioPaperTrackStore:
    """Internal SQLite ledger for modeled opening and close valuation.

    Opening fills and the completed-session close valuation are committed atomically
    under fenced leases. A close is valuation evidence only and never represents a
    venue sell. Canonical hashes detect accidental corruption and inconsistent writes;
    they are not authentication against an actor who can rewrite both database content
    and hashes. External mutation still requires a secret-backed integrity boundary.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._write_transaction() as connection:
            self._create_migration_ledger(connection)
            migration_rows = connection.execute(
                "SELECT version, applied_at FROM app_schema_migrations "
                "WHERE component = ? ORDER BY version",
                (_COMPONENT,),
            ).fetchall()
            versions = [
                _stored_integer(row["version"], "schema version")
                for row in migration_rows
            ]
            if any(version > _DATABASE_SCHEMA_VERSION for version in versions):
                raise PortfolioPaperSchemaError(
                    "Portfolio-paper database schema is newer than this build."
                )
            for row in migration_rows:
                try:
                    _stored_datetime(row["applied_at"], "migration applied_at")
                except PortfolioPaperCorruptionError as error:
                    raise PortfolioPaperSchemaError(
                        "Portfolio-paper migration time is not canonical UTC."
                    ) from error
            if not versions:
                existing_component_tables = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name LIKE 'portfolio_paper_%' "
                    "LIMIT 1"
                ).fetchone()
                if existing_component_tables is not None:
                    raise PortfolioPaperSchemaError(
                        "Portfolio-paper migration history is missing version 1."
                    )
                self._apply_migration_1(connection)
                applied_at = datetime.now(UTC).isoformat()
                connection.execute(
                    "INSERT INTO app_schema_migrations "
                    "(component, version, applied_at) VALUES (?, 1, ?)",
                    (_COMPONENT, applied_at),
                )
                versions = [1]
            if versions == [1]:
                self._verify_schema(connection, version=1)
                self._apply_migration_2(connection)
                applied_at = datetime.now(UTC).isoformat()
                connection.execute(
                    "INSERT INTO app_schema_migrations "
                    "(component, version, applied_at) VALUES (?, 2, ?)",
                    (_COMPONENT, applied_at),
                )
                versions = [1, 2]
            if versions == [1, 2]:
                self._verify_schema(connection, version=2)
                self._apply_migration_3(connection)
                applied_at = datetime.now(UTC).isoformat()
                connection.execute(
                    "INSERT INTO app_schema_migrations "
                    "(component, version, applied_at) VALUES (?, 3, ?)",
                    (_COMPONENT, applied_at),
                )
                versions = [1, 2, 3]
            if versions != [1, 2, 3]:
                raise PortfolioPaperSchemaError(
                    "Portfolio-paper migration history must contain exactly "
                    "versions 1, 2, and 3."
                )
            self._verify_schema(connection, version=3)
            persisted_versions = [
                _stored_integer(row["version"], "schema version")
                for row in connection.execute(
                    "SELECT version FROM app_schema_migrations "
                    "WHERE component = ? ORDER BY version",
                    (_COMPONENT,),
                ).fetchall()
            ]
            if persisted_versions != [1, 2, 3]:
                raise PortfolioPaperSchemaError(
                    "Portfolio-paper migration history changed during initialization."
                )
            for table in (
                "portfolio_paper_tracks",
                "portfolio_paper_decisions",
                "portfolio_paper_advances",
                "portfolio_paper_executions",
                "portfolio_paper_execution_batches",
                "portfolio_paper_modeled_fills",
                "portfolio_paper_close_settlements",
            ):
                if connection.execute(
                    f"PRAGMA foreign_key_check({table})"
                ).fetchone() is not None:
                    raise PortfolioPaperCorruptionError(
                        f"Portfolio-paper table {table} contains orphaned rows."
                    )
            self._validate_all_tracks(connection)

    @staticmethod
    def _create_migration_ledger(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_schema_migrations (
                component TEXT NOT NULL,
                version INTEGER NOT NULL,
                applied_at TEXT NOT NULL,
                PRIMARY KEY(component, version)
            ) STRICT
            """
        )

    @staticmethod
    def _apply_migration_1(connection: sqlite3.Connection) -> None:
        PortfolioPaperTrackStore._create_migration_ledger(connection)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_paper_tracks (
                created_sequence INTEGER PRIMARY KEY AUTOINCREMENT
                    CHECK(created_sequence > 0),
                id TEXT NOT NULL UNIQUE,
                portfolio_experiment_id TEXT NOT NULL UNIQUE,
                tracking_mode TEXT NOT NULL
                    CHECK(tracking_mode = 'execution_paper'),
                status TEXT NOT NULL CHECK(status = 'internal_only'),
                config_schema_version INTEGER NOT NULL,
                configuration_hash TEXT NOT NULL,
                config_payload TEXT NOT NULL,
                state_schema_version INTEGER NOT NULL,
                state_revision INTEGER NOT NULL CHECK(state_revision >= 0),
                state_session TEXT,
                state_hash TEXT NOT NULL,
                state_payload TEXT NOT NULL,
                refresh_generation INTEGER NOT NULL DEFAULT 0
                    CHECK(refresh_generation >= 0),
                refresh_owner TEXT,
                refresh_lease_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_checked_at TEXT,
                last_error TEXT,
                CHECK(
                    (refresh_owner IS NULL AND refresh_lease_until IS NULL)
                    OR
                    (refresh_owner IS NOT NULL AND refresh_lease_until IS NOT NULL)
                )
            ) STRICT
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_paper_decisions (
                id TEXT PRIMARY KEY NOT NULL,
                track_id TEXT NOT NULL,
                information_session TEXT NOT NULL,
                target_schema_version INTEGER NOT NULL,
                target_hash TEXT NOT NULL,
                target_payload TEXT NOT NULL,
                market_input_hash TEXT NOT NULL,
                market_input_payload TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                persisted_at TEXT NOT NULL,
                UNIQUE(track_id, information_session),
                FOREIGN KEY(track_id)
                    REFERENCES portfolio_paper_tracks(id) ON DELETE CASCADE
            ) STRICT
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_paper_advances (
                id TEXT PRIMARY KEY NOT NULL,
                track_id TEXT NOT NULL,
                session TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                command_schema_version INTEGER NOT NULL,
                command_hash TEXT NOT NULL,
                expected_revision INTEGER NOT NULL,
                resulting_revision INTEGER NOT NULL,
                previous_state_hash TEXT NOT NULL,
                resulting_state_hash TEXT NOT NULL,
                command_payload TEXT NOT NULL,
                advance_payload TEXT NOT NULL,
                committed_at TEXT NOT NULL,
                UNIQUE(track_id, session),
                UNIQUE(track_id, idempotency_key),
                UNIQUE(track_id, resulting_revision),
                UNIQUE(id, track_id, session),
                CHECK(resulting_revision = expected_revision + 1),
                FOREIGN KEY(track_id)
                    REFERENCES portfolio_paper_tracks(id) ON DELETE CASCADE
            ) STRICT
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_paper_executions (
                advance_id TEXT NOT NULL,
                track_id TEXT NOT NULL,
                session TEXT NOT NULL,
                symbol TEXT NOT NULL,
                execution_schema_version INTEGER NOT NULL,
                execution_hash TEXT NOT NULL,
                execution_payload TEXT NOT NULL,
                PRIMARY KEY(track_id, session, symbol),
                FOREIGN KEY(advance_id, track_id, session)
                    REFERENCES portfolio_paper_advances(id, track_id, session)
                    ON DELETE CASCADE,
                FOREIGN KEY(track_id)
                    REFERENCES portfolio_paper_tracks(id) ON DELETE CASCADE
            ) STRICT
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_portfolio_paper_due "
            "ON portfolio_paper_tracks("
            "status, last_checked_at, refresh_lease_until, created_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_portfolio_paper_decisions_track "
            "ON portfolio_paper_decisions(track_id, decided_at DESC)"
        )

    @staticmethod
    def _apply_migration_2(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_portfolio_paper_decision_execution_identity "
            "ON portfolio_paper_decisions(id, track_id)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_paper_execution_batches (
                id TEXT PRIMARY KEY NOT NULL,
                track_id TEXT NOT NULL,
                decision_id TEXT NOT NULL,
                execution_session TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                expected_revision INTEGER NOT NULL
                    CHECK(expected_revision >= 0),
                fence_generation INTEGER NOT NULL
                    CHECK(fence_generation >= 1),
                command_schema_version INTEGER NOT NULL,
                command_hash TEXT NOT NULL,
                command_payload TEXT NOT NULL,
                batch_schema_version INTEGER NOT NULL,
                batch_hash TEXT NOT NULL,
                batch_payload TEXT NOT NULL,
                quote_set_hash TEXT NOT NULL,
                fill_set_hash TEXT NOT NULL,
                accepted_at TEXT NOT NULL,
                committed_at TEXT NOT NULL,
                UNIQUE(track_id, decision_id),
                UNIQUE(track_id, execution_session),
                UNIQUE(track_id, idempotency_key),
                UNIQUE(id, track_id, decision_id, execution_session),
                CHECK(id = idempotency_key),
                FOREIGN KEY(decision_id, track_id)
                    REFERENCES portfolio_paper_decisions(id, track_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY(track_id)
                    REFERENCES portfolio_paper_tracks(id) ON DELETE RESTRICT
            ) STRICT
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_paper_modeled_fills (
                batch_id TEXT NOT NULL,
                track_id TEXT NOT NULL,
                decision_id TEXT NOT NULL,
                execution_session TEXT NOT NULL,
                symbol TEXT NOT NULL,
                fill_schema_version INTEGER NOT NULL,
                fill_hash TEXT NOT NULL,
                fill_payload TEXT NOT NULL,
                PRIMARY KEY(track_id, execution_session, symbol),
                UNIQUE(batch_id, symbol),
                FOREIGN KEY(
                    batch_id, track_id, decision_id, execution_session
                ) REFERENCES portfolio_paper_execution_batches(
                    id, track_id, decision_id, execution_session
                ) ON DELETE CASCADE
            ) STRICT
            """
        )

    @staticmethod
    def _apply_migration_3(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_paper_close_settlements (
                id TEXT PRIMARY KEY NOT NULL,
                track_id TEXT NOT NULL,
                decision_id TEXT NOT NULL,
                opening_batch_id TEXT NOT NULL,
                execution_session TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                source_revision INTEGER NOT NULL CHECK(source_revision = 0),
                target_revision INTEGER NOT NULL
                    CHECK(target_revision = source_revision + 1),
                fence_generation INTEGER NOT NULL
                    CHECK(fence_generation >= 1),
                command_schema_version INTEGER NOT NULL,
                command_hash TEXT NOT NULL,
                command_payload TEXT NOT NULL,
                bar_set_schema_version INTEGER NOT NULL,
                bar_revision_set_hash TEXT NOT NULL,
                bar_set_hash TEXT NOT NULL,
                bar_set_payload TEXT NOT NULL,
                account_schema_version INTEGER NOT NULL,
                account_hash TEXT NOT NULL,
                account_payload TEXT NOT NULL,
                forward_state_schema_version INTEGER NOT NULL,
                forward_state_hash TEXT NOT NULL,
                forward_state_payload TEXT NOT NULL,
                settlement_schema_version INTEGER NOT NULL,
                settlement_hash TEXT NOT NULL,
                settlement_payload TEXT NOT NULL,
                accepted_at TEXT NOT NULL,
                settled_at TEXT NOT NULL,
                committed_at TEXT NOT NULL,
                UNIQUE(track_id, execution_session),
                UNIQUE(track_id, idempotency_key),
                UNIQUE(track_id, target_revision),
                UNIQUE(id, track_id, decision_id, opening_batch_id,
                       execution_session),
                CHECK(id = idempotency_key),
                FOREIGN KEY(
                    opening_batch_id, track_id, decision_id, execution_session
                ) REFERENCES portfolio_paper_execution_batches(
                    id, track_id, decision_id, execution_session
                ) ON DELETE RESTRICT
            ) STRICT
            """
        )

    @staticmethod
    def _verify_schema(
        connection: sqlite3.Connection,
        *,
        version: Literal[1, 2, 3] = 1,
    ) -> None:
        required: dict[str, set[str]] = {
            "app_schema_migrations": {
                "component",
                "version",
                "applied_at",
            },
            "portfolio_paper_tracks": {
                "created_sequence",
                "id",
                "portfolio_experiment_id",
                "tracking_mode",
                "status",
                "config_schema_version",
                "configuration_hash",
                "config_payload",
                "state_schema_version",
                "state_revision",
                "state_session",
                "state_hash",
                "state_payload",
                "refresh_generation",
                "refresh_owner",
                "refresh_lease_until",
                "created_at",
                "updated_at",
                "last_checked_at",
                "last_error",
            },
            "portfolio_paper_decisions": {
                "id",
                "track_id",
                "information_session",
                "target_schema_version",
                "target_hash",
                "target_payload",
                "market_input_hash",
                "market_input_payload",
                "observed_at",
                "decided_at",
                "persisted_at",
            },
            "portfolio_paper_advances": {
                "id",
                "track_id",
                "session",
                "idempotency_key",
                "command_schema_version",
                "command_hash",
                "expected_revision",
                "resulting_revision",
                "previous_state_hash",
                "resulting_state_hash",
                "command_payload",
                "advance_payload",
                "committed_at",
            },
            "portfolio_paper_executions": {
                "advance_id",
                "track_id",
                "session",
                "symbol",
                "execution_schema_version",
                "execution_hash",
                "execution_payload",
            },
        }
        if version >= 2:
            required.update(
                {
                    "portfolio_paper_execution_batches": {
                        "id",
                        "track_id",
                        "decision_id",
                        "execution_session",
                        "idempotency_key",
                        "expected_revision",
                        "fence_generation",
                        "command_schema_version",
                        "command_hash",
                        "command_payload",
                        "batch_schema_version",
                        "batch_hash",
                        "batch_payload",
                        "quote_set_hash",
                        "fill_set_hash",
                        "accepted_at",
                        "committed_at",
                    },
                    "portfolio_paper_modeled_fills": {
                        "batch_id",
                        "track_id",
                        "decision_id",
                        "execution_session",
                        "symbol",
                        "fill_schema_version",
                        "fill_hash",
                        "fill_payload",
                    },
                }
            )
        if version >= 3:
            required["portfolio_paper_close_settlements"] = {
                "id",
                "track_id",
                "decision_id",
                "opening_batch_id",
                "execution_session",
                "idempotency_key",
                "source_revision",
                "target_revision",
                "fence_generation",
                "command_schema_version",
                "command_hash",
                "command_payload",
                "bar_set_schema_version",
                "bar_revision_set_hash",
                "bar_set_hash",
                "bar_set_payload",
                "account_schema_version",
                "account_hash",
                "account_payload",
                "forward_state_schema_version",
                "forward_state_hash",
                "forward_state_payload",
                "settlement_schema_version",
                "settlement_hash",
                "settlement_payload",
                "accepted_at",
                "settled_at",
                "committed_at",
            }
        expected_component_objects = set(required) - {"app_schema_migrations"}
        actual_component_objects = {
            (str(row["type"]), str(row["name"]))
            for row in connection.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE type IN ('table', 'view') "
                "AND name LIKE 'portfolio_paper_%'"
            ).fetchall()
        }
        expected_component_objects_with_types = {
            ("table", name) for name in expected_component_objects
        }
        if actual_component_objects != expected_component_objects_with_types:
            raise PortfolioPaperSchemaError(
                "Portfolio-paper schema contains unexpected component tables "
                "or views."
            )
        reference = sqlite3.connect(":memory:")
        reference.row_factory = sqlite3.Row
        try:
            PortfolioPaperTrackStore._apply_migration_1(reference)
            if version >= 2:
                PortfolioPaperTrackStore._apply_migration_2(reference)
            if version >= 3:
                PortfolioPaperTrackStore._apply_migration_3(reference)
            expected_sql = {
                table: _normalized_table_sql(reference, table)
                for table in required
            }
            expected_indexes = _user_index_definitions(reference, set(required))
        finally:
            reference.close()
        for table, expected in expected_sql.items():
            if _normalized_table_sql(connection, table) != expected:
                raise PortfolioPaperSchemaError(
                    f"Portfolio-paper table {table} differs from schema version {version}."
                )
        placeholders = ",".join("?" for _ in required)
        triggers = connection.execute(
            "SELECT name FROM sqlite_master "
            f"WHERE type = 'trigger' AND tbl_name IN ({placeholders})",
            tuple(required),
        ).fetchall()
        if triggers:
            raise PortfolioPaperSchemaError(
                "Portfolio-paper schema version 1 does not permit table triggers."
            )
        table_info: dict[str, dict[str, sqlite3.Row]] = {}
        for table, expected_columns in required.items():
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            info = {str(row["name"]): row for row in rows}
            table_info[table] = info
            actual_columns = set(info)
            if not rows or expected_columns != actual_columns:
                raise PortfolioPaperSchemaError(
                    f"Portfolio-paper table {table} has an unexpected column layout."
                )
            if not _is_strict_table(connection, table):
                raise PortfolioPaperSchemaError(
                    f"Portfolio-paper table {table} must use SQLite STRICT typing."
                )
            for column, row in info.items():
                expected_type = (
                    "INTEGER"
                    if column.endswith("_schema_version")
                    or (
                        table == "app_schema_migrations"
                        and column == "version"
                    )
                    or column
                    in {
                        "created_sequence",
                        "state_revision",
                        "refresh_generation",
                        "expected_revision",
                        "resulting_revision",
                        "command_schema_version",
                        "batch_schema_version",
                        "fill_schema_version",
                        "fence_generation",
                        "source_revision",
                        "target_revision",
                        "bar_set_schema_version",
                        "account_schema_version",
                        "forward_state_schema_version",
                        "settlement_schema_version",
                    }
                    else "TEXT"
                )
                if str(row["type"]).upper() != expected_type:
                    raise PortfolioPaperSchemaError(
                        f"Portfolio-paper table {table} has an invalid column type."
                    )
        if _primary_key_columns(
            connection,
            "app_schema_migrations",
        ) != ("component", "version"):
            raise PortfolioPaperSchemaError(
                "Application migration ledger has an invalid primary key."
            )
        if _unique_column_sets(connection, "app_schema_migrations") != {
            ("component", "version")
        }:
            raise PortfolioPaperSchemaError(
                "Application migration ledger has invalid uniqueness."
            )
        if _foreign_key_column_groups(
            connection,
            "app_schema_migrations",
        ):
            raise PortfolioPaperSchemaError(
                "Application migration ledger must not have foreign keys."
            )
        if _normalized_table_sql(
            connection,
            "app_schema_migrations",
        ).count("check(") != 0:
            raise PortfolioPaperSchemaError(
                "Application migration ledger has unexpected check constraints."
            )
        tracks_sql = _normalized_table_sql(connection, "portfolio_paper_tracks")
        if (
            "check(tracking_mode='execution_paper')" not in tracks_sql
            or "check(status='internal_only')" not in tracks_sql
            or "check(state_revision>=0)" not in tracks_sql
            or "check(refresh_generation>=0)" not in tracks_sql
            or (
                "check((refresh_ownerisnullandrefresh_lease_untilisnull)"
                "or(refresh_ownerisnotnullandrefresh_lease_untilisnotnull))"
                not in tracks_sql
            )
        ):
            raise PortfolioPaperSchemaError(
                "Portfolio-paper tracks are missing required integrity checks."
            )
        if (
            tracks_sql.count("check(") != 6
            or "created_sequenceintegerprimarykeyautoincrement" not in tracks_sql
            or "check(created_sequence>0)" not in tracks_sql
        ):
            raise PortfolioPaperSchemaError(
                "Portfolio-paper tracks have unexpected integrity constraints."
            )
        if _normalized_table_sql(
            connection,
            "portfolio_paper_decisions",
        ).count("check(") != 0:
            raise PortfolioPaperSchemaError(
                "Portfolio-paper decisions have unexpected check constraints."
            )
        if _primary_key_columns(
            connection,
            "portfolio_paper_tracks",
        ) != ("created_sequence",):
            raise PortfolioPaperSchemaError(
                "Portfolio-paper tracks have an invalid primary key."
            )
        if _unique_column_sets(connection, "portfolio_paper_tracks") != {
            ("id",),
            ("portfolio_experiment_id",),
        }:
            raise PortfolioPaperSchemaError(
                "Portfolio-paper tracks are missing experiment uniqueness."
            )

        decision_uniques = _unique_column_sets(
            connection,
            "portfolio_paper_decisions",
        )
        expected_decision_uniques = {
            ("id",),
            ("track_id", "information_session"),
        }
        if version >= 2:
            expected_decision_uniques.add(
                ("id", "track_id")
            )
        if decision_uniques != expected_decision_uniques:
            raise PortfolioPaperSchemaError(
                "Portfolio-paper decisions are missing their session uniqueness."
            )
        if _primary_key_columns(
            connection,
            "portfolio_paper_decisions",
        ) != ("id",):
            raise PortfolioPaperSchemaError(
                "Portfolio-paper decisions have an invalid primary key."
            )
        decision_foreign_keys = _foreign_key_column_groups(
            connection,
            "portfolio_paper_decisions",
        )
        if decision_foreign_keys != {
            (
                ("track_id",),
                "portfolio_paper_tracks",
                ("id",),
                "CASCADE",
            )
        }:
            raise PortfolioPaperSchemaError(
                "Portfolio-paper decisions have invalid foreign keys."
            )
        advance_uniques = _unique_column_sets(
            connection,
            "portfolio_paper_advances",
        )
        expected_advance_uniques = {
            ("id",),
            ("track_id", "session"),
            ("track_id", "idempotency_key"),
            ("track_id", "resulting_revision"),
            ("id", "track_id", "session"),
        }
        if expected_advance_uniques != advance_uniques:
            raise PortfolioPaperSchemaError(
                "Portfolio-paper advances are missing idempotency uniqueness."
            )
        if _primary_key_columns(
            connection,
            "portfolio_paper_advances",
        ) != ("id",):
            raise PortfolioPaperSchemaError(
                "Portfolio-paper advances have an invalid primary key."
            )
        advance_foreign_keys = _foreign_key_column_groups(
            connection,
            "portfolio_paper_advances",
        )
        if advance_foreign_keys != {
            (
                ("track_id",),
                "portfolio_paper_tracks",
                ("id",),
                "CASCADE",
            )
        }:
            raise PortfolioPaperSchemaError(
                "Portfolio-paper advances have invalid foreign keys."
            )
        advances_sql = _normalized_table_sql(
            connection,
            "portfolio_paper_advances",
        )
        if (
            "check(resulting_revision=expected_revision+1)"
            not in advances_sql
            or advances_sql.count("check(") != 1
        ):
            raise PortfolioPaperSchemaError(
                "Portfolio-paper advances are missing revision continuity."
            )

        if _primary_key_columns(
            connection,
            "portfolio_paper_executions",
        ) != ("track_id", "session", "symbol"):
            raise PortfolioPaperSchemaError(
                "Portfolio-paper executions have an invalid primary key."
            )
        execution_foreign_keys = _foreign_key_column_groups(
            connection,
            "portfolio_paper_executions",
        )
        expected_execution_foreign_keys = {
            (
                ("advance_id", "track_id", "session"),
                "portfolio_paper_advances",
                ("id", "track_id", "session"),
                "CASCADE",
            ),
            (("track_id",), "portfolio_paper_tracks", ("id",), "CASCADE"),
        }
        if expected_execution_foreign_keys != execution_foreign_keys:
            raise PortfolioPaperSchemaError(
                "Portfolio-paper executions have invalid foreign keys."
            )
        if _unique_column_sets(connection, "portfolio_paper_executions") != {
            ("track_id", "session", "symbol")
        }:
            raise PortfolioPaperSchemaError(
                "Portfolio-paper executions have invalid uniqueness."
            )
        if _normalized_table_sql(
            connection,
            "portfolio_paper_executions",
        ).count("check(") != 0:
            raise PortfolioPaperSchemaError(
                "Portfolio-paper executions have unexpected check constraints."
            )
        if version >= 2:
            batch_table = "portfolio_paper_execution_batches"
            fill_table = "portfolio_paper_modeled_fills"
            if _primary_key_columns(connection, batch_table) != ("id",):
                raise PortfolioPaperSchemaError(
                    "Portfolio-paper opening batches have an invalid primary key."
                )
            if _unique_column_sets(connection, batch_table) != {
                ("id",),
                ("track_id", "decision_id"),
                ("track_id", "execution_session"),
                ("track_id", "idempotency_key"),
                ("id", "track_id", "decision_id", "execution_session"),
            }:
                raise PortfolioPaperSchemaError(
                    "Portfolio-paper opening batches have invalid uniqueness."
                )
            if _foreign_key_column_groups(connection, batch_table) != {
                (
                    ("decision_id", "track_id"),
                    "portfolio_paper_decisions",
                    ("id", "track_id"),
                    "RESTRICT",
                ),
                (("track_id",), "portfolio_paper_tracks", ("id",), "RESTRICT"),
            }:
                raise PortfolioPaperSchemaError(
                    "Portfolio-paper opening batches have invalid foreign keys."
                )
            batches_sql = _normalized_table_sql(connection, batch_table)
            if (
                "check(id=idempotency_key)" not in batches_sql
                or "check(expected_revision>=0)" not in batches_sql
                or "check(fence_generation>=1)" not in batches_sql
                or batches_sql.count("check(") != 3
            ):
                raise PortfolioPaperSchemaError(
                    "Portfolio-paper opening batches have invalid integrity checks."
                )
            if _primary_key_columns(connection, fill_table) != (
                "track_id",
                "execution_session",
                "symbol",
            ):
                raise PortfolioPaperSchemaError(
                    "Portfolio-paper modeled fills have an invalid primary key."
                )
            if _unique_column_sets(connection, fill_table) != {
                ("track_id", "execution_session", "symbol"),
                ("batch_id", "symbol"),
            }:
                raise PortfolioPaperSchemaError(
                    "Portfolio-paper modeled fills have invalid uniqueness."
                )
            if _foreign_key_column_groups(connection, fill_table) != {
                (
                    (
                        "batch_id",
                        "track_id",
                        "decision_id",
                        "execution_session",
                    ),
                    "portfolio_paper_execution_batches",
                    ("id", "track_id", "decision_id", "execution_session"),
                    "CASCADE",
                )
            }:
                raise PortfolioPaperSchemaError(
                    "Portfolio-paper modeled fills have invalid foreign keys."
                )
            if _normalized_table_sql(connection, fill_table).count("check(") != 0:
                raise PortfolioPaperSchemaError(
                    "Portfolio-paper modeled fills have unexpected checks."
                )
        if version >= 3:
            settlement_table = "portfolio_paper_close_settlements"
            if _primary_key_columns(connection, settlement_table) != ("id",):
                raise PortfolioPaperSchemaError(
                    "Portfolio-paper close settlements have an invalid primary key."
                )
            if _unique_column_sets(connection, settlement_table) != {
                ("id",),
                ("track_id", "execution_session"),
                ("track_id", "idempotency_key"),
                ("track_id", "target_revision"),
                (
                    "id",
                    "track_id",
                    "decision_id",
                    "opening_batch_id",
                    "execution_session",
                ),
            }:
                raise PortfolioPaperSchemaError(
                    "Portfolio-paper close settlements have invalid uniqueness."
                )
            if _foreign_key_column_groups(connection, settlement_table) != {
                (
                    (
                        "opening_batch_id",
                        "track_id",
                        "decision_id",
                        "execution_session",
                    ),
                    "portfolio_paper_execution_batches",
                    ("id", "track_id", "decision_id", "execution_session"),
                    "RESTRICT",
                )
            }:
                raise PortfolioPaperSchemaError(
                    "Portfolio-paper close settlements have invalid foreign keys."
                )
            settlement_sql = _normalized_table_sql(
                connection,
                settlement_table,
            )
            required_checks = {
                "check(source_revision=0)",
                "check(target_revision=source_revision+1)",
                "check(fence_generation>=1)",
                "check(id=idempotency_key)",
            }
            if (
                any(check not in settlement_sql for check in required_checks)
                or settlement_sql.count("check(") != len(required_checks)
            ):
                raise PortfolioPaperSchemaError(
                    "Portfolio-paper close settlements have invalid integrity checks."
                )
        required_not_null: dict[str, set[str]] = {
            "app_schema_migrations": set(required["app_schema_migrations"]),
            "portfolio_paper_tracks": {
                "id",
                "portfolio_experiment_id",
                "tracking_mode",
                "status",
                "config_schema_version",
                "configuration_hash",
                "config_payload",
                "state_schema_version",
                "state_revision",
                "state_hash",
                "state_payload",
                "refresh_generation",
                "created_at",
                "updated_at",
            },
            "portfolio_paper_decisions": set(required["portfolio_paper_decisions"]),
            "portfolio_paper_advances": set(required["portfolio_paper_advances"]),
            "portfolio_paper_executions": set(required["portfolio_paper_executions"]),
        }
        if version >= 2:
            required_not_null["portfolio_paper_execution_batches"] = set(
                required["portfolio_paper_execution_batches"]
            )
            required_not_null["portfolio_paper_modeled_fills"] = set(
                required["portfolio_paper_modeled_fills"]
            )
        if version >= 3:
            required_not_null["portfolio_paper_close_settlements"] = set(
                required["portfolio_paper_close_settlements"]
            )
        for table, expected_columns in required_not_null.items():
            actual = {
                column
                for column, row in table_info[table].items()
                if int(row["notnull"]) == 1
            }
            if actual != expected_columns:
                raise PortfolioPaperSchemaError(
                    f"Portfolio-paper table {table} has invalid nullability."
                )
        for table, info in table_info.items():
            actual_defaults = {
                column: str(row["dflt_value"])
                for column, row in info.items()
                if row["dflt_value"] is not None
            }
            expected_defaults = (
                {"refresh_generation": "0"}
                if table == "portfolio_paper_tracks"
                else {}
            )
            if actual_defaults != expected_defaults:
                raise PortfolioPaperSchemaError(
                    f"Portfolio-paper table {table} has invalid defaults."
                )
        actual_indexes = _user_index_definitions(connection, set(required))
        if actual_indexes != expected_indexes:
            raise PortfolioPaperSchemaError(
                f"Portfolio-paper schema version {version} has unexpected indexes."
            )

    def create(
        self,
        config: PortfolioPaperConfig,
        decision: PortfolioPaperDecisionCertificate,
    ) -> PortfolioPaperTrackRecord:
        """Atomically persist a zero-return state and its pending target evidence."""

        safe_config = PortfolioPaperConfig.model_validate(
            config.model_dump(mode="python")
        )
        safe_certificate = PortfolioPaperDecisionCertificate.model_validate(
            decision.model_dump(mode="python")
        )
        safe_target = safe_certificate.target
        configuration_hash = _canonical_hash(safe_config)
        if safe_certificate.configuration_hash != configuration_hash:
            raise ValueError(
                "Decision certificate does not match the frozen paper configuration."
            )
        if (
            safe_certificate.basket_identity_hash
            != safe_config.basket_identity.identity_hash
        ):
            raise ValueError(
                "Decision certificate does not match the stable paper basket identity."
            )
        if (
            safe_certificate.calculation_version
            != safe_config.calculation_version
            or safe_certificate.volatility_lookback
            != safe_config.volatility_lookback
            or safe_certificate.maximum_asset_weight
            != safe_config.maximum_asset_weight
        ):
            raise ValueError(
                "Decision calculation parameters do not match the frozen config."
            )
        if safe_target.method != safe_config.method:
            raise ValueError("Decision method does not match the frozen config.")
        _validate_minimum_activation_budget(safe_config, safe_certificate)
        track_id = uuid4().hex
        state = _rebuild_activation_state(safe_config, safe_target)
        config_payload = _canonical_json(safe_config)
        state_payload = _canonical_json(state)
        target_payload = _canonical_json(safe_target)
        certificate_payload = _canonical_json(safe_certificate)
        observed = max(bar.observed_at for bar in safe_certificate.bars)
        decided = safe_certificate.decided_at
        created_record: PortfolioPaperTrackRecord | None = None
        try:
            with self._write_transaction() as connection:
                now = _database_now(connection)
                if now >= safe_certificate.execution_deadline:
                    raise ValueError(
                        "Portfolio-paper decision expired before durable activation."
                    )
                stored_decision = PortfolioPaperDecision(
                    id=safe_certificate.decision_id,
                    track_id=track_id,
                    target=safe_target,
                    target_hash=_canonical_hash(safe_target),
                    certificate=safe_certificate,
                    certificate_hash=_canonical_hash(safe_certificate),
                    persisted_at=now,
                )
                connection.execute(
                    """
                    INSERT INTO portfolio_paper_tracks (
                        id, portfolio_experiment_id, tracking_mode, status,
                        config_schema_version, configuration_hash, config_payload,
                        state_schema_version, state_revision, state_session,
                        state_hash, state_payload, refresh_generation,
                        created_at, updated_at
                    ) VALUES (?, ?, 'execution_paper', 'internal_only',
                              ?, ?, ?, ?, 0, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        track_id,
                        safe_config.portfolio_experiment_id,
                        safe_config.schema_version,
                        configuration_hash,
                        config_payload,
                        state.schema_version,
                        state.session,
                        _canonical_hash(state),
                        state_payload,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO portfolio_paper_decisions (
                        id, track_id, information_session,
                        target_schema_version, target_hash, target_payload,
                        market_input_hash, market_input_payload,
                        observed_at, decided_at, persisted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stored_decision.id,
                        track_id,
                        safe_target.information_session,
                        safe_target.schema_version,
                        stored_decision.target_hash,
                        target_payload,
                        stored_decision.certificate_hash,
                        certificate_payload,
                        observed.isoformat(),
                        decided.isoformat(),
                        stored_decision.persisted_at.isoformat(),
                    ),
                )
                created_row = connection.execute(
                    "SELECT * FROM portfolio_paper_tracks WHERE id = ?",
                    (track_id,),
                ).fetchone()
                if created_row is None:  # pragma: no cover - same-transaction invariant
                    raise PortfolioPaperCorruptionError(
                        "Created portfolio-paper track disappeared before commit."
                    )
                created_record = self._row_to_record(connection, created_row)
        except sqlite3.IntegrityError as error:
            raise PortfolioPaperConflictError(
                "A portfolio-paper track or activation decision already exists."
            ) from error
        if created_record is None:  # pragma: no cover - defensive assignment guard
            raise PortfolioPaperCorruptionError(
                "Created portfolio-paper track could not be validated before commit."
            )
        return created_record

    def get(self, track_id: str) -> PortfolioPaperTrackRecord | None:
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM portfolio_paper_tracks WHERE id = ?",
                (track_id,),
            ).fetchone()
            return self._row_to_record(connection, row) if row is not None else None

    def list(self, *, limit: int = 100) -> list[PortfolioPaperTrackRecord]:
        if not 1 <= limit <= 500:
            raise ValueError("Portfolio-paper list limit must be between 1 and 500.")
        with self._read_transaction() as connection:
            self._validate_all_tracks(connection)
            rows = connection.execute(
                "SELECT * FROM portfolio_paper_tracks "
                "ORDER BY created_sequence DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._row_to_record(connection, row) for row in rows]

    def validate_execution_quotes(
        self,
        *,
        track_id: str,
        decision_id: str,
        basket: PortfolioPaperBasketContract,
        quotes: Sequence[ExecutionQuote],
    ) -> dict[str, ExecutionQuote]:
        """Authenticate a committed pending decision before pure quote validation.

        This is still not an execution command. A future advance operation must repeat
        these checks inside the same fenced write transaction that commits the ledger.
        """

        from .portfolio_paper_execution import (
            build_portfolio_paper_activation_opening_batch,
        )

        safe_basket = PortfolioPaperBasketContract.model_validate(
            basket.model_dump(mode="python")
        )
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM portfolio_paper_tracks WHERE id = ?",
                (track_id,),
            ).fetchone()
            if row is None:
                raise PortfolioPaperConflictError(
                    "Execution quote validation requires an existing committed track."
                )
            record = self._row_to_record(connection, row)
            pending = record.pending_decision
            if pending is None or pending.id != decision_id:
                raise PortfolioPaperConflictError(
                    "Execution quote validation requires the current pending decision."
                )
            accepted_at = _database_now(connection)
            accepted_quotes = _validate_execution_quotes_against_receipt(
                basket=safe_basket,
                decision=pending.receipt,
                quotes=quotes,
                accepted_at=accepted_at,
            )
            build_portfolio_paper_activation_opening_batch(
                track_id=record.id,
                state_revision=record.state_revision,
                available_cash=record.state.cash,
                configuration_hash=record.configuration_hash,
                decision_receipt=pending.receipt,
                basket=safe_basket,
                execution_quotes=accepted_quotes,
                fee_rate=record.config.fee_rate,
                slippage_rate=record.config.slippage_rate,
                accepted_at=accepted_at,
            )
            return accepted_quotes

    def commit_opening_if_current(
        self,
        *,
        track_id: str,
        owner: str,
        generation: int,
        expected_revision: int,
        basket: PortfolioPaperBasketContract,
        quotes: Sequence[ExecutionQuote],
    ) -> PortfolioPaperExecutionBatch:
        """Commit the activation opening exactly once under a current fenced lease.

        Quote acquisition is deliberately outside this method. The supplied models
        are cloned before the write transaction; the authenticated receipt, quote
        contract, deterministic batch, and all persisted rows are then revalidated
        under one ``BEGIN IMMEDIATE`` transaction.
        """

        from .portfolio_paper_execution import (
            build_portfolio_paper_activation_opening_batch,
        )

        normalized_owner = _normalize_owner(owner)
        if isinstance(generation, bool) or generation < 1:
            raise ValueError("Opening fence generation must be a positive integer.")
        if isinstance(expected_revision, bool) or expected_revision < 0:
            raise ValueError("Opening expected revision must be a non-negative integer.")
        safe_basket = PortfolioPaperBasketContract.model_validate(
            basket.model_dump(mode="python")
        )
        safe_quotes = tuple(
            ExecutionQuote.model_validate(quote.model_dump(mode="python"))
            for quote in quotes
        )
        committed_batch: PortfolioPaperExecutionBatch | None = None
        try:
            with self._write_transaction() as connection:
                database_now = _database_now(connection)
                row = connection.execute(
                    "SELECT * FROM portfolio_paper_tracks WHERE id = ?",
                    (track_id,),
                ).fetchone()
                if row is None:
                    raise PortfolioPaperConflictError(
                        "Opening commit requires an existing portfolio-paper track."
                    )
                record = self._row_to_record(connection, row)
                pending = record.pending_decision
                if pending is None:
                    raise PortfolioPaperConflictError(
                        "Opening commit requires the durable pending decision."
                    )
                activation_state = _rebuild_activation_state(
                    record.config,
                    pending.target,
                )
                if record.opening_batch_id is not None:
                    existing, _committed_at = self._load_opening_batch(
                        connection,
                        record.id,
                        record.config,
                        activation_state,
                        pending,
                        record.refresh_generation,
                    )
                    if existing is None:  # pragma: no cover - read-model invariant
                        raise PortfolioPaperCorruptionError(
                            "Opening read model lost its persisted batch."
                        )
                    if (
                        expected_revision != existing.command.state_revision
                        or safe_basket.identity.identity_hash
                        != existing.command.basket_identity_hash
                        or safe_basket.basket_hash != existing.command.basket_hash
                    ):
                        raise PortfolioPaperConflictError(
                            "Opening retry describes a different logical command."
                        )
                    committed_batch = existing
                else:
                    if database_now < record.updated_at:
                        raise PortfolioPaperConflictError(
                            "Database clock precedes the durable track timestamp."
                        )
                    if database_now > pending.certificate.execution_deadline:
                        raise PortfolioPaperConflictError(
                            "Opening decision has passed its certified deadline."
                        )
                    if (
                        record.state_revision != expected_revision
                        or record.state != activation_state
                        or record.refresh_owner != normalized_owner
                        or record.refresh_generation != generation
                        or record.refresh_lease_until is None
                        or record.refresh_lease_until <= database_now
                    ):
                        raise PortfolioPaperConflictError(
                            "Opening commit lost its current fenced lease."
                        )
                    accepted_quotes = _validate_execution_quotes_against_receipt(
                        basket=safe_basket,
                        decision=pending.receipt,
                        quotes=safe_quotes,
                        accepted_at=database_now,
                    )
                    candidate = build_portfolio_paper_activation_opening_batch(
                        track_id=record.id,
                        state_revision=record.state_revision,
                        available_cash=record.state.cash,
                        configuration_hash=record.configuration_hash,
                        decision_receipt=pending.receipt,
                        basket=safe_basket,
                        execution_quotes=accepted_quotes,
                        fee_rate=record.config.fee_rate,
                        slippage_rate=record.config.slippage_rate,
                        accepted_at=database_now,
                    )
                    connection.execute(
                        """
                        INSERT INTO portfolio_paper_execution_batches (
                            id, track_id, decision_id, execution_session,
                            idempotency_key, expected_revision, fence_generation,
                            command_schema_version, command_hash, command_payload,
                            batch_schema_version, batch_hash, batch_payload,
                            quote_set_hash, fill_set_hash, accepted_at, committed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            candidate.command.idempotency_key,
                            record.id,
                            pending.id,
                            candidate.command.execution_session,
                            candidate.command.idempotency_key,
                            expected_revision,
                            generation,
                            candidate.command.schema_version,
                            candidate.command.command_hash,
                            _canonical_json(candidate.command),
                            candidate.schema_version,
                            candidate.batch_hash,
                            _canonical_json(candidate),
                            candidate.quote_set_hash,
                            candidate.fill_set_hash,
                            database_now.isoformat(),
                            database_now.isoformat(),
                        ),
                    )
                    _opening_commit_checkpoint("after_batch", None)
                    for index, fill in enumerate(candidate.fills, start=1):
                        connection.execute(
                            """
                            INSERT INTO portfolio_paper_modeled_fills (
                                batch_id, track_id, decision_id, execution_session,
                                symbol, fill_schema_version, fill_hash, fill_payload
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                candidate.command.idempotency_key,
                                record.id,
                                pending.id,
                                candidate.command.execution_session,
                                fill.symbol,
                                fill.schema_version,
                                fill.fill_hash,
                                _canonical_json(fill),
                            ),
                        )
                        _opening_commit_checkpoint("after_fill", index)
                    persisted, committed_at = self._load_opening_batch(
                        connection,
                        record.id,
                        record.config,
                        record.state,
                        pending,
                        record.refresh_generation,
                    )
                    if persisted != candidate or committed_at != database_now:
                        raise PortfolioPaperCorruptionError(
                            "Opening batch failed same-transaction readback validation."
                        )
                    cursor = connection.execute(
                        """
                        UPDATE portfolio_paper_tracks
                        SET refresh_owner = NULL,
                            refresh_lease_until = NULL,
                            last_error = NULL,
                            last_checked_at = ?,
                            updated_at = ?
                        WHERE id = ?
                          AND state_revision = ?
                          AND refresh_owner = ?
                          AND refresh_generation = ?
                          AND refresh_lease_until > ?
                        """,
                        (
                            database_now.isoformat(),
                            database_now.isoformat(),
                            record.id,
                            expected_revision,
                            normalized_owner,
                            generation,
                            database_now.isoformat(),
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise PortfolioPaperConflictError(
                            "Opening commit lost its fenced lease before finalization."
                        )
                    updated = connection.execute(
                        "SELECT * FROM portfolio_paper_tracks WHERE id = ?",
                        (record.id,),
                    ).fetchone()
                    if updated is None:  # pragma: no cover - same transaction invariant
                        raise PortfolioPaperCorruptionError(
                            "Opened portfolio-paper track disappeared before commit."
                        )
                    updated_record = self._row_to_record(connection, updated)
                    if (
                        updated_record.opening_batch_id
                        != candidate.command.idempotency_key
                        or updated_record.state_revision != expected_revision
                        or updated_record.refresh_owner is not None
                        or updated_record.refresh_lease_until is not None
                        or updated_record.last_checked_at != database_now
                        or updated_record.updated_at != database_now
                        or updated_record.last_error is not None
                    ):
                        raise PortfolioPaperCorruptionError(
                            "Opening track finalization failed strict validation."
                        )
                    committed_batch = candidate
        except sqlite3.IntegrityError as error:
            raise PortfolioPaperConflictError(
                "Opening batch conflicts with an existing logical execution."
            ) from error
        if committed_batch is None:  # pragma: no cover - assignment guard
            raise PortfolioPaperCorruptionError(
                "Opening batch was not available after its transaction."
            )
        return committed_batch

    def commit_close_settlement_if_current(
        self,
        *,
        track_id: str,
        owner: str,
        generation: int,
        expected_revision: int,
        bars: Sequence[CertifiedPortfolioBar],
    ) -> PortfolioPaperModeledCloseSettlement:
        """Atomically value an opening at certified completed-session closes.

        This commits no close order or fill. The exact account is rebuilt from the
        durable opening quantities and supplied certified close bars inside the same
        fenced transaction that advances the compatibility state to revision one.
        """

        from .portfolio_paper_settlement import (
            build_portfolio_paper_close_valuation,
        )

        normalized_owner = _normalize_owner(owner)
        if isinstance(generation, bool) or generation < 1:
            raise ValueError(
                "Close-settlement fence generation must be a positive integer."
            )
        if (
            isinstance(expected_revision, bool)
            or expected_revision != 0
        ):
            raise ValueError("Close settlement requires expected revision zero.")
        if isinstance(bars, (str, bytes)) or not isinstance(bars, Sequence):
            raise ValueError("Close-settlement bars must be a sequence.")
        safe_bars = tuple(
            CertifiedPortfolioBar.model_validate(
                bar.model_dump(mode="python")
                if isinstance(bar, CertifiedPortfolioBar)
                else bar
            )
            for bar in bars
        )
        committed: PortfolioPaperModeledCloseSettlement | None = None
        try:
            with self._write_transaction() as connection:
                database_now = _database_now(connection)
                row = connection.execute(
                    "SELECT * FROM portfolio_paper_tracks WHERE id = ?",
                    (track_id,),
                ).fetchone()
                if row is None:
                    raise PortfolioPaperConflictError(
                        "Close settlement requires an existing paper track."
                    )
                record = self._row_to_record(connection, row)
                activation_decision = record.pending_decision
                if activation_decision is None:  # pragma: no cover - record invariant
                    raise PortfolioPaperCorruptionError(
                        "Close settlement lost the durable activation decision."
                    )
                activation_state = _rebuild_activation_state(
                    record.config,
                    activation_decision.target,
                )
                opening_batch, _opening_committed_at = self._load_opening_batch(
                    connection,
                    record.id,
                    record.config,
                    activation_state,
                    activation_decision,
                    record.refresh_generation,
                )
                if opening_batch is None:
                    raise PortfolioPaperConflictError(
                        "Close settlement requires a committed opening batch."
                    )
                if record.settlement_id is not None:
                    existing, _committed_at = self._load_close_settlement(
                        connection,
                        record.id,
                        record.config,
                        record.state,
                        activation_state,
                        activation_decision,
                        opening_batch,
                        record.refresh_generation,
                    )
                    if existing is None:  # pragma: no cover - record invariant
                        raise PortfolioPaperCorruptionError(
                            "Settled read model lost its close settlement."
                        )
                    candidate = build_portfolio_paper_close_valuation(
                        opening_batch=opening_batch,
                        bars=safe_bars,
                        initial_state=activation_state,
                        accepted_at=existing.bar_set.accepted_at,
                        settled_at=existing.settled_at,
                    )
                    if (
                        candidate.command.idempotency_key
                        != existing.command.idempotency_key
                        or candidate != existing
                    ):
                        raise PortfolioPaperConflictError(
                            "Close-settlement retry describes different evidence."
                        )
                    committed = existing
                else:
                    if database_now < record.updated_at:
                        raise PortfolioPaperConflictError(
                            "Database clock precedes the durable track timestamp."
                        )
                    if (
                        record.state_revision != expected_revision
                        or record.state != activation_state
                        or record.refresh_owner != normalized_owner
                        or record.refresh_generation != generation
                        or record.refresh_lease_until is None
                        or record.refresh_lease_until <= database_now
                    ):
                        raise PortfolioPaperConflictError(
                            "Close settlement lost its current fenced lease."
                        )
                    candidate = build_portfolio_paper_close_valuation(
                        opening_batch=opening_batch,
                        bars=safe_bars,
                        initial_state=activation_state,
                        accepted_at=database_now,
                        settled_at=database_now,
                    )
                    command = candidate.command
                    bar_set = candidate.bar_set
                    account = candidate.account
                    forward_state = candidate.forward_state
                    connection.execute(
                        """
                        INSERT INTO portfolio_paper_close_settlements (
                            id, track_id, decision_id, opening_batch_id,
                            execution_session, idempotency_key,
                            source_revision, target_revision, fence_generation,
                            command_schema_version, command_hash, command_payload,
                            bar_set_schema_version, bar_revision_set_hash,
                            bar_set_hash, bar_set_payload,
                            account_schema_version, account_hash, account_payload,
                            forward_state_schema_version, forward_state_hash,
                            forward_state_payload, settlement_schema_version,
                            settlement_hash, settlement_payload,
                            accepted_at, settled_at, committed_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        (
                            command.idempotency_key,
                            record.id,
                            activation_decision.id,
                            opening_batch.command.idempotency_key,
                            command.execution_session,
                            command.idempotency_key,
                            command.source_state_revision,
                            command.target_state_revision,
                            generation,
                            command.schema_version,
                            command.command_hash,
                            _canonical_json(command),
                            bar_set.schema_version,
                            bar_set.revision_set_hash,
                            bar_set.bar_set_hash,
                            _canonical_json(bar_set),
                            account.schema_version,
                            account.account_hash,
                            _canonical_json(account),
                            forward_state.schema_version,
                            candidate.forward_state_hash,
                            _canonical_json(forward_state),
                            candidate.schema_version,
                            candidate.settlement_hash,
                            _canonical_json(candidate),
                            database_now.isoformat(),
                            database_now.isoformat(),
                            database_now.isoformat(),
                        ),
                    )
                    _settlement_commit_checkpoint("after_settlement")
                    state_payload = _canonical_json(forward_state)
                    cursor = connection.execute(
                        """
                        UPDATE portfolio_paper_tracks
                        SET state_schema_version = ?,
                            state_revision = ?,
                            state_session = ?,
                            state_hash = ?,
                            state_payload = ?,
                            refresh_owner = NULL,
                            refresh_lease_until = NULL,
                            last_error = NULL,
                            last_checked_at = ?,
                            updated_at = ?
                        WHERE id = ?
                          AND state_revision = ?
                          AND refresh_owner = ?
                          AND refresh_generation = ?
                          AND refresh_lease_until > ?
                        """,
                        (
                            forward_state.schema_version,
                            command.target_state_revision,
                            forward_state.session,
                            candidate.forward_state_hash,
                            state_payload,
                            database_now.isoformat(),
                            database_now.isoformat(),
                            record.id,
                            expected_revision,
                            normalized_owner,
                            generation,
                            database_now.isoformat(),
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise PortfolioPaperConflictError(
                            "Close settlement lost its fence before finalization."
                        )
                    _settlement_commit_checkpoint("after_state")
                    updated = connection.execute(
                        "SELECT * FROM portfolio_paper_tracks WHERE id = ?",
                        (record.id,),
                    ).fetchone()
                    if updated is None:  # pragma: no cover - transaction invariant
                        raise PortfolioPaperCorruptionError(
                            "Settled paper track disappeared before commit."
                        )
                    updated_record = self._row_to_record(connection, updated)
                    if (
                        updated_record.settlement_id != command.idempotency_key
                        or updated_record.settlement_session
                        != command.execution_session
                        or updated_record.settlement_committed_at != database_now
                        or updated_record.state != forward_state
                        or updated_record.state_revision
                        != command.target_state_revision
                        or updated_record.refresh_owner is not None
                        or updated_record.refresh_lease_until is not None
                        or updated_record.last_error is not None
                        or updated_record.last_checked_at != database_now
                        or updated_record.updated_at != database_now
                    ):
                        raise PortfolioPaperCorruptionError(
                            "Close-settlement track finalization failed validation."
                        )
                    committed = candidate
        except sqlite3.IntegrityError as error:
            raise PortfolioPaperConflictError(
                "Close settlement conflicts with an existing logical valuation."
            ) from error
        if committed is None:  # pragma: no cover - assignment guard
            raise PortfolioPaperCorruptionError(
                "Close settlement was unavailable after its transaction."
            )
        return committed

    def delete(self, track_id: str) -> bool:
        with self._write_transaction() as connection:
            database_now = _database_now(connection)
            row = connection.execute(
                "SELECT * FROM portfolio_paper_tracks WHERE id = ?",
                (track_id,),
            ).fetchone()
            if row is None:
                return False
            record = self._row_to_record(connection, row)
            if record.settlement_id is not None:
                raise PortfolioPaperConflictError(
                    "A portfolio-paper track with a committed close settlement "
                    "cannot be deleted."
                )
            if record.opening_batch_id is not None:
                raise PortfolioPaperConflictError(
                    "A portfolio-paper track with a committed opening cannot be deleted."
                )
            if (
                record.refresh_owner is not None
                and record.refresh_lease_until is not None
                and record.refresh_lease_until > database_now
            ):
                raise PortfolioPaperConflictError(
                    "A portfolio-paper track with an active lease cannot be deleted."
                )
            cursor = connection.execute(
                "DELETE FROM portfolio_paper_tracks WHERE id = ?",
                (track_id,),
            )
            return cursor.rowcount == 1

    def claim_due(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        due_before: datetime | None = None,
    ) -> PortfolioPaperLease | None:
        """Claim one internal track with a durable, monotonically fenced lease."""

        normalized_owner = _normalize_owner(owner)
        _require_utc(now, "Lease claim time")
        requested_due_cutoff = (
            _require_utc(due_before, "Lease due cutoff")
            if due_before is not None
            else None
        )
        if lease_for <= timedelta(0) or lease_for > timedelta(hours=1):
            raise ValueError("Lease duration must be positive and at most one hour.")
        with self._write_transaction() as connection:
            database_now = _database_now(connection)
            self._validate_all_tracks(connection)
            due_cutoff = requested_due_cutoff or database_now
            candidate_rows = connection.execute(
                """
                SELECT * FROM portfolio_paper_tracks
                WHERE status = 'internal_only'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM portfolio_paper_execution_batches AS opening
                      WHERE opening.track_id = portfolio_paper_tracks.id
                  )
                  AND (
                      last_error IS NULL
                      OR last_error <> ?
                  )
                  AND (last_checked_at IS NULL OR last_checked_at <= ?)
                  AND (
                      refresh_owner IS NULL
                      OR refresh_lease_until <= ?
                )
                ORDER BY COALESCE(last_checked_at, created_at),
                         created_at, created_sequence
                """,
                (
                    _OPENING_WINDOW_TERMINAL_ERROR,
                    due_cutoff.isoformat(),
                    database_now.isoformat(),
                ),
            ).fetchall()
            row: sqlite3.Row | None = None
            current_record: PortfolioPaperTrackRecord | None = None
            expires_at: datetime | None = None
            for candidate_row in candidate_rows:
                candidate_record = self._row_to_record(
                    connection,
                    candidate_row,
                )
                pending = candidate_record.pending_decision
                if pending is None:
                    continue
                execution_start = _stored_datetime(
                    pending.certificate.execution_session,
                    "certified execution session",
                )
                effective_now = _effective_track_now(
                    candidate_record,
                    database_now,
                )
                candidate_expires_at = effective_now + lease_for
                execution_deadline = pending.certificate.execution_deadline
                if (
                    database_now + lease_for > execution_deadline
                    or candidate_expires_at > execution_deadline
                ):
                    terminalized = connection.execute(
                        """
                        UPDATE portfolio_paper_tracks
                        SET last_error = ?,
                            last_checked_at = ?,
                            updated_at = ?,
                            refresh_owner = NULL,
                            refresh_lease_until = NULL
                        WHERE id = ?
                          AND NOT EXISTS (
                              SELECT 1
                              FROM portfolio_paper_execution_batches AS opening
                              WHERE opening.track_id = portfolio_paper_tracks.id
                          )
                          AND (
                              refresh_owner IS NULL
                              OR refresh_lease_until <= ?
                          )
                        """,
                        (
                            _OPENING_WINDOW_TERMINAL_ERROR,
                            effective_now.isoformat(),
                            effective_now.isoformat(),
                            candidate_record.id,
                            database_now.isoformat(),
                        ),
                    )
                    if terminalized.rowcount != 1:
                        raise PortfolioPaperCorruptionError(
                            "Unsafe portfolio opening failed terminalization."
                        )
                    terminal_row = connection.execute(
                        "SELECT * FROM portfolio_paper_tracks WHERE id = ?",
                        (candidate_record.id,),
                    ).fetchone()
                    if terminal_row is None:
                        raise PortfolioPaperCorruptionError(
                            "Terminal portfolio opening disappeared."
                        )
                    terminal_record = self._row_to_record(
                        connection,
                        terminal_row,
                    )
                    if (
                        terminal_record.last_error
                        != _OPENING_WINDOW_TERMINAL_ERROR
                        or terminal_record.last_checked_at != effective_now
                        or terminal_record.updated_at != effective_now
                        or terminal_record.refresh_owner is not None
                        or terminal_record.refresh_lease_until is not None
                        or terminal_record.opening_batch_id is not None
                    ):
                        raise PortfolioPaperCorruptionError(
                            "Unsafe portfolio opening failed post-write validation."
                        )
                    continue
                if (
                    execution_start <= database_now
                    and candidate_expires_at <= execution_deadline
                ):
                    row = candidate_row
                    current_record = candidate_record
                    expires_at = candidate_expires_at
                    break
            if row is None or current_record is None or expires_at is None:
                return None
            track_id = str(row["id"])
            if _stored_integer(
                row["created_sequence"],
                "track creation sequence",
            ) < 1:
                raise PortfolioPaperCorruptionError(
                    "Track creation sequence must be positive."
                )
            effective_now = _effective_track_now(current_record, database_now)
            cursor = connection.execute(
                """
                UPDATE portfolio_paper_tracks
                SET refresh_generation = refresh_generation + 1,
                    refresh_owner = ?,
                    refresh_lease_until = ?,
                    updated_at = ?
                WHERE id = ?
                  AND (
                      refresh_owner IS NULL
                      OR refresh_lease_until <= ?
                  )
                """,
                (
                    normalized_owner,
                    expires_at.isoformat(),
                    effective_now.isoformat(),
                    track_id,
                    database_now.isoformat(),
                ),
            )
            if cursor.rowcount != 1:  # pragma: no cover - BEGIN IMMEDIATE fences writers
                return None
            claimed = connection.execute(
                "SELECT * FROM portfolio_paper_tracks WHERE id = ?",
                (track_id,),
            ).fetchone()
            if claimed is None:
                raise PortfolioPaperCorruptionError(
                    "Claimed portfolio-paper track disappeared."
                )
            claimed_record = self._row_to_record(connection, claimed)
            if (
                claimed_record.refresh_owner != normalized_owner
                or claimed_record.refresh_lease_until != expires_at
                or claimed_record.updated_at != effective_now
            ):
                raise PortfolioPaperCorruptionError(
                    "Claimed portfolio-paper lease failed post-write validation."
                )
            return PortfolioPaperLease(
                track_id=track_id,
                owner=normalized_owner,
                generation=claimed_record.refresh_generation,
                state_revision=claimed_record.state_revision,
                expires_at=expires_at,
            )

    def claim_due_settlement(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
    ) -> PortfolioPaperLease | None:
        """Claim one mature opened track for certified close valuation.

        The caller timestamp is validated only as an aware API input. Settlement
        maturity and expired-lease takeover are decided exclusively from SQLite's
        authoritative UTC clock.
        """

        normalized_owner = _normalize_owner(owner)
        _require_utc(now, "Settlement lease claim time")
        if lease_for <= timedelta(0) or lease_for > timedelta(hours=1):
            raise ValueError("Lease duration must be positive and at most one hour.")
        with self._write_transaction() as connection:
            database_now = _database_now(connection)
            self._validate_all_tracks(connection)
            candidate_rows = connection.execute(
                """
                SELECT * FROM portfolio_paper_tracks
                WHERE status = 'internal_only'
                  AND state_revision = 0
                  AND EXISTS (
                      SELECT 1
                      FROM portfolio_paper_execution_batches AS opening
                      WHERE opening.track_id = portfolio_paper_tracks.id
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM portfolio_paper_close_settlements AS settlement
                      WHERE settlement.track_id = portfolio_paper_tracks.id
                  )
                  AND (
                      refresh_owner IS NULL
                      OR refresh_lease_until <= ?
                  )
                ORDER BY created_at, created_sequence
                """,
                (database_now.isoformat(),),
            ).fetchall()
            current_record: PortfolioPaperTrackRecord | None = None
            expires_at: datetime | None = None
            for candidate_row in candidate_rows:
                candidate_record = self._row_to_record(
                    connection,
                    candidate_row,
                )
                if (
                    candidate_record.state_revision != 0
                    or candidate_record.opening_session is None
                    or candidate_record.opening_batch_id is None
                    or candidate_record.settlement_id is not None
                ):
                    raise PortfolioPaperCorruptionError(
                        "Settlement candidate does not match the opened revision-zero state."
                    )
                execution_session = _stored_datetime(
                    candidate_record.opening_session,
                    "settlement execution session",
                )
                maturity = execution_session + timedelta(days=1, minutes=2)
                if database_now < maturity:
                    continue
                effective_now = _effective_track_now(
                    candidate_record,
                    database_now,
                )
                current_record = candidate_record
                expires_at = effective_now + lease_for
                break
            if current_record is None or expires_at is None:
                return None
            cursor = connection.execute(
                """
                UPDATE portfolio_paper_tracks
                SET refresh_generation = refresh_generation + 1,
                    refresh_owner = ?,
                    refresh_lease_until = ?,
                    updated_at = ?
                WHERE id = ?
                  AND state_revision = 0
                  AND EXISTS (
                      SELECT 1
                      FROM portfolio_paper_execution_batches AS opening
                      WHERE opening.track_id = portfolio_paper_tracks.id
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM portfolio_paper_close_settlements AS settlement
                      WHERE settlement.track_id = portfolio_paper_tracks.id
                  )
                  AND (
                      refresh_owner IS NULL
                      OR refresh_lease_until <= ?
                  )
                """,
                (
                    normalized_owner,
                    expires_at.isoformat(),
                    _effective_track_now(
                        current_record,
                        database_now,
                    ).isoformat(),
                    current_record.id,
                    database_now.isoformat(),
                ),
            )
            if cursor.rowcount != 1:  # pragma: no cover - BEGIN IMMEDIATE fences writers
                return None
            claimed = connection.execute(
                "SELECT * FROM portfolio_paper_tracks WHERE id = ?",
                (current_record.id,),
            ).fetchone()
            if claimed is None:
                raise PortfolioPaperCorruptionError(
                    "Claimed settlement track disappeared."
                )
            claimed_record = self._row_to_record(connection, claimed)
            effective_now = _effective_track_now(
                current_record,
                database_now,
            )
            if (
                claimed_record.state_revision != 0
                or claimed_record.opening_batch_id is None
                or claimed_record.settlement_id is not None
                or claimed_record.refresh_owner != normalized_owner
                or claimed_record.refresh_lease_until != expires_at
                or claimed_record.updated_at != effective_now
            ):
                raise PortfolioPaperCorruptionError(
                    "Claimed settlement lease failed post-write validation."
                )
            return PortfolioPaperLease(
                track_id=current_record.id,
                owner=normalized_owner,
                generation=claimed_record.refresh_generation,
                state_revision=claimed_record.state_revision,
                expires_at=expires_at,
            )

    def release_claim(
        self,
        *,
        track_id: str,
        owner: str,
        generation: int,
        now: datetime,
    ) -> bool:
        normalized_owner = _normalize_owner(owner)
        _require_utc(now, "Lease release time")
        with self._write_transaction() as connection:
            database_now = _database_now(connection)
            row = connection.execute(
                "SELECT * FROM portfolio_paper_tracks WHERE id = ?",
                (track_id,),
            ).fetchone()
            if row is None:
                return False
            current_record = self._row_to_record(connection, row)
            effective_now = _effective_track_now(current_record, database_now)
            cursor = connection.execute(
                """
                UPDATE portfolio_paper_tracks
                SET refresh_owner = NULL,
                    refresh_lease_until = NULL,
                    updated_at = ?
                WHERE id = ?
                  AND refresh_owner = ?
                  AND refresh_generation = ?
                  AND refresh_lease_until > ?
                """,
                (
                    effective_now.isoformat(),
                    track_id,
                    normalized_owner,
                    generation,
                    database_now.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                return False
            released = connection.execute(
                "SELECT * FROM portfolio_paper_tracks WHERE id = ?",
                (track_id,),
            ).fetchone()
            if released is None:
                raise PortfolioPaperCorruptionError(
                    "Released portfolio-paper track disappeared."
                )
            released_record = self._row_to_record(connection, released)
            if (
                released_record.refresh_owner is not None
                or released_record.refresh_lease_until is not None
                or released_record.updated_at != effective_now
            ):
                raise PortfolioPaperCorruptionError(
                    "Released portfolio-paper lease failed post-write validation."
                )
            return True

    def set_error_if_current(
        self,
        *,
        track_id: str,
        owner: str,
        generation: int,
        expected_revision: int,
        message: str,
        checked_at: datetime,
    ) -> bool:
        """Record an error only for the still-current fenced state, then release it."""

        _require_utc(checked_at, "Error check time")
        normalized_owner = _normalize_owner(owner)
        normalized_message = message.strip()[:1_000]
        if not normalized_message:
            raise ValueError("Portfolio-paper error message must not be empty.")
        with self._write_transaction() as connection:
            database_now = _database_now(connection)
            row = connection.execute(
                "SELECT * FROM portfolio_paper_tracks WHERE id = ?",
                (track_id,),
            ).fetchone()
            if row is None:
                return False
            current_record = self._row_to_record(connection, row)
            effective_now = _effective_track_now(current_record, database_now)
            cursor = connection.execute(
                """
                UPDATE portfolio_paper_tracks
                SET last_error = ?,
                    last_checked_at = ?,
                    updated_at = ?,
                    refresh_owner = NULL,
                    refresh_lease_until = NULL
                WHERE id = ?
                  AND state_revision = ?
                  AND refresh_owner = ?
                  AND refresh_generation = ?
                  AND refresh_lease_until > ?
                """,
                (
                    normalized_message,
                    effective_now.isoformat(),
                    effective_now.isoformat(),
                    track_id,
                    expected_revision,
                    normalized_owner,
                    generation,
                    database_now.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                return False
            checked = connection.execute(
                "SELECT * FROM portfolio_paper_tracks WHERE id = ?",
                (track_id,),
            ).fetchone()
            if checked is None:
                raise PortfolioPaperCorruptionError(
                    "Checked portfolio-paper track disappeared."
                )
            checked_record = self._row_to_record(connection, checked)
            if (
                checked_record.refresh_owner is not None
                or checked_record.refresh_lease_until is not None
                or checked_record.last_error != normalized_message
                or checked_record.last_checked_at != effective_now
                or checked_record.updated_at != effective_now
            ):
                raise PortfolioPaperCorruptionError(
                    "Portfolio-paper error update failed post-write validation."
                )
            return True

    def _row_to_record(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> PortfolioPaperTrackRecord:
        try:
            track_id = str(row["id"])
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM portfolio_paper_decisions
                     WHERE track_id = ?) AS decisions,
                    (SELECT COUNT(*) FROM portfolio_paper_advances
                     WHERE track_id = ?) AS advances,
                    (SELECT COUNT(*) FROM portfolio_paper_executions
                     WHERE track_id = ?) AS executions,
                    (SELECT COUNT(*) FROM portfolio_paper_execution_batches
                     WHERE track_id = ?) AS opening_batches,
                    (SELECT COUNT(*) FROM portfolio_paper_modeled_fills
                     WHERE track_id = ?) AS modeled_fills,
                    (SELECT COUNT(*) FROM portfolio_paper_close_settlements
                     WHERE track_id = ?) AS close_settlements
                """,
                (track_id, track_id, track_id, track_id, track_id, track_id),
            ).fetchone()
            if (
                counts is None
                or _stored_integer(counts["decisions"], "decision count") != 1
                or _stored_integer(counts["advances"], "advance count") != 0
                or _stored_integer(counts["executions"], "execution count") != 0
                or _stored_integer(
                    counts["opening_batches"],
                    "opening batch count",
                )
                not in {0, 1}
                or _stored_integer(
                    counts["close_settlements"],
                    "close settlement count",
                )
                not in {0, 1}
            ):
                raise PortfolioPaperCorruptionError(
                    "Internal-only portfolio-paper tracks require exactly one "
                    "decision, at most one opening and close settlement, and no "
                    "legacy advances or executions."
                )
            config = _load_canonical_model(
                str(row["config_payload"]),
                PortfolioPaperConfig,
                "configuration",
            )
            state = _load_canonical_model(
                str(row["state_payload"]),
                PortfolioForwardState,
                "state",
            )
            if (
                _stored_integer(
                    row["config_schema_version"],
                    "configuration schema version",
                )
                != config.schema_version
            ):
                raise PortfolioPaperCorruptionError(
                    "Configuration schema column does not match its payload."
                )
            if str(row["configuration_hash"]) != _canonical_hash(config):
                raise PortfolioPaperCorruptionError(
                    "Configuration hash does not match its payload."
                )
            if (
                _stored_integer(
                    row["state_schema_version"],
                    "state schema version",
                )
                != state.schema_version
            ):
                raise PortfolioPaperCorruptionError(
                    "State schema column does not match its payload."
                )
            if str(row["state_hash"]) != _canonical_hash(state):
                raise PortfolioPaperCorruptionError(
                    "State hash does not match its payload."
                )
            row_session = (
                str(row["state_session"]) if row["state_session"] is not None else None
            )
            if row_session != state.session:
                raise PortfolioPaperCorruptionError(
                    "State session column does not match its payload."
                )
            if str(row["portfolio_experiment_id"]) != config.portfolio_experiment_id:
                raise PortfolioPaperCorruptionError(
                    "Experiment id column does not match the frozen configuration."
                )
            pending_decision = self._load_pending_decision(
                connection,
                track_id,
                state.pending_target,
            )
            activation_state = _rebuild_activation_state(
                config,
                pending_decision.target,
            )
            opening_batch, opening_committed_at = self._load_opening_batch(
                connection,
                track_id,
                config,
                activation_state,
                pending_decision,
                _stored_integer(
                    row["refresh_generation"],
                    "refresh generation",
                ),
            )
            modeled_fill_count = _stored_integer(
                counts["modeled_fills"],
                "modeled fill count",
            )
            if modeled_fill_count != (
                len(opening_batch.fills) if opening_batch is not None else 0
            ):
                raise PortfolioPaperCorruptionError(
                    "Modeled-fill row count does not match the opening batch."
                )
            settlement, settlement_committed_at = self._load_close_settlement(
                connection,
                track_id,
                config,
                state,
                activation_state,
                pending_decision,
                opening_batch,
                _stored_integer(
                    row["refresh_generation"],
                    "refresh generation",
                ),
            )
            if _stored_integer(
                counts["close_settlements"],
                "close settlement count",
            ) != (1 if settlement is not None else 0):
                raise PortfolioPaperCorruptionError(
                    "Close-settlement row count does not match the validated state."
                )
            record = PortfolioPaperTrackRecord.model_validate(
                {
                    "id": track_id,
                    "tracking_mode": str(row["tracking_mode"]),
                    "status": str(row["status"]),
                    "config": config,
                    "configuration_hash": str(row["configuration_hash"]),
                    "state_revision": _stored_integer(
                        row["state_revision"],
                        "state revision",
                    ),
                    "state": state,
                    "pending_decision": pending_decision,
                    "opening_batch_id": (
                        opening_batch.command.idempotency_key
                        if opening_batch is not None
                        else None
                    ),
                    "opening_session": (
                        opening_batch.command.execution_session
                        if opening_batch is not None
                        else None
                    ),
                    "opening_committed_at": opening_committed_at,
                    "settlement_id": (
                        settlement.command.idempotency_key
                        if settlement is not None
                        else None
                    ),
                    "settlement_session": (
                        settlement.command.execution_session
                        if settlement is not None
                        else None
                    ),
                    "settlement_committed_at": settlement_committed_at,
                    "refresh_generation": _stored_integer(
                        row["refresh_generation"],
                        "refresh generation",
                    ),
                    "refresh_owner": (
                        str(row["refresh_owner"])
                        if row["refresh_owner"] is not None
                        else None
                    ),
                    "refresh_lease_until": _optional_datetime(
                        row["refresh_lease_until"]
                    ),
                    "created_at": _stored_datetime(row["created_at"], "created_at"),
                    "updated_at": _stored_datetime(row["updated_at"], "updated_at"),
                    "last_checked_at": _optional_datetime(row["last_checked_at"]),
                    "last_error": (
                        str(row["last_error"])
                        if row["last_error"] is not None
                        else None
                    ),
                }
            )
            return record
        except PortfolioPaperCorruptionError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise PortfolioPaperCorruptionError(
                "Persisted portfolio-paper track failed strict validation."
            ) from error

    def _validate_all_tracks(self, connection: sqlite3.Connection) -> None:
        for row in connection.execute(
            "SELECT * FROM portfolio_paper_tracks ORDER BY created_sequence"
        ).fetchall():
            self._row_to_record(connection, row)

    @staticmethod
    def _load_pending_decision(
        connection: sqlite3.Connection,
        track_id: str,
        expected_target: PortfolioForwardTarget | None,
    ) -> PortfolioPaperDecision:
        rows = connection.execute(
            "SELECT * FROM portfolio_paper_decisions WHERE track_id = ?",
            (track_id,),
        ).fetchall()
        if len(rows) != 1:
            raise PortfolioPaperCorruptionError(
                "Track must retain exactly one durable activation decision."
            )
        row = rows[0]
        target = _load_canonical_model(
            str(row["target_payload"]),
            PortfolioForwardTarget,
            "decision target",
        )
        certificate = _load_canonical_model(
            str(row["market_input_payload"]),
            PortfolioPaperDecisionCertificate,
            "decision certificate",
        )
        if (
            _stored_integer(
                row["target_schema_version"],
                "target schema version",
            )
            != target.schema_version
        ):
            raise PortfolioPaperCorruptionError(
                "Decision target schema column does not match its payload."
            )
        target_hash = str(row["target_hash"])
        certificate_hash = str(row["market_input_hash"])
        if target_hash != _canonical_hash(target):
            raise PortfolioPaperCorruptionError(
                "Decision target hash does not match its payload."
            )
        if certificate_hash != _canonical_hash(certificate):
            raise PortfolioPaperCorruptionError(
                "Decision certificate hash does not match its payload."
            )
        if str(row["information_session"]) != target.information_session:
            raise PortfolioPaperCorruptionError(
                "Decision session column does not match its target."
            )
        if expected_target is not None and target != expected_target:
            raise PortfolioPaperCorruptionError(
                "Current pending target does not match the activation decision."
            )
        observed_at = _stored_datetime(row["observed_at"], "observed_at")
        decided_at = _stored_datetime(row["decided_at"], "decided_at")
        persisted_at = _stored_datetime(row["persisted_at"], "persisted_at")
        expected_observed_at = max(bar.observed_at for bar in certificate.bars)
        if observed_at != expected_observed_at:
            raise PortfolioPaperCorruptionError(
                "Decision observation column does not match its certificate."
            )
        if decided_at != certificate.decided_at:
            raise PortfolioPaperCorruptionError(
                "Decision timestamp column does not match its certificate."
            )
        return PortfolioPaperDecision(
            id=str(row["id"]),
            track_id=str(row["track_id"]),
            target=target,
            target_hash=target_hash,
            certificate=certificate,
            certificate_hash=certificate_hash,
            persisted_at=persisted_at,
        )

    @staticmethod
    def _load_opening_batch(
        connection: sqlite3.Connection,
        track_id: str,
        config: PortfolioPaperConfig,
        state: PortfolioForwardState,
        pending_decision: PortfolioPaperDecision,
        refresh_generation: int,
    ) -> tuple[PortfolioPaperExecutionBatch | None, datetime | None]:
        from .portfolio_paper_execution import (
            PortfolioPaperExecutionBatch,
            PortfolioPaperExecutionCommand,
            PortfolioPaperModeledFill,
        )

        rows = connection.execute(
            """
            SELECT * FROM portfolio_paper_execution_batches
            WHERE track_id = ?
            """,
            (track_id,),
        ).fetchall()
        if not rows:
            stray_fill = connection.execute(
                "SELECT 1 FROM portfolio_paper_modeled_fills "
                "WHERE track_id = ? LIMIT 1",
                (track_id,),
            ).fetchone()
            if stray_fill is not None:
                raise PortfolioPaperCorruptionError(
                    "Modeled fills exist without an opening batch."
                )
            return None, None
        if len(rows) != 1:
            raise PortfolioPaperCorruptionError(
                "A portfolio-paper track has more than one opening batch."
            )
        row = rows[0]
        batch = _load_canonical_model(
            str(row["batch_payload"]),
            PortfolioPaperExecutionBatch,
            "opening batch",
        )
        command = _load_canonical_model(
            str(row["command_payload"]),
            PortfolioPaperExecutionCommand,
            "opening command",
        )
        if command != batch.command:
            raise PortfolioPaperCorruptionError(
                "Opening command payload does not match its batch."
            )
        expected_command_identity = (
            str(row["id"]),
            str(row["track_id"]),
            str(row["decision_id"]),
            str(row["execution_session"]),
            str(row["idempotency_key"]),
        )
        actual_command_identity = (
            command.idempotency_key,
            command.track_id,
            command.decision_id,
            command.execution_session,
            command.idempotency_key,
        )
        if expected_command_identity != actual_command_identity:
            raise PortfolioPaperCorruptionError(
                "Opening batch identity columns do not match its command."
            )
        fence_generation = _stored_integer(
            row["fence_generation"],
            "opening fence generation",
        )
        if (
            _stored_integer(
                row["expected_revision"],
                "opening expected revision",
            )
            != command.state_revision
            or fence_generation < 1
            or fence_generation > refresh_generation
            or _stored_integer(
                row["command_schema_version"],
                "opening command schema version",
            )
            != command.schema_version
            or _stored_integer(
                row["batch_schema_version"],
                "opening batch schema version",
            )
            != batch.schema_version
            or str(row["command_hash"]) != command.command_hash
            or str(row["batch_hash"]) != batch.batch_hash
            or str(row["quote_set_hash"]) != batch.quote_set_hash
            or str(row["fill_set_hash"]) != batch.fill_set_hash
        ):
            raise PortfolioPaperCorruptionError(
                "Opening batch audit columns do not match its payload."
            )
        accepted_at = _stored_datetime(row["accepted_at"], "opening accepted_at")
        committed_at = _stored_datetime(row["committed_at"], "opening committed_at")
        execution_start = _stored_datetime(
            pending_decision.certificate.execution_session,
            "certified execution session",
        )
        if (
            accepted_at != command.accepted_at
            or accepted_at < pending_decision.persisted_at
            or accepted_at < execution_start
            or committed_at != accepted_at
            or committed_at > pending_decision.certificate.execution_deadline
        ):
            raise PortfolioPaperCorruptionError(
                "Opening batch audit timestamps do not match its command."
            )
        certificate = pending_decision.certificate
        if (
            command.track_id != track_id
            or command.decision_id != pending_decision.id
            or command.execution_session != certificate.execution_session
            or command.state_revision != state.valuation_count
            or command.configuration_hash != _canonical_hash(config)
            or command.basket_identity_hash != config.basket_identity.identity_hash
            or command.certified_basket_hash != certificate.basket_hash
            or command.certificate_hash != pending_decision.certificate_hash
            or command.target_hash != _canonical_hash(pending_decision.target)
            or command.symbols != config.symbols
            or command.target_weights
            != {
                symbol: _exact_decimal(pending_decision.target.weights[symbol])
                for symbol in config.symbols
            }
            or command.available_cash != _exact_decimal(state.cash)
            or command.fee_rate != _exact_decimal(config.fee_rate)
            or command.slippage_rate != _exact_decimal(config.slippage_rate)
        ):
            raise PortfolioPaperCorruptionError(
                "Opening batch command does not match the durable track identity."
            )
        fill_rows = connection.execute(
            """
            SELECT * FROM portfolio_paper_modeled_fills
            WHERE track_id = ? AND batch_id = ?
            ORDER BY symbol
            """,
            (track_id, command.idempotency_key),
        ).fetchall()
        if len(fill_rows) != len(batch.fills):
            raise PortfolioPaperCorruptionError(
                "Opening batch does not have its complete modeled-fill set."
            )
        expected_fills = {fill.symbol: fill for fill in batch.fills}
        if len(expected_fills) != len(batch.fills):
            raise PortfolioPaperCorruptionError(
                "Opening batch payload repeats a modeled-fill symbol."
            )
        loaded_symbols: set[str] = set()
        for fill_row in fill_rows:
            fill = _load_canonical_model(
                str(fill_row["fill_payload"]),
                PortfolioPaperModeledFill,
                "modeled fill",
            )
            symbol = str(fill_row["symbol"])
            if symbol in loaded_symbols or expected_fills.get(symbol) != fill:
                raise PortfolioPaperCorruptionError(
                    "Modeled-fill rows do not exactly match the opening batch."
                )
            loaded_symbols.add(symbol)
            if (
                str(fill_row["batch_id"]) != command.idempotency_key
                or str(fill_row["track_id"]) != track_id
                or str(fill_row["decision_id"]) != pending_decision.id
                or str(fill_row["execution_session"])
                != command.execution_session
                or _stored_integer(
                    fill_row["fill_schema_version"],
                    "modeled fill schema version",
                )
                != fill.schema_version
                or str(fill_row["fill_hash"]) != fill.fill_hash
            ):
                raise PortfolioPaperCorruptionError(
                    "Modeled-fill audit columns do not match their payload."
                )
        if loaded_symbols != set(expected_fills):
            raise PortfolioPaperCorruptionError(
                "Opening batch modeled-fill set is incomplete."
            )
        return batch, committed_at

    @staticmethod
    def _load_close_settlement(
        connection: sqlite3.Connection,
        track_id: str,
        config: PortfolioPaperConfig,
        state: PortfolioForwardState,
        activation_state: PortfolioForwardState,
        activation_decision: PortfolioPaperDecision,
        opening_batch: PortfolioPaperExecutionBatch | None,
        refresh_generation: int,
    ) -> tuple[PortfolioPaperModeledCloseSettlement | None, datetime | None]:
        from .portfolio_paper_settlement import (
            PortfolioPaperCloseBarSet,
            PortfolioPaperCloseValuationCommand,
            PortfolioPaperModeledCloseAccount,
            PortfolioPaperModeledCloseSettlement,
            build_portfolio_paper_close_valuation,
        )

        rows = connection.execute(
            "SELECT * FROM portfolio_paper_close_settlements WHERE track_id = ?",
            (track_id,),
        ).fetchall()
        if not rows:
            if state != activation_state or state.valuation_count != 0:
                raise PortfolioPaperCorruptionError(
                    "An unsettled track must retain its exact activation state."
                )
            return None, None
        if len(rows) != 1:
            raise PortfolioPaperCorruptionError(
                "A portfolio-paper track has more than one close settlement."
            )
        if opening_batch is None:
            raise PortfolioPaperCorruptionError(
                "A close settlement cannot exist without its opening batch."
            )
        row = rows[0]
        settlement = _load_canonical_model(
            str(row["settlement_payload"]),
            PortfolioPaperModeledCloseSettlement,
            "close settlement",
        )
        command = _load_canonical_model(
            str(row["command_payload"]),
            PortfolioPaperCloseValuationCommand,
            "close settlement command",
        )
        bar_set = _load_canonical_model(
            str(row["bar_set_payload"]),
            PortfolioPaperCloseBarSet,
            "close settlement bar set",
        )
        account = _load_canonical_model(
            str(row["account_payload"]),
            PortfolioPaperModeledCloseAccount,
            "close settlement account",
        )
        forward_state = _load_canonical_model(
            str(row["forward_state_payload"]),
            PortfolioForwardState,
            "close settlement forward state",
        )
        if (
            command != settlement.command
            or bar_set != settlement.bar_set
            or account != settlement.account
            or forward_state != settlement.forward_state
        ):
            raise PortfolioPaperCorruptionError(
                "Close settlement component payloads do not match their envelope."
            )
        expected_identity = (
            command.idempotency_key,
            command.track_id,
            command.decision_id,
            command.opening_batch_id,
            command.execution_session,
            command.idempotency_key,
        )
        stored_identity = (
            str(row["id"]),
            str(row["track_id"]),
            str(row["decision_id"]),
            str(row["opening_batch_id"]),
            str(row["execution_session"]),
            str(row["idempotency_key"]),
        )
        if stored_identity != expected_identity:
            raise PortfolioPaperCorruptionError(
                "Close settlement identity columns do not match its command."
            )
        if (
            _stored_integer(row["source_revision"], "settlement source revision")
            != command.source_state_revision
            or _stored_integer(row["target_revision"], "settlement target revision")
            != command.target_state_revision
            or _stored_integer(
                row["fence_generation"],
                "settlement fence generation",
            )
            != refresh_generation
            or _stored_integer(
                row["command_schema_version"],
                "settlement command schema version",
            )
            != command.schema_version
            or _stored_integer(
                row["bar_set_schema_version"],
                "settlement bar-set schema version",
            )
            != bar_set.schema_version
            or _stored_integer(
                row["account_schema_version"],
                "settlement account schema version",
            )
            != account.schema_version
            or _stored_integer(
                row["forward_state_schema_version"],
                "settlement forward-state schema version",
            )
            != forward_state.schema_version
            or _stored_integer(
                row["settlement_schema_version"],
                "settlement schema version",
            )
            != settlement.schema_version
            or str(row["command_hash"]) != command.command_hash
            or str(row["bar_revision_set_hash"])
            != bar_set.revision_set_hash
            or str(row["bar_set_hash"]) != bar_set.bar_set_hash
            or str(row["account_hash"]) != account.account_hash
            or str(row["forward_state_hash"]) != settlement.forward_state_hash
            or str(row["settlement_hash"]) != settlement.settlement_hash
        ):
            raise PortfolioPaperCorruptionError(
                "Close settlement audit columns do not match its payload."
            )
        accepted_at = _stored_datetime(
            row["accepted_at"],
            "close settlement accepted_at",
        )
        settled_at = _stored_datetime(
            row["settled_at"],
            "close settlement settled_at",
        )
        committed_at = _stored_datetime(
            row["committed_at"],
            "close settlement committed_at",
        )
        if (
            accepted_at != bar_set.accepted_at
            or settled_at != settlement.settled_at
            or committed_at != settled_at
        ):
            raise PortfolioPaperCorruptionError(
                "Close settlement audit timestamps do not match its payload."
            )
        opening_command = opening_batch.command
        if (
            command.track_id != track_id
            or command.decision_id != activation_decision.id
            or command.opening_batch_id != opening_command.idempotency_key
            or command.opening_batch_hash != opening_batch.batch_hash
            or command.execution_session != opening_command.execution_session
            or command.source_state_revision != 0
            or command.target_state_revision != 1
            or command.source_state_hash != _canonical_hash(activation_state)
            or command.configuration_hash != _canonical_hash(config)
            or command.target_hash != activation_decision.target_hash
            or command.symbols != config.symbols
            or command.initial_cash != opening_command.available_cash
            or command.opening_ending_cash != opening_batch.ending_cash
            or command.opening_raw_notional
            != opening_batch.total_raw_notional
            or command.opening_fee != opening_batch.total_fee
            or command.opening_slippage != opening_batch.total_slippage
            or command.opening_cash_debit != opening_batch.total_cash_debit
            or command.fee_rate != opening_command.fee_rate
            or command.slippage_rate != opening_command.slippage_rate
            or command.target_weights != opening_command.target_weights
        ):
            raise PortfolioPaperCorruptionError(
                "Close settlement is not bound to its durable activation and opening."
            )
        candidate = build_portfolio_paper_close_valuation(
            opening_batch=opening_batch,
            bars=bar_set.bars,
            initial_state=activation_state,
            accepted_at=accepted_at,
            settled_at=settled_at,
        )
        if candidate != settlement:
            raise PortfolioPaperCorruptionError(
                "Close settlement cannot be rebuilt from its opening and close bars."
            )
        if any(
            fill.symbol != position.symbol
            or fill.quantity != position.quantity
            for fill, position in zip(
                opening_batch.fills,
                account.positions,
                strict=True,
            )
        ):
            raise PortfolioPaperCorruptionError(
                "Close settlement position quantities do not match opening fills."
            )
        if state != settlement.forward_state:
            raise PortfolioPaperCorruptionError(
                "Current track state does not match its close settlement."
            )
        return settlement, committed_at


def _rebuild_activation_state(
    config: PortfolioPaperConfig,
    target: PortfolioForwardTarget,
) -> PortfolioForwardState:
    target_payload = target.model_dump(mode="python")
    target_payload["weights"] = {
        symbol: target.weights[symbol] for symbol in config.symbols
    }
    ordered_target = PortfolioForwardTarget.model_validate(target_payload)
    state = initialize_portfolio_forward_state(
        list(config.symbols),
        config.initial_cash,
        config.method,
        fee_rate=config.fee_rate,
        slippage_rate=config.slippage_rate,
        volatility_lookback=config.volatility_lookback,
        maximum_asset_weight=config.maximum_asset_weight,
        session=ordered_target.information_session,
        pending_target=ordered_target,
    )
    _validate_activation_state(state, config, ordered_target)
    return state


def _validate_activation_state(
    state: PortfolioForwardState,
    config: PortfolioPaperConfig,
    target: PortfolioForwardTarget,
) -> None:
    _validate_state_against_config(state, config)
    if (
        state.valuation_count != 0
        or state.rebalance_count != 0
        or state.cash != config.initial_cash
        or state.equity != config.initial_cash
        or state.total_return != 0
        or state.max_drawdown != 0
        or state.total_cost != 0
        or any(state.shares.values())
        or any(state.target_weights.values())
        or any(state.realized_weights.values())
        or state.last_prices
    ):
        raise ValueError("Portfolio-paper activation must be a full-cash zero state.")
    if state.pending_target != target:
        raise ValueError(
            "Portfolio-paper activation state must persist its pending target."
        )


def _validate_minimum_activation_budget(
    config: PortfolioPaperConfig,
    certificate: PortfolioPaperDecisionCertificate,
) -> None:
    latest_closes = {
        bar.symbol: bar.close
        for bar in certificate.bars
        if bar.session == certificate.target.information_session
    }
    cost_multiplier = (
        Fraction(1)
        + Fraction(_exact_decimal(config.slippage_rate))
        + Fraction(_exact_decimal(config.fee_rate))
    )
    initial_cash = Fraction(_exact_decimal(config.initial_cash))
    for contract in certificate.basket.instruments:
        weight = certificate.target.weights[contract.symbol]
        if weight <= 0:
            raise ValueError(
                "Activation opening requires a positive target weight for every symbol."
            )
        close = Fraction(_exact_decimal(latest_closes[contract.symbol]))
        common_step = _common_decimal_step(
            contract.rules.lot_step_size,
            contract.rules.market_step_size,
        )
        common_step_fraction = Fraction(common_step)
        raw_minimum_quantity = Fraction(
            max(
                contract.rules.lot_min_quantity,
                contract.rules.market_min_quantity,
            )
        )
        minimum_steps = -(
            -raw_minimum_quantity.numerator * common_step_fraction.denominator
            // (
                raw_minimum_quantity.denominator
                * common_step_fraction.numerator
            )
        )
        minimum_quantity = minimum_steps * common_step_fraction
        market_min_notional = (
            Fraction(contract.rules.min_notional)
            if contract.rules.min_notional_applies_to_market
            else Fraction(0)
        )
        minimum_exchange_notional = max(
            market_min_notional,
            minimum_quantity * close,
        )
        if (
            initial_cash * Fraction(_exact_decimal(weight))
            < minimum_exchange_notional * cost_multiplier
        ):
            raise ValueError(
                "Initial cash cannot fund every all-or-nothing Binance market order."
            )


def _validate_state_against_config(
    state: PortfolioForwardState,
    config: PortfolioPaperConfig,
) -> None:
    if (
        state.symbols != config.symbols
        or state.method != config.method
        or state.initial_cash != config.initial_cash
        or state.fee_rate != config.fee_rate
        or state.slippage_rate != config.slippage_rate
        or state.volatility_lookback != config.volatility_lookback
        or state.maximum_asset_weight != config.maximum_asset_weight
    ):
        raise ValueError(
            "Portfolio-forward state does not match its frozen paper configuration."
        )


def _canonical_json(value: object) -> str:
    ready: object = (
        value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    )
    try:
        return json.dumps(
            ready,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Payload must contain strict finite JSON values.") from error


def _exact_decimal(value: Decimal | float | int) -> Decimal:
    """Preserve decimal rule boundaries when bridging float-based engine state."""

    return value if isinstance(value, Decimal) else Decimal(str(value))


def _common_decimal_step(first: Decimal, second: Decimal) -> Decimal:
    """Return the smallest exact quantity step satisfying both Binance filters."""

    if not all(value.is_finite() and value > 0 for value in (first, second)):
        raise ValueError("Quantity steps must be finite positive decimals.")
    first_tuple = first.as_tuple()
    second_tuple = second.as_tuple()
    exponent = min(
        int(str(first_tuple.exponent)),
        int(str(second_tuple.exponent)),
    )

    def integer_units(value: Decimal) -> int:
        decimal_tuple = value.as_tuple()
        coefficient = int("".join(str(digit) for digit in decimal_tuple.digits))
        coefficient *= -1 if decimal_tuple.sign else 1
        decimal_exponent = int(str(decimal_tuple.exponent))
        scale = int("1" + "0" * (decimal_exponent - exponent))
        return coefficient * scale

    first_units = integer_units(first)
    second_units = integer_units(second)
    common_units = abs(first_units * second_units) // gcd(
        first_units,
        second_units,
    )
    return Decimal(
        (
            0,
            tuple(int(digit) for digit in str(common_units)),
            exponent,
        )
    )


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _load_canonical_model(
    payload: str,
    model: type[ModelT],
    label: str,
) -> ModelT:
    try:
        decoded = json.loads(payload)
        result = model.model_validate(decoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise PortfolioPaperCorruptionError(
            f"Persisted {label} is not a valid model payload."
        ) from error
    if _canonical_json(result) != payload:
        raise PortfolioPaperCorruptionError(
            f"Persisted {label} is not canonical JSON."
        )
    return result


def _require_utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware.")
    return value.astimezone(UTC)


def _normalize_owner(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Lease owner must be a string.")
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise ValueError("Lease owner must contain between 1 and 200 characters.")
    return normalized


def _effective_track_now(
    record: PortfolioPaperTrackRecord,
    database_now: datetime,
) -> datetime:
    timestamps = [database_now, record.created_at, record.updated_at]
    if record.last_checked_at is not None:
        timestamps.append(record.last_checked_at)
    return max(timestamps)


def _stored_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PortfolioPaperCorruptionError(f"Stored {label} is not an integer.")
    return value


def _stored_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise PortfolioPaperCorruptionError(f"Stored {label} is not text.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise PortfolioPaperCorruptionError(
            f"Stored {label} is not a valid timestamp."
        ) from error
    if parsed.tzinfo is None:
        raise PortfolioPaperCorruptionError(
            f"Stored {label} must be timezone-aware."
        )
    normalized = parsed.astimezone(UTC)
    if normalized.isoformat() != value:
        raise PortfolioPaperCorruptionError(
            f"Stored {label} is not canonical UTC."
        )
    return normalized


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else _stored_datetime(value, "optional timestamp")


def _database_now(connection: sqlite3.Connection) -> datetime:
    row = connection.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now') AS utc_now"
    ).fetchone()
    if row is None or row["utc_now"] is None:
        raise PortfolioPaperStoreError("SQLite did not return its authoritative clock.")
    try:
        value = datetime.fromisoformat(str(row["utc_now"]))
    except ValueError as error:  # pragma: no cover - SQLite contract boundary
        raise PortfolioPaperStoreError(
            "SQLite returned an invalid authoritative UTC timestamp."
        ) from error
    if value.tzinfo is None:  # pragma: no cover - fixed SQLite format above
        raise PortfolioPaperStoreError(
            "SQLite authoritative timestamp was not timezone-aware."
        )
    return value.astimezone(UTC)


def _opening_commit_checkpoint(
    stage: Literal["after_batch", "after_fill"],
    fill_index: int | None,
) -> None:
    """Private deterministic fault-injection seam used by atomicity tests."""

    del stage, fill_index


def _settlement_commit_checkpoint(
    stage: Literal["after_settlement", "after_state"],
) -> None:
    """Private deterministic fault-injection seam used by atomicity tests."""

    del stage


def _normalized_table_sql(
    connection: sqlite3.Connection,
    table: str,
) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None or row["sql"] is None:
        raise PortfolioPaperSchemaError(
            f"Portfolio-paper table {table} has no stored definition."
        )
    return "".join(str(row["sql"]).lower().split())


def _is_strict_table(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT strict FROM pragma_table_list WHERE schema = 'main' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None and int(row["strict"]) == 1


def _user_index_definitions(
    connection: sqlite3.Connection,
    tables: set[str],
) -> dict[str, str]:
    if not tables:
        return {}
    placeholders = ",".join("?" for _ in tables)
    rows = connection.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type = 'index' AND sql IS NOT NULL "
        f"AND tbl_name IN ({placeholders}) ORDER BY name",
        tuple(sorted(tables)),
    ).fetchall()
    return {
        str(row["name"]): "".join(str(row["sql"]).lower().split())
        for row in rows
    }


def _unique_column_sets(
    connection: sqlite3.Connection,
    table: str,
) -> set[tuple[str, ...]]:
    result: set[tuple[str, ...]] = set()
    for index_row in connection.execute(f"PRAGMA index_list({table})").fetchall():
        if int(index_row["unique"]) != 1:
            continue
        if int(index_row["partial"]) != 0:
            raise PortfolioPaperSchemaError(
                f"Portfolio-paper table {table} has a partial unique index."
            )
        index_name = str(index_row["name"])
        key_rows = [
            row
            for row in connection.execute(
                f"PRAGMA index_xinfo({index_name})"
            ).fetchall()
            if int(row["key"]) == 1
        ]
        ordered = sorted(key_rows, key=lambda row: int(row["seqno"]))
        if any(
            row["name"] is None
            or int(row["cid"]) < 0
            or str(row["coll"]).upper() != "BINARY"
            or int(row["desc"]) != 0
            for row in ordered
        ):
            raise PortfolioPaperSchemaError(
                f"Portfolio-paper table {table} has an unsupported unique index."
            )
        columns = tuple(
            str(column_row["name"])
            for column_row in ordered
        )
        result.add(columns)
    return result


def _primary_key_columns(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[str, ...]:
    rows = [
        row
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        if int(row["pk"]) > 0
    ]
    return tuple(
        str(row["name"])
        for row in sorted(rows, key=lambda item: int(item["pk"]))
    )


def _foreign_key_column_groups(
    connection: sqlite3.Connection,
    table: str,
) -> set[tuple[tuple[str, ...], str, tuple[str, ...], str]]:
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in connection.execute(f"PRAGMA foreign_key_list({table})").fetchall():
        grouped.setdefault(int(row["id"]), []).append(row)
    result: set[tuple[tuple[str, ...], str, tuple[str, ...], str]] = set()
    for rows in grouped.values():
        ordered = sorted(rows, key=lambda row: int(row["seq"]))
        result.add(
            (
                tuple(str(row["from"]) for row in ordered),
                str(ordered[0]["table"]),
                tuple(str(row["to"]) for row in ordered),
                str(ordered[0]["on_delete"]).upper(),
            )
        )
    return result
