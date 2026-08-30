# quantsieve-providers

Traceable provider adapters with one async interface for instrument search, history, quotes, fundamentals, capital flow, and news.

- `AKShareProvider` for A shares
- `YFinanceProvider` for US markets, with source-labeled public K-line fallbacks when Yahoo is rate limited
- `BinanceProvider` for public spot pairs such as `BTCUSDT` (no account key required)
- `FuturesProvider` for global commodities such as WTI crude oil, Brent, gold, silver, and natural gas
- `MacroProvider` for token-free official daily index and FX series from Nasdaq, Cboe, and the ECB
- `SECEdgarProvider` for official 13F filing tables
- shared TTL-aware SQLite JSON cache

```bash
python -m pip install "./packages/providers[all]"
```

The distribution is not published on PyPI yet; install it from a cloned repository.

Every response is a `DataEnvelope` containing rows, metadata, and citations.
