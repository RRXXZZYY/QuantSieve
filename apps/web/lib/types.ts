export type Citation = {
  source: string;
  url?: string | null;
  retrieved_at?: string;
  as_of?: string | null;
  note?: string | null;
};

export type Demo = {
  id: string;
  title: string;
  prompt: string;
  answer: string;
  citations: Citation[];
};

export type Strategy = {
  id: string;
  name: string;
  description: string;
  parameters: Record<string, number>;
  category: string;
  best_for: string;
  risk_note: string;
  recommended_intervals: BarInterval[];
  warmup_bars: number;
};

export type BarInterval = "15m" | "1h" | "4h" | "1d" | "1wk";

export type MarketHistoryCapabilities = {
  symbol: string;
  provider: Instrument["provider"];
  intervals: Record<
    BarInterval,
    { supported: boolean; max_history_days: number | null }
  >;
};

export type DiscoveryHorizonRequirements = {
  intervals: Record<
    BarInterval,
    { full_sample_days: number; holdout_days: number }
  >;
};

export type EngineBacktestConfig = {
  initial_cash: number;
  fee_rate: number;
  slippage_rate: number;
  annual_periods: number;
  bar_interval: BarInterval;
  signal_delay_bars: number;
};

export type Instrument = {
  symbol: string;
  name: string;
  market: "CN" | "US" | "ETF" | "INDEX" | "FOREX" | "CRYPTO" | "FUTURES";
  exchange: string;
  currency: string;
  provider: "akshare" | "yfinance" | "binance" | "futures" | "macro";
  asset_type: string;
};

export type EquityPoint = {
  date: string;
  equity: number;
  return: number;
  position: number;
};

export type PortfolioMetrics = {
  bars: number;
  duration_years: number;
  total_return: number;
  annualized_return: number;
  annualized_return_capped: boolean;
  annualized_return_cap: number;
  annualized_volatility: number;
  sharpe_ratio: number;
  max_drawdown: number;
  rebalances: number;
  turnover_ratio: number;
  transaction_cost_ratio: number;
};

export type PortfolioResult = {
  method:
    | "initial_equal_hold"
    | "periodic_equal"
    | "periodic_inverse_volatility";
  metrics: PortfolioMetrics;
  equity: Array<{ date: string; equity: number; return: number }>;
  allocations: Array<{
    date: string;
    weights: Record<string, number>;
    turnover: number;
    cost: number;
  }>;
  last_rebalance_target_weights: Record<string, number>;
  ending_realized_weights: Record<string, number>;
  latest_weights: Record<string, number>;
  config: EngineBacktestConfig;
};

export type PortfolioMethod = PortfolioResult["method"];

export type PortfolioBacktestRequest = {
  assets: Array<{
    symbol: string;
    provider: Instrument["provider"] | "auto";
    currency: string;
  }>;
  start: string;
  end: string;
  volatility_lookback: number;
  rebalance_bars: number;
  maximum_asset_weight: number;
  config?: EngineBacktestConfig | null;
};

export type PortfolioDataQuality = {
  actual_start?: string | null;
  actual_end?: string | null;
  annual_periods: number;
  source_bars: Record<string, number>;
  alignment: "common_daily_session_labels";
  valuation_limit: string;
};

export type PortfolioAssumptions = {
  volatility_lookback: number;
  rebalance_bars: number;
  maximum_asset_weight: number;
  fee_rate: number;
  slippage_rate: number;
  cash_return: number;
  execution: "next_open" | "next_common_session_open_proxy";
};

export type PortfolioResearchDecision = {
  risk_evidence_passed: boolean;
  drawdown_improved_segments: number;
  sharpe_improved_segments: number;
  evaluable_segments: number;
  total_segments: number;
  evidence_checks: {
    all_segments_evaluable: boolean;
    all_segments_drawdown_strictly_improved: boolean;
    majority_segments_sharpe_improved: boolean;
    full_sample_sharpe_improved: boolean;
    full_sample_positive_return: boolean;
    full_sample_invested: boolean;
    minimum_segment_bars: number;
  };
  title: string;
  reason: string;
};

export type PortfolioBacktestPayload = {
  run_id?: string;
  run_expires_at?: string;
  calculation_version?: string;
  assets: Array<{
    symbol: string;
    requested_symbol?: string | null;
    provider: string;
    currency: string;
    metadata: Record<string, unknown>;
  }>;
  start: string;
  end: string;
  common_bars: number;
  data_quality: PortfolioDataQuality;
  assumptions: PortfolioAssumptions;
  results: Record<
    "initial_equal_hold" | "periodic_equal" | "periodic_inverse_volatility",
    PortfolioResult
  >;
  segments: Array<{
    index: number;
    start: string;
    end: string;
    results: Record<
      "initial_equal_hold" | "periodic_equal" | "periodic_inverse_volatility",
      PortfolioResult
    >;
  }>;
  research_decision: PortfolioResearchDecision;
  citations: Citation[];
};

export type PortfolioExperimentAsset = Instrument & {
  requested_symbol?: string | null;
  metadata: Record<string, unknown>;
};

