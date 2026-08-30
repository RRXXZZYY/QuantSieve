import type {
  BacktestPayload,
  ExperimentCreatePayload,
  ExperimentFromRunPayload,
} from "./types";

const LEGACY_EXPERIMENT_PATH = "/api/v1/experiments" as const;
const VERIFIED_EXPERIMENT_PATH = "/api/v1/experiments/from-run" as const;

type BacktestRunReceipt = Pick<BacktestPayload, "run_id" | "run_expires_at">;

export type BacktestExperimentSaveRequest =
  | {
      provenance: "server_verified";
      path: typeof VERIFIED_EXPERIMENT_PATH;
      body: ExperimentFromRunPayload;
    }
  | {
      provenance: "legacy_unverified";
      path: typeof LEGACY_EXPERIMENT_PATH;
      body: ExperimentCreatePayload;
    };

export function backtestRunReceiptHasExpired(
  expiresAt: string | undefined,
  now = Date.now(),
): boolean {
  if (!expiresAt) return false;
  const expiresAtMs = Date.parse(expiresAt);
  return Number.isFinite(expiresAtMs) && expiresAtMs <= now;
}

export function buildBacktestExperimentSaveRequest({
  run,
  name,
  notes,
  instrumentName,
  legacyPayload,
  now,
}: {
  run: BacktestRunReceipt;
  name: string;
  notes?: string | null;
  instrumentName: string;
  legacyPayload: ExperimentCreatePayload | null;
  now?: number;
}): BacktestExperimentSaveRequest {
  const normalizedName = name.trim();
  if (!normalizedName) {
    throw new RangeError("实验名称不能为空。");
  }

  const rawRunId: unknown = run.run_id;
  if (rawRunId !== undefined) {
    if (typeof rawRunId !== "string" || !rawRunId.trim()) {
      throw new RangeError("服务端运行回执无效，请重新运行后再保存。");
    }
    if (backtestRunReceiptHasExpired(run.run_expires_at, now)) {
      throw new RangeError("服务端运行回执已过期，请重新运行后再保存。");
    }
    const normalizedInstrumentName = instrumentName.trim();
    if (!normalizedInstrumentName) {
      throw new RangeError("实验标的名称不能为空。");
    }
    return {
      provenance: "server_verified",
      path: VERIFIED_EXPERIMENT_PATH,
      body: {
        name: normalizedName,
        notes: notes?.trim() || null,
        run_id: rawRunId.trim(),
        instrument_name: normalizedInstrumentName,
      },
    };
  }

  if (!legacyPayload) {
    throw new RangeError("旧版结果缺少可保存的实验快照，请重新运行后再保存。");
  }
  return {
    provenance: "legacy_unverified",
    path: LEGACY_EXPERIMENT_PATH,
    body: legacyPayload,
  };
}
