from __future__ import annotations

import json
from importlib.resources import files
from typing import Any


def load_demos() -> list[dict[str, Any]]:
    path = files("quantsieve_api").joinpath("data/demos.json")
    return list(json.loads(path.read_text(encoding="utf-8")))
