const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "/backend").replace(/\/$/, "");

const FIELD_LABELS: Record<string, string> = {
  symbol: "标的",
  provider: "数据源",
  strategy_id: "策略模板",
  start: "开始日期",
  end: "结束日期",
  interval: "K 线周期",
  objective: "优化目标",
  train_ratio: "开发样本比例",
  minimum_trades_per_year: "最低年交易次数",
  maximum_trades_per_year: "最高年交易次数",
  minimum_exposure: "最低持仓率",
  minimum_annualized_return: "最低年化收益",
  maximum_drawdown: "最大回撤",
  maximum_cash_streak_bars: "连续空仓 K 线",
  walk_forward_windows: "走步窗口数",
};

type ApiValidationIssue = {
  loc?: unknown;
  msg?: unknown;
};

function fieldLabel(location: unknown): string {
  if (!Array.isArray(location)) return "请求";
  const fields = location
    .filter((part): part is string | number => typeof part === "string" || typeof part === "number")
    .filter((part) => part !== "body" && part !== "query")
    .map((part) => FIELD_LABELS[String(part)] ?? String(part));
  return fields.join(" / ") || "请求";
}

/** Turn FastAPI's structured 422 payload into an actionable user-facing error. */
export function describeApiError(
  payload: unknown,
  fallback: string,
  status?: number,
): string {
  const upstreamPrefix = "数据源或研究引擎暂不可用，请稍后重试。";
  if (!payload || typeof payload !== "object" || !("detail" in payload)) {
    return status === 502 ? upstreamPrefix : fallback;
  }
  const detail = (payload as { detail?: unknown }).detail;
  if (typeof detail === "string" && detail.trim()) {
    return status === 502 ? `${upstreamPrefix}原因：${detail}` : detail;
  }
  if (!Array.isArray(detail)) return status === 502 ? upstreamPrefix : fallback;
  const issues = detail
    .filter((item): item is ApiValidationIssue => item !== null && typeof item === "object")
    .map((item) => {
      const message = typeof item.msg === "string" ? item.msg : "参数不符合要求";
      return `${fieldLabel(item.loc)}：${message}`;
    })
    .filter(Boolean)
    .slice(0, 3);
  return issues.length > 0
    ? `请求参数有误：${issues.join("；")}`
    : status === 502
      ? upstreamPrefix
      : fallback;
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(
      describeApiError(payload, `Request failed with status ${response.status}`, response.status),
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export type StreamEvent = {
  event: string;
  data: unknown;
};

export async function apiStream(
  path: string,
  body: unknown,
  onEvent: (event: StreamEvent) => void,
): Promise<void> {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok || !response.body) {
    const payload = await response.json().catch(() => null);
    throw new Error(
      describeApiError(payload, `Request failed with status ${response.status}`, response.status),
    );
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const blocks = buffer.split(/\r?\n\r?\n/);
    buffer = blocks.pop() ?? "";
    for (const block of blocks) {
      let event = "message";
      const data: string[] = [];
      for (const line of block.split(/\r?\n/)) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      const raw = data.join("\n");
      if (!raw) continue;
      let parsed: unknown = raw;
      try {
        parsed = JSON.parse(raw);
      } catch {
        // Keep non-JSON SSE payloads as text.
      }
      onEvent({ event, data: parsed });
    }
    if (done) break;
  }
}
