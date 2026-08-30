# Factor research contract

QuantSieve's first factor workflow is a cross-sectional diagnostic for an
explicit, fixed basket of instruments. It is not presented as a profitable
strategy, a historical index-membership reconstruction, or an unbiased
point-in-time fundamental study.

## API

`GET /api/v1/factors/catalog` publishes the server-owned recipes and limits.
`POST /api/v1/factors/research` accepts three to twenty instruments:

```json
{
  "instruments": [
    {"symbol": "SPY", "provider": "yfinance"},
    {"symbol": "QQQ", "provider": "yfinance"},
    {"symbol": "IWM", "provider": "yfinance"}
  ],
  "factor_id": "momentum",
  "lookback": 20,
  "horizons": [1, 5],
  "quantiles": 3,
  "interval": "1d",
  "start": "2023-01-01",
  "end": "2026-01-01",
  "finalized_bars_only": true
}
```

The server also caps the product of candidate decision dates, instruments, and
horizons at 120,000 observation pairs. Oversized synchronous requests fail
before factor artifacts are built and must be split into smaller studies.

The endpoint is intentionally bounded rather than a distributed job system.
Each API process uses a configurable semaphore (default `1`, allowed range
`1`–`8`) and one deadline covering both capacity wait and execution (default
`45` seconds, maximum `300`). Exhausting the deadline while waiting returns
`503` with `Retry-After: 1`; exhausting it after admission returns `504`.
These limits are process-local and do not coordinate capacity across replicas.
A provider fetch canceled by the execution deadline is not recorded as a
successful research receipt.

Only the following server-defined price/volume recipes are accepted:

- `momentum`: trailing close-to-close return.
- `reversal`: the negative of trailing close-to-close return.
- `low_volatility`: the negative trailing sample standard deviation of returns.
- `volume_surprise`: the latest completed volume relative to its trailing mean.

The direction of every recipe is fixed before evaluation. QuantSieve never
flips a factor after seeing full-sample results.

## Information timing

For a nominal decision index `d` on the exact shared-day sequence, a feature
may read bars only through `d-1`. `decision_time` is the maximum modeled
availability of **every row in every instrument's complete declared lookback
window**, not merely the latest `d-1` row. The workflow reserves shared day `d`
as an exact one-bar delay and does not enter before `d+1`:

```text
nominal decision   = shared day d
feature cutoff     = close[d-1] and earlier
feature available  = max(available_at of all lookback rows and assets)
entry evidence     = open[d+1] row and its available_at
exit evidence      = close[d+horizon] row and its available_at
label available at = max(available_at of all entry and exit rows)
```

This exact shared-bar delay is deliberate: some completed daily bars, including
Binance bars under the configured policy, are modeled as becoming available
after the next UTC day has already opened. A candidate date is dropped if any
lookback dependency is not available by research start or if the basket-wide
feature evidence is not available before the delayed entry.

Features and labels are built separately and paired only after their timing
checks pass. Each serialized label binds the entry-open and exit-close
effective and availability timestamps separately. A label whose combined
availability is later than the research-start UTC instant is neither analyzed
nor persisted. The implementation does not use backward fill, centered
windows, negative shifts, or cross-instrument forward fill. Appending future
bars cannot change already evaluated feature values.

The frozen research-start cutoff is returned as `run_as_of_at` and is bound
again in the recipe, persisted result, parameters, engine configuration, and
therefore the run manifest identity. Reproduction never has to infer this
cutoff from the later receipt or storage timestamp.

Every selected instrument must return certified completed daily bars. The
service intersects exact UTC day labels across the whole basket; it does not
invent missing sessions. A mixed crypto/equity/futures basket therefore drops
crypto-only weekends and any other non-shared dates.

## Evidence and diagnostics

