import { describe, expect, it } from "vitest";

import {
  FactorResearchContractError,
  parseFactorResearchPayload,
} from "./factor-research-contract";

const RUN_AS_OF_AT = "2026-01-10T00:00:00+00:00";
const RECORDED_AT = "2026-01-10T00:00:01+00:00";
const EXPIRES_AT = "2026-01-11T00:00:01+00:00";

const ASSETS = [
  {
    symbol: "SPY",
    provider: "yfinance",
    snapshotId: "a".repeat(64),
    evidenceId: "d".repeat(64),
  },
  {
    symbol: "BTCUSDT",
    provider: "binance",
    snapshotId: "b".repeat(64),
    evidenceId: "e".repeat(64),
  },
  {
    symbol: "CL=F",
    provider: "futures",
    snapshotId: "c".repeat(64),
    evidenceId: "f".repeat(64),
  },
] as const;

function snapshot(asset: (typeof ASSETS)[number], index: number) {
  const hashDigit = String(index + 3);
  return {
    schema_version: 2,
    snapshot_id: asset.snapshotId,
    provider: asset.provider,
    symbol: asset.symbol,
    interval: "1d",
    requested_start: "2025-01-01",
    requested_end: "2026-01-09",
    first_observation_at: "2025-01-01T00:00:00+00:00",
    last_observation_at: "2026-01-09T00:00:00+00:00",
    row_count: 250,
    analysis_data_hash: hashDigit.repeat(64),
    source_rows_hash: String(index + 6).repeat(64),
    metadata_hash: ["9", "a", "b"][index].repeat(64),
    citations_hash: ["c", "d", "e"][index].repeat(64),
    finalized_only: true,
    quality_status: "passed",
    quality_issues: [],
    revision_of: null,
    observed_at: RUN_AS_OF_AT,
  };
}

function observation(
  asset: (typeof ASSETS)[number],
  {
    featureEffectiveAt,
    featureAvailableAt,
    entryAt,
    entryAvailableAt,
    exitAt,
    exitAvailableAt,
  }: {
    featureEffectiveAt: string;
    featureAvailableAt: string;
    entryAt: string;
    entryAvailableAt: string;
    exitAt: string;
    exitAvailableAt: string;
  },
) {
  return {
    symbol: asset.symbol,
    provider: asset.provider,
    factor_value: "0.125",
    forward_return: "0.02",
    feature_effective_at: featureEffectiveAt,
    feature_available_at: featureAvailableAt,
    entry_effective_at: entryAt,
    entry_available_at: entryAvailableAt,
    exit_effective_at: exitAt,
    exit_available_at: exitAvailableAt,
    label_available_at: exitAvailableAt,
  };
}

