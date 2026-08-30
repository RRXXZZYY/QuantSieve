import type {
  DatasetSnapshot,
  FactorDiagnostics,
  FactorHorizonDiagnostics,
  FactorResearchAssetCoverage,
  FactorResearchPayload,
  FactorResearchProviderCapabilities,
  FactorResearchTimeSeriesRow,
  RunManifest,
} from "./types";

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const RUN_ID_PATTERN = /^[0-9a-f]{32}$/;
const REVISION_PATTERN = /^[0-9a-f]{7,64}$/;
const DECIMAL_PATTERN = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;
const ISO_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const ISO_INSTANT_PATTERN =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

const PROVIDERS = [
  "akshare",
  "yfinance",
  "binance",
  "futures",
  "macro",
] as const;
const FACTOR_IDS = [
  "momentum",
  "reversal",
  "low_volatility",
  "volume_surprise",
] as const;
const AVAILABILITY_SOURCES = [
  "row_available_at",
  "row_finalized_at",
  "provider_policy_next_utc_day_estimate",
] as const;

type JsonRecord = Record<string, unknown>;

export class FactorResearchContractError extends Error {
  readonly path: string;

  constructor(path: string, expectation: string) {
    super(
      `Factor research response contract error at ${path}: ${expectation}.`,
    );
    this.name = "FactorResearchContractError";
    this.path = path;
  }
}

function fail(path: string, expectation: string): never {
  throw new FactorResearchContractError(path, expectation);
}

function record(value: unknown, path: string): JsonRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    fail(path, "expected an object");
  }
  return value as JsonRecord;
}

function array(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) fail(path, "expected an array");
  return value;
}

function string(value: unknown, path: string): string {
  if (typeof value !== "string" || value.trim() === "") {
    fail(path, "expected a non-empty string");
  }
  return value;
}

function literal<T extends string | number | boolean>(
  value: unknown,
  expected: T,
  path: string,
): T {
  if (value !== expected) fail(path, `expected ${JSON.stringify(expected)}`);
  return expected;
}

function oneOf<T extends string | number>(
  value: unknown,
  allowed: readonly T[],
  path: string,
): T {
  if (!allowed.includes(value as T)) {
    fail(path, `expected one of ${allowed.join(", ")}`);
  }
  return value as T;
}

function boolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") fail(path, "expected a boolean");
  return value;
}

function integer(
  value: unknown,
  path: string,
  { minimum = 0, maximum }: { minimum?: number; maximum?: number } = {},
): number {
  if (
    typeof value !== "number" ||
    !Number.isInteger(value) ||
    value < minimum ||
    (maximum !== undefined && value > maximum)
  ) {
    fail(
      path,
      `expected an integer from ${minimum}${
        maximum === undefined ? "" : ` through ${maximum}`
      }`,
    );
  }
  return value;
}

function decimal(value: unknown, path: string): string {
  const parsed = string(value, path);
  if (
    !DECIMAL_PATTERN.test(parsed) ||
    !Number.isFinite(Number(parsed))
  ) {
    fail(path, "expected a finite decimal string");
  }
  return parsed;
}

function nullableDecimal(value: unknown, path: string): string | null {
  return value === null ? null : decimal(value, path);
}

function sha256(value: unknown, path: string): string {
  const parsed = string(value, path);
  if (!SHA256_PATTERN.test(parsed)) {
    fail(path, "expected a lowercase SHA-256 hex digest");
  }
  return parsed;
}

function instant(value: unknown, path: string): string {
  const parsed = string(value, path);
  if (
    !ISO_INSTANT_PATTERN.test(parsed) ||
    !Number.isFinite(Date.parse(parsed))
  ) {
    fail(path, "expected an ISO-8601 timestamp with timezone");
  }
  return parsed;
}

function optionalInstant(
  value: unknown,
  path: string,
): string | null | undefined {
  if (value === undefined || value === null) return value;
  return instant(value, path);
}

function dateOrNull(value: unknown, path: string): string | null {
  if (value === null) return null;
  const parsed = string(value, path);
  if (
    !ISO_DATE_PATTERN.test(parsed) ||
    !Number.isFinite(Date.parse(`${parsed}T00:00:00Z`))
  ) {
    fail(path, "expected an ISO date or null");
  }
  return parsed;
}

function stringArray(value: unknown, path: string): string[] {
  return array(value, path).map((item, index) =>
    string(item, `${path}[${index}]`),
  );
}

function requireKey(object: JsonRecord, key: string, path: string): unknown {
  if (!Object.prototype.hasOwnProperty.call(object, key)) {
    fail(`${path}.${key}`, "required field is missing");
  }
  return object[key];
}

function provider(value: unknown, path: string) {
  return oneOf(value, PROVIDERS, path);
}

