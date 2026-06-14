import {
  DeleteBackupDialog,
  OperationDialog,
  RestoreDialog,
  S3ConfigDialog,
  ScheduleDialog,
} from "@/components/backup-dialogs";
import { EmptyState, ErrorState, LoadingState } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, apiErrorMessage } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { formatBytes } from "@/lib/format";
/**
 * Backups (Phase 8): per-site schedules, run-now, restore wizard, retention.
 */
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Archive,
  CalendarClock,
  CloudUpload,
  Play,
  RotateCcw,
  Settings2,
  Trash2,
} from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";

type BackupEntry = components["schemas"]["BackupResponse"];
type Site = components["schemas"]["SiteResponse"];

function formatBackupTime(backupId: string): string {
  // 20260611T031500Z -> 2026-06-11 03:15 UTC
  const m = backupId.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})\d{2}Z$/);
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]} UTC` : backupId;
}

function RunNowButton({
  site,
  onStarted,
}: {
  site: Site;
  onStarted: (operationId: number) => void;
}) {
  const run = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/sites/{site_id}/backups", {
        params: { path: { site_id: site.id } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Could not start the backup"));
      return data; /* as any */
    },
    onSuccess: (data) => onStarted(data.operation_id),
    onError: (err) => toast.error(err.message),
  });
  return (
    <Button
      variant="outline"
      size="sm"
      loading={run.isPending}
      onClick={() => run.mutate()}
      aria-label={`Back up ${site.domain} now`}
    >
      <Play className="h-4 w-4" aria-hidden /> Back up now
    </Button>
  );
}

export function BackupsPage() {
  const [schedulingFor, setSchedulingFor] = useState<{ id: number; domain: string } | null>(null);
  const [restoring, setRestoring] = useState<BackupEntry | null>(null);
  const [deleting, setDeleting] = useState<BackupEntry | null>(null);
  const [operation, setOperation] = useState<{ id: number; title: string } | null>(null);
  const [s3Open, setS3Open] = useState(false);

  const meta = useQuery({
    queryKey: ["backups", "meta"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/backups/meta");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load backup settings"));
      return data; /* as any */
    },
  });

  const sites = useQuery({
    queryKey: ["sites"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/sites");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load sites"));
      return data; /* as any */
    },
  });

  const backups = useQuery({
    queryKey: ["backups", "list"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/backups");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load backups"));
      return data; /* as any */
    },
  });

  const activeSites = (sites.data ?? []).filter((s) => s.status === "active");
  const siteIdByDomain = new Map(activeSites.map((s) => [s.domain, s.id]));

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">Backups</h1>
        <div className="flex items-center gap-2">
          {meta.data?.s3_enabled && (
            <Badge variant="secondary">
              <CloudUpload className="mr-1 h-3 w-3" aria-hidden /> S3 mirror on
            </Badge>
          )}
          <Button variant="outline" size="sm" onClick={() => setS3Open(true)}>
            <Settings2 className="h-4 w-4" aria-hidden /> S3 settings
          </Button>
        </div>
      </div>

      <section className="space-y-3">
        <h2 className="flex items-center gap-2 text-lg font-medium">
          <CalendarClock className="h-4 w-4" aria-hidden /> Sites
        </h2>
        {sites.isPending ? (
          <LoadingState label="Loading sites…" />
        ) : sites.isError ? (
          <ErrorState message={sites.error.message} onRetry={() => sites.refetch()} />
        ) : activeSites.length === 0 ? (
          <EmptyState
            title="No active sites"
            description="Create a site first — backups belong to sites."
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Site</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {activeSites.map((site) => (
                <TableRow key={site.id}>
                  <TableCell className="font-mono text-xs">
                    <Link to={`/sites/${site.id}`} className="hover:underline">
                      {site.domain}
                    </Link>
                  </TableCell>
                  <TableCell className="space-x-2 text-right">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setSchedulingFor({ id: site.id, domain: site.domain })}
                    >
                      <CalendarClock className="h-4 w-4" aria-hidden /> Schedule
                    </Button>
                    <RunNowButton
                      site={site}
                      onStarted={(id) => setOperation({ id, title: `Backing up ${site.domain}` })}
                    />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </section>

      <section className="space-y-3">
        <h2 className="flex items-center gap-2 text-lg font-medium">
          <Archive className="h-4 w-4" aria-hidden /> Stored backups
        </h2>
        {backups.isPending ? (
          <LoadingState label="Loading backups…" />
        ) : backups.isError ? (
          <ErrorState message={backups.error.message} onRetry={() => backups.refetch()} />
        ) : backups.data.length === 0 ? (
          <EmptyState
            title="No backups yet"
            description="Run one now or enable a schedule — backups appear here."
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Site</TableHead>
                <TableHead>Created</TableHead>
                <TableHead>Size</TableHead>
                <TableHead>Contents</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {backups.data.map((entry) => {
                const siteId = siteIdByDomain.get(entry.domain) ?? null;
                return (
                  <TableRow key={`${entry.domain}-${entry.backup_id}`}>
                    <TableCell className="font-mono text-xs">{entry.domain}</TableCell>
                    <TableCell className="font-mono text-xs">
                      {formatBackupTime(entry.backup_id)}
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {formatBytes(entry.size_bytes)}
                    </TableCell>
                    <TableCell className="space-x-1">
                      <Badge variant="outline">files</Badge>
                      {entry.databases.length > 0 && (
                        <Badge variant="outline">
                          {entry.databases.length} db{entry.databases.length > 1 ? "s" : ""}
                        </Badge>
                      )}
                      {entry.wordpress && <Badge variant="secondary">WordPress</Badge>}
                      {entry.s3 && <Badge variant="secondary">S3</Badge>}
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Restore ${entry.backup_id} of ${entry.domain}`}
                        disabled={siteId === null}
                        onClick={() => setRestoring(entry)}
                      >
                        <RotateCcw className="h-4 w-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Delete ${entry.backup_id} of ${entry.domain}`}
                        disabled={siteId === null}
                        onClick={() => setDeleting(entry)}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </section>

      <ScheduleDialog
        site={schedulingFor}
        s3Enabled={meta.data?.s3_enabled ?? false}
        onClose={() => setSchedulingFor(null)}
      />
      <S3ConfigDialog open={s3Open} onClose={() => setS3Open(false)} />
      <RestoreDialog
        target={restoring}
        siteId={restoring ? (siteIdByDomain.get(restoring.domain) ?? null) : null}
        onStarted={(id) => setOperation({ id, title: `Restoring ${restoring?.domain ?? "site"}` })}
        onClose={() => setRestoring(null)}
      />
      <DeleteBackupDialog
        target={deleting}
        siteId={deleting ? (siteIdByDomain.get(deleting.domain) ?? null) : null}
        onClose={() => setDeleting(null)}
      />
      <OperationDialog
        title={operation?.title ?? ""}
        operationId={operation?.id ?? null}
        onClose={() => setOperation(null)}
      />
    </div>
  );
}
