import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";

const apiInternalUrl = (
  process.env.QUANTSIEVE_API_INTERNAL_URL ?? "http://127.0.0.1:8000"
).replace(/\/$/, "");

const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

function copyHeaders(source: Headers, excluded: Set<string>) {
  const headers = new Headers();
  source.forEach((value, key) => {
    if (!excluded.has(key.toLowerCase()) && !HOP_BY_HOP_HEADERS.has(key.toLowerCase())) {
      headers.set(key, value);
    }
  });
  return headers;
}

function responseHeaders(source: Record<string, string | string[] | undefined>) {
  const headers = new Headers();
  for (const [key, value] of Object.entries(source)) {
    if (!value || HOP_BY_HOP_HEADERS.has(key.toLowerCase()) || key === "content-length") {
      continue;
    }
    for (const item of Array.isArray(value) ? value : [value]) {
      headers.append(key, item);
    }
  }
  return headers;
}

/**
 * Next's undici-based fetch treats an upstream close after a valid 4xx response
 * as a network failure in this deployment. Node's HTTP client exposes the
 * completed response instead, including its useful JSON error detail.
 */
async function nodeFetch(input: string | URL, init?: RequestInit): Promise<Response> {
  const target = new URL(input.toString());
  const body = init?.body;
  const payload = body === undefined || body === null ? undefined : Buffer.from(body as ArrayBuffer);
  const requestHeaders = Object.fromEntries(new Headers(init?.headers).entries());
  const send = target.protocol === "https:" ? httpsRequest : httpRequest;

  return new Promise<Response>((resolve, reject) => {
    const upstreamRequest = send(
      {
        protocol: target.protocol,
        hostname: target.hostname,
        port: target.port || undefined,
        path: `${target.pathname}${target.search}`,
        method: init?.method,
        headers: requestHeaders,
      },
      (upstreamResponse) => {
        const chunks: Buffer[] = [];
        let settled = false;
        const finish = () => {
          if (settled) {
            return;
          }
          settled = true;
          resolve(
            new Response(Buffer.concat(chunks), {
              status: upstreamResponse.statusCode ?? 502,
              statusText: upstreamResponse.statusMessage,
              headers: responseHeaders(upstreamResponse.headers),
            }),
          );
        };
        upstreamResponse.on("data", (chunk: Buffer) => chunks.push(chunk));
        upstreamResponse.on("error", (error) => {
          if (chunks.length > 0) {
            finish();
            return;
          }
          reject(error);
        });
        upstreamResponse.on("end", finish);
        // Some upstream 4xx responses deliberately close the socket instead of
        // emitting `end`. Their headers/body are already available, so retain
        // the diagnostic response rather than turning it into a 502.
        upstreamResponse.on("close", finish);
      },
    );
    upstreamRequest.on("error", reject);
    if (payload) {
      upstreamRequest.write(payload);
    }
    upstreamRequest.end();
  });
}

export function backendTargetUrl(requestUrl: string, path: string[]) {
  const query = new URL(requestUrl).search;
  const pathname = path.map((segment) => encodeURIComponent(segment)).join("/");
  return `${apiInternalUrl}/${pathname}${query}`;
}

/**
 * Forward API calls without relying on a Next rewrite.  The rewrite proxy can
 * turn an intentional upstream 4xx response into a generic 500 when the API
 * closes its HTTP connection after the response.  A route handler reads the
 * response itself, so the workbench retains the status and useful detail.
 */
export async function proxyBackendRequest(
  request: Request,
  path: string[],
  fetchImpl: typeof fetch = nodeFetch as typeof fetch,
) {
  const method = request.method.toUpperCase();
  const body = method === "GET" || method === "HEAD" ? undefined : await request.arrayBuffer();
  const headers = copyHeaders(request.headers, new Set(["host", "content-length"]));

  try {
    const upstream = await fetchImpl(backendTargetUrl(request.url, path), {
      method,
      headers,
      body,
      redirect: "manual",
    });
    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: copyHeaders(upstream.headers, new Set(["content-length"])),
    });
  } catch {
    return Response.json(
      { detail: "研究引擎暂时无法连接，请稍后重试。" },
      { status: 502 },
    );
  }
}
