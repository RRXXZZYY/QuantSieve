from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

NUMBER_PATTERN = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?%?")


def _normalized_number(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace(",", "").removesuffix("%"))
    except InvalidOperation:
        return None


def enforce_grounded_numbers(text: str, tool_payloads: list[dict[str, Any]]) -> tuple[str, bool]:
    """Remove numeric claims that cannot be found in a tool response.

    This is a final guardrail in addition to the system prompt. Numeric formatting
    differences such as thousands separators are accepted, but substring matches
    are not.
    """
    evidence = json.dumps(tool_payloads, ensure_ascii=False, default=str)
    evidence_numbers = {
        value
        for token in NUMBER_PATTERN.findall(evidence)
        if (value := _normalized_number(token)) is not None
    }
    grounded = True

    def replace(match: re.Match[str]) -> str:
        nonlocal grounded
        token = match.group(0)
        value = _normalized_number(token)
        if value is not None and value in evidence_numbers:
            return token
        grounded = False
        return "[未经验证数字已省略]"

    return NUMBER_PATTERN.sub(replace, text), grounded