export type PortfolioExperimentCreatePayload = {
  schema_version: 1;
  kind: "portfolio";
  name: string;
  notes?: string | null;
  assets: PortfolioExperimentAsset[];
  interval: "1d";
  start: string;
  end: string;
  focus_method: PortfolioMethod;
  run_request: PortfolioBacktestRequest;
  data_quality: PortfolioDataQuality;
  assumptions: PortfolioAssumptions;
  common_bars: number;
  results: PortfolioBacktestPayload["results"];
  segments: PortfolioBacktestPayload["segments"];
  research_decision: PortfolioResearchDecision;
  citations: Citation[];
  calculation_version?: string;
};

export type PortfolioExperimentFromRunPayload = {
  run_id: string;
  name: string;
  notes?: string | null;
  focus_method: PortfolioMethod;
  assets: Array<{
    symbol: string;
    requested_symbol?: string | null;
    name: string;
    market: string;
    exchange: string;
    asset_type: string;
  }>;
};

export type PortfolioExperimentRecord = PortfolioExperimentCreatePayload & {
  id: string;
  source_run_id?: string | null;
  created_at: string;
  updated_at: string;
};

export type PortfolioExperimentSummary = {
  schema_version: 1;
  kind: "portfolio";
  id: string;
  source_run_id?: string | null;
  name: string;
  notes?: string | null;
  symbols: string[];
  asset_count: number;
  interval: "1d";
  start: string;
  end: string;
  focus_method: PortfolioMethod;
  common_bars: number;
  total_return: number;
  max_drawdown: number;
  sharpe_ratio: number;
  risk_evidence_passed: boolean;
  created_at: string;
  updated_at: string;
};

export type PortfolioPaperObservationPhase =
  | "awaiting_opening"
  | "opening_window_expired"
  | "awaiting_close_valuation"
  | "valued";

export type PortfolioPaperValuationSummary = {
  cash: number;
  equity: number;
  total_return: number;
  peak_equity: number;
  max_drawdown: number;
  realized_weights: Record<string, number>;
  turnover_ratio: number;
  turnover_notional: number;
  fee_paid: number;
  slippage_paid: number;
  total_cost: number;
  valuation_count: 1;
};

export type PortfolioPaperObservation = {
  id: string;
  portfolio_experiment_id: string;
  symbols: string[];
  method: PortfolioMethod;
  quote_currency: "USDT";
  venue: "Binance Spot";
  scope: "one_session_modeled_observation";
  initial_cash: number;
  fee_rate: number;
  slippage_rate: number;
  information_session: string;
  execution_session: string;
  target_weights: Record<string, number>;
  created_at: string;
  updated_at: string;
  opening_at: string | null;
  valuation_at: string | null;
  attention_required: boolean;
  phase: PortfolioPaperObservationPhase;
  valuation: PortfolioPaperValuationSummary | null;
};

export type PortfolioPaperPublicStatus = {
  availability: "internal_only";
  scope: "one_session_modeled_observation";
  activation_available: false;
  opening: { enabled: boolean; running: boolean };
  settlement: { enabled: boolean; running: boolean };
};

export type PortfolioPaperPreflightAsset = {
  symbol: string;
  status:
    | "eligible_for_internal_review"
    | "research_only"
    | "verification_unavailable";
  reasons: string[];
  rules_verified_at: string | null;
};

export type PortfolioPaperPreflight = {
  availability: "internal_only";
  scope: "one_session_modeled_observation";
  activation_available: false;
  review_status:
    | "ready_for_internal_review"
    | "verification_incomplete"
    | "not_in_scope";
  portfolio_experiment_id: string;
  reviewed_at: string;
  assets: PortfolioPaperPreflightAsset[];
  next_step: string;
};

export type BacktestMetrics = {
  bars: number;
  duration_years: number;
  total_return: number;
  annualized_return: number;
  annualized_return_capped: boolean;
  annualized_return_cap: number;
  annualized_volatility: number;
  sharpe_ratio: number;
  max_drawdown: number;
  win_rate: number;
  trades: number;
  closed_trades: number;
  trades_per_year: number;
  average_holding_bars: number;
  profit_factor: number;
  exposure_ratio: number;
  max_cash_streak: number;
  max_cash_streak_ratio: number;
};

export type TradeQuality = {
  closed_trades: number;
  sample_quality: "insufficient" | "developing" | "mature";
  win_rate: number;
  win_rate_confidence_low: number;
  win_rate_confidence_high: number;
  average_winner: number;
  average_loser: number;
  payoff_ratio: number | null;
  expectancy: number;
};

export type OptimizationObjective =
  | "total_return"
  | "sharpe_ratio"
  | "drawdown_control"
  | "balanced";

export type HoldoutValidationCode =
  | "passed"
  | "negative_return"
  | "benchmark_capture_failed"
  | "exposure_matched_lag"
  | "weak_market_lag"
  | "sample_insufficient"
  | "history_horizon_insufficient"
  | "return_target_failed"
  | "drawdown_limit_failed"
  | "frequency_too_low"
  | "exposure_too_low"
  | "frequency_too_high"
  | "cash_streak_too_long"
  | "cost_stress_failed"
  | "unclassified";

