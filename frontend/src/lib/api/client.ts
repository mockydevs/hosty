/**
 * Typed API client (openapi-fetch over the generated schema).
 *
 * - Injects the in-memory access token on every request.
 * - On a 401 from a non-auth endpoint, attempts ONE refresh and retries the
 *   original request with the new token.
 */
import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./schema";

let accessToken: string | null = null;

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

export function getAccessToken(): string | null {
  return accessToken;
}

type RefreshHandler = () => Promise<boolean>;
let refreshHandler: RefreshHandler | null = null;

/** Registered by the AuthProvider; returns true if a new token was obtained. */
export function setRefreshHandler(handler: RefreshHandler | null): void {
  refreshHandler = handler;
}

/** Pre-send clones so a request with a body can be retried after refresh. */
const retryClones = new WeakMap<Request, Request>();

const authMiddleware: Middleware = {
  async onRequest({ request }) {
    retryClones.set(request, request.clone());
    if (accessToken) {
      request.headers.set("Authorization", `Bearer ${accessToken}`);
    }
    return request;
  },
  async onResponse({ request, response }) {
    if (response.status !== 401) return response;
    if (new URL(request.url).pathname.startsWith("/api/auth/")) return response;
    if (!refreshHandler || !(await refreshHandler()) || !accessToken) return response;
    const clone = retryClones.get(request);
    if (!clone) return response;
    const retry = new Request(clone);
    retry.headers.set("Authorization", `Bearer ${accessToken}`);
    return fetch(retry);
  },
};

// Resolve against the page origin: relative URLs work in browsers but not in
// Node's fetch implementation (used by Vitest/jsdom).
export const api = createClient<paths>({
  baseUrl: globalThis.location?.origin ?? "http://localhost",
  credentials: "include",
  // Late-bound so test fetch stubs installed after module load are honored.
  fetch: (request) => globalThis.fetch(request),
});
api.use(authMiddleware);

/** Extract the message from the backend's error envelope, with a fallback. */
export function apiErrorMessage(error: unknown, fallback = "Something went wrong"): string {
  if (error && typeof error === "object" && "error" in error) {
    const inner = (error as { error?: { message?: unknown } }).error;
    if (inner && typeof inner.message === "string") return inner.message;
  }
  return fallback;
}