Each source series becomes an immutable `DatasetSnapshot`. The receipt retains
the complete fetched source rows, metadata, and citations, including dates
later excluded from the cross-market intersection, so a replay can
independently derive the alignment instead of trusting an already-filtered
subset. A separate `evidence_id` binds the dataset role and ordinal,
`snapshot_id`, requested bounds, `source_rows_hash`, `metadata_hash`, and
`citations_hash`; the legacy `snapshot_id` remains available but is not a
substitute for this complete acquisition-evidence binding.

Every horizon gets a separate content-addressed `FactorResearchPanel` and
`FactorDiagnostics` artifact. The API persists all of them in the server-owned
`ResearchRunStore` and returns a short-lived receipt plus a `RunManifest`.
Receipts default to a 24-hour lifetime. Recreating exactly the same manifest
reuses its existing run; if it has expired, the store renews that same run
instead of creating conflicting content. Expired runs older than the default
seven-day retention grace may be removed in bounded batches of 100 during later
create operations. These are retention semantics, not a promise of permanent
artifact hosting.

Diagnostics include:

- Pearson IC and rank IC by date, their mean, sample volatility, and IR.
- Equal-weighted quantile returns and top-minus-bottom return.
- Top-minus-bottom turnover and quantile monotonicity.
- Coverage, rejected dates, latest evaluated scores, citations, and source
  snapshot identities.

Coverage is disclosed at several distinct levels:

- Source and alignment coverage: `source_distinct_days`, `common_days`, and
  `alignment_day_retention_rate`.
- Per-horizon `decision_date_coverage_by_horizon` and
  `dropped_reason_counts_by_horizon`.
- Per-asset `source_rows`, `aligned_rows`, `aligned_row_ratio`,
  `numeric_input_basis`, `availability_sources`, provider `capabilities`,
  `snapshot_id`, and `evidence_id`.
- `FactorDiagnostics.coverage_rate`, which is the evaluated
  cross-sectional asset-pair count divided by the eligible pair count. It is
  not source-history or calendar coverage.

Exact decimal statistics are serialized as JSON strings. Clients may convert
them to floating-point values for display, but the strings are retained in
the evidence hash.

Provider acquisition has a separate app-wide bounded-work gate in addition to
the request-concurrency gate. If an HTTP deadline cancels a provider coroutine
that is awaiting a non-cancellable blocking thread, its provider slot remains
leased until that real blocking work stops. Later requests therefore cannot
accumulate blocking calls beyond the configured provider-work limit.

## Scientific-validity boundary

`RunManifest.reproducibility_status` says whether the recorded calculation can
be replayed. It does not certify that a study is free of selection bias.

The current workflow deliberately reports:

```text
universe_semantics       = fixed_user_selected_ex_post
point_in_time_universe   = false
source_availability      = provider_policy_estimate
point_in_time_validation_passed = false
survivorship_bias_controlled    = false
research_only            = true
tradable_conclusion      = false
```

When a row supplies `available_at` or `finalized_at`, that timestamp is
consumed. Otherwise the service applies a conservative provider-policy
estimate. Passing this modeled timing contract is **not** independently
verified point-in-time publication or revision evidence.

Public providers do not currently supply a complete historical constituent
timeline, row-level publication timestamps, or revision chains for
fundamentals. Fundamental factors are therefore disabled. Futures and macro
series remain research references; they do not identify a broker-tradable
contract, roll schedule, margin model, or executable index instrument.

Equal factor values are never split across a computed quantile boundary merely
by sorting instrument identifiers. If a tie would cross that boundary,
diagnostic construction fails closed; a tie that stays wholly inside one group
is allowed.

The reported quantile returns contain no execution costs and are not a
portfolio backtest. For horizons greater than one shared bar, adjacent labels
overlap. The reported period volatility and information ratios do not apply an
embargo, HAC/Newey-West correction, or other adjustment for that dependence,
so they are descriptive diagnostics rather than independent-sample inference.
Use the event-driven simulator and an explicit instrument contract before
making an execution claim.