export type OptimizationResult = {
  objective: OptimizationObjective;
  selected_parameters: Record<string, number>;
  train_ratio: number;
  split_date: string;
  candidates_evaluated: number;
  candidates_eligible: number;
  minimum_trades: number;
  minimum_trades_per_year: number;
  minimum_exposure: number;
  minimum_annualized_return?: number | null;
  maximum_drawdown?: number | null;
  maximum_cash_streak_ratio: number;
  maximum_cash_streak_bars?: number | null;
  maximum_trades_per_year?: number | null;
  minimum_profitable_fold_ratio: number;
  minimum_timing_positive_fold_ratio: number;
  train_metrics: BacktestMetrics;
  validation_metrics: BacktestMetrics;
  development_benchmark_metrics: BacktestMetrics;
  development_exposure_matched_benchmark_metrics: BacktestMetrics;
  validation_benchmark_metrics: BacktestMetrics;
  validation_exposure_matched_benchmark_metrics: BacktestMetrics;
  validation_excess_return: number;
  validation_timing_excess_return: number;
  validation_passed: boolean;
  validation_code: HoldoutValidationCode;
  validation_reason: string;
  forward_observation_eligible: boolean;
  validation_trade_quality?: TradeQuality;
  cost_stress_passed: boolean;
  cost_stress_tests: Array<{
    multiplier: number;
    fee_rate: number;
    slippage_rate: number;
      metrics: BacktestMetrics;
      positive_return: boolean;
      exposure_matched_total_return: number;
      timing_excess_return: number;
      beats_exposure_matched: boolean;
      passed: boolean;
  }>;
  walk_forward_folds: Array<{
    train_start: string;
    train_end: string;
    validation_start: string;
    validation_end: string;
    selected_parameters: Record<string, number>;
    validation_metrics: BacktestMetrics;
    exposure_matched_benchmark_metrics: BacktestMetrics;
    timing_excess_return: number;
    timing_value_added: boolean;
  }>;
  walk_forward_method: "expanding_window_reoptimization";
  walk_forward_execution_state_carried: boolean;
  top_candidates: Array<{
    parameters: Record<string, number>;
    score: number;
    score_stability: number;
    metrics: BacktestMetrics;
    fold_scores: number[];
    fold_timing_excess_returns: number[];
    profitable_fold_ratio: number;
    timing_positive_fold_ratio: number;
  }>;
};

export type BacktestDiagnostics = {
  signal_state: {
    status: "pending_entry" | "pending_exit" | "holding" | "cash";
    requested_signal: number;
    executed_position: number;
    pending_action: "buy_next_open" | "sell_next_open" | "hold" | "wait";
    latest_bar_at: string;
    last_signal_change_at: string;
    last_signal_from: number;
    last_signal_to: number;
    bars_in_signal_state: number;
    bars_in_executed_cash: number;
  };
  trade_quality: TradeQuality;
  segments: Array<{
    index: number;
    start: string;
    end: string;
    bars: number;
    strategy_return: number;
    benchmark_return: number;
    excess_return: number;
    max_drawdown: number;
    profitable: boolean;
    beats_benchmark: boolean;
  }>;
  profitable_segment_ratio: number;
  benchmark_beaten_segment_ratio: number;
  worst_segment_return: number;
};

export type DatasetSnapshot = {
  schema_version: 1 | 2;
  snapshot_id: string;
  provider: string;
  symbol: string;
  interval: string;
  requested_start: string | null;
  requested_end: string | null;
  first_observation_at: string;
  last_observation_at: string;
  row_count: number;
  analysis_data_hash: string;
  source_rows_hash: string;
  metadata_hash: string;
  citations_hash: string;
  finalized_only: boolean;
  quality_status: "passed" | "degraded" | "failed";
  quality_issues: string[];
  revision_of: string | null;
  observed_at: string;
};

export type RunManifest = {
  schema_version: 1;
  manifest_id: string;
  run_kind:
    | "single_backtest"
    | "portfolio_backtest"
    | "cross_market_study"
    | "custom_backtest"
    | "factor_research"
    | "paper_decision";
  application_version: string;
  engine_version: string;
  source_revision: string | null;
  dependency_lock_hash: string | null;
  datasets: DatasetSnapshot[];
  run_request_hash: string;
  parameters_hash: string;
  engine_config_hash: string;
  cost_model_hash: string;
  execution_model: string;
  randomness_used: boolean;
  random_seed: number | null;
  result_hash: string;
  uncaptured_inputs: Array<"indicator_warmup">;
  reproducibility_status: "complete" | "incomplete";
  missing_requirements: Array<
    | "source_revision"
    | "dependency_lock"
    | "dataset_quality"
    | "indicator_warmup"
  >;
  recorded_at: string;
};

export type FactorId =
  | "momentum"
  | "reversal"
  | "low_volatility"
  | "volume_surprise";

