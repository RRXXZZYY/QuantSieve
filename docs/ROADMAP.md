# QuantSieve product roadmap

QuantSieve's long-term target is a complete, self-hosted quantitative research
and trading workflow. "Complete" means that evidence can move through one
consistent lifecycle:

```text
data snapshot
  -> universe / feature / signal
  -> portfolio target
  -> risk decision
  -> simulated execution
  -> positions / cash / reconciliation
  -> attribution / monitoring / audit
```

The project is currently a trustworthy research workbench with a vectorized
backtester, a first factor-research SDK/API/page, robust parameter validation,
experiment records, market-event monitoring, server-issued run evidence, and
durable plus event-driven simulation primitives. It is not yet a production
order-management or live-trading system.

## Implementation checkpoint — 2026-07-29

This checkpoint distinguishes implemented invariants from roadmap intent:

| Area | Implemented now | Still required before the exit gate |
| --- | --- | --- |
| Reproducible research | Version-two OHLCV snapshot identities bind normalized bars, complete source rows, metadata, citations, and requested bounds while retaining legacy v1 read compatibility; strict `RunManifest`, server-owned run receipts, receipt-backed experiment archiving, and explicit incomplete-provenance reasons | Full point-in-time catalog coverage, calendars/corporate actions, artifact export/replay on a second machine, and broader stale-data policy |
| Factor research | Price/volume factor SDK, REST catalog/research API, and web page; finalized shared-day intersection; `d-1` feature cutoff; one exact shared-bar delay; `open[d+1]` to `close[d+h]` labels; independent acquisition `evidence_id`; fail-closed quantile ties; layered capability/coverage and limitation disclosures; per-horizon IC, rank IC, IC/IR, quantile, long-short, turnover, and monotonicity | Historical constituent timelines, independently verified row-level publication/revision evidence, neutralization, feature cache, model registry, costed portfolio construction, dependence-aware inference for overlapping labels, and unbiased point-in-time fundamental factors |
| Instrument master | Strict versioned records across nine asset classes, point-in-time resolution, revision links, futures metadata, and tamper-evident hashes | Provider-backed catalog ingestion, symbol-change/corporate-action workflows, and exchange-calendar integration |
| Shared risk | Hashed research/allocation limits plus an exact-Decimal Order Risk Kernel bound to every offline event-simulator and durable Paper OMS submit; allow/reject evidence, active-order reservations, kill-switch evidence, and execution envelopes are persisted and replayed, with execution-boundary rechecks | Add an append-only time-indexed kill-switch transition audit, authenticated remote controls, VaR/ES, stress testing, attribution, and real-time portfolio valuation |
| Continuous simulation | Durable paper accounts, an order-event state machine, namespaced fill idempotency, exact-decimal FIFO positions, balanced journals, atomic order/ledger commits, reconciliation, REST access, server-owned uncached Binance Spot price/rule/fee evidence, persisted pre-trade allow/reject decisions, direction-aware fill-envelope rechecks, and a fail-closed SQLite kill switch shared across restarts | Scheduler/rebalancer, integration of the event-driven fill engine with durable accounts, authenticated and time-indexed operator controls, testnet adapters, deterministic operational replay, daily reconciliation jobs, alerts, and UI |
| Event-driven simulation | Offline deterministic next-bar engine using the paper order state machine and exact-decimal ledger; market/limit orders, latency, gap priority, volume participation, partial fills, fees/slippage, persisted per-order risk decisions, execution envelopes, atomic fills, self-contained canonical-input receipts, deterministic full reruns, and hash-chain replay | Multiple accounts/instruments/currencies, shorts/borrow/margin, richer order types, instrument trading rules, corporate actions/futures lifecycle, externally authenticated market-data provenance, durable account/fill-engine integration, and testnet adapters |
| Operations and security | Localhost/private-network defaults, guarded NAS build/deploy with backup and rollback, and allowlisted clean-history public export | Application authentication/authorization, structured audit events, metrics/traces/SLOs, restore drills, job history, and multi-user isolation |

Live account connectivity remains intentionally absent. The durable paper OMS
is a simulation boundary, not evidence that the M2 or M3 exit gates have been
met.

The implemented [factor workflow](FACTOR_RESEARCH.md) uses a fixed,
user-selected ex-post universe. It consumes row availability evidence when
supplied but otherwise applies a provider-policy estimate; that is modeled
timing, not independent point-in-time publication/revision validation. Its
gross quantile returns contain no trading costs, and multi-bar labels overlap
without dependence-aware inference; it is a diagnostic, not a tradable
portfolio result.

The implemented [event-driven simulator](EVENT_SIMULATION.md) is limited to
one account, one instrument, one quote currency, and long-only operation. The
exact-Decimal Order Risk Kernel is persisted and replayed against every
simulator and durable Paper OMS submit. The simulator uses one static
kill-switch snapshot and rechecks it at submit, acknowledgement, and
candidate-fill boundaries. The durable Paper OMS freshly reads a fail-closed
SQLite authority shared across restarts and rechecks it at submit and fill. It
does not yet expose an authenticated operator-control API or retain a
time-indexed transition audit. Simulator receipts retain
canonical bars and intents for deterministic full reruns. Content-addressed
reproduction does not independently authenticate market data supplied by an
untrusted producer. There is no real-account or live-order path.

