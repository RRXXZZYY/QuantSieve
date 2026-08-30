import { proxyBackendRequest } from "@/lib/backend-proxy";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type RouteContext = { params: Promise<{ path: string[] }> };

async function proxy(request: Request, context: RouteContext) {
  const { path } = await context.params;
  return proxyBackendRequest(request, path);
}

export { proxy as DELETE, proxy as GET, proxy as HEAD, proxy as OPTIONS, proxy as PATCH, proxy as POST, proxy as PUT };