export type FactorResearchAssetRequest = {
  symbol: string;
  provider: Instrument["provider"];
};

export type FactorResearchRequest = {
  instruments: FactorResearchAssetRequest[];
  factor_id: FactorId;
  lookback: number;
  horizons: number[];
  quantiles: number;
  interval: "1d";
  start: string;
  end: string;
  finalized_bars_only: true;
};

export type FactorResearchAvailabilitySource =
  | "row_available_at"
  | "row_finalized_at"
  | "provider_policy_next_utc_day_estimate";

export type FactorResearchNumericInputBasis =
  | "exact_provider_decimal"
  | "provider_numeric_projection";

export type FactorResearchProviderCapabilities = {
  finalized_bars_only?: boolean;
  bar_finalization_policy?: string;
  bar_finalization_verified?: boolean;
  exchange_clock_verified?: boolean;
  reference_series?: boolean;
  tradable_quote?: boolean;
  execution_ready?: boolean;
  ohlc_derived_from_close?: boolean;
  fallback?: boolean;
  price_basis?: string;
  repair_applied?: boolean;
  repaired_rows?: number;
  execution_note?: string;
};

export type FactorResearchAssetCoverage = {
  symbol: string;
  provider: Instrument["provider"];
  source_rows: number;
  aligned_rows: number;
  aligned_row_ratio: string;
  numeric_input_basis: FactorResearchNumericInputBasis;
  availability_sources: FactorResearchAvailabilitySource[];
  capabilities: FactorResearchProviderCapabilities;
  snapshot_id: string;
  evidence_id: string;
};

export type FactorPeriodDiagnostics = {
  period_at: string;
  eligible_count: number;
  sample_count: number;
  coverage_rate: string;
  pearson_ic: string;
  rank_ic: string;
  quantile_returns: string[];
  long_short_return: string;
  long_short_turnover: string | null;
};

export type FactorDiagnostics = {
  schema_version: 1;
  diagnostics_id: string;
  panel_id: string;
  feature_name: string;
  label_name: string;
  quantile_count: number;
  period_count: number;
  observation_count: number;
  coverage_rate: string;
  pearson_ic_mean: string;
  pearson_ic_volatility: string;
  pearson_ic_information_ratio: string | null;
  rank_ic_mean: string;
  rank_ic_volatility: string;
  rank_ic_information_ratio: string | null;
  quantile_mean_returns: string[];
  long_short_mean_return: string;
  long_short_volatility: string;
  long_short_information_ratio: string | null;
  average_turnover: string;
  quantile_monotonicity: string;
  periods: FactorPeriodDiagnostics[];
};

export type FactorHorizonDiagnostics = {
  panel_id: string;
  diagnostics: FactorDiagnostics;
  dropped_dates: Array<{
    period_at: string;
    reason: string;
  }>;
};

export type FactorLatestScore = {
  symbol: string;
  provider: Instrument["provider"];
  factor_value: string;
};

export type FactorResearchCoverage = {
  requested_assets: number;
  common_days: number;
  source_distinct_days: number;
  alignment_day_retention_rate: string;
  requested_decision_days: number;
  candidate_observation_pairs: number;
  evaluated_periods_by_horizon: Record<string, number>;
  decision_date_coverage_by_horizon: Record<string, string>;
  dropped_reason_counts_by_horizon: Record<string, Record<string, number>>;
  per_asset: FactorResearchAssetCoverage[];
  alignment: "exact_shared_utc_day_intersection";
  forward_fill: false;
};

export type FactorResearchRecipe = {
  run_as_of_at: string;
  factor_id: FactorId;
  lookback: number;
  horizons: number[];
  quantiles: number;
  interval: "1d";
  formula: string;
  direction: string;
  feature_information_cutoff: "d-1";
  feature_availability: string;
  decision_time: "maximum modeled feature-window availability";
  execution_delay: "one exact shared daily bar";
  label_formula: "open[d+1] to close[d+h]";
  label_evidence: "entry open and exit close rows are bound separately";
  label_available_at: "maximum modeled availability of all entry and exit rows";
  label_persistence_cutoff: "not later than the research-start UTC instant";
  fundamentals_enabled: false;
  cost_model: "none";
};

export type FactorResearchTimeSeriesRow = {
  nominal_decision_at: string;
  feature_cutoff_at: string;
  period_at: string;
  entry_at: string;
  label_available_at: string;
  observations: Array<{
    symbol: string;
    provider: Instrument["provider"];
    factor_value: string;
    forward_return: string;
    feature_effective_at: string;
    feature_available_at: string;
    entry_effective_at: string;
    entry_available_at: string;
    exit_effective_at: string;
    exit_available_at: string;
    label_available_at: string;
  }>;
};

export type FactorResearchLatestScores = {
  period_at: string;
  entry_at: string;
  scores: FactorLatestScore[];
};

export type FactorResearchLimitations = {
  universe_semantics: "fixed_user_selected_ex_post";
  point_in_time_universe: boolean;
  source_availability: "provider_policy_estimate";
  point_in_time_validation_passed: boolean;
  survivorship_bias_controlled: boolean;
  survivorship_bias_status: string;
  fundamentals_enabled: boolean;
  research_only: boolean;
  tradable_conclusion: boolean;
  note: string;
};

