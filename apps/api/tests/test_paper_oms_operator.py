from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from quantsieve_api.paper_oms import (
    PaperOmsRevisionError,
    ServerOwnedPaperKillSwitch,
)
from quantsieve_api.paper_oms_operator import CLEAR_CONFIRMATION_PHRASE, main


def _payload(output: str) -> dict[str, object]:
    value = json.loads(output)
    assert isinstance(value, dict)
    return value


def test_status_and_revision_guarded_transitions_emit_sanitized_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_marker = "private-server-address"
    database = tmp_path / secret_marker / "paper-oms.db"
    authority = ServerOwnedPaperKillSwitch(database)

    assert main(["--database", str(database), "status"]) == 0
    status = _payload(capsys.readouterr().out)
    assert status["mode"] == "PAPER_SIMULATION_ONLY"
    assert status["warning"] == "PAPER SIMULATION ONLY - NO LIVE TRADING"
    assert status["status"] == "engaged"
    assert status["revision"] == 1
    assert len(str(status["state_hash"])) == 64
    assert secret_marker not in json.dumps(status)

    assert (
        main(
            [
                "--database",
                str(database),
                "clear",
                "--expected-revision",
                "1",
                "--confirm",
                CLEAR_CONFIRMATION_PHRASE,
            ]
        )
        == 0
    )
    cleared = _payload(capsys.readouterr().out)
    assert cleared["status"] == "clear"
    assert cleared["revision"] == 2
    assert cleared["activated_at"] is None

    assert (
        main(
            [
                "--database",
                str(database),
                "engage",
                "--expected-revision",
                "2",
                "--reason-code",
                "OPERATOR_HALT",
            ]
        )
        == 0
    )
    engaged = _payload(capsys.readouterr().out)
    assert engaged["status"] == "engaged"
    assert engaged["revision"] == 3
    assert authority.read().snapshot.revision == 3


def test_clear_requires_exact_confirmation_and_does_not_mutate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "paper-oms.db"
    authority = ServerOwnedPaperKillSwitch(database)

    with pytest.raises(SystemExit) as stopped:
        main(
            [
                "--database",
                str(database),
                "clear",
                "--expected-revision",
                "1",
                "--confirm",
                CLEAR_CONFIRMATION_PHRASE.lower(),
            ]
        )
    assert stopped.value.code == 2
    error = _payload(capsys.readouterr().err)
    assert error["error"] == "confirmation_mismatch"
    assert error["effective_status"] == "engaged_fail_closed"
    assert authority.read().snapshot.status == "engaged"
    assert authority.read().snapshot.revision == 1


def test_stale_revision_fails_closed_without_a_second_transition(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "paper-oms.db"
    authority = ServerOwnedPaperKillSwitch(database)
    authority.clear_if_revision(expected_revision=1)

    with pytest.raises(SystemExit) as stopped:
        main(
            [
                "--database",
                str(database),
                "engage",
                "--expected-revision",
                "1",
                "--reason-code",
                "STALE_OPERATOR",
            ]
        )
    assert stopped.value.code == 3
    error = _payload(capsys.readouterr().err)
    assert error["error"] == "revision_conflict"
    assert authority.read().snapshot.status == "clear"
    assert authority.read().snapshot.revision == 2


@pytest.mark.parametrize("failure", ["missing_table", "corrupt_hash", "missing_row"])
def test_missing_or_corrupt_authority_is_never_initialized_or_cleared(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    database = tmp_path / f"{failure}.db"
    if failure == "missing_table":
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE unrelated (value INTEGER)")
    else:
        ServerOwnedPaperKillSwitch(database)
        with sqlite3.connect(database) as connection:
            if failure == "corrupt_hash":
                connection.execute(
                    "UPDATE paper_oms_kill_switch SET state_hash = ? WHERE singleton = 1",
                    ("0" * 64,),
                )
            else:
                connection.execute("DELETE FROM paper_oms_kill_switch")

    with pytest.raises(SystemExit) as stopped:
        main(["--database", str(database), "status"])
    assert stopped.value.code == 1
    error = _payload(capsys.readouterr().err)
    assert error["effective_status"] == "engaged_fail_closed"

    if failure == "missing_table":
        with sqlite3.connect(database) as connection:
            assert (
                connection.execute(
                    """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'paper_oms_kill_switch'
                """
                ).fetchone()
                is None
            )


def test_revision_compare_and_transition_are_atomic_across_instances(
    tmp_path: Path,
) -> None:
    database = tmp_path / "paper-oms.db"
    first = ServerOwnedPaperKillSwitch(database)
    second = ServerOwnedPaperKillSwitch(database)
    barrier = Barrier(2)

    def engage(authority: ServerOwnedPaperKillSwitch, reason: str) -> str:
        barrier.wait()
        try:
            result = authority.engage_if_revision(
                reason_code=reason,
                expected_revision=1,
            )
        except PaperOmsRevisionError:
            return "conflict"
        return f"updated:{result.snapshot.revision}"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(engage, first, "FIRST"),
            executor.submit(engage, second, "SECOND"),
        ]
        results = {future.result() for future in futures}

    assert results == {"updated:2", "conflict"}
    final = first.read()
    assert final.snapshot.status == "engaged"
    assert final.snapshot.revision == 2
