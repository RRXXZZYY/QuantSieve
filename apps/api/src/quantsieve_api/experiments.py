from __future__ import annotations

import builtins
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from quantsieve_engine import RunManifest

from .schemas import (
    CrossMarketBacktestRunRecord,
    CrossMarketExperimentCreate,
    CrossMarketExperimentRecord,
    CrossMarketExperimentSummary,
    ExperimentCreate,
    ExperimentRecord,
    PortfolioBacktestRequest,
    PortfolioBacktestRunRecord,
    PortfolioExperimentCreate,
    PortfolioExperimentRecord,
    PortfolioExperimentSummary,
    StrategyRobustnessRequest,
)


class PortfolioBacktestRunExpiredError(LookupError):
    """Raised when a server-issued portfolio run can no longer be archived."""


class CrossMarketBacktestRunExpiredError(LookupError):
    """Raised when a server-issued cross-market run can no longer be archived."""


class ExperimentStore:
    """Persist compact, reproducible backtest snapshots in the shared app database."""

    def __init__(
        self,
        path: str | Path,
        *,
        portfolio_run_ttl: timedelta = timedelta(hours=24),
    ) -> None:
        if portfolio_run_ttl <= timedelta(0):
            raise ValueError("portfolio_run_ttl must be positive")
        self.path = Path(path)
        self.portfolio_run_ttl = portfolio_run_ttl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS backtest_experiments (
                    id TEXT PRIMARY KEY,
                    source_run_id TEXT,
                    manifest_id TEXT,
                    provenance_status TEXT NOT NULL DEFAULT 'legacy_unverified',
                    name TEXT NOT NULL,
                    notes TEXT,
                    symbol TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    optimized INTEGER NOT NULL,
                    validation_passed INTEGER,
                    total_return REAL NOT NULL,
                    max_drawdown REAL NOT NULL,
                    sharpe_ratio REAL NOT NULL,
                    excess_return REAL NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            experiment_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(backtest_experiments)"
                ).fetchall()
            }
            if "source_run_id" not in experiment_columns:
                connection.execute(
                    "ALTER TABLE backtest_experiments ADD COLUMN source_run_id TEXT"
                )
            if "manifest_id" not in experiment_columns:
                connection.execute(
                    "ALTER TABLE backtest_experiments ADD COLUMN manifest_id TEXT"
                )
            if "provenance_status" not in experiment_columns:
                connection.execute(
                    "ALTER TABLE backtest_experiments "
                    "ADD COLUMN provenance_status TEXT NOT NULL "
                    "DEFAULT 'legacy_unverified'"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_experiments_created "
                "ON backtest_experiments(created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_experiments_symbol "
                "ON backtest_experiments(symbol, strategy_id)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_experiments_source_run "
                "ON backtest_experiments(source_run_id) "
                "WHERE source_run_id IS NOT NULL"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_experiments_manifest "
                "ON backtest_experiments(manifest_id) "
                "WHERE manifest_id IS NOT NULL"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_experiments (
                    id TEXT PRIMARY KEY,
                    source_run_id TEXT,
                    schema_version INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    notes TEXT,
                    symbols TEXT NOT NULL,
                    asset_count INTEGER NOT NULL,
                    interval TEXT NOT NULL,
                    focus_method TEXT NOT NULL,
                    start TEXT NOT NULL,
                    end TEXT NOT NULL,
                    common_bars INTEGER NOT NULL,
                    total_return REAL NOT NULL,
                    max_drawdown REAL NOT NULL,
                    sharpe_ratio REAL NOT NULL,
                    risk_evidence_passed INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            portfolio_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(portfolio_experiments)"
                ).fetchall()
            }
            if "source_run_id" not in portfolio_columns:
                connection.execute(
                    "ALTER TABLE portfolio_experiments "
                    "ADD COLUMN source_run_id TEXT"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_portfolio_experiments_created "
                "ON portfolio_experiments(created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_portfolio_experiments_focus "
                "ON portfolio_experiments(focus_method, created_at DESC)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "idx_portfolio_experiments_source_run "
                "ON portfolio_experiments(source_run_id) "
                "WHERE source_run_id IS NOT NULL"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_backtest_runs (
                    run_id TEXT PRIMARY KEY,
                    request_payload TEXT NOT NULL,
                    result_payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_portfolio_backtest_runs_expires "
                "ON portfolio_backtest_runs(expires_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cross_market_experiments (
                    id TEXT PRIMARY KEY,
                    source_run_id TEXT NOT NULL UNIQUE,
                    schema_version INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    notes TEXT,
                    strategy_id TEXT NOT NULL,
                    symbols TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    decision_status TEXT NOT NULL,
                    validated_markets INTEGER NOT NULL,
                    rejected_markets INTEGER NOT NULL,
                    unavailable_markets INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_cross_market_experiments_created "
                "ON cross_market_experiments(created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_cross_market_experiments_strategy "
                "ON cross_market_experiments(strategy_id, created_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cross_market_backtest_runs (
                    run_id TEXT PRIMARY KEY,
                    request_payload TEXT NOT NULL,
                    result_payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_cross_market_backtest_runs_expires "
                "ON cross_market_backtest_runs(expires_at)"
            )

    def create(
        self,
        experiment: ExperimentCreate,
        *,
        source_run_id: str | None = None,
        run_manifest: RunManifest | None = None,
    ) -> ExperimentRecord:
        if (source_run_id is None) != (run_manifest is None):
            raise ValueError(
                "Server-verified experiments require both a source run and manifest."
            )
        with self._lock, self._connect() as connection:
            if source_run_id is not None:
                existing = connection.execute(
                    "SELECT payload FROM backtest_experiments "
                    "WHERE source_run_id = ?",
                    (source_run_id,),
                ).fetchone()
                if existing is not None:
                    return ExperimentRecord.model_validate(
                        json.loads(existing["payload"])
                    )

            now = datetime.now(UTC)
            record = ExperimentRecord(
                id=uuid4().hex,
                source_run_id=source_run_id,
                provenance_status=(
                    "server_verified"
                    if source_run_id is not None
                    else "legacy_unverified"
                ),
                run_manifest=run_manifest,
                created_at=now,
                updated_at=now,
                **experiment.model_dump(),
            )
            payload = record.model_dump(mode="json")
            validation_passed = (
                record.validation.validation_passed if record.validation else None
            )
            connection.execute(
                """
                INSERT INTO backtest_experiments (
                    id, source_run_id, manifest_id, provenance_status,
                    name, notes, symbol, strategy_id, interval, optimized,
                    validation_passed, total_return, max_drawdown, sharpe_ratio,
                    excess_return, payload, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.source_run_id,
                    (
                        record.run_manifest.manifest_id
                        if record.run_manifest is not None
                        else None
                    ),
                    record.provenance_status,
                    record.name,
                    record.notes,
                    record.instrument.symbol,
                    record.strategy.id,
                    record.interval,
                    int(record.optimized),
                    validation_passed,
                    record.metrics.total_return,
                    record.metrics.max_drawdown,
                    record.metrics.sharpe_ratio,
                    record.comparison.excess_return,
                    json.dumps(payload, ensure_ascii=False),
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
        return record

    def get_by_source_run_id(self, source_run_id: str) -> ExperimentRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM backtest_experiments WHERE source_run_id = ?",
                (source_run_id,),
            ).fetchone()
        if row is None:
            return None
        return ExperimentRecord.model_validate(json.loads(row["payload"]))

    def list(
        self,
        *,
        query: str = "",
        symbol: str | None = None,
        limit: int = 50,
    ) -> list[ExperimentRecord]:
        clauses: list[str] = []
        parameters: list[object] = []
        if query:
            clauses.append(
                "(name LIKE ? OR notes LIKE ? OR symbol LIKE ? OR strategy_id LIKE ?)"
            )
            pattern = f"%{query.strip()}%"
            parameters.extend([pattern, pattern, pattern, pattern])
        if symbol:
            clauses.append("symbol = ?")
            parameters.append(symbol.upper())
        sql = "SELECT payload FROM backtest_experiments"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [
            ExperimentRecord.model_validate(json.loads(row["payload"])) for row in rows
        ]

    def get(self, experiment_id: str) -> ExperimentRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM backtest_experiments WHERE id = ?",
                (experiment_id,),
            ).fetchone()
        if row is None:
            return None
        return ExperimentRecord.model_validate(json.loads(row["payload"]))

    def delete(self, experiment_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM backtest_experiments WHERE id = ?",
                (experiment_id,),
            )
            return cursor.rowcount > 0

    def create_portfolio(
        self,
        experiment: PortfolioExperimentCreate,
        *,
        source_run_id: str | None = None,
    ) -> PortfolioExperimentRecord:
        with self._lock, self._connect() as connection:
            if source_run_id is not None:
                existing = connection.execute(
                    "SELECT payload FROM portfolio_experiments "
                    "WHERE source_run_id = ?",
                    (source_run_id,),
                ).fetchone()
                if existing is not None:
                    return PortfolioExperimentRecord.model_validate(
                        json.loads(existing["payload"])
                    )

            now = datetime.now(UTC)
            record = PortfolioExperimentRecord(
                id=uuid4().hex,
                source_run_id=source_run_id,
                created_at=now,
                updated_at=now,
                **experiment.model_dump(),
            )
            focus_metrics = record.results[record.focus_method].metrics
            symbols = [asset.symbol for asset in record.assets]
            payload = record.model_dump(mode="json")
            try:
                connection.execute(
                    """
                    INSERT INTO portfolio_experiments (
                        id, source_run_id, schema_version, name, notes, symbols,
                        asset_count, interval, focus_method, start, end,
                        common_bars, total_return, max_drawdown, sharpe_ratio,
                        risk_evidence_passed, payload, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        record.source_run_id,
                        record.schema_version,
                        record.name,
                        record.notes,
                        json.dumps(
                            symbols,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        len(symbols),
                        record.interval,
                        record.focus_method,
                        record.start.isoformat(),
                        record.end.isoformat(),
                        record.common_bars,
                        focus_metrics.total_return,
                        focus_metrics.max_drawdown,
                        focus_metrics.sharpe_ratio,
                        int(record.research_decision.risk_evidence_passed),
                        json.dumps(payload, ensure_ascii=False),
                        record.created_at.isoformat(),
                        record.updated_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError:
                if source_run_id is None:
                    raise
                existing = connection.execute(
                    "SELECT payload FROM portfolio_experiments "
                    "WHERE source_run_id = ?",
                    (source_run_id,),
                ).fetchone()
                if existing is None:
                    raise
                return PortfolioExperimentRecord.model_validate(
                    json.loads(existing["payload"])
                )
        return record

    def list_portfolios(
        self,
        *,
        query: str = "",
        symbol: str | None = None,
        limit: int = 50,
    ) -> builtins.list[PortfolioExperimentSummary]:
        clauses: list[str] = []
        parameters: list[object] = []
        if query:
            clauses.append(
                "(name LIKE ? OR COALESCE(notes, '') LIKE ? "
                "OR symbols LIKE ? OR focus_method LIKE ?)"
            )
            pattern = f"%{query.strip()}%"
            parameters.extend([pattern, pattern, pattern, pattern])
        if symbol:
            clauses.append("symbols LIKE ?")
            normalized_symbol = symbol.strip().upper()
            parameters.append(f'%"{normalized_symbol}"%')
        sql = (
            "SELECT id, source_run_id, schema_version, name, notes, symbols, "
            "asset_count, interval, focus_method, start, end, common_bars, "
            "total_return, max_drawdown, sharpe_ratio, risk_evidence_passed, "
            "created_at, updated_at FROM portfolio_experiments"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [
            PortfolioExperimentSummary(
                id=row["id"],
                source_run_id=row["source_run_id"],
                schema_version=row["schema_version"],
                name=row["name"],
                notes=row["notes"],
                symbols=json.loads(row["symbols"]),
                asset_count=row["asset_count"],
                interval=row["interval"],
                focus_method=row["focus_method"],
                start=row["start"],
                end=row["end"],
                common_bars=row["common_bars"],
                total_return=row["total_return"],
                max_drawdown=row["max_drawdown"],
                sharpe_ratio=row["sharpe_ratio"],
                risk_evidence_passed=bool(row["risk_evidence_passed"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def get_portfolio(
        self,
        experiment_id: str,
    ) -> PortfolioExperimentRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM portfolio_experiments WHERE id = ?",
                (experiment_id,),
            ).fetchone()
        if row is None:
            return None
        return PortfolioExperimentRecord.model_validate(json.loads(row["payload"]))

    def get_portfolio_by_source_run_id(
        self,
        source_run_id: str,
    ) -> PortfolioExperimentRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM portfolio_experiments WHERE source_run_id = ?",
                (source_run_id,),
            ).fetchone()
        if row is None:
            return None
        return PortfolioExperimentRecord.model_validate(json.loads(row["payload"]))

    def delete_portfolio(self, experiment_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM portfolio_experiments WHERE id = ?",
                (experiment_id,),
            )
            return cursor.rowcount > 0

    def create_portfolio_run(
        self,
        run_request: PortfolioBacktestRequest,
        result_payload: dict[str, Any],
    ) -> PortfolioBacktestRunRecord:
        now = datetime.now(UTC)
        expires_at = now + self.portfolio_run_ttl
        record = PortfolioBacktestRunRecord(
            run_id=uuid4().hex,
            run_request=run_request,
            result_payload=result_payload,
            created_at=now,
            expires_at=expires_at,
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM portfolio_backtest_runs WHERE expires_at <= ?",
                (now.isoformat(),),
            )
            connection.execute(
                """
                INSERT INTO portfolio_backtest_runs (
                    run_id, request_payload, result_payload, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    json.dumps(
                        record.run_request.model_dump(mode="json"),
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        record.result_payload,
                        ensure_ascii=False,
                        default=str,
                    ),
                    record.created_at.isoformat(),
                    record.expires_at.isoformat(),
                ),
            )
        return record

    def get_portfolio_run(
        self,
        run_id: str,
    ) -> PortfolioBacktestRunRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT run_id, request_payload, result_payload, created_at, "
                "expires_at FROM portfolio_backtest_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at <= datetime.now(UTC):
            raise PortfolioBacktestRunExpiredError(
                "组合运行回执已过期，请重新运行组合回测后再保存。"
            )
        return PortfolioBacktestRunRecord(
            run_id=row["run_id"],
            run_request=PortfolioBacktestRequest.model_validate(
                json.loads(row["request_payload"])
            ),
            result_payload=json.loads(row["result_payload"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=expires_at,
        )

    def create_cross_market_run(
        self,
        run_request: StrategyRobustnessRequest,
        result_payload: dict[str, Any],
    ) -> CrossMarketBacktestRunRecord:
        now = datetime.now(UTC)
        expires_at = now + self.portfolio_run_ttl
        record = CrossMarketBacktestRunRecord(
            run_id=uuid4().hex,
            run_request=run_request,
            result_payload=result_payload,
            created_at=now,
            expires_at=expires_at,
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM cross_market_backtest_runs WHERE expires_at <= ?",
                (now.isoformat(),),
            )
            connection.execute(
                """
                INSERT INTO cross_market_backtest_runs (
                    run_id, request_payload, result_payload, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    json.dumps(record.run_request.model_dump(mode="json"), ensure_ascii=False),
                    json.dumps(record.result_payload, ensure_ascii=False, default=str),
                    record.created_at.isoformat(),
                    record.expires_at.isoformat(),
                ),
            )
        return record

    def get_cross_market_run(
        self,
        run_id: str,
    ) -> CrossMarketBacktestRunRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT run_id, request_payload, result_payload, created_at, "
                "expires_at FROM cross_market_backtest_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at <= datetime.now(UTC):
            raise CrossMarketBacktestRunExpiredError(
                "跨市场运行回执已过期，请重新运行跨市场复现后再保存。"
            )
        return CrossMarketBacktestRunRecord(
            run_id=row["run_id"],
            run_request=StrategyRobustnessRequest.model_validate(
                json.loads(row["request_payload"])
            ),
            result_payload=json.loads(row["result_payload"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=expires_at,
        )

    def create_cross_market(
        self,
        experiment: CrossMarketExperimentCreate,
        *,
        source_run_id: str,
    ) -> CrossMarketExperimentRecord:
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT payload FROM cross_market_experiments WHERE source_run_id = ?",
                (source_run_id,),
            ).fetchone()
            if existing is not None:
                return CrossMarketExperimentRecord.model_validate(
                    json.loads(existing["payload"])
                )
            now = datetime.now(UTC)
            record = CrossMarketExperimentRecord(
                id=uuid4().hex,
                source_run_id=source_run_id,
                created_at=now,
                updated_at=now,
                **experiment.model_dump(),
            )
            payload = record.model_dump(mode="json")
            symbols = [str(market["symbol"]).upper() for market in record.markets]
            decision = record.research_decision
            connection.execute(
                """
                INSERT INTO cross_market_experiments (
                    id, source_run_id, schema_version, name, notes, strategy_id,
                    symbols, interval, objective, decision_status, validated_markets,
                    rejected_markets, unavailable_markets, payload, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.source_run_id,
                    record.schema_version,
                    record.name,
                    record.notes,
                    record.strategy.id,
                    json.dumps(symbols, ensure_ascii=False, separators=(",", ":")),
                    record.interval,
                    record.objective,
                    decision["status"],
                    record.summary.get("validated", 0),
                    record.summary.get("rejected", 0),
                    record.summary.get("unavailable", 0),
                    json.dumps(payload, ensure_ascii=False),
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
        return record

    def list_cross_market(
        self,
        *,
        query: str = "",
        symbol: str | None = None,
        limit: int = 50,
    ) -> builtins.list[CrossMarketExperimentSummary]:
        clauses: list[str] = []
        parameters: list[object] = []
        if query:
            clauses.append(
                "(name LIKE ? OR COALESCE(notes, '') LIKE ? "
                "OR symbols LIKE ? OR strategy_id LIKE ?)"
            )
            pattern = f"%{query.strip()}%"
            parameters.extend([pattern, pattern, pattern, pattern])
        if symbol:
            clauses.append("symbols LIKE ?")
            parameters.append(f'%"{symbol.strip().upper()}"%')
        sql = (
            "SELECT id, source_run_id, schema_version, name, notes, strategy_id, "
            "symbols, interval, objective, decision_status, validated_markets, "
            "rejected_markets, unavailable_markets, created_at, updated_at "
            "FROM cross_market_experiments"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [
            CrossMarketExperimentSummary(
                id=row["id"],
                source_run_id=row["source_run_id"],
                schema_version=row["schema_version"],
                kind="cross_market",
                name=row["name"],
                notes=row["notes"],
                strategy_id=row["strategy_id"],
                symbols=json.loads(row["symbols"]),
                interval=row["interval"],
                objective=row["objective"],
                decision_status=row["decision_status"],
                validated_markets=row["validated_markets"],
                rejected_markets=row["rejected_markets"],
                unavailable_markets=row["unavailable_markets"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def get_cross_market(self, experiment_id: str) -> CrossMarketExperimentRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM cross_market_experiments WHERE id = ?",
                (experiment_id,),
            ).fetchone()
        if row is None:
            return None
        return CrossMarketExperimentRecord.model_validate(json.loads(row["payload"]))

    def get_cross_market_by_source_run_id(
        self, source_run_id: str
    ) -> CrossMarketExperimentRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM cross_market_experiments WHERE source_run_id = ?",
                (source_run_id,),
            ).fetchone()
        if row is None:
            return None
        return CrossMarketExperimentRecord.model_validate(json.loads(row["payload"]))

    def delete_cross_market(self, experiment_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM cross_market_experiments WHERE id = ?",
                (experiment_id,),
            )
            return cursor.rowcount > 0