export type FactorResearchDatasetSnapshot = {
  symbol: string;
  provider: Instrument["provider"];
  role: "universe";
  ordinal: number;
  evidence_id: string;
  snapshot: DatasetSnapshot;
};

export type FactorResearchCitation = Citation & {
  symbol: string;
  provider: Instrument["provider"];
};

export type FactorResearchPayload = {
  run_as_of_at: string;
  recipe: FactorResearchRecipe;
  coverage: FactorResearchCoverage;
  diagnostics_by_horizon: Record<string, FactorHorizonDiagnostics>;
  time_series: Record<string, FactorResearchTimeSeriesRow[]>;
  latest_scores: Record<string, FactorResearchLatestScores>;
  limitations: FactorResearchLimitations;
  citations: FactorResearchCitation[];
  dataset_snapshots: FactorResearchDatasetSnapshot[];
  run_manifest: RunManifest;
  run_id: string;
  run_expires_at: string;
};

export type BacktestPayload = {
  run_id?: string;
  run_expires_at?: string;
  dataset_snapshot?: DatasetSnapshot;
  run_manifest?: RunManifest;
  symbol: string;
  interval: BarInterval;
  data_metadata: Record<string, unknown>;
  strategy: Strategy;
  ohlcv: Array<Record<string, string | number | null>>;
  result: {
    metrics: BacktestMetrics;
    equity: EquityPoint[];
    trades: Array<Record<string, string | number | boolean>>;
    position_cycles?: Array<Record<string, string | number | boolean>>;
    config: EngineBacktestConfig;
  };
  benchmark: {
    strategy: Strategy;
    result: {
      metrics: BacktestMetrics;
      equity: EquityPoint[];
      trades: Array<Record<string, string | number | boolean>>;
      position_cycles?: Array<Record<string, string | number | boolean>>;
      config: EngineBacktestConfig;
    };
  };
  exposure_matched_benchmark: {
    target_exposure: number;
    result: {
      metrics: BacktestMetrics;
      equity: EquityPoint[];
      trades: Array<Record<string, string | number | boolean>>;
      position_cycles?: Array<Record<string, string | number | boolean>>;
      config: EngineBacktestConfig;
    };
  };
  comparison: {
    excess_return: number;
    excess_annualized_return: number;
    drawdown_improvement: number;
    beats_benchmark: boolean;
    positive_return: boolean;
  };
  timing_comparison: {
    target_exposure: number;
    excess_return: number;
    beats_exposure_matched: boolean;
  };
  diagnostics: BacktestDiagnostics;
  optimization?: OptimizationResult;
  citations: Citation[];
};

export type StrategyRobustnessPayload = {
  run_id?: string;
  run_expires_at?: string;
  strategy: Strategy;
  interval: BarInterval;
  objective: OptimizationObjective;
  parameter_policy: "independently_optimized_per_market";
  parameter_policy_note: string;
  final_holdout_min_closed_trades: number;
  markets: Array<{
    symbol: string;
    provider: string;
    status: "completed" | "rejected" | "unavailable";
    validation_passed: boolean;
    validation_code: HoldoutValidationCode | "constraints_too_strict" | "unavailable";
    validation_reason: string;
    forward_observation_eligible: boolean;
    evidence: {
      data_metadata: Record<string, unknown>;
      strategy: Strategy;
      full_sample_metrics: BacktestMetrics;
      optimization: OptimizationResult;
      citations: Citation[];
    } | null;
  }>;
  summary: {
    requested: number;
    completed: number;
    validated: number;
    provisional: number;
    rejected: number;
    unavailable: number;
  };
  research_decision: {
    status:
      | "validated_across_markets"
      | "mixed_evidence"
      | "provisional_only"
      | "rejected_across_markets"
      | "incomplete";
    title: string;
    reason: string;
  };
};

export type CrossMarketExperimentRecord = StrategyRobustnessPayload & {
  id: string;
  source_run_id: string;
  schema_version: 1;
  kind: "cross_market";
  name: string;
  notes?: string | null;
  run_id?: undefined;
  run_expires_at?: undefined;
  created_at: string;
  updated_at: string;
};

export type StrategyDiscoveryCandidate = {
  strategy: Strategy;
  validation_passed: boolean;
  validation_code: HoldoutValidationCode;
  validation_reason: string;
  forward_observation_eligible: boolean;
  validation_score: number;
  selection_score: number;
  backtest: BacktestPayload;
};

