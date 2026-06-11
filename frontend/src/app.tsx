import { LoadingState } from "@/components/states";
import { ThemeProvider, useTheme } from "@/components/theme";
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
import { Navigate, Route, Routes, useLocation } from "react-router";
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
const BackupsPage = lazy(() => import("@/pages/backups").then((m) => ({ default: m.BackupsPage })));
const SettingsPage = lazy(() =>
  import("@/pages/settings").then((m) => ({ default: m.SettingsPage })),
);

function RequireAuth({ children }: { children: ReactNode }) {
  const { status } = useAuth();
  const location = useLocation();
  if (status === "loading") return <LoadingState full />;
  if (status === "anonymous") {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
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
                <Route path="/dns" element={<DnsPage />} />
                <Route path="/dns/:zoneId" element={<DnsZonePage />} />
                <Route path="/backups" element={<BackupsPage />} />
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
