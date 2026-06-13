import { ThemeToggle } from "@/components/theme";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";
import {
  Archive,
  Boxes,
  Database,
  Gauge,
  Globe,
  Key,
  LayoutDashboard,
  LogOut,
  Menu,
  Network,
  ScrollText,
  Server,
  Settings,
  UsersRound,
  X,
} from "lucide-react";
import { useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router";
import { toast } from "sonner";

const NAV = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard, end: true },
  { to: "/sites", label: "Sites", icon: Globe },
  // v2 (ADR-013): blueprint-deployed container workloads.
  { to: "/stacks", label: "Deployments", icon: Boxes },
  { to: "/databases", label: "Databases", icon: Database },
  // Phase 11b: DNS zones are tenant-scoped — clients manage their own.
  { to: "/dns", label: "DNS", icon: Network },
  { to: "/backups", label: "Backups", icon: Archive },
  { to: "/usage", label: "Usage", icon: Gauge },
  { to: "/audit", label: "Audit log", icon: ScrollText },
  { to: "/users", label: "Users", icon: UsersRound, adminOnly: true },
  { to: "/security", label: "Keys & Tokens", icon: Key },
  { to: "/settings", label: "Settings", icon: Settings },
] as const;

/** Phase 11d: loud, persistent banner while an admin acts as a client. */
function ImpersonationBanner() {
  const { user, stopImpersonating } = useAuth();
  if (!user?.impersonated_by) return null;
  return (
    <div
      role="alert"
      className="flex items-center justify-between gap-3 bg-amber-500 px-4 py-2 text-sm font-medium text-black"
    >
      <span>
        Impersonating <strong>{user.username}</strong> as {user.impersonated_by} — every action is
        audited.
      </span>
      <Button
        variant="outline"
        size="sm"
        className="border-black/30 bg-transparent text-black hover:bg-black/10"
        onClick={async () => {
          await stopImpersonating();
          toast.success("Back to your own session");
        }}
      >
        Stop impersonating
      </Button>
    </div>
  );
}

function NavItems({ onNavigate }: { onNavigate?: () => void }) {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const items = NAV.filter((item) => !("adminOnly" in item && item.adminOnly) || isAdmin);
  return (
    <nav className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto p-3" aria-label="Main">
      {items.map(({ to, label, icon: Icon, ...rest }) => (
        <NavLink
          key={to}
          to={to}
          end={"end" in rest}
          onClick={onNavigate}
          className={({ isActive }) =>
            cn(
              "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
              isActive
                ? "bg-accent text-accent-foreground"
                : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
            )
          }
        >
          <Icon className="h-4 w-4" aria-hidden />
          {label}
        </NavLink>
      ))}
    </nav>
  );
}

function SidebarFooter() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  return (
    <div className="flex shrink-0 items-center justify-between border-t border-border p-3">
      <div className="min-w-0">
        <p className="truncate text-sm font-medium">{user?.username}</p>
        <p className="text-xs text-muted-foreground">{user?.role}</p>
      </div>
      <div className="flex items-center gap-1">
        <ThemeToggle />
        <Button
          variant="ghost"
          size="icon"
          aria-label="Log out"
          onClick={async () => {
            await logout();
            navigate("/login");
          }}
        >
          <LogOut className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}

function Brand() {
  return (
    <div className="flex items-center gap-2 px-5 py-4">
      <Server className="h-5 w-5" aria-hidden />
      <span className="text-lg font-semibold tracking-tight">HostyPanel</span>
    </div>
  );
}

export function AppLayout() {
  const [mobileOpen, setMobileOpen] = useState(false);

  return (
    <div className="min-h-dvh">
      {/* Desktop sidebar */}
      <aside className="fixed inset-y-0 left-0 hidden w-60 shrink-0 flex-col border-r border-border bg-background md:flex">
        <Brand />
        <NavItems />
        <SidebarFooter />
      </aside>

      {/* Mobile top bar */}
      <div className="fixed inset-x-0 top-0 z-40 flex items-center justify-between border-b border-border bg-background px-4 py-2 md:hidden">
        <Brand />
        <Button
          variant="ghost"
          size="icon"
          aria-label="Open menu"
          onClick={() => setMobileOpen(true)}
        >
          <Menu className="h-5 w-5" />
        </Button>
      </div>

      {/* Mobile drawer */}
      {mobileOpen && (
        <div className="fixed inset-0 z-50 md:hidden">
          <button
            type="button"
            aria-label="Close menu"
            className="absolute inset-0 bg-black/50"
            onClick={() => setMobileOpen(false)}
          />
          <div className="absolute inset-y-0 left-0 flex h-dvh w-72 flex-col border-r border-border bg-background shadow-lg">
            <div className="flex items-center justify-between pr-2">
              <Brand />
              <Button
                variant="ghost"
                size="icon"
                aria-label="Close menu"
                onClick={() => setMobileOpen(false)}
              >
                <X className="h-5 w-5" />
              </Button>
            </div>
            <NavItems onNavigate={() => setMobileOpen(false)} />
            <SidebarFooter />
          </div>
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col md:pl-60">
        <div className="pt-14 md:pt-0">
          <ImpersonationBanner />
        </div>
        <main className="flex-1 px-4 pb-8 pt-6 md:px-8 md:pt-8">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