function validateCapabilities(
  value: unknown,
  path: string,
): FactorResearchProviderCapabilities {
  const capabilities = record(value, path);
  const booleanKeys = [
    "finalized_bars_only",
    "bar_finalization_verified",
    "exchange_clock_verified",
    "reference_series",
    "tradable_quote",
    "execution_ready",
    "ohlc_derived_from_close",
    "fallback",
    "repair_applied",
  ] as const;
  const stringKeys = [
    "bar_finalization_policy",
    "price_basis",
    "execution_note",
  ] as const;

  for (const key of booleanKeys) {
    if (capabilities[key] !== undefined) {
      boolean(capabilities[key], `${path}.${key}`);
    }
  }
  for (const key of stringKeys) {
    if (capabilities[key] !== undefined) {
      string(capabilities[key], `${path}.${key}`);
    }
  }
  if (capabilities.repaired_rows !== undefined) {
    integer(capabilities.repaired_rows, `${path}.repaired_rows`);
  }
  literal(
    requireKey(capabilities, "finalized_bars_only", path),
    true,
    `${path}.finalized_bars_only`,
  );
  return capabilities as FactorResearchProviderCapabilities;
}

function validateAssetCoverage(
  value: unknown,
  path: string,
): FactorResearchAssetCoverage {
  const asset = record(value, path);
  string(requireKey(asset, "symbol", path), `${path}.symbol`);
  provider(requireKey(asset, "provider", path), `${path}.provider`);
  integer(requireKey(asset, "source_rows", path), `${path}.source_rows`, {
    minimum: 1,
  });
  integer(requireKey(asset, "aligned_rows", path), `${path}.aligned_rows`, {
    minimum: 1,
  });
  decimal(
    requireKey(asset, "aligned_row_ratio", path),
    `${path}.aligned_row_ratio`,
  );
  oneOf(
    requireKey(asset, "numeric_input_basis", path),
    ["exact_provider_decimal", "provider_numeric_projection"] as const,
    `${path}.numeric_input_basis`,
  );
  const sources = array(
    requireKey(asset, "availability_sources", path),
    `${path}.availability_sources`,
  );
  if (sources.length === 0) {
    fail(`${path}.availability_sources`, "expected at least one source");
  }
  sources.forEach((source, index) =>
    oneOf(
      source,
      AVAILABILITY_SOURCES,
      `${path}.availability_sources[${index}]`,
    ),
  );
  validateCapabilities(
    requireKey(asset, "capabilities", path),
    `${path}.capabilities`,
  );
  sha256(requireKey(asset, "snapshot_id", path), `${path}.snapshot_id`);
  sha256(requireKey(asset, "evidence_id", path), `${path}.evidence_id`);
  return asset as FactorResearchAssetCoverage;
}

function validatePeriodDiagnostics(
  value: unknown,
  path: string,
  expectedQuantiles: number,
): void {
  const period = record(value, path);
  instant(requireKey(period, "period_at", path), `${path}.period_at`);
  integer(
    requireKey(period, "eligible_count", path),
    `${path}.eligible_count`,
  );
  integer(
    requireKey(period, "sample_count", path),
    `${path}.sample_count`,
  );
  decimal(
    requireKey(period, "coverage_rate", path),
    `${path}.coverage_rate`,
  );
  decimal(requireKey(period, "pearson_ic", path), `${path}.pearson_ic`);
  decimal(requireKey(period, "rank_ic", path), `${path}.rank_ic`);
  const quantileReturns = array(
    requireKey(period, "quantile_returns", path),
    `${path}.quantile_returns`,
  );
  if (quantileReturns.length !== expectedQuantiles) {
    fail(
      `${path}.quantile_returns`,
      "length must match recipe.quantiles",
    );
  }
  quantileReturns.forEach((item, index) =>
    decimal(item, `${path}.quantile_returns[${index}]`),
  );
  decimal(
    requireKey(period, "long_short_return", path),
    `${path}.long_short_return`,
  );
  nullableDecimal(
    requireKey(period, "long_short_turnover", path),
    `${path}.long_short_turnover`,
  );
}

function validateDiagnostics(
  value: unknown,
  path: string,
  expectedPanelId: string,
  expectedQuantiles: number,
): FactorDiagnostics {
  const diagnostics = record(value, path);
  literal(requireKey(diagnostics, "schema_version", path), 1, `${path}.schema_version`);
  sha256(
    requireKey(diagnostics, "diagnostics_id", path),
    `${path}.diagnostics_id`,
  );
  const panelId = sha256(
    requireKey(diagnostics, "panel_id", path),
    `${path}.panel_id`,
  );
  if (panelId !== expectedPanelId) {
    fail(`${path}.panel_id`, "must match the horizon panel_id");
  }
  string(
    requireKey(diagnostics, "feature_name", path),
    `${path}.feature_name`,
  );
  string(
    requireKey(diagnostics, "label_name", path),
    `${path}.label_name`,
  );
  const quantileCount = integer(
    requireKey(diagnostics, "quantile_count", path),
    `${path}.quantile_count`,
    { minimum: 2, maximum: 10 },
  );
  if (quantileCount !== expectedQuantiles) {
    fail(`${path}.quantile_count`, "must match recipe.quantiles");
  }
  const periodCount = integer(
    requireKey(diagnostics, "period_count", path),
    `${path}.period_count`,
    { minimum: 2 },
  );
  integer(
    requireKey(diagnostics, "observation_count", path),
    `${path}.observation_count`,
    { minimum: 1 },
  );
  for (const key of [
    "coverage_rate",
    "pearson_ic_mean",
    "pearson_ic_volatility",
    "rank_ic_mean",
    "rank_ic_volatility",
    "long_short_mean_return",
    "long_short_volatility",
    "average_turnover",
    "quantile_monotonicity",
  ] as const) {
    decimal(requireKey(diagnostics, key, path), `${path}.${key}`);
  }
  for (const key of [
    "pearson_ic_information_ratio",
    "rank_ic_information_ratio",
    "long_short_information_ratio",
  ] as const) {
    nullableDecimal(requireKey(diagnostics, key, path), `${path}.${key}`);
  }
  const quantileReturns = array(
    requireKey(diagnostics, "quantile_mean_returns", path),
    `${path}.quantile_mean_returns`,
  );
  if (quantileReturns.length !== expectedQuantiles) {
    fail(
      `${path}.quantile_mean_returns`,
      "length must match recipe.quantiles",
    );
  }
  quantileReturns.forEach((item, index) =>
    decimal(item, `${path}.quantile_mean_returns[${index}]`),
  );
  const periods = array(
    requireKey(diagnostics, "periods", path),
    `${path}.periods`,
  );
  if (periods.length !== periodCount) {
    fail(`${path}.periods`, "length must match diagnostics.period_count");
  }
  periods.forEach((period, index) =>
    validatePeriodDiagnostics(
      period,
      `${path}.periods[${index}]`,
      expectedQuantiles,
    ),
  );
  return diagnostics as FactorDiagnostics;
}

