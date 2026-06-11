/** Test helpers: a fetch mock keyed by "METHOD /path". */
import { vi } from "vitest";

type Responder = (req: Request) => Response | Promise<Response>;

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function errorEnvelope(message: string, status: number): Response {
  return jsonResponse({ error: { code: "error", message } }, status);
}

/**
 * Install a fetch mock. Routes map "METHOD /path" to a responder or a static
 * Response. Unmatched requests fail the test loudly.
 */
export function mockFetch(routes: Record<string, Responder | Response>): typeof fetch {
  const impl = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const req = new Request(input, init);
    const key = `${req.method} ${new URL(req.url).pathname}`;
    const route = routes[key];
    if (!route) throw new Error(`Unmocked request: ${key}`);
    return route instanceof Response ? route.clone() : route(req);
  };
  const mocked = vi.fn(impl);
  vi.stubGlobal("fetch", mocked);
  return mocked as unknown as typeof fetch;
}

export const anonymousAuthRoutes = {
  // No refresh cookie on first load.
  "POST /api/auth/refresh": () => errorEnvelope("Not authenticated", 401),
  "GET /api/auth/setup": () => jsonResponse({ setup_required: false }),
};
