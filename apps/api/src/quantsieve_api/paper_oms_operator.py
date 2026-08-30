"""Local-only operator CLI for the durable Paper OMS kill switch.

This module deliberately has no HTTP integration. Run it only inside the API
container (or on the host that owns the SQLite database).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn, TextIO

from .paper_oms import (
    PaperOmsIntegrityError,
    PaperOmsKillSwitchObservation,
    PaperOmsRevisionError,
    ServerOwnedPaperKillSwitch,
)

CLEAR_CONFIRMATION_PHRASE = "CLEAR PAPER OMS KILL SWITCH - SIMULATION ONLY"
_MODE = "PAPER_SIMULATION_ONLY"
_WARNING = "PAPER SIMULATION ONLY - NO LIVE TRADING"


class _OperatorInputError(ValueError):
    """A sanitized operator-input failure safe to classify at the CLI boundary."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m quantsieve_api.paper_oms_operator",
        description="Operate the local durable Paper OMS kill switch.",
    )
    parser.add_argument(
        "--database",
        required=True,
        help="Explicit path to an existing Paper OMS SQLite database.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Read and verify the durable authority.")

    engage = subparsers.add_parser("engage", help="Engage the Paper OMS kill switch.")
    engage.add_argument("--expected-revision", required=True, type=int)
    engage.add_argument("--reason-code", required=True)

    clear = subparsers.add_parser("clear", help="Clear the Paper OMS kill switch.")
    clear.add_argument("--expected-revision", required=True, type=int)
    clear.add_argument(
        "--confirm",
        required=True,
        help=f'Exact phrase: "{CLEAR_CONFIRMATION_PHRASE}"',
    )
    return parser


def _preflight_existing_authority(database: Path) -> None:
    """Refuse to initialize an authority from an operator command."""

    if not database.is_file():
        raise _OperatorInputError("database_missing")
    try:
        connection = sqlite3.connect(database, isolation_level=None)
        try:
            table = connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'paper_oms_kill_switch'
                """
            ).fetchone()
            if table is None:
                raise _OperatorInputError("authority_missing")
            rows = connection.execute("SELECT COUNT(*) FROM paper_oms_kill_switch").fetchone()
            if rows is None or rows[0] != 1:
                raise _OperatorInputError("authority_missing_or_ambiguous")
        finally:
            connection.close()
    except _OperatorInputError:
        raise
    except (OSError, sqlite3.DatabaseError) as exc:
        raise _OperatorInputError("authority_unreadable") from exc


def _observation_payload(
    observation: PaperOmsKillSwitchObservation,
) -> dict[str, object]:
    snapshot = observation.snapshot
    return {
        "ok": True,
        "mode": _MODE,
        "warning": _WARNING,
        "status": snapshot.status,
        "revision": snapshot.revision,
        "state_hash": snapshot.state_hash,
        "activated_at": (
            snapshot.activated_at.isoformat() if snapshot.activated_at is not None else None
        ),
        "observed_at": observation.observed_at.isoformat(),
        "available_at": observation.available_at.isoformat(),
    }


def _emit(payload: dict[str, object], *, stream: TextIO | None = None) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
        file=stream if stream is not None else sys.stdout,
    )


def _fail(error: str, *, exit_code: int) -> NoReturn:
    _emit(
        {
            "ok": False,
            "mode": _MODE,
            "warning": _WARNING,
            "effective_status": "engaged_fail_closed",
            "error": error,
        },
        stream=sys.stderr,
    )
    raise SystemExit(exit_code)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database = Path(args.database)

    if args.command == "clear" and args.confirm != CLEAR_CONFIRMATION_PHRASE:
        _fail("confirmation_mismatch", exit_code=2)
    if args.command in {"engage", "clear"} and args.expected_revision < 1:
        _fail("invalid_expected_revision", exit_code=2)

    try:
        _preflight_existing_authority(database)
        authority = ServerOwnedPaperKillSwitch(database)
        if args.command == "status":
            observation = authority.read()
        elif args.command == "engage":
            observation = authority.engage_if_revision(
                reason_code=args.reason_code,
                expected_revision=args.expected_revision,
            )
        else:
            observation = authority.clear_if_revision(
                expected_revision=args.expected_revision,
            )
    except PaperOmsRevisionError:
        _fail("revision_conflict", exit_code=3)
    except (_OperatorInputError, PaperOmsIntegrityError):
        _fail("authority_unavailable_or_invalid", exit_code=1)
    except (OSError, sqlite3.DatabaseError, ValueError):
        _fail("operation_rejected", exit_code=2)

    _emit(_observation_payload(observation))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