function validateHorizonDiagnostics(
  value: unknown,
  path: string,
  expectedQuantiles: number,
): FactorHorizonDiagnostics {
  const horizon = record(value, path);
  const panelId = sha256(
    requireKey(horizon, "panel_id", path),
    `${path}.panel_id`,
  );
  validateDiagnostics(
    requireKey(horizon, "diagnostics", path),
    `${path}.diagnostics`,
    panelId,
    expectedQuantiles,
  );
  array(
    requireKey(horizon, "dropped_dates", path),
    `${path}.dropped_dates`,
  ).forEach((item, index) => {
    const dropped = record(item, `${path}.dropped_dates[${index}]`);
    instant(
      requireKey(dropped, "period_at", `${path}.dropped_dates[${index}]`),
      `${path}.dropped_dates[${index}].period_at`,
    );
    string(
      requireKey(dropped, "reason", `${path}.dropped_dates[${index}]`),
      `${path}.dropped_dates[${index}].reason`,
    );
  });
  return horizon as FactorHorizonDiagnostics;
}

function validateObservation(
  value: unknown,
  path: string,
  periodAt: string,
  entryAt: string,
  runAsOfAt: string,
  assets: ReadonlySet<string>,
): { assetKey: string; labelAvailableAt: string } {
  const observation = record(value, path);
  const symbol = string(
    requireKey(observation, "symbol", path),
    `${path}.symbol`,
  );
  const providerName = provider(
    requireKey(observation, "provider", path),
    `${path}.provider`,
  );
  const assetKey = `${providerName}:${symbol}`;
  if (!assets.has(assetKey)) {
    fail(path, "observation asset is not present in coverage.per_asset");
  }
  decimal(
    requireKey(observation, "factor_value", path),
    `${path}.factor_value`,
  );
  decimal(
    requireKey(observation, "forward_return", path),
    `${path}.forward_return`,
  );
  const featureEffectiveAt = instant(
    requireKey(observation, "feature_effective_at", path),
    `${path}.feature_effective_at`,
  );
  const featureAvailableAt = instant(
    requireKey(observation, "feature_available_at", path),
    `${path}.feature_available_at`,
  );
  const entryEffectiveAt = instant(
    requireKey(observation, "entry_effective_at", path),
    `${path}.entry_effective_at`,
  );
  const entryAvailableAt = instant(
    requireKey(observation, "entry_available_at", path),
    `${path}.entry_available_at`,
  );
  const exitEffectiveAt = instant(
    requireKey(observation, "exit_effective_at", path),
    `${path}.exit_effective_at`,
  );
  const exitAvailableAt = instant(
    requireKey(observation, "exit_available_at", path),
    `${path}.exit_available_at`,
  );
  const labelAvailableAt = instant(
    requireKey(observation, "label_available_at", path),
    `${path}.label_available_at`,
  );

  if (Date.parse(featureEffectiveAt) > Date.parse(featureAvailableAt)) {
    fail(`${path}.feature_available_at`, "must not precede feature_effective_at");
  }
  if (Date.parse(featureAvailableAt) > Date.parse(periodAt)) {
    fail(`${path}.feature_available_at`, "must not be later than period_at");
  }
  if (entryEffectiveAt !== entryAt) {
    fail(`${path}.entry_effective_at`, "must match the series entry_at");
  }
  if (Date.parse(entryEffectiveAt) > Date.parse(entryAvailableAt)) {
    fail(`${path}.entry_available_at`, "must not precede entry_effective_at");
  }
  if (Date.parse(exitEffectiveAt) < Date.parse(entryEffectiveAt)) {
    fail(`${path}.exit_effective_at`, "must not precede entry_effective_at");
  }
  if (Date.parse(exitEffectiveAt) > Date.parse(exitAvailableAt)) {
    fail(`${path}.exit_available_at`, "must not precede exit_effective_at");
  }
  const expectedLabelAvailableAt = Math.max(
    Date.parse(entryAvailableAt),
    Date.parse(exitAvailableAt),
  );
  if (Date.parse(labelAvailableAt) !== expectedLabelAvailableAt) {
    fail(
      `${path}.label_available_at`,
      "must equal max(entry_available_at, exit_available_at)",
    );
  }
  if (Date.parse(labelAvailableAt) > Date.parse(runAsOfAt)) {
    fail(`${path}.label_available_at`, "must not be later than run_as_of_at");
  }
  return { assetKey, labelAvailableAt };
}

