import {
  CreateDatabaseDialog,
  type Credentials,
  CredentialsDialog,
  DeleteDatabaseDialog,
} from "@/components/database-dialogs";
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
/**
 * Databases (Week 15): global list with orphan/missing detection, create per
 * site, delete with confirm, reset password (shown once), open in Adminer.
 */
import { useMutation, useQuery } from "@tanstack/react-query";
import { Database as DatabaseIcon, ExternalLink, KeyRound, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";

type Entry = components["schemas"]["DatabaseListEntry"];
type Site = components["schemas"]["SiteResponse"];

export function useDatabases() {
  return useQuery({
    queryKey: ["databases"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/databases");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load databases"));
      return data;
    },
  });
}

export function openAdminer() {
  return api.POST("/api/databases/adminer-session").then(({ data, error }) => {
    if (error || !data) {
      toast.error(apiErrorMessage(error, "Could not open Adminer"));
      return;
    }
    window.open(data.url, "_blank", "noopener");
  });
}

function ResetPasswordButton({
  databaseId,
  onCredentials,
}: {
  databaseId: number;
  onCredentials: (creds: Credentials) => void;
}) {
  const reset = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/databases/{database_id}/reset-password", {
        params: { path: { database_id: databaseId } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Reset failed"));
      return data;
    },
    onSuccess: onCredentials,
    onError: (err) => toast.error(err.message),
  });
  return (
    <Button
      variant="ghost"
      size="icon"
      aria-label="Reset password"
      loading={reset.isPending}
      onClick={() => reset.mutate()}
    >
      <KeyRound className="h-4 w-4" />
    </Button>
  );
}

export function DatabasesPage() {
  const databases = useDatabases();
  const [createFor, setCreateFor] = useState<number | null>(null);
  const [creds, setCreds] = useState<Credentials | null>(null);
  const [deleting, setDeleting] = useState<{ id: number; name: string } | null>(null);

  const sites = useQuery({
    queryKey: ["sites"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/sites");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load sites"));
      return data;
    },
  });
  const activeSites: Site[] = (sites.data ?? []).filter((s) => s.status === "active");

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">Databases</h1>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => void openAdminer()}>
            <ExternalLink className="h-4 w-4" aria-hidden /> Open Adminer
          </Button>
          {activeSites.length > 0 && (
            <Button onClick={() => setCreateFor(activeSites[0]?.id ?? null)}>
              <Plus className="h-4 w-4" aria-hidden /> New database
            </Button>
          )}
        </div>
      </div>

      {activeSites.length > 1 && createFor !== null && (
        <div className="max-w-xs">
          <label htmlFor="db-site-select" className="mb-1 block text-sm font-medium">
            For site
          </label>
          <select
            id="db-site-select"
            className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm"
            value={createFor}
            onChange={(e) => setCreateFor(Number(e.target.value))}
          >
            {activeSites.map((s) => (
              <option key={s.id} value={s.id}>
                {s.domain}
              </option>
            ))}
          </select>
        </div>
      )}

      {databases.isPending ? (
        <LoadingState label="Loading databases…" />
      ) : databases.isError ? (
        <ErrorState message={databases.error.message} onRetry={() => databases.refetch()} />
      ) : databases.data.length === 0 ? (
        <EmptyState
          title="No databases yet"
          description={
            activeSites.length
              ? "Create one for any active site."
              : "Create a site first — databases belong to sites."
          }
        />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Site</TableHead>
              <TableHead>Purpose</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {databases.data.map((entry: Entry) =>
              entry.database ? (
                <TableRow key={`db-${entry.database.id}`}>
                  <TableCell className="font-mono text-xs">
                    <span className="flex items-center gap-2">
                      <DatabaseIcon className="h-4 w-4 text-muted-foreground" aria-hidden />
                      {entry.database.name}
                      {entry.missing && <Badge variant="destructive">missing on server</Badge>}
                    </span>
                  </TableCell>
                  <TableCell>
                    <Link to={`/sites/${entry.database.site_id}`} className="hover:underline">
                      {entry.site_domain}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant={entry.database.purpose === "wordpress" ? "secondary" : "outline"}
                    >
                      {entry.database.purpose}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right">
                    <ResetPasswordButton databaseId={entry.database.id} onCredentials={setCreds} />
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label={`Delete ${entry.database.name}`}
                      onClick={() =>
                        entry.database &&
                        setDeleting({ id: entry.database.id, name: entry.database.name })
                      }
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </TableCell>
                </TableRow>
              ) : (
                <TableRow key={`orphan-${entry.orphan_name}`}>
                  <TableCell className="font-mono text-xs">
                    <span className="flex items-center gap-2">
                      <DatabaseIcon className="h-4 w-4 text-muted-foreground" aria-hidden />
                      {entry.orphan_name}
                      <Badge variant="outline">orphan — not managed by Hosty</Badge>
                    </span>
                  </TableCell>
                  <TableCell className="text-muted-foreground">—</TableCell>
                  <TableCell className="text-muted-foreground">—</TableCell>
                  <TableCell />
                </TableRow>
              ),
            )}
          </TableBody>
        </Table>
      )}

      {createFor !== null && (
        <CreateDatabaseDialog
          siteId={createFor}
          open
          onClose={() => setCreateFor(null)}
          onCreated={setCreds}
        />
      )}
      <CredentialsDialog creds={creds} onClose={() => setCreds(null)} />
      <DeleteDatabaseDialog target={deleting} onClose={() => setDeleting(null)} />
    </div>
  );
}
