import { describe, expect, it, vi } from "vitest";

import { backendTargetUrl, proxyBackendRequest } from "./backend-proxy";

describe("backend proxy", () => {
  it("keeps the query string while encoding route segments", () => {
    expect(
      backendTargetUrl("http://localhost/backend/api/v1/market?provider=binance", [
        "api",
        "v1",
        "market",
        "BTC/USDT",
      ]),
    ).toBe("http://127.0.0.1:8000/api/v1/market/BTC%2FUSDT?provider=binance");
  });

  it("passes an upstream 422 body through unchanged", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "Binance public market data is unavailable." }), {
        status: 422,
        headers: { "content-type": "application/json", connection: "close" },
      }),
    );
    const response = await proxyBackendRequest(
      new Request("http://localhost/backend/api/v1/backtests/discover", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: '{"symbol":"BTCUSDT"}',
      }),
      ["api", "v1", "backtests", "discover"],
      fetchImpl,
    );

    expect(response.status).toBe(422);
    await expect(response.json()).resolves.toEqual({
      detail: "Binance public market data is unavailable.",
    });
    expect(fetchImpl).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/v1/backtests/discover",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("returns a readable 502 only when the engine cannot be reached", async () => {
    const response = await proxyBackendRequest(
      new Request("http://localhost/backend/api/v1/health"),
      ["api", "v1", "health"],
      vi.fn().mockRejectedValue(new TypeError("socket hang up")),
    );

    expect(response.status).toBe(502);
    await expect(response.json()).resolves.toEqual({
      detail: "研究引擎暂时无法连接，请稍后重试。",
    });
  });
});