export type StrategyDiscoveryPayload = {
  symbol: string;
  interval: BarInterval;
  objective: OptimizationObjective;
  data_metadata: Record<string, unknown>;
  development_market_regime: {
    classification: "trending" | "range_bound" | "mixed" | "insufficient";
    direction: "rising" | "falling" | "flat";
    net_return: number;
    path_efficiency: number;
    sample_bars: number;
    reason: string;
  };
  status:
    | "validated_candidate"
    | "provisional_candidate"
    | "no_validated_candidate"
    | "development_rejected"
    | "constraints_too_strict";
  selection_protocol: {
    development_ratio: number;
    templates_screened: number;
    screening_parameter_policy:
      "deterministic_two_stage_coverage_then_adaptive_refinement";
    screening_data_scope: "development_only";
    screening_parameter_policy_symbol_agnostic: true;
    screening_adaptation_scope: "requested_symbol_development_data";
    screening_initial_parameter_limit_per_template: number;
    screening_parameter_limit_per_template: number;
    screening_parameter_grid_total: number;
    screening_parameter_sets_evaluated: number;
    screening_grid_coverage_ratio: number;
    screening_constraints: {
      candidates_evaluated: number;
      candidates_passing_base_constraints: number;
      constraint_failures: Array<{
        reason: string;
        failed_candidates: number;
        failure_ratio: number;
      }>;
    };
    shortlisted: number;
    shortlist_selection_policy: "development_rank_with_feasible_family_coverage";
    feasible_strategy_families: Array<
      "mean_reversion" | "risk_managed_allocation" | "trend_or_breakout"
    >;
    shortlist_strategy_families: Array<
      "mean_reversion" | "risk_managed_allocation" | "trend_or_breakout"
    >;
    optimized: number;
    development_validated: number;
    development_selection_eligible: number;
    development_selection_min_closed_trades: number;
    holdout_evaluated: number;
    final_holdout_ratio: number;
    final_holdout_min_closed_trades: number;
    forward_observation_min_closed_trades: number;
    holdout_used_for_shortlisting: false;
    holdout_execution_state_carried: true;
  };
  shortlist: Array<{
    strategy: Strategy;
    metrics: BacktestMetrics;
    comparison: BacktestPayload["comparison"];
    exposure_matched_benchmark_metrics: BacktestMetrics;
      timing_comparison: BacktestPayload["timing_comparison"];
      score: number;
      screening_objective_score: number;
      screening_selected_stage: "coverage" | "adaptive_refinement";
      quality: "outperform" | "defensive" | "lagging" | "negative";
      screening_candidates_evaluated: number;
      screening_candidates_passing_base_constraints: number;
      screening_grid_total: number;
      screening_grid_coverage_ratio: number;
      screening_budget: {
        initial_limit: number;
        maximum: number;
        evaluated: number;
        full_grid_evaluated: boolean;
        truncated_by_budget: boolean;
      };
      screening_parameter_coverage: Record<
        string,
        {
          sampled_levels: number;
          available_levels: number;
          coverage_ratio: number;
          extrema_covered: boolean;
        }
      >;
      screening_constraint_reasons: string[];
    interval_recommended: boolean;
    market_regime_fit: "aligned" | "neutral" | "counter_regime";
    market_regime_fit_reason: string;
  }>;
  selection_trials: Array<{
    strategy: Strategy;
    selection_score: number;
    development_validation_passed: boolean;
    development_selection_eligible: boolean;
    development_validation_code: OptimizationResult["validation_code"];
    development_validation_reason: string;
    development_validation_metrics: {
      total_return: number;
      trades_per_year: number;
      closed_trades: number;
      timing_excess_return: number;
      cost_stress_passed: boolean;
    };
  }>;
  candidates: StrategyDiscoveryCandidate[];
  failures: Array<{
    strategy_id: string;
    strategy_name: string;
    reason: string;
  }>;
  champion: StrategyDiscoveryCandidate | null;
  provisional: StrategyDiscoveryCandidate | null;
  best_available: StrategyDiscoveryCandidate | null;
  research_decision: {
    mode:
      | "validated_active"
      | "freeze_for_evidence"
      | "development_rejected"
      | "passive_baseline"
      | "no_actionable_strategy";
    title: string;
    reason: string;
  };
  passive_baseline: {
    preferred: boolean;
    backtest: BacktestPayload;
    risk_budgeted: {
      requested_max_drawdown: number;
      target_exposure: number;
      cash_reserve_ratio: number;
      budget_satisfied: boolean;
      calibration_start: string;
      calibration_end: string;
      calibration_budget_satisfied: boolean;
      full_sample_budget_satisfied: boolean;
      backtest: BacktestPayload;
    };
  };
  elapsed_ms: number;
  citations: Citation[];
};

export type ExperimentRecord = {
  id: string;
  source_run_id?: string | null;
  provenance_status?: "legacy_unverified" | "server_verified";
  run_manifest?: RunManifest | null;
  name: string;
  notes?: string | null;
  instrument: Instrument;
  strategy: Pick<Strategy, "id" | "name" | "category" | "parameters">;
  interval: BarInterval;
  start: string;
  end: string;
  optimized: boolean;
  run_request: Record<string, unknown>;
  engine_config: Record<string, unknown>;
  data_metadata: Record<string, unknown>;
  metrics: BacktestMetrics;
  benchmark_metrics: BacktestMetrics;
  exposure_matched_benchmark_metrics?: BacktestMetrics | null;
  comparison: BacktestPayload["comparison"];
  timing_comparison?: BacktestPayload["timing_comparison"] | null;
  diagnostics?: BacktestDiagnostics | null;
  validation?: {
    objective: OptimizationObjective;
    split_date: string;
    validation_passed: boolean;
    validation_code: HoldoutValidationCode;
    validation_reason: string;
    forward_observation_eligible: boolean;
    development_metrics: BacktestMetrics;
    validation_metrics: BacktestMetrics;
    validation_benchmark_metrics: BacktestMetrics;
    validation_exposure_matched_benchmark_metrics?: BacktestMetrics | null;
  } | null;
  citations: Citation[];
  created_at: string;
  updated_at: string;
};

