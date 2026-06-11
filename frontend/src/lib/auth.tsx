import { api, setAccessToken, setRefreshHandler } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
/**
 * Auth context: bootstraps the session from the refresh cookie, exposes
 * login/logout, and registers the refresh handler used by the API client
 * to transparently retry 401s.
 */
import {
  type ReactNode,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

export type User = components["schemas"]["UserResponse"];

type AuthStatus = "loading" | "authenticated" | "anonymous";

interface AuthContextValue {
  status: AuthStatus;
  user: User | null;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** Re-fetch /me (e.g. after setup). */
  reload: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/** POST /api/auth/refresh; stores the new access token. Returns success. */
async function tryRefresh(): Promise<boolean> {
  const { data, error } = await api.POST("/api/auth/refresh");
  if (error || !data) {
    setAccessToken(null);
    return false;
  }
  setAccessToken(data.access_token);
  return true;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [user, setUser] = useState<User | null>(null);
  // Single-flight: concurrent 401s must not fire parallel refreshes.
  const refreshing = useRef<Promise<boolean> | null>(null);

  const refresh = useCallback(async (): Promise<boolean> => {
    refreshing.current ??= tryRefresh().finally(() => {
      refreshing.current = null;
    });
    return refreshing.current;
  }, []);

  const loadUser = useCallback(async () => {
    const { data, error } = await api.GET("/api/auth/me");
    if (error || !data) {
      setAccessToken(null);
      setUser(null);
      setStatus("anonymous");
      return;
    }
    setUser(data);
    setStatus("authenticated");
  }, []);

  // Bootstrap: refresh cookie → access token → /me.
  useEffect(() => {
    setRefreshHandler(refresh);
    void (async () => {
      if (await refresh()) {
        await loadUser();
      } else {
        setStatus("anonymous");
      }
    })();
    return () => setRefreshHandler(null);
  }, [refresh, loadUser]);

  const login = useCallback(
    async (username: string, password: string) => {
      const { data, error, response } = await api.POST("/api/auth/login", {
        body: { username, password },
      });
      if (error || !data) {
        throw new ApiError(error, response.status);
      }
      setAccessToken(data.access_token);
      await loadUser();
    },
    [loadUser],
  );

  const logout = useCallback(async () => {
    await api.POST("/api/auth/logout").catch(() => undefined);
    setAccessToken(null);
    setUser(null);
    setStatus("anonymous");
  }, []);

  const value = useMemo(
    () => ({ status, user, login, logout, reload: loadUser }),
    [status, user, login, logout, loadUser],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(body: unknown, status: number) {
    super(extractMessage(body, status));
    this.name = "ApiError";
    this.status = status;
  }
}

function extractMessage(body: unknown, status: number): string {
  if (body && typeof body === "object" && "error" in body) {
    const inner = (body as { error?: { message?: unknown } }).error;
    if (inner && typeof inner.message === "string") return inner.message;
  }
  return status === 429 ? "Too many attempts — try again shortly" : "Request failed";
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
