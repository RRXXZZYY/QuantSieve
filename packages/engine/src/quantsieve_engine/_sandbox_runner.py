from __future__ import annotations

import json
import os
import sys
from io import StringIO
from pathlib import Path

import pandas as pd


def _limit_resources(memory_mb: int) -> None:
    if os.name == "posix":
        import resource

        memory_bytes = memory_mb * 1024 * 1024
        resource_symbols = vars(resource)
        setrlimit = resource_symbols["setrlimit"]
        setrlimit(resource_symbols["RLIMIT_AS"], (memory_bytes, memory_bytes))
        setrlimit(resource_symbols["RLIMIT_CPU"], (3, 3))


def main() -> None:
    input_path, code_path, output_path, memory = sys.argv[1:5]
    _limit_resources(int(memory))
    input_json = Path(input_path).read_text(encoding="utf-8")
    data = pd.read_json(StringIO(input_json), orient="split")
    namespace: dict[str, object] = {}
    code = Path(code_path).read_text(encoding="utf-8")
    exec(compile(code, code_path, "exec"), namespace)
    generator = namespace.get("generate_signals")
    if not callable(generator):
        raise TypeError("generate_signals(data) is missing.")
    signals = generator(data)
    if not isinstance(signals, pd.Series):
        signals = pd.Series(signals, index=data.index)
    Path(output_path).write_text(
        json.dumps({"signals": signals.fillna(0).astype(float).tolist()}),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