export type ExperimentCreatePayload = Omit<
  ExperimentRecord,
  | "id"
  | "source_run_id"
  | "provenance_status"
  | "run_manifest"
  | "created_at"
  | "updated_at"
>;

export type ExperimentFromRunPayload = {
  name: string;
  notes: string | null;
  run_id: string;
  instrument_name: string;
};

export type PaperForwardExecution = {
  side: "buy" | "sell";
  executed_at: string;
  raw_open_price: number;
  modeled_fill_price: number;
  position_before: number;
  position_after: number;
  turnover: number;
  friction_rate: number;
  friction_amount: number;
};

export type PaperForwardState = {
  schema_version: 1 | 2;
  calculation_origin: "activation" | "migration";
  started_at: string;
  last_bar_at: string;
  bars: number;
  initial_equity: number;
  equity: number;
  total_return: number;
  peak_equity: number;
  max_drawdown: number;
  benchmark_equity: number;
  benchmark_total_return: number;
  benchmark_peak_equity: number;
  benchmark_max_drawdown: number;
  excess_return: number;
  position: number;
  benchmark_position: number;
  execution_delay_bars: number;
  pending_targets: number[];
  cycle_kind: "flat_to_flat" | "satellite_over_core";
  cycle_return_semantics:
    | "compounded_strategy_return"
    | "compounded_relative_to_core";
  cycle_baseline_position: number;
  pending_cycle_baseline_targets: number[];
  quality_calculation_origin:
    | "activation"
    | "cycle_semantics_migration"
    | "legacy_lot_statistics";
  quality_started_at?: string | null;
  quality_bars: number;
  quality_sample_status:
    | "tracking"
    | "awaiting_baseline_reset"
    | "legacy_pending_migration";
  quality_reset_reason: "none" | "legacy_lot_statistics_excluded";
  open_cycle_started_at?: string | null;
  open_cycle_strategy_growth: number;
  open_cycle_baseline_growth: number;
  open_cycle_holding_bars: number;
  orders: number;
  round_trips: number;
  total_friction_paid: number;
  benchmark_friction_paid: number;
  open_trade_entry_price?: number | null;
  open_lots?: Array<{
    entered_at: string;
    entry_price: number;
    position_size: number;
  }>;
  last_closed_trade_return?: number | null;
  winning_round_trips: number;
  losing_round_trips: number;
  closed_trade_return_sum: number;
  closed_trade_gain_sum: number;
  closed_trade_loss_sum: number;
  exposed_bars: number;
  exposure_sum?: number | null;
  cash_streak_bars: number;
  max_cash_streak_bars: number;
  last_bar_return: number;
  benchmark_last_bar_return: number;
  execution?: PaperForwardExecution | null;
};

export type PaperForwardHealth = {
  status: "baseline" | "collecting" | "healthy" | "watch" | "review";
  title: string;
  summary: string;
  assessment_ready: boolean;
  evidence_bars: number;
  minimum_evidence_bars: number;
  round_trips: number;
  minimum_round_trips: number;
  cycle_kind: "flat_to_flat" | "satellite_over_core";
  cycle_return_semantics:
    | "compounded_strategy_return"
    | "compounded_relative_to_core";
  quality_calculation_origin:
    | "activation"
    | "cycle_semantics_migration"
    | "legacy_lot_statistics";
  quality_sample_status:
    | "tracking"
    | "awaiting_baseline_reset"
    | "legacy_pending_migration";
  quality_started_at?: string | null;
  drawdown_limit: number;
  drawdown_limit_source:
    | "saved_constraint"
    | "holdout_reference"
    | "default_reference";
  expected_return_reference?: number | null;
  trades_per_year?: number | null;
  exposure_ratio?: number | null;
  win_rate?: number | null;
  win_rate_confidence_low?: number | null;
  win_rate_confidence_high?: number | null;
  expectancy?: number | null;
  profit_factor?: number | null;
  warning_codes: string[];
  reasons: string[];
};

export type PaperSignalSnapshot = {
  id: string;
  track_id: string;
  checked_at: string;
  data_as_of: string;
  window_start: string;
  window_end: string;
  latest_price: number;
  interval: BarInterval;
  signal_state: BacktestDiagnostics["signal_state"];
  metrics: BacktestMetrics;
  benchmark_metrics: BacktestMetrics;
  comparison: BacktestPayload["comparison"];
  diagnostics: BacktestDiagnostics;
  citations: Citation[];
  forward?: PaperForwardState | null;
};

