import { LoadingState } from "@/components/states";
import { ThemeProvider, useTheme } from "@/components/theme";
import { AppLayout } from "@/layout/app-layout";
import { AuthProvider, useAuth } from "@/lib/auth";
import { DashboardPage } from "@/pages/dashboard";
import { LoginPage } from "@/pages/login";
import { SettingsPage } from "@/pages/settings";
import { BackupsPage, DatabasesPage, DnsPage, SitesPage } from "@/pages/stubs";
/**
 * Route table + providers, separated from main.tsx so tests can mount the
 * whole app inside a MemoryRouter.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router";
import { Toaster } from "sonner";

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
              <Route path="/databases" element={<DatabasesPage />} />
              <Route path="/dns" element={<DnsPage />} />
              <Route path="/backups" element={<BackupsPage />} />
              <Route path="/settings" element={<SettingsPage />} />
            </Route>
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
          <ThemedToaster />
        </AuthProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
