# @quantsieve/api

FastAPI application connecting sourced market tools, the monitor, the backtest engine, and an OpenAI-compatible BYOK agent.

From the repository root, install the complete local workspace so the API can
resolve its sibling engine, monitor, and provider packages:

```bash
python -m pip install -r requirements-dev.txt
quantsieve-api
```

Open `http://localhost:8000/docs`.

The API never persists per-request LLM keys. A final grounding guard removes numeric claims that are absent from tool output.