function validPayload(): Record<string, unknown> {
  const snapshots = ASSETS.map(snapshot);
  const firstTiming = {
    featureEffectiveAt: "2026-01-01T00:00:00+00:00",
    featureAvailableAt: "2026-01-02T00:00:00+00:00",
    entryAt: "2026-01-03T00:00:00+00:00",
    entryAvailableAt: "2026-01-04T00:00:00+00:00",
    exitAt: "2026-01-03T00:00:00+00:00",
    exitAvailableAt: "2026-01-04T00:02:00+00:00",
  };
  const secondTiming = {
    featureEffectiveAt: "2026-01-02T00:00:00+00:00",
    featureAvailableAt: "2026-01-03T00:00:00+00:00",
    entryAt: "2026-01-04T00:00:00+00:00",
    entryAvailableAt: "2026-01-05T00:00:00+00:00",
    exitAt: "2026-01-04T00:00:00+00:00",
    exitAvailableAt: "2026-01-05T00:02:00+00:00",
  };
  return {
    run_as_of_at: RUN_AS_OF_AT,
    recipe: {
      run_as_of_at: RUN_AS_OF_AT,
      factor_id: "momentum",
      lookback: 20,
      horizons: [1],
      quantiles: 3,
      interval: "1d",
      formula: "close[d-1] / close[d-lookback-1] - 1",
      direction: "higher_is_stronger",
      feature_information_cutoff: "d-1",
      feature_availability:
        "maximum modeled availability across every row in each declared lookback window and the full basket",
      decision_time: "maximum modeled feature-window availability",
      execution_delay: "one exact shared daily bar",
      label_formula: "open[d+1] to close[d+h]",
      label_evidence: "entry open and exit close rows are bound separately",
      label_available_at:
        "maximum modeled availability of all entry and exit rows",
      label_persistence_cutoff:
        "not later than the research-start UTC instant",
      fundamentals_enabled: false,
      cost_model: "none",
    },
    coverage: {
      requested_assets: 3,
      common_days: 250,
      source_distinct_days: 365,
      alignment_day_retention_rate: "0.6849",
      requested_decision_days: 100,
      candidate_observation_pairs: 300,
      evaluated_periods_by_horizon: { "1": 2 },
      decision_date_coverage_by_horizon: { "1": "0.02" },
      dropped_reason_counts_by_horizon: { "1": {} },
      per_asset: ASSETS.map((asset) => ({
        symbol: asset.symbol,
        provider: asset.provider,
        source_rows: 250,
        aligned_rows: 250,
        aligned_row_ratio: "1",
        numeric_input_basis: "exact_provider_decimal",
        availability_sources: ["row_finalized_at"],
        capabilities: {
          finalized_bars_only: true,
          bar_finalization_policy: "completed_daily_bars",
          bar_finalization_verified: false,
          reference_series: asset.provider === "futures",
        },
        snapshot_id: asset.snapshotId,
        evidence_id: asset.evidenceId,
      })),
      alignment: "exact_shared_utc_day_intersection",
      forward_fill: false,
    },
    diagnostics_by_horizon: {
      "1": {
        panel_id: "1".repeat(64),
        diagnostics: {
          schema_version: 1,
          diagnostics_id: "2".repeat(64),
          panel_id: "1".repeat(64),
          feature_name: "momentum",
          label_name: "forward_return_1d",
          quantile_count: 3,
          period_count: 2,
          observation_count: 6,
          coverage_rate: "1",
          pearson_ic_mean: "0.2",
          pearson_ic_volatility: "0.1",
          pearson_ic_information_ratio: "2",
          rank_ic_mean: "0.25",
          rank_ic_volatility: "0.1",
          rank_ic_information_ratio: "2.5",
          quantile_mean_returns: ["-0.01", "0.01", "0.03"],
          long_short_mean_return: "0.04",
          long_short_volatility: "0.02",
          long_short_information_ratio: "2",
          average_turnover: "0.4",
          quantile_monotonicity: "1",
          periods: [
            {
              period_at: firstTiming.featureAvailableAt,
              eligible_count: 3,
              sample_count: 3,
              coverage_rate: "1",
              pearson_ic: "0.2",
              rank_ic: "0.25",
              quantile_returns: ["-0.01", "0.01", "0.03"],
              long_short_return: "0.04",
              long_short_turnover: null,
            },
            {
              period_at: secondTiming.featureAvailableAt,
              eligible_count: 3,
              sample_count: 3,
              coverage_rate: "1",
              pearson_ic: "0.2",
              rank_ic: "0.25",
              quantile_returns: ["-0.01", "0.01", "0.03"],
              long_short_return: "0.04",
              long_short_turnover: "0.4",
            },
          ],
        },
        dropped_dates: [],
      },
    },
    time_series: {
      "1": [
        {
          nominal_decision_at: "2026-01-02T00:00:00+00:00",
          feature_cutoff_at: firstTiming.featureEffectiveAt,
          period_at: firstTiming.featureAvailableAt,
          entry_at: firstTiming.entryAt,
          label_available_at: firstTiming.exitAvailableAt,
          observations: ASSETS.map((asset) =>
            observation(asset, firstTiming),
          ),
        },
        {
          nominal_decision_at: "2026-01-03T00:00:00+00:00",
          feature_cutoff_at: secondTiming.featureEffectiveAt,
          period_at: secondTiming.featureAvailableAt,
          entry_at: secondTiming.entryAt,
          label_available_at: secondTiming.exitAvailableAt,
          observations: ASSETS.map((asset) =>
            observation(asset, secondTiming),
          ),
        },
      ],
    },
    latest_scores: {
      "1": {
        period_at: secondTiming.featureAvailableAt,
        entry_at: secondTiming.entryAt,
        scores: ASSETS.map((asset) => ({
          symbol: asset.symbol,
          provider: asset.provider,
          factor_value: "0.125",
        })),
      },
    },
    limitations: {
      universe_semantics: "fixed_user_selected_ex_post",
      point_in_time_universe: false,
      source_availability: "provider_policy_estimate",
      point_in_time_validation_passed: false,
      survivorship_bias_controlled: false,
      survivorship_bias_status: "not_controlled",
      fundamentals_enabled: false,
      research_only: true,
      tradable_conclusion: false,
      note: "Research-only diagnostic.",
    },
    citations: ASSETS.map((asset) => ({
      symbol: asset.symbol,
      provider: asset.provider,
      source: `${asset.provider} source`,
      url: `https://example.test/${asset.provider}/${encodeURIComponent(asset.symbol)}`,
      retrieved_at: RUN_AS_OF_AT,
      as_of: RUN_AS_OF_AT,
      note: null,
    })),
    dataset_snapshots: ASSETS.map((asset, index) => ({
      symbol: asset.symbol,
      provider: asset.provider,
      role: "universe",
      ordinal: index,
      evidence_id: asset.evidenceId,
      snapshot: snapshots[index],
    })),
    run_manifest: {
      schema_version: 1,
      manifest_id: "0".repeat(64),
      run_kind: "factor_research",
      application_version: "0.1.0-test",
      engine_version: "quantsieve-factor-research-v1",
      source_revision: null,
      dependency_lock_hash: null,
      datasets: snapshots,
      run_request_hash: "1".repeat(64),
      parameters_hash: "2".repeat(64),
      engine_config_hash: "3".repeat(64),
      cost_model_hash: "4".repeat(64),
      execution_model: "factor-window-entry-exit-test",
      randomness_used: false,
      random_seed: null,
      result_hash: "5".repeat(64),
      uncaptured_inputs: [],
      reproducibility_status: "incomplete",
      missing_requirements: ["source_revision", "dependency_lock"],
      recorded_at: RECORDED_AT,
    },
    run_id: "a".repeat(32),
    run_expires_at: EXPIRES_AT,
  };
}