function validateTimeSeriesRow(
  value: unknown,
  path: string,
  runAsOfAt: string,
  assets: ReadonlySet<string>,
  expectedAssetCount: number,
): FactorResearchTimeSeriesRow {
  const row = record(value, path);
  instant(
    requireKey(row, "nominal_decision_at", path),
    `${path}.nominal_decision_at`,
  );
  const featureCutoffAt = instant(
    requireKey(row, "feature_cutoff_at", path),
    `${path}.feature_cutoff_at`,
  );
  const periodAt = instant(
    requireKey(row, "period_at", path),
    `${path}.period_at`,
  );
  const entryAt = instant(
    requireKey(row, "entry_at", path),
    `${path}.entry_at`,
  );
  const labelAvailableAt = instant(
    requireKey(row, "label_available_at", path),
    `${path}.label_available_at`,
  );
  if (Date.parse(featureCutoffAt) >= Date.parse(entryAt)) {
    fail(`${path}.feature_cutoff_at`, "must be earlier than entry_at");
  }
  if (Date.parse(periodAt) >= Date.parse(entryAt)) {
    fail(`${path}.period_at`, "must be earlier than entry_at");
  }
  const observations = array(
    requireKey(row, "observations", path),
    `${path}.observations`,
  );
  if (observations.length !== expectedAssetCount) {
    fail(`${path}.observations`, "length must match coverage.requested_assets");
  }
  const parsedObservations = observations.map((observation, index) =>
    validateObservation(
      observation,
      `${path}.observations[${index}]`,
      periodAt,
      entryAt,
      runAsOfAt,
      assets,
    ),
  );
  if (
    new Set(parsedObservations.map((observation) => observation.assetKey))
      .size !== expectedAssetCount
  ) {
    fail(`${path}.observations`, "asset identities must be unique");
  }
  if (
    Date.parse(labelAvailableAt) !==
    Math.max(
      ...parsedObservations.map((observation) =>
        Date.parse(observation.labelAvailableAt),
      ),
    )
  ) {
    fail(
      `${path}.label_available_at`,
      "must equal the latest observation label availability",
    );
  }
  return row as FactorResearchTimeSeriesRow;
}

function validateDatasetSnapshot(
  value: unknown,
  path: string,
): DatasetSnapshot {
  const snapshot = record(value, path);
  oneOf(
    requireKey(snapshot, "schema_version", path),
    [1, 2] as const,
    `${path}.schema_version`,
  );
  sha256(requireKey(snapshot, "snapshot_id", path), `${path}.snapshot_id`);
  string(requireKey(snapshot, "provider", path), `${path}.provider`);
  string(requireKey(snapshot, "symbol", path), `${path}.symbol`);
  literal(requireKey(snapshot, "interval", path), "1d", `${path}.interval`);
  dateOrNull(
    requireKey(snapshot, "requested_start", path),
    `${path}.requested_start`,
  );
  dateOrNull(
    requireKey(snapshot, "requested_end", path),
    `${path}.requested_end`,
  );
  instant(
    requireKey(snapshot, "first_observation_at", path),
    `${path}.first_observation_at`,
  );
  instant(
    requireKey(snapshot, "last_observation_at", path),
    `${path}.last_observation_at`,
  );
  integer(requireKey(snapshot, "row_count", path), `${path}.row_count`, {
    minimum: 1,
  });
  for (const key of [
    "analysis_data_hash",
    "source_rows_hash",
    "metadata_hash",
    "citations_hash",
  ] as const) {
    sha256(requireKey(snapshot, key, path), `${path}.${key}`);
  }
  literal(
    requireKey(snapshot, "finalized_only", path),
    true,
    `${path}.finalized_only`,
  );
  oneOf(
    requireKey(snapshot, "quality_status", path),
    ["passed", "degraded", "failed"] as const,
    `${path}.quality_status`,
  );
  stringArray(
    requireKey(snapshot, "quality_issues", path),
    `${path}.quality_issues`,
  );
  const revisionOf = requireKey(snapshot, "revision_of", path);
  if (revisionOf !== null) sha256(revisionOf, `${path}.revision_of`);
  instant(requireKey(snapshot, "observed_at", path), `${path}.observed_at`);
  return snapshot as DatasetSnapshot;
}

