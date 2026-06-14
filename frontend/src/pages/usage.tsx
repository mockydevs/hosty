import { EmptyState, ErrorState, LoadingState } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, apiErrorMessage } from "@/lib/api/client";
import { useAuth } from "@/lib/auth";
import { formatBytes } from "@/lib/format";
/**
 * Usage (Phase 11c/11d): clients see their own sites' disk/DB/bandwidth;
 * admins additionally get the per-client summary and a CSV export.
 */
import { useQuery } from "@tanstack/react-query";
import { Download } from "lucide-react";
import { useState } from "react";

function monthDefault(): string {
  return new Date().toISOString().slice(0, 7);
}

function MyUsage({ month }: { month: string }) {
  const my = useQuery({
    queryKey: ["usage", "me", month],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/usage/me", {
        params: { query: { month } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load usage"));
      return data; /* as any */
    },
  });

  if (my.isPending) return <LoadingState label="Measuring usage…" />;
  if (my.isError) return <ErrorState message={my.error.message} onRetry={() => my.refetch()} />;

  const data = my.data;
  const diskLimit = data.max_disk_mb !== null ? data.max_disk_mb * 1024 * 1024 : null;
  const overQuota = diskLimit !== null && data.disk_bytes > diskLimit;

  return (
    <div className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-3">
        <Card>
          <CardHeader>
            <CardDescription>Disk</CardDescription>
            <CardTitle className="text-xl">
              {formatBytes(data.disk_bytes)}
              {diskLimit !== null && (
                <span className="ml-1 text-sm font-normal text-muted-foreground">
                  of {formatBytes(diskLimit)}
                </span>
              )}
              {overQuota && (
                <Badge variant="destructive" className="ml-2">
                  Over quota
                </Badge>
              )}
            </CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Databases</CardDescription>
            <CardTitle className="text-xl">{formatBytes(data.db_bytes)}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Bandwidth ({month})</CardDescription>
            <CardTitle className="text-xl">{formatBytes(data.bandwidth_bytes)}</CardTitle>
          </CardHeader>
        </Card>
      </div>

      {data.sites.length === 0 ? (
        <EmptyState title="No sites yet" description="Usage shows up once you host something." />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Site</TableHead>
              <TableHead>Disk</TableHead>
              <TableHead>Databases</TableHead>
              <TableHead>Bandwidth</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.sites.map((s) => (
              <TableRow key={s.site_id}>
                <TableCell className="font-medium">{s.domain}</TableCell>
                <TableCell>{formatBytes(s.disk_bytes)}</TableCell>
                <TableCell>{formatBytes(s.db_bytes)}</TableCell>
                <TableCell>{formatBytes(s.bandwidth_bytes)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}

/** The endpoint needs the bearer token, so a plain <a download> won't do. */
async function exportCsv(month: string): Promise<void> {
  const { data, error } = await api.GET("/api/usage/export", {
    params: { query: { month } },
    parseAs: "text",
  });
  if (error || data === undefined) return;
  const blob = new Blob([data], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `hosty-usage-${month}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

function AllClientsUsage({ month }: { month: string }) {
  const all = useQuery({
    queryKey: ["usage", "all", month],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/usage", {
        params: { query: { month } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load usage"));
      return data; /* as any */
    },
  });

  if (all.isPending) return <LoadingState label="Measuring usage…" />;
  if (all.isError) return <ErrorState message={all.error.message} onRetry={() => all.refetch()} />;

  return (
    <section aria-label="Per-client usage" className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-lg font-semibold tracking-tight">Per client</h2>
        <Button variant="outline" size="sm" onClick={() => exportCsv(month)}>
          <Download className="h-4 w-4" aria-hidden /> Export CSV
        </Button>
      </div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Client</TableHead>
            <TableHead>Sites</TableHead>
            <TableHead>Disk</TableHead>
            <TableHead className="hidden sm:table-cell">Databases</TableHead>
            <TableHead>Bandwidth</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {all.data.map((c) => {
            const limit = c.max_disk_mb !== null ? c.max_disk_mb * 1024 * 1024 : null;
            const over = limit !== null && c.disk_bytes > limit;
            return (
              <TableRow key={c.user_id}>
                <TableCell className="font-medium">{c.username}</TableCell>
                <TableCell>{c.site_count}</TableCell>
                <TableCell>
                  {formatBytes(c.disk_bytes)}
                  {limit !== null && (
                    <span className="text-muted-foreground"> / {formatBytes(limit)}</span>
                  )}
                  {over && (
                    <Badge variant="destructive" className="ml-2">
                      Over
                    </Badge>
                  )}
                </TableCell>
                <TableCell className="hidden sm:table-cell">{formatBytes(c.db_bytes)}</TableCell>
                <TableCell>{formatBytes(c.bandwidth_bytes)}</TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </section>
  );
}

export function UsagePage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const [month, setMonth] = useState(monthDefault());

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Usage</h1>
          <p className="text-sm text-muted-foreground">
            Disk and database sizes are measured live; bandwidth comes from the web server's access
            log for the selected month.
          </p>
        </div>
        <label className="flex items-center gap-2 text-sm" htmlFor="usage-month">
          Month
          <Input
            id="usage-month"
            type="month"
            className="w-40"
            value={month}
            onChange={(e) => setMonth(e.target.value || monthDefault())}
          />
        </label>
      </div>

      <MyUsage month={month} />
      {isAdmin && <AllClientsUsage month={month} />}
    </div>
  );
}