## Capability standard

| Capability | Current state | Definition of done |
| --- | --- | --- |
| Data governance | Multi-market providers, citations, cache, finalized-bar and OHLCV checks | Point-in-time instrument master, calendars, corporate actions, immutable dataset versions, revision history, data-quality reports, and reproducible snapshot IDs |
| Research and factors | Indicator/custom strategies plus the first fixed-universe price/volume factor SDK/API/page with explicit shared-bar timing, acquisition evidence IDs, fail-closed tie handling, capability/coverage disclosure, IC/IR, grouped returns, turnover, monotonicity, and short-lived run receipts | Point-in-time universes and independently verified availability/revisions, neutralization, feature cache, model registry, dependence-aware inference, costed portfolio construction, and notebook workflow |
| Backtest realism | Next-bar-open vectorized execution plus a deterministic single-account/single-instrument long-only event simulator with partial fills, latency, volume budgets, fees, per-order risk decisions, envelopes, self-contained input evidence, and full deterministic replay | Multiple instruments/accounts, richer order types, liquidity/impact, borrow/margin, dividends/tax, futures roll and expiry, externally authenticated market-data provenance, and testnet parity |
| Portfolio and risk | Basic allocation experiments, user drawdown constraints, a shared research/allocation Risk Kernel, and exact-Decimal persisted pre-trade decisions in the offline event simulator and durable Paper OMS | Add append-only time-indexed kill-switch transitions, authenticated remote controls, VaR/ES, stress tests, optimization, multi-currency valuation, and attribution |
| Paper trading | Forward-only tracking, limited portfolio observation, and durable simulation-only account/order/ledger APIs with server-owned Binance Spot price/rule/fee evidence, idempotent persisted allow/reject decisions, legacy-order failure closure, direction-aware fill-envelope rechecks, and a fail-closed persistent kill switch | Scheduled rebalance, multi-asset and multi-currency accounts, authenticated time-indexed operator controls, testnet adapters, daily reconciliation jobs, alerts, and operator UI |
| Live execution | Not supported | Explicitly gated broker/exchange adapters, secret vault, idempotent OMS, pre-trade controls, kill switch, reconciliation, disaster recovery |
| Operations and audit | Health/status endpoints and source monitoring | Structured logs, metrics/traces, job history, SLO alerts, configuration/user audit, immutable trade-event log, backup/restore drills |
| Platform and ecosystem | REST, MCP, Docker, provider interfaces | Versioned plugin SDK, CLI, migrations, job queue, reproducible artifacts, SBOM/provenance, compatibility policy |

## Delivery order

### M0 — Reproducible foundation

- Versioned data catalog and instrument master
- Immutable run manifest containing code, engine, data, parameters, costs,
  execution model, random seed, and result hashes
- One shared risk-kernel contract
- Platform authentication baseline, audit events, metrics, and restore drill

Exit gate: another machine can reproduce a saved result from its manifest, and
bad/stale data fails closed.

### M1 — Complete research loop

- Factor/universe/feature/label SDK and initial cross-sectional reports
  (implemented for fixed ex-post price/volume universes; see
  [the current contract](FACTOR_RESEARCH.md))
- Point-in-time constituent/revision data, neutralization, feature cache, and
  costed portfolio construction
- Portfolio optimizers, exposure analysis, stress testing, and attribution
- Multi-currency valuation

Exit gate: a user can move from a versioned dataset to a risk-constrained
portfolio without custom glue code.

### M2 — Execution-faithful simulation

- Event-driven engine alongside the existing fast vectorized engine
  (single-account, single-instrument, long-only foundation implemented; see
  [the current contract](EVENT_SIMULATION.md))
- Shared order, fill, fee, position, cash, and reconciliation semantics
- Persisted Risk Kernel decision bound before every offline event-simulator and
  durable Paper OMS submit, plus a fail-closed SQLite kill switch shared across
  Paper OMS restarts (implemented); add authenticated, time-indexed operator
  controls and transition audit
- Continuous paper accounts and exchange/broker testnet adapters
- Operational dashboards, alerts, and deterministic replay

Exit gate: the same strategy and risk decision produce explainable,
reconcilable outcomes in backtest and continuous simulation.

### M3 — Hardened self-hosting

- Optional authentication and role boundaries for trusted small teams
- Encrypted runtime secret injection without browser or image persistence
- Backup/restore drills, audit trails, rate limits, observability, and incident
  runbooks
- Reproducible deployment receipts and upgrade/rollback checks

Exit gate: a documented self-hosted deployment can be upgraded, restored, and
operated without weakening the evidence or simulation boundaries.

## Project boundary

QuantSieve remains an open-source research and simulation workbench. Real
account custody, broker/exchange credentials, and real-order execution are not
planned for this repository. Research correctness, data provenance,
reproducible backtests, continuous simulation, APIs, and the security baseline
remain part of the same public project.

The public repository is a generated clean-history snapshot. It never contains
private plans, production topology, server addresses, operator paths,
credentials, databases, backups, or deployment receipts. See
[`SECURITY.md`](../SECURITY.md) for the release boundary.
