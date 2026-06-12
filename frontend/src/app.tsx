import { LoadingState } from "@/components/states";
import { ThemeProvider, useTheme } from "@/components/theme";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { AppLayout } from "@/layout/app-layout";
import { AuthProvider, useAuth } from "@/lib/auth";
/**
 * Route table + providers, separated from main.tsx so tests can mount the
 * whole app inside a MemoryRouter.
 *
 * Pages are lazy (Week 24): each route ships as its own chunk so the initial
 * bundle stays inside the 300KB budget (login included: it pulls the whole
 * form stack — zod, react-hook-form — which authenticated reloads never need).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode, Suspense, lazy, useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router";
import { Toaster } from "sonner";

const LoginPage = lazy(() => import("@/pages/login").then((m) => ({ default: m.LoginPage })));
const DashboardPage = lazy(() =>
  import("@/pages/dashboard").then((m) => ({ default: m.DashboardPage })),
);
const SitesPage = lazy(() => import("@/pages/sites").then((m) => ({ default: m.SitesPage })));
const SiteDetailPage = lazy(() =>
  import("@/pages/site-detail").then((m) => ({ default: m.SiteDetailPage })),
);
const DatabasesPage = lazy(() =>
  import("@/pages/databases").then((m) => ({ default: m.DatabasesPage })),
);
const DnsPage = lazy(() => import("@/pages/dns").then((m) => ({ default: m.DnsPage })));
const DnsZonePage = lazy(() => import("@/pages/dns").then((m) => ({ default: m.DnsZonePage })));
const CloudflareZonesPage = lazy(() =>
  import("@/pages/cloudflare").then((m) => ({ default: m.CloudflareZonesPage })),
);
const CloudflareZonePage = lazy(() =>
  import("@/pages/cloudflare").then((m) => ({ default: m.CloudflareZonePage })),
);
const BackupsPage = lazy(() => import("@/pages/backups").then((m) => ({ default: m.BackupsPage })));
const SettingsPage = lazy(() =>
  import("@/pages/settings").then((m) => ({ default: m.SettingsPage })),
);
const AuditPage = lazy(() => import("@/pages/audit").then((m) => ({ default: m.AuditPage })));
const UsersPage = lazy(() => import("@/pages/users").then((m) => ({ default: m.UsersPage })));
const ChangePasswordForm = lazy(() =>
  import("@/pages/settings").then((m) => ({ default: m.ChangePasswordForm })),
);

/** Phase 11a: accounts with a temporary password must change it before
 * anything else — the API blocks every other endpoint with 403 anyway. */
function ForcedPasswordChange() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle>Set a new password</CardTitle>
          <CardDescription>
            Hi {user?.username} — your account uses a temporary password. Choose your own to
            continue; you will then log in again with it.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Suspense fallback={<LoadingState />}>
            <ChangePasswordForm
              onChanged={async () => {
                await logout();
                navigate("/login");
              }}
            />
          </Suspense>
        </CardContent>
      </Card>
    </div>
  );
}

function RequireAuth({ children }: { children: ReactNode }) {
  const { status, user } = useAuth();
  const location = useLocation();
  if (status === "loading") return <LoadingState full />;
  if (status === "anonymous") {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  if (user?.must_change_password) return <ForcedPasswordChange />;
  return <>{children}</>;
}

/** Admin-only routes render a redirect for clients (the API enforces 403 anyway). */
function RequireAdmin({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  if (user && user.role !== "admin") return <Navigate to="/" replace />;
  return <>{children}</>;
}

function ThemedToaster() {
  const { resolved } = useTheme();
  return <Toaster richColors position="top-right" theme={resolved} />;
}

export function App() {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
      }),
  );

  return (
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <Suspense fallback={<LoadingState full />}>
            <Routes>
              <Route path="/login" element={<LoginPage />} />
              <Route
                element={
                  <RequireAuth>
                    <AppLayout />
                  </RequireAuth>
                }
              >
                <Route index element={<DashboardPage />} />
                <Route path="/sites" element={<SitesPage />} />
                <Route path="/sites/:siteId" element={<SiteDetailPage />} />
                <Route path="/databases" element={<DatabasesPage />} />
                <Route
                  path="/dns"
                  element={
                    <RequireAdmin>
                      <DnsPage />
                    </RequireAdmin>
                  }
                />
                <Route
                  path="/dns/cloudflare"
                  element={
                    <RequireAdmin>
                      <CloudflareZonesPage />
                    </RequireAdmin>
                  }
                />
                <Route
                  path="/dns/cloudflare/:cfZoneId"
                  element={
                    <RequireAdmin>
                      <CloudflareZonePage />
                    </RequireAdmin>
                  }
                />
                <Route
                  path="/dns/:zoneId"
                  element={
                    <RequireAdmin>
                      <DnsZonePage />
                    </RequireAdmin>
                  }
                />
                <Route path="/backups" element={<BackupsPage />} />
                <Route path="/audit" element={<AuditPage />} />
                <Route
                  path="/users"
                  element={
                    <RequireAdmin>
                      <UsersPage />
                    </RequireAdmin>
                  }
                />
                <Route path="/settings" element={<SettingsPage />} />
              </Route>
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Suspense>
          <ThemedToaster />
        </AuthProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