function nestedRecord(
  value: Record<string, unknown>,
  ...keys: string[]
): Record<string, unknown> {
  let current = value;
  for (const key of keys) {
    current = current[key] as Record<string, unknown>;
  }
  return current;
}

describe("factor research runtime response contract", () => {
  it("accepts a complete evidence-bound response", () => {
    const payload = validPayload();

    expect(parseFactorResearchPayload(payload)).toBe(payload);
  });

  it("rejects a malformed top-level payload with a stable readable path", () => {
    const payload = validPayload();
    delete payload.recipe;

    expect(() => parseFactorResearchPayload(payload)).toThrowError(
      new FactorResearchContractError(
        "$.recipe",
        "required field is missing",
      ),
    );
  });

  it("rejects deep provider-capability drift before rendering", () => {
    const payload = validPayload();
    const perAsset = nestedRecord(payload, "coverage").per_asset as Array<
      Record<string, unknown>
    >;
    nestedRecord(perAsset[1], "capabilities").finalized_bars_only = "true";

    expect(() => parseFactorResearchPayload(payload)).toThrow(
      "$.coverage.per_asset[1].capabilities.finalized_bars_only",
    );
  });

  it("rejects deep timing and evidence-binding drift", () => {
    const timingDrift = validPayload();
    const series = nestedRecord(timingDrift, "time_series")["1"] as Array<
      Record<string, unknown>
    >;
    const observations = series[0].observations as Array<
      Record<string, unknown>
    >;
    observations[2].label_available_at = "2026-01-09T00:00:00+00:00";

    expect(() => parseFactorResearchPayload(timingDrift)).toThrow(
      "$.time_series.1[0].observations[2].label_available_at",
    );

    const evidenceDrift = validPayload();
    const datasets = evidenceDrift.dataset_snapshots as Array<
      Record<string, unknown>
    >;
    datasets[2].evidence_id = "0".repeat(64);

    expect(() => parseFactorResearchPayload(evidenceDrift)).toThrow(
      "$.dataset_snapshots[2]",
    );
  });

  it("rejects unsafe citation links and manifest dataset drift", () => {
    const unsafeCitation = validPayload();
    const citations = unsafeCitation.citations as Array<
      Record<string, unknown>
    >;
    citations[0].url = "javascript:alert(1)";
    expect(() => parseFactorResearchPayload(unsafeCitation)).toThrow(
      "$.citations[0].url",
    );

    const manifestDrift = validPayload();
    const manifest = nestedRecord(manifestDrift, "run_manifest");
    const manifestDatasets = manifest.datasets as Array<
      Record<string, unknown>
    >;
    manifestDatasets.reverse();
    expect(() => parseFactorResearchPayload(manifestDrift)).toThrow(
      "$.run_manifest.datasets",
    );
  });

  it("rejects duplicated observations and stale latest-score projections", () => {
    const duplicateObservation = validPayload();
    const duplicateSeries = nestedRecord(
      duplicateObservation,
      "time_series",
    )["1"] as Array<Record<string, unknown>>;
    const duplicateRows = duplicateSeries[0].observations as Array<
      Record<string, unknown>
    >;
    duplicateRows[2] = { ...duplicateRows[1] };
    expect(() => parseFactorResearchPayload(duplicateObservation)).toThrow(
      "$.time_series.1[0].observations",
    );

    const staleLatest = validPayload();
    const staleScores = nestedRecord(staleLatest, "latest_scores", "1")
      .scores as Array<Record<string, unknown>>;
    staleScores[1].factor_value = "0.126";
    expect(() => parseFactorResearchPayload(staleLatest)).toThrow(
      "$.latest_scores.1.scores[1].factor_value",
    );
  });

  it("binds diagnostic periods and citations to rendered evidence", () => {
    const diagnosticDrift = validPayload();
    const diagnostics = nestedRecord(
      diagnosticDrift,
      "diagnostics_by_horizon",
      "1",
      "diagnostics",
    );
    const periods = diagnostics.periods as Array<Record<string, unknown>>;
    periods[0].period_at = "2026-01-02T00:00:01+00:00";
    expect(() => parseFactorResearchPayload(diagnosticDrift)).toThrow(
      "$.diagnostics_by_horizon.1.diagnostics.periods[0].period_at",
    );

    const citationDrift = validPayload();
    const citations = citationDrift.citations as Array<
      Record<string, unknown>
    >;
    citations[2].symbol = "UNBOUND";
    expect(() => parseFactorResearchPayload(citationDrift)).toThrow(
      "$.citations[2]",
    );
  });
});
