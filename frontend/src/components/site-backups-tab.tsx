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
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, Play, RotateCcw, Settings2, Trash2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

type BackupEntry = components["schemas"]["BackupResponse"];
type Site = components["schemas"]["SiteResponse"];

function formatBackupTime(backupId: string): string {
  const match = backupId.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})\d{2}Z$/);
  return match ? `${match[1]}-${match[2]}-${match[3]} ${match[4]}:${match[5]} UTC` : backupId;
}

export function SiteBackupsTab({ site }: { site: Site }) {
  const queryClient = useQueryClient();
  const [scheduling, setScheduling] = useState(false);
  const [s3Open, setS3Open] = useState(false);
  const [restoring, setRestoring] = useState<BackupEntry | null>(null);
  const [deleting, setDeleting] = useState<BackupEntry | null>(null);
  const [operation, setOperation] = useState<{ id: number; title: string } | null>(null);

  const meta = useQuery({
    queryKey: ["backups", "meta"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/backups/meta");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load backup settings"));
      return data;
    },
    enabled: site.status === "active",
  });

  const backups = useQuery({
    queryKey: ["sites", site.id, "backups"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/sites/{site_id}/backups", {
        params: { path: { site_id: site.id } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load backups"));
      return data;
    },
    enabled: site.status === "active",
  });

  const runNow = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/sites/{site_id}/backups", {
        params: { path: { site_id: site.id } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Could not start the backup"));
      return data;
    },
    onSuccess: (data) =>
      setOperation({ id: data.operation_id, title: `Backing up ${site.domain}` }),
    onError: (err) => toast.error(err.message),
  });

  if (site.status !== "active") {
    return <p className="text-sm text-muted-foreground">Available once the site is active.</p>;
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap gap-2">
          <Button size="sm" loading={runNow.isPending} onClick={() => runNow.mutate()}>
            <Play className="h-4 w-4" aria-hidden /> Back up now
          </Button>
          <Button size="sm" variant="outline" onClick={() => setScheduling(true)}>
            <CalendarClock className="h-4 w-4" aria-hidden /> Schedule
          </Button>
          <Button size="sm" variant="outline" onClick={() => setS3Open(true)}>
            <Settings2 className="h-4 w-4" aria-hidden /> S3 settings
          </Button>
        </div>
        <div className="flex flex-wrap gap-2">
          {meta.data?.scheduler_enabled && <Badge variant="secondary">Scheduler on</Badge>}
          {meta.data?.s3_enabled && <Badge variant="secondary">S3 configured</Badge>}
        </div>
      </div>

      {backups.isPending ? (
        <LoadingState label="Loading backups..." />
      ) : backups.isError ? (
        <ErrorState message={backups.error.message} onRetry={() => backups.refetch()} />
      ) : backups.data.length === 0 ? (
        <EmptyState
          title="No backups for this site"
          description="Run one now or enable a schedule to create the first backup."
        />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Created</TableHead>
              <TableHead>Size</TableHead>
              <TableHead>Contents</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {backups.data.map((entry) => (
              <TableRow key={entry.backup_id}>
                <TableCell className="font-mono text-xs">
                  {formatBackupTime(entry.backup_id)}
                </TableCell>
                <TableCell className="font-mono text-xs">{formatBytes(entry.size_bytes)}</TableCell>
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
                    aria-label={`Restore backup ${entry.backup_id}`}
                    onClick={() => setRestoring(entry)}
                  >
                    <RotateCcw className="h-4 w-4" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={`Delete backup ${entry.backup_id}`}
                    onClick={() => setDeleting(entry)}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <ScheduleDialog
        site={scheduling ? { id: site.id, domain: site.domain } : null}
        s3Enabled={meta.data?.s3_enabled ?? false}
        onClose={() => setScheduling(false)}
      />
      <S3ConfigDialog open={s3Open} onClose={() => setS3Open(false)} />
      <RestoreDialog
        target={restoring}
        siteId={site.id}
        onStarted={(id) => setOperation({ id, title: `Restoring ${site.domain}` })}
        onClose={() => setRestoring(null)}
      />
      <DeleteBackupDialog target={deleting} siteId={site.id} onClose={() => setDeleting(null)} />
      <OperationDialog
        title={operation?.title ?? ""}
        operationId={operation?.id ?? null}
        onClose={async () => {
          setOperation(null);
          await queryClient.invalidateQueries({ queryKey: ["sites", site.id, "backups"] });
          await queryClient.invalidateQueries({ queryKey: ["backups"] });
        }}
      />
    </div>
  );
}
