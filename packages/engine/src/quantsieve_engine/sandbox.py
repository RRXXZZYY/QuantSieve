from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

ALLOWED_IMPORTS = {"math", "numpy", "pandas"}
FORBIDDEN_CALLS = {
    "__import__",
    "breakpoint",
    "compile",
    "eval",
    "exec",
    "globals",
    "input",
    "locals",
    "open",
}


class SandboxError(RuntimeError):
    pass


class SandboxExecutor:
    """Execute a signal function in a constrained child process.

    This reduces accidental damage but is not a hardened multi-tenant security boundary.
    """

    def __init__(self, timeout_seconds: float = 15.0, memory_mb: int = 768) -> None:
        self.timeout_seconds = timeout_seconds
        self.memory_mb = memory_mb

    def validate(self, code: str) -> None:
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            raise SandboxError(f"Strategy code has invalid syntax: {exc}") from exc
        has_function = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = [
                    alias.name.split(".", 1)[0]
                    for alias in (node.names if isinstance(node, ast.Import) else [])
                ]
                if isinstance(node, ast.ImportFrom) and node.module:
                    modules.append(node.module.split(".", 1)[0])
                if any(module not in ALLOWED_IMPORTS for module in modules):
                    raise SandboxError("Only pandas, numpy, and math imports are allowed.")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in FORBIDDEN_CALLS
            ):
                raise SandboxError(f"Call to {node.func.id!r} is not allowed.")
            if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                raise SandboxError("Dunder attribute access is not allowed.")
            if isinstance(node, ast.FunctionDef) and node.name == "generate_signals":
                has_function = True
        if not has_function:
            raise SandboxError("Strategy code must define generate_signals(data).")

    def execute(self, code: str, data: pd.DataFrame) -> pd.Series:
        self.validate(code)
        runner = Path(__file__).with_name("_sandbox_runner.py")
        with tempfile.TemporaryDirectory(prefix="quantsieve-strategy-") as temp_directory:
            temp = Path(temp_directory)
            input_path = temp / "input.json"
            code_path = temp / "strategy.py"
            output_path = temp / "output.json"
            input_path.write_text(data.to_json(orient="split", date_format="iso"), encoding="utf-8")
            code_path.write_text(code, encoding="utf-8")
            command = [
                sys.executable,
                "-I",
                str(runner),
                str(input_path),
                str(code_path),
                str(output_path),
                str(self.memory_mb),
            ]
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise SandboxError("Strategy execution timed out.") from exc
            if completed.returncode != 0:
                message = completed.stderr.strip()
                if not message and completed.returncode < 0:
                    message = (
                        "Strategy worker was terminated after exceeding a resource limit."
                    )
                message = message or "Strategy execution failed."
                raise SandboxError(message[-1000:])
            payload: dict[str, Any] = json.loads(output_path.read_text(encoding="utf-8"))
            values = payload.get("signals")
            if not isinstance(values, list) or len(values) != len(data):
                raise SandboxError("generate_signals(data) must return one signal per input row.")
            return pd.Series(values, index=data.index, dtype=float)