function validateRunManifest(
  value: unknown,
  path: string,
  runAsOfAt: string,
  snapshotIds: readonly string[],
): RunManifest {
  const manifest = record(value, path);
  literal(requireKey(manifest, "schema_version", path), 1, `${path}.schema_version`);
  sha256(requireKey(manifest, "manifest_id", path), `${path}.manifest_id`);
  literal(
    requireKey(manifest, "run_kind", path),
    "factor_research",
    `${path}.run_kind`,
  );
  string(
    requireKey(manifest, "application_version", path),
    `${path}.application_version`,
  );
  string(
    requireKey(manifest, "engine_version", path),
    `${path}.engine_version`,
  );
  const sourceRevision = requireKey(manifest, "source_revision", path);
  if (
    sourceRevision !== null &&
    (typeof sourceRevision !== "string" ||
      !REVISION_PATTERN.test(sourceRevision))
  ) {
    fail(`${path}.source_revision`, "expected a source revision or null");
  }
  const dependencyLockHash = requireKey(
    manifest,
    "dependency_lock_hash",
    path,
  );
  if (dependencyLockHash !== null) {
    sha256(dependencyLockHash, `${path}.dependency_lock_hash`);
  }
  const datasets = array(
    requireKey(manifest, "datasets", path),
    `${path}.datasets`,
  ).map((dataset, index) =>
    validateDatasetSnapshot(dataset, `${path}.datasets[${index}]`),
  );
  const manifestSnapshotIds = datasets.map((dataset) => dataset.snapshot_id);
  if (
    manifestSnapshotIds.length !== snapshotIds.length ||
    manifestSnapshotIds.some(
      (snapshotId, index) => snapshotId !== snapshotIds[index],
    )
  ) {
    fail(`${path}.datasets`, "must match dataset_snapshots in order");
  }
  for (const key of [
    "run_request_hash",
    "parameters_hash",
    "engine_config_hash",
    "cost_model_hash",
    "result_hash",
  ] as const) {
    sha256(requireKey(manifest, key, path), `${path}.${key}`);
  }
  string(
    requireKey(manifest, "execution_model", path),
    `${path}.execution_model`,
  );
  literal(
    requireKey(manifest, "randomness_used", path),
    false,
    `${path}.randomness_used`,
  );
  if (requireKey(manifest, "random_seed", path) !== null) {
    fail(`${path}.random_seed`, "expected null when randomness_used=false");
  }
  array(
    requireKey(manifest, "uncaptured_inputs", path),
    `${path}.uncaptured_inputs`,
  ).forEach((item, index) =>
    literal(
      item,
      "indicator_warmup",
      `${path}.uncaptured_inputs[${index}]`,
    ),
  );
  oneOf(
    requireKey(manifest, "reproducibility_status", path),
    ["complete", "incomplete"] as const,
    `${path}.reproducibility_status`,
  );
  array(
    requireKey(manifest, "missing_requirements", path),
    `${path}.missing_requirements`,
  ).forEach((item, index) =>
    oneOf(
      item,
      [
        "source_revision",
        "dependency_lock",
        "dataset_quality",
        "indicator_warmup",
      ] as const,
      `${path}.missing_requirements[${index}]`,
    ),
  );
  const recordedAt = instant(
    requireKey(manifest, "recorded_at", path),
    `${path}.recorded_at`,
  );
  if (Date.parse(recordedAt) < Date.parse(runAsOfAt)) {
    fail(`${path}.recorded_at`, "must not precede run_as_of_at");
  }
  return manifest as RunManifest;
}

function validateCitation(value: unknown, path: string): void {
  const citation = record(value, path);
  string(requireKey(citation, "symbol", path), `${path}.symbol`);
  provider(requireKey(citation, "provider", path), `${path}.provider`);
  string(requireKey(citation, "source", path), `${path}.source`);
  const url = citation.url;
  if (url !== undefined && url !== null) {
    const parsedUrl = string(url, `${path}.url`);
    let protocol: string;
    try {
      protocol = new URL(parsedUrl).protocol;
    } catch {
      fail(`${path}.url`, "expected a valid http(s) URL or null");
    }
    if (protocol !== "http:" && protocol !== "https:") {
      fail(`${path}.url`, "expected a valid http(s) URL or null");
    }
  }
  optionalInstant(citation.retrieved_at, `${path}.retrieved_at`);
  optionalInstant(citation.as_of, `${path}.as_of`);
  if (citation.note !== undefined && citation.note !== null) {
    string(citation.note, `${path}.note`);
  }
}

/**
 * Validate the complete factor-research rendering boundary before state is set.
 * The returned cast is safe only because all fields used by the page have been
 * checked above, including nested evidence and timing relationships.
 */
