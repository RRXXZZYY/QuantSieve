# quantsieve-mcp

Key-free A-share, US-market, Binance spot, and global commodity-futures tools over the Model Context Protocol.

```bash
python -m pip install "./packages/providers[all]" ./packages/mcp
quantsieve-mcp
```

The distributions are not published on PyPI yet; this command is intentionally
repository-local.

Tools:

- `search_market_instruments` — search by Chinese/English name or symbol across asset classes
- `get_market_history`
- `get_latest_quote`
- `get_fundamentals`
- `get_capital_flow`
- `get_company_news`

Data tools also resolve names such as `贵州茅台`, `Apple`, `Bitcoin`, or `原油` before fetching.
Each result retains the provider's citation and retrieval metadata.
