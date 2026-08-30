# quantsieve-engine

Auditable quantitative-research primitives for QuantSieve. The package keeps
the fast pandas/NumPy backtester while also defining strict factor-research,
order-risk, paper-ledger, and offline event-simulation contracts.

- long/cash signals with one-bar execution delay
- fees and slippage
- equity curve, calendar-time annualized return, bar-sampled volatility/Sharpe, drawdown, allocation-change events, and independent decision cycles
- buy-once fixed-share benchmarks initialized at a strategy's average capital exposure, with price-driven exposure drift and no free rebalancing
- historical drawdown-budget calibration that finds the largest initial fixed-share allocation within a requested limit
- long-only multi-asset allocation backtests with next-open rebalancing, exact holdings, turnover costs, equal-weight and capped inverse-volatility rules
- per-fold timing-alpha evidence and minimum positive-timing-window constraints during walk-forward parameter selection
- research strategy templates, including a fractional core-plus-trend allocation and two passive allocation baselines
- local child-process runner with AST import/call validation and timeouts
- fixed-universe cross-sectional factor diagnostics with a `d-1` information
  cutoff, one exact shared daily-bar delay, `open[d+1]` to `close[d+h]` labels,
  fail-closed quantile ties, and content-addressed panels/diagnostics
- a deterministic single-account, single-instrument, single-quote-currency,
  long-only event simulator with exact-Decimal ledger economics, persisted
  submit-time Order Risk Kernel decisions, execution envelopes, atomic fills,
  and hash-chain replay

The runner reduces accidental local damage; it is not a hardened multi-tenant sandbox.

The factor workflow is a gross, fixed ex-post-basket diagnostic. Modeled
provider availability is not independently verified point-in-time evidence,
and adjacent multi-bar labels overlap without dependence-aware inference. See
the [factor research contract](../../docs/FACTOR_RESEARCH.md).

Version-two event-simulation receipts retain canonical bars and intents;
verification deterministically re-runs them and compares every record, order,
fill, ledger projection, and replay hash. The lower-level record replay helper
checks only the internal consistency of the supplied stream and is not a
substitute for full-run verification. The static kill-switch evidence is
rechecked at submit, acknowledgement, and fill boundaries, but it remains a
run snapshot rather than a time-indexed operational control plane. The durable
Paper OMS also persists an exact-Decimal decision for every new order from
server-owned price and kill-switch evidence, replays allow/reject outcomes, and
rechecks the direction-aware approved envelope at fill time. Its fail-closed
SQLite kill switch is shared across restarts, but there is no authenticated
operator-control API or time-indexed transition audit. There is no
broker, exchange, testnet, or live-order adapter. See the
[event simulation contract](../../docs/EVENT_SIMULATION.md).