export type PaperTrack = {
  id: string;
  experiment: ExperimentRecord;
  status: "active" | "paused";
  created_at: string;
  updated_at: string;
  last_checked_at?: string | null;
  last_error?: string | null;
  snapshot_count: number;
  observed_bar_count: number;
  forward_health?: PaperForwardHealth | null;
  snapshots: PaperSignalSnapshot[];
};

export type PaperSchedulerStatus = {
  enabled: boolean;
  running: boolean;
  poll_seconds: number;
  last_run_at?: string | null;
  last_refreshed: number;
  last_error?: string | null;
};

export type StrategyComparisonPayload = {
  symbol: string;
  interval: BarInterval;
  evaluation_mode: "template_default_snapshot";
  evaluation_note: string;
  data_metadata: Record<string, unknown>;
  bars: number;
  benchmark: {
    strategy: Strategy;
    metrics: BacktestMetrics;
  };
  results: Array<{
    strategy: Strategy;
    metrics: BacktestMetrics;
    comparison: BacktestPayload["comparison"];
    exposure_matched_benchmark_metrics: BacktestMetrics;
    timing_comparison: BacktestPayload["timing_comparison"];
    score: number;
    quality: "outperform" | "defensive" | "baseline" | "lagging" | "negative";
  }>;
  citations: Citation[];
};

export type BacktestArtifact = BacktestPayload & {
  artifact_type: "backtest";
  strategy_code?: string | null;
};

export type ResearchSnapshotArtifact = {
  artifact_type: "research_snapshot";
  symbol: string;
  name: string;
  question: string;
  as_of: string;
  bars: number;
  price: number;
  reference_series?: boolean;
  returns: {
    one_period: number;
    twenty_period: number;
    sixty_period: number;
    one_year: number;
  };
  risk: {
    annualized_volatility: number;
    max_drawdown: number;
    current_drawdown: number;
  };
  trend: {
    label: string;
    ma20?: number | null;
    ma60?: number | null;
    ma200?: number | null;
  };
  range: {
    low: number;
    high: number;
    position: number;
  };
  modules: Array<{
    id: string;
    label: string;
    status: "ready" | "unavailable";
    detail: string;
  }>;
};

export type ResearchArtifact = BacktestArtifact | ResearchSnapshotArtifact;

export type MonitorEvent = {
  source: string;
  source_id: string;
  profile_id: string;
  profile_name: string;
  kind: "social" | "news" | "filing" | "market" | "geopolitical";
  title: string;
  content: string;
  url: string;
  occurred_at: string;
  available_at: string;
  analysis?: string | null;
  market_relevance: "critical" | "high" | "medium" | "low" | "unrelated" | "unrated";
  impact_assets: Array<{
    asset: string;
    direction: "up" | "down" | "volatile" | "uncertain";
    reason: string;
  }>;
  analysis_method?: "ai" | "rules" | null;
  tags: string[];
};

export type MonitorTranslationField = {
  text: string;
  status: "translated" | "identity" | "unavailable" | "too_long";
  cache_hit: boolean;
  error_code?: string | null;
};

export type MonitorEventTranslation = {
  source: string;
  source_id: string;
  source_fingerprint?: string | null;
  status: "translated" | "identity" | "partial" | "unavailable" | "not_found";
  title_zh?: string | null;
  content_zh?: string | null;
  analysis_zh?: string | null;
  impact_reasons_zh: string[];
  fields: Record<string, MonitorTranslationField>;
  translated_fields: number;
  cached_fields: number;
  unavailable_fields: number;
};

export type MonitorTranslationResponse = {
  target_language: "zh-Hans";
  engine: string;
  availability: "disabled" | "configured" | "ready" | "degraded";
  items: MonitorEventTranslation[];
};

export type MonitorProfile = {
  id: string;
  display_name: string;
  handle?: string | null;
  cik?: string | null;
  pulse_symbol?: string | null;
  pulse_provider?: string | null;
  pulse_context?: string | null;
  category: string;
  tags: string[];
};

export type MonitorRefreshResult = {
  fetched: number;
  visible: number;
  delayed: number;
  sources: Record<string, number>;
  kinds: Record<string, number>;
  source_status: Record<string, MonitorSourceStatus>;
  analysis_method: "ai" | "rules";
};

export type MonitorSourceStatus = {
  attempted: number;
  succeeded: number;
  failed: number;
  configured?: boolean;
  message?: string;
};

export type MonitorStatus = {
  x_configured: boolean;
  analysis_configured: boolean;
  translation?: {
    enabled: boolean;
    engine: string;
    availability: "disabled" | "configured" | "ready" | "degraded";
    last_success_at?: string | null;
    last_error_code?: string | null;
  };
  scheduler: {
    enabled: boolean;
    running: boolean;
    poll_seconds: number;
    last_refresh_at?: string | null;
    last_event_count: number;
    last_error?: string | null;
    sources: Record<string, MonitorSourceStatus>;
  };
};