export function parseFactorResearchPayload(
  value: unknown,
): FactorResearchPayload {
  const payload = record(value, "$");
  const runAsOfAt = instant(
    requireKey(payload, "run_as_of_at", "$"),
    "$.run_as_of_at",
  );

  const recipe = record(requireKey(payload, "recipe", "$"), "$.recipe");
  const recipeRunAsOfAt = instant(
    requireKey(recipe, "run_as_of_at", "$.recipe"),
    "$.recipe.run_as_of_at",
  );
  if (recipeRunAsOfAt !== runAsOfAt) {
    fail("$.recipe.run_as_of_at", "must equal top-level run_as_of_at");
  }
  const factorId = oneOf(
    requireKey(recipe, "factor_id", "$.recipe"),
    FACTOR_IDS,
    "$.recipe.factor_id",
  );
  integer(requireKey(recipe, "lookback", "$.recipe"), "$.recipe.lookback", {
    minimum: 5,
    maximum: 252,
  });
  const horizons = array(
    requireKey(recipe, "horizons", "$.recipe"),
    "$.recipe.horizons",
  ).map((item, index) =>
    integer(item, `$.recipe.horizons[${index}]`, {
      minimum: 1,
      maximum: 63,
    }),
  );
  if (
    horizons.length === 0 ||
    horizons.length > 4 ||
    new Set(horizons).size !== horizons.length
  ) {
    fail("$.recipe.horizons", "expected one through four unique horizons");
  }
  const quantiles = integer(
    requireKey(recipe, "quantiles", "$.recipe"),
    "$.recipe.quantiles",
    { minimum: 2, maximum: 10 },
  );
  literal(requireKey(recipe, "interval", "$.recipe"), "1d", "$.recipe.interval");
  string(requireKey(recipe, "formula", "$.recipe"), "$.recipe.formula");
  string(requireKey(recipe, "direction", "$.recipe"), "$.recipe.direction");
  literal(
    requireKey(recipe, "feature_information_cutoff", "$.recipe"),
    "d-1",
    "$.recipe.feature_information_cutoff",
  );
  string(
    requireKey(recipe, "feature_availability", "$.recipe"),
    "$.recipe.feature_availability",
  );
  literal(
    requireKey(recipe, "decision_time", "$.recipe"),
    "maximum modeled feature-window availability",
    "$.recipe.decision_time",
  );
  literal(
    requireKey(recipe, "execution_delay", "$.recipe"),
    "one exact shared daily bar",
    "$.recipe.execution_delay",
  );
  literal(
    requireKey(recipe, "label_formula", "$.recipe"),
    "open[d+1] to close[d+h]",
    "$.recipe.label_formula",
  );
  string(
    requireKey(recipe, "label_evidence", "$.recipe"),
    "$.recipe.label_evidence",
  );
  string(
    requireKey(recipe, "label_available_at", "$.recipe"),
    "$.recipe.label_available_at",
  );
  string(
    requireKey(recipe, "label_persistence_cutoff", "$.recipe"),
    "$.recipe.label_persistence_cutoff",
  );
  literal(
    requireKey(recipe, "fundamentals_enabled", "$.recipe"),
    false,
    "$.recipe.fundamentals_enabled",
  );
  literal(
    requireKey(recipe, "cost_model", "$.recipe"),
    "none",
    "$.recipe.cost_model",
  );

  const coverage = record(
    requireKey(payload, "coverage", "$"),
    "$.coverage",
  );
  const requestedAssets = integer(
    requireKey(coverage, "requested_assets", "$.coverage"),
    "$.coverage.requested_assets",
    { minimum: 3, maximum: 20 },
  );
  for (const key of [
    "common_days",
    "source_distinct_days",
    "requested_decision_days",
    "candidate_observation_pairs",
  ] as const) {
    integer(
      requireKey(coverage, key, "$.coverage"),
      `$.coverage.${key}`,
      { minimum: 1 },
    );
  }
  decimal(
    requireKey(coverage, "alignment_day_retention_rate", "$.coverage"),
    "$.coverage.alignment_day_retention_rate",
  );
  literal(
    requireKey(coverage, "alignment", "$.coverage"),
    "exact_shared_utc_day_intersection",
    "$.coverage.alignment",
  );
  literal(
    requireKey(coverage, "forward_fill", "$.coverage"),
    false,
    "$.coverage.forward_fill",
  );
  const perAsset = array(
    requireKey(coverage, "per_asset", "$.coverage"),
    "$.coverage.per_asset",
  ).map((asset, index) =>
    validateAssetCoverage(asset, `$.coverage.per_asset[${index}]`),
  );
  if (perAsset.length !== requestedAssets) {
    fail("$.coverage.per_asset", "length must match requested_assets");
  }
  const assetKeys = new Set(
    perAsset.map((asset) => `${asset.provider}:${asset.symbol}`),
  );
  if (assetKeys.size !== perAsset.length) {
    fail("$.coverage.per_asset", "provider and symbol pairs must be unique");
  }
  const snapshotIds = new Set(perAsset.map((asset) => asset.snapshot_id));
  const evidenceIds = new Set(perAsset.map((asset) => asset.evidence_id));
  if (snapshotIds.size !== perAsset.length) {
    fail("$.coverage.per_asset", "snapshot_id values must be unique");
  }
  if (evidenceIds.size !== perAsset.length) {
    fail("$.coverage.per_asset", "evidence_id values must be unique");
  }

  const horizonKeys = horizons.map(String);
  for (const [field, validator] of [
    ["evaluated_periods_by_horizon", integer],
    ["decision_date_coverage_by_horizon", decimal],
  ] as const) {
    const mapping = record(
      requireKey(coverage, field, "$.coverage"),
      `$.coverage.${field}`,
    );
    for (const key of horizonKeys) {
      validator(
        requireKey(mapping, key, `$.coverage.${field}`),
        `$.coverage.${field}.${key}`,
      );
    }
  }
  const droppedCounts = record(
    requireKey(coverage, "dropped_reason_counts_by_horizon", "$.coverage"),
    "$.coverage.dropped_reason_counts_by_horizon",
  );
  for (const key of horizonKeys) {
    const reasons = record(
      requireKey(
        droppedCounts,
        key,
        "$.coverage.dropped_reason_counts_by_horizon",
      ),
      `$.coverage.dropped_reason_counts_by_horizon.${key}`,
    );
    for (const [reason, count] of Object.entries(reasons)) {
      if (reason.trim() === "") {
        fail(
          `$.coverage.dropped_reason_counts_by_horizon.${key}`,
          "reason keys must be non-empty",
        );
      }
      integer(
        count,
        `$.coverage.dropped_reason_counts_by_horizon.${key}.${reason}`,
      );
    }
  }

  const diagnosticsByHorizon = record(
    requireKey(payload, "diagnostics_by_horizon", "$"),
    "$.diagnostics_by_horizon",
  );
  const timeSeries = record(
    requireKey(payload, "time_series", "$"),
    "$.time_series",
  );
  const latestScores = record(
    requireKey(payload, "latest_scores", "$"),
    "$.latest_scores",
  );
  for (const horizonKey of horizonKeys) {
    const horizon = validateHorizonDiagnostics(
      requireKey(
        diagnosticsByHorizon,
        horizonKey,
        "$.diagnostics_by_horizon",
      ),
      `$.diagnostics_by_horizon.${horizonKey}`,
      quantiles,
    );
    if (horizon.diagnostics.feature_name !== factorId) {
      fail(
        `$.diagnostics_by_horizon.${horizonKey}.diagnostics.feature_name`,
        "must match recipe.factor_id",
      );
    }
    if (
      horizon.diagnostics.label_name !== `forward_return_${horizonKey}d`
    ) {
      fail(
        `$.diagnostics_by_horizon.${horizonKey}.diagnostics.label_name`,
        "must match the horizon label",
      );
    }
    const series = array(
      requireKey(timeSeries, horizonKey, "$.time_series"),
      `$.time_series.${horizonKey}`,
    ).map((row, index) =>
      validateTimeSeriesRow(
        row,
        `$.time_series.${horizonKey}[${index}]`,
        runAsOfAt,
        assetKeys,
        requestedAssets,
      ),
    );
    if (series.length !== horizon.diagnostics.period_count) {
      fail(
        `$.time_series.${horizonKey}`,
        "length must match diagnostics.period_count",
      );
    }
    const expectedPeriods = integer(
      record(
        requireKey(
          coverage,
          "evaluated_periods_by_horizon",
          "$.coverage",
        ),
        "$.coverage.evaluated_periods_by_horizon",
      )[horizonKey],
      `$.coverage.evaluated_periods_by_horizon.${horizonKey}`,
    );
    if (series.length !== expectedPeriods) {
      fail(
        `$.time_series.${horizonKey}`,
        "length must match evaluated_periods_by_horizon",
      );
    }
    horizon.diagnostics.periods.forEach((period, index) => {
      if (period.period_at !== series[index].period_at) {
        fail(
          `$.diagnostics_by_horizon.${horizonKey}.diagnostics.periods[${index}].period_at`,
          "must match the corresponding time-series period",
        );
      }
    });
    const latest = record(
      requireKey(latestScores, horizonKey, "$.latest_scores"),
      `$.latest_scores.${horizonKey}`,
    );
    const latestPeriodAt = instant(
      requireKey(latest, "period_at", `$.latest_scores.${horizonKey}`),
      `$.latest_scores.${horizonKey}.period_at`,
    );
    const latestEntryAt = instant(
      requireKey(latest, "entry_at", `$.latest_scores.${horizonKey}`),
      `$.latest_scores.${horizonKey}.entry_at`,
    );
    const lastSeriesRow = series[series.length - 1];
    if (latestPeriodAt !== lastSeriesRow.period_at) {
      fail(
        `$.latest_scores.${horizonKey}.period_at`,
        "must match the latest time-series period",
      );
    }
    if (latestEntryAt !== lastSeriesRow.entry_at) {
      fail(
        `$.latest_scores.${horizonKey}.entry_at`,
        "must match the latest time-series entry",
      );
    }
    const scores = array(
      requireKey(latest, "scores", `$.latest_scores.${horizonKey}`),
      `$.latest_scores.${horizonKey}.scores`,
    );
    if (scores.length !== requestedAssets) {
      fail(
        `$.latest_scores.${horizonKey}.scores`,
        "length must match coverage.requested_assets",
      );
    }
    const latestFactors = new Map(
      lastSeriesRow.observations.map((observation) => [
        `${observation.provider}:${observation.symbol}`,
        observation.factor_value,
      ]),
    );
    const scoreAssetKeys = new Set<string>();
    scores.forEach((score, index) => {
      const parsed = record(
        score,
        `$.latest_scores.${horizonKey}.scores[${index}]`,
      );
      const symbol = string(
        requireKey(
          parsed,
          "symbol",
          `$.latest_scores.${horizonKey}.scores[${index}]`,
        ),
        `$.latest_scores.${horizonKey}.scores[${index}].symbol`,
      );
      const providerName = provider(
        requireKey(
          parsed,
          "provider",
          `$.latest_scores.${horizonKey}.scores[${index}]`,
        ),
        `$.latest_scores.${horizonKey}.scores[${index}].provider`,
      );
      const factorValue = decimal(
        requireKey(
          parsed,
          "factor_value",
          `$.latest_scores.${horizonKey}.scores[${index}]`,
        ),
        `$.latest_scores.${horizonKey}.scores[${index}].factor_value`,
      );
      const assetKey = `${providerName}:${symbol}`;
      if (!assetKeys.has(assetKey)) {
        fail(
          `$.latest_scores.${horizonKey}.scores[${index}]`,
          "asset is not present in coverage.per_asset",
        );
      }
      if (scoreAssetKeys.has(assetKey)) {
        fail(
          `$.latest_scores.${horizonKey}.scores`,
          "asset identities must be unique",
        );
      }
      scoreAssetKeys.add(assetKey);
      if (latestFactors.get(assetKey) !== factorValue) {
        fail(
          `$.latest_scores.${horizonKey}.scores[${index}].factor_value`,
          "must match the latest time-series observation",
        );
      }
    });
  }

  const datasets = array(
    requireKey(payload, "dataset_snapshots", "$"),
    "$.dataset_snapshots",
  ).map((item, index) => {
    const evidence = record(item, `$.dataset_snapshots[${index}]`);
    const symbol = string(
      requireKey(evidence, "symbol", `$.dataset_snapshots[${index}]`),
      `$.dataset_snapshots[${index}].symbol`,
    );
    const providerName = provider(
      requireKey(evidence, "provider", `$.dataset_snapshots[${index}]`),
      `$.dataset_snapshots[${index}].provider`,
    );
    literal(
      requireKey(evidence, "role", `$.dataset_snapshots[${index}]`),
      "universe",
      `$.dataset_snapshots[${index}].role`,
    );
    const ordinal = integer(
      requireKey(evidence, "ordinal", `$.dataset_snapshots[${index}]`),
      `$.dataset_snapshots[${index}].ordinal`,
    );
    if (ordinal !== index) {
      fail(
        `$.dataset_snapshots[${index}].ordinal`,
        "must match its response position",
      );
    }
    const evidenceId = sha256(
      requireKey(evidence, "evidence_id", `$.dataset_snapshots[${index}]`),
      `$.dataset_snapshots[${index}].evidence_id`,
    );
    const snapshot = validateDatasetSnapshot(
      requireKey(evidence, "snapshot", `$.dataset_snapshots[${index}]`),
      `$.dataset_snapshots[${index}].snapshot`,
    );
    const coverageAsset = perAsset[index];
    if (
      coverageAsset.symbol !== symbol ||
      coverageAsset.provider !== providerName ||
      coverageAsset.snapshot_id !== snapshot.snapshot_id ||
      coverageAsset.evidence_id !== evidenceId
    ) {
      fail(
        `$.dataset_snapshots[${index}]`,
        "must match coverage.per_asset at the same ordinal",
      );
    }
    if (
      snapshot.symbol !== symbol ||
      snapshot.provider !== providerName
    ) {
      fail(
        `$.dataset_snapshots[${index}].snapshot`,
        "provider and symbol must match the evidence binding",
      );
    }
    return snapshot;
  });
  if (datasets.length !== requestedAssets) {
    fail(
      "$.dataset_snapshots",
      "length must match coverage.requested_assets",
    );
  }

  const manifest = validateRunManifest(
    requireKey(payload, "run_manifest", "$"),
    "$.run_manifest",
    runAsOfAt,
    datasets.map((dataset) => dataset.snapshot_id),
  );
  const runId = string(requireKey(payload, "run_id", "$"), "$.run_id");
  if (!RUN_ID_PATTERN.test(runId)) {
    fail("$.run_id", "expected a lowercase 32-character run id");
  }
  const expiresAt = instant(
    requireKey(payload, "run_expires_at", "$"),
    "$.run_expires_at",
  );
  if (Date.parse(expiresAt) <= Date.parse(manifest.recorded_at)) {
    fail("$.run_expires_at", "must be later than run_manifest.recorded_at");
  }

  const limitations = record(
    requireKey(payload, "limitations", "$"),
    "$.limitations",
  );
  literal(
    requireKey(limitations, "universe_semantics", "$.limitations"),
    "fixed_user_selected_ex_post",
    "$.limitations.universe_semantics",
  );
  literal(
    requireKey(limitations, "source_availability", "$.limitations"),
    "provider_policy_estimate",
    "$.limitations.source_availability",
  );
  for (const key of [
    "point_in_time_universe",
    "point_in_time_validation_passed",
    "survivorship_bias_controlled",
    "fundamentals_enabled",
    "research_only",
    "tradable_conclusion",
  ] as const) {
    boolean(
      requireKey(limitations, key, "$.limitations"),
      `$.limitations.${key}`,
    );
  }
  string(
    requireKey(limitations, "survivorship_bias_status", "$.limitations"),
    "$.limitations.survivorship_bias_status",
  );
  string(requireKey(limitations, "note", "$.limitations"), "$.limitations.note");

  const citations = array(
    requireKey(payload, "citations", "$"),
    "$.citations",
  );
  if (citations.length < requestedAssets) {
    fail("$.citations", "expected at least one citation per asset");
  }
  const citedAssets = new Set<string>();
  citations.forEach((citation, index) => {
    validateCitation(citation, `$.citations[${index}]`);
    const parsed = citation as JsonRecord;
    const assetKey = `${String(parsed.provider)}:${String(parsed.symbol)}`;
    if (!assetKeys.has(assetKey)) {
      fail(
        `$.citations[${index}]`,
        "asset is not present in coverage.per_asset",
      );
    }
    citedAssets.add(assetKey);
  });
  if (citedAssets.size !== requestedAssets) {
    fail("$.citations", "expected at least one citation for every asset");
  }

  return payload as FactorResearchPayload;
}
