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
import { openAdminer, useDatabases } from "@/pages/databases";
import { useMutation } from "@tanstack/react-query";
/** Site detail → Databases tab: this site's databases + create. */
import { ExternalLink, KeyRound, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

type Site = components["schemas"]["SiteResponse"];

export function SiteDatabasesTab({ site }: { site: Site }) {
  const databases = useDatabases();
  const [createOpen, setCreateOpen] = useState(false);
  const [creds, setCreds] = useState<Credentials | null>(null);
  const [deleting, setDeleting] = useState<{ id: number; name: string } | null>(null);

  const reset = useMutation({
    mutationFn: async (databaseId: number) => {
      const { data, error } = await api.POST("/api/databases/{database_id}/reset-password", {
        params: { path: { database_id: databaseId } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Reset failed"));
      return data;
    },
    onSuccess: setCreds,
    onError: (err) => toast.error(err.message),
  });

  if (site.status !== "active") {
    return <p className="text-sm text-muted-foreground">Available once the site is active.</p>;
  }
  if (databases.isPending) return <LoadingState label="Loading databases…" />;
  if (databases.isError) {
    return <ErrorState message={databases.error.message} onRetry={() => databases.refetch()} />;
  }

  const rows = databases.data.filter((e) => e.database && e.database.site_id === site.id);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        <Button size="sm" onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" aria-hidden /> New database
        </Button>
        <Button size="sm" variant="outline" onClick={() => void openAdminer()}>
          <ExternalLink className="h-4 w-4" aria-hidden /> Open Adminer
        </Button>
      </div>

      {rows.length === 0 ? (
        <EmptyState
          title="No databases for this site"
          description="WordPress installs create one automatically; you can also add your own."
        />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>User</TableHead>
              <TableHead>Purpose</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((entry) =>
              entry.database ? (
                <TableRow key={entry.database.id}>
                  <TableCell className="font-mono text-xs">{entry.database.name}</TableCell>
                  <TableCell className="font-mono text-xs">{entry.database.db_user}</TableCell>
                  <TableCell>
                    <Badge
                      variant={entry.database.purpose === "wordpress" ? "secondary" : "outline"}
                    >
                      {entry.database.purpose}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label={`Reset password for ${entry.database.name}`}
                      loading={reset.isPending}
                      onClick={() => entry.database && reset.mutate(entry.database.id)}
                    >
                      <KeyRound className="h-4 w-4" />
                    </Button>
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
              ) : null,
            )}
          </TableBody>
        </Table>
      )}

      <CreateDatabaseDialog
        siteId={site.id}
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={setCreds}
      />
      <CredentialsDialog creds={creds} onClose={() => setCreds(null)} />
      <DeleteDatabaseDialog target={deleting} onClose={() => setDeleting(null)} />
    </div>
  );
}
