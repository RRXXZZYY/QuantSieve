import pandas as pd
import pytest
from quantsieve_engine import SandboxError, SandboxExecutor


def test_sandbox_runs_valid_strategy() -> None:
    data = pd.DataFrame(
        {
            "open": [1, 2, 3],
            "high": [1, 2, 3],
            "low": [1, 2, 3],
            "close": [1, 2, 3],
            "volume": [1, 1, 1],
        }
    )
    signals = SandboxExecutor().execute(
        "def generate_signals(data):\n    return (data['close'] > 1).astype(float)\n",
        data,
    )

    assert signals.tolist() == [0.0, 1.0, 1.0]


def test_sandbox_rejects_filesystem_access() -> None:
    with pytest.raises(SandboxError, match="not allowed"):
        SandboxExecutor().validate(
            "def generate_signals(data):\n    open('secret.txt').read()\n    return []\n"
        )
