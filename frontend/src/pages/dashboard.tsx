import { ErrorState } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogActions,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { api, apiErrorMessage } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { formatBytes, formatUptime } from "@/lib/format";
/**
 * Dashboard: live resource gauges, managed service status, and recent audited activity.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Cpu, HardDrive, MemoryStick, RotateCcw, ScrollText } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";

type ServiceStatus = components["schemas"]["ServiceStatusResponse"];
type AuditEntry = components["schemas"]["AuditEntryResponse"];

const SERVICE_LABELS: Record<string, string> = {
  caddy: "Caddy",
  mariadb: "MariaDB",
  "php8.3-fpm": "PHP-FPM 8.3",
  pdns: "PowerDNS",
};

function serviceLabel(unit: string): string {
  return SERVICE_LABELS[unit] ?? unit;
}

function statusVariant(code: number): "success" | "secondary" | "destructive" {
  if (code < 300) return "success";
  if (code < 500) return "secondary";
  return "destructive";
}

function activityActor(entry: AuditEntry): string {
  return entry.username ?? "anonymous";
}

function activityLabel(entry: AuditEntry): string {
  return `${entry.method} ${entry.path}`;
}

function Gauge({
  title,
  icon: Icon,
  percent,
  detail,
}: {
  title: string;
  icon: typeof Cpu;
  percent: number;
  detail: string;
}) {
  const tone = percent >= 90 ? "text-destructive" : percent >= 75 ? "text-yellow-500" : "";
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium">{title}</CardTitle>
        <Icon className="h-4 w-4 text-muted-foreground" aria-hidden />
      </CardHeader>
      <CardContent className="space-y-2">
        <p className={`text-2xl font-semibold ${tone}`}>{percent.toFixed(0)}%</p>
        <Progress value={percent} aria-label={`${title} usage`} />
        <p className="text-xs text-muted-foreground">{detail}</p>
      </CardContent>
    </Card>
  );
}

function ServiceCard({ service }: { service: ServiceStatus }) {
  const [confirmOpen, setConfirmOpen] = useState(false);
  const queryClient = useQueryClient();
  const running = service.active_state === "active";

  const restart = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/system/services/{unit}/actions/{action}", {
        params: { path: { unit: service.unit, action: "restart" } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Restart failed"));
      return data;
    },
    onSuccess: () => {
      toast.success(`${serviceLabel(service.unit)} restarted`);
      void queryClient.invalidateQueries({ queryKey: ["system", "services"] });
    },
    onError: (err) => toast.error(err.message),
    onSettled: () => setConfirmOpen(false),
  });

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium">{serviceLabel(service.unit)}</CardTitle>
        {service.available ? (
          <Badge variant={running ? "success" : "destructive"}>
            {running ? "Running" : service.active_state}
          </Badge>
        ) : (
          <Badge variant="outline">Not installed</Badge>
        )}
      </CardHeader>
      <CardContent className="flex items-center justify-between">
        <p className="text-xs text-muted-foreground">
          {service.available ? `${service.sub_state} / ${service.enabled}` : "Unavailable"}
        </p>
        {service.available && (
          <Button
            variant="ghost"
            size="icon"
            aria-label={`Restart ${serviceLabel(service.unit)}`}
            onClick={() => setConfirmOpen(true)}
          >
            <RotateCcw className="h-4 w-4" />
          </Button>
        )}
      </CardContent>
      <Dialog open={confirmOpen} onClose={() => setConfirmOpen(false)}>
        <DialogContent>
          <DialogTitle>Restart {serviceLabel(service.unit)}?</DialogTitle>
          <DialogDescription>
            Active connections to this service may be dropped while it restarts.
          </DialogDescription>
          <DialogActions>
            <Button variant="outline" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              loading={restart.isPending}
              onClick={() => restart.mutate()}
            >
              Restart
            </Button>
          </DialogActions>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function RecentActivityCard() {
  const audit = useQuery({
    queryKey: ["audit", "recent"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/audit", {
        params: { query: { limit: 5, offset: 0 } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load recent activity"));
      return data;
    },
    refetchInterval: 15_000,
  });

  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-4 space-y-0">
        <div>
          <CardTitle className="flex items-center gap-2 text-base">
            <ScrollText className="h-4 w-4" aria-hidden /> Recent activity
          </CardTitle>
          <CardDescription>Latest audited panel changes.</CardDescription>
        </div>
        <Link
          to="/settings"
          className="inline-flex h-8 items-center justify-center rounded-md border border-border px-3 text-xs font-medium transition-colors hover:bg-accent hover:text-accent-foreground"
        >
          View audit log
        </Link>
      </CardHeader>
      <CardContent>
        {audit.isPending ? (
          <div className="space-y-3">
            {["a", "b", "c"].map((key) => (
              <div key={key} className="flex items-center justify-between gap-3">
                <Skeleton className="h-4 w-56" />
                <Skeleton className="h-5 w-12" />
              </div>
            ))}
          </div>
        ) : audit.isError ? (
          <ErrorState message={audit.error.message} onRetry={() => audit.refetch()} />
        ) : audit.data.entries.length === 0 ? (
          <p className="py-4 text-sm text-muted-foreground">No audited activity yet.</p>
        ) : (
          <div className="divide-y divide-border">
            {audit.data.entries.map((entry) => (
              <div
                key={entry.id}
                className="grid gap-2 py-3 text-sm sm:grid-cols-[minmax(0,1fr)_auto]"
              >
                <div className="min-w-0">
                  <p className="truncate font-mono text-xs">{activityLabel(entry)}</p>
                  <p className="text-xs text-muted-foreground">
                    {activityActor(entry)} / {new Date(entry.created_at).toLocaleString()}
                  </p>
                </div>
                <Badge variant={statusVariant(entry.status_code)}>{entry.status_code}</Badge>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function GaugeSkeletons() {
  return (
    <>
      {["cpu", "ram", "disk"].map((k) => (
        <Card key={k}>
          <CardHeader className="pb-2">
            <Skeleton className="h-4 w-24" />
          </CardHeader>
          <CardContent className="space-y-2">
            <Skeleton className="h-8 w-16" />
            <Skeleton className="h-2 w-full" />
          </CardContent>
        </Card>
      ))}
    </>
  );
}

export function DashboardPage() {
  const stats = useQuery({
    queryKey: ["system", "stats"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/system/stats");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load system stats"));
      return data;
    },
    refetchInterval: 5_000,
  });

  const services = useQuery({
    queryKey: ["system", "services"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/system/services");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load services"));
      return data;
    },
    refetchInterval: 10_000,
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
        {stats.data && (
          <p className="text-sm text-muted-foreground">
            Up {formatUptime(stats.data.uptime_seconds)} / load{" "}
            {stats.data.load_avg.map((n) => n.toFixed(2)).join(" / ")}
          </p>
        )}
      </div>

      <section aria-label="Resource usage" className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {stats.isPending ? (
          <GaugeSkeletons />
        ) : stats.isError ? (
          <div className="sm:col-span-2 lg:col-span-3">
            <ErrorState message={stats.error.message} onRetry={() => stats.refetch()} />
          </div>
        ) : (
          <>
            <Gauge
              title="CPU"
              icon={Cpu}
              percent={stats.data.cpu_percent}
              detail={`load ${stats.data.load_avg[0]?.toFixed(2) ?? "?"}`}
            />
            <Gauge
              title="Memory"
              icon={MemoryStick}
              percent={stats.data.memory_percent}
              detail={`${formatBytes(stats.data.memory_used)} of ${formatBytes(stats.data.memory_total)}`}
            />
            <Gauge
              title="Disk"
              icon={HardDrive}
              percent={stats.data.disk_percent}
              detail={`${formatBytes(stats.data.disk_used)} of ${formatBytes(stats.data.disk_total)}`}
            />
          </>
        )}
      </section>

      <section aria-label="Services" className="space-y-3">
        <h2 className="text-lg font-medium">Services</h2>
        {services.isPending ? (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {["a", "b", "c", "d"].map((k) => (
              <Card key={k}>
                <CardHeader className="pb-2">
                  <Skeleton className="h-4 w-20" />
                </CardHeader>
                <CardContent>
                  <Skeleton className="h-4 w-28" />
                </CardContent>
              </Card>
            ))}
          </div>
        ) : services.isError ? (
          <ErrorState message={services.error.message} onRetry={() => services.refetch()} />
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {services.data.map((s) => (
              <ServiceCard key={s.unit} service={s} />
            ))}
          </div>
        )}
      </section>

      <RecentActivityCard />
    </div>
  );
}
