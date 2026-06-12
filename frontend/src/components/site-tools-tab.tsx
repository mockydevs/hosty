import { FormField } from "@/components/form-field";
import { OperationProgress } from "@/components/operation-progress";
import { EmptyState, LoadingState } from "@/components/states";
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
import { Input } from "@/components/ui/input";
import { api, apiErrorMessage, getAccessToken } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
/**
 * Site tools (Phase 11d): staging clones, site import (files/SQL upload) and
 * the PHP error log viewer.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FlaskConical, RefreshCcw, ScrollText, Upload } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router";
import { toast } from "sonner";

type Site = components["schemas"]["SiteResponse"];

// --- staging ---------------------------------------------------------------------

function PushDialog({ site, onClose }: { site: Site; onClose: () => void }) {
  const [confirm, setConfirm] = useState("");
  const [operationId, setOperationId] = useState<number | null>(null);
  const queryClient = useQueryClient();

  const push = useMutation({
    mutationFn: async () => {
      const { data, error, response } = await api.POST("/api/sites/{site_id}/staging/push", {
        params: { path: { site_id: site.id } },
        body: { confirm_domain: confirm },
      });
      if (error || !data) {
        throw new Error(apiErrorMessage(error, `Push failed (${response.status})`));
      }
      return data;
    },
    onSuccess: (data) => setOperationId(data.operation_id),
    onError: (err) => toast.error(err.message),
  });

  return (
    <Dialog open onClose={onClose}>
      <DialogContent>
        <DialogTitle>Push staging to production?</DialogTitle>
        <DialogDescription>
          Production files are mirrored from staging (deletions included) and the database is
          replayed. This overwrites production — take a backup first.
        </DialogDescription>
        {operationId ? (
          <OperationProgress
            operationId={operationId}
            onFinished={async (op) => {
              await queryClient.invalidateQueries({ queryKey: ["sites"] });
              if (op.status === "succeeded") toast.success("Staging pushed to production");
              onClose();
            }}
          />
        ) : (
          <>
            <FormField label="Type the production domain to confirm" htmlFor="push-confirm">
              <Input
                id="push-confirm"
                autoComplete="off"
                spellCheck={false}
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
              />
            </FormField>
            <DialogActions>
              <Button variant="outline" onClick={onClose}>
                Cancel
              </Button>
              <Button
                variant="destructive"
                disabled={confirm.trim().length === 0}
                loading={push.isPending}
                onClick={() => push.mutate()}
              >
                Push to production
              </Button>
            </DialogActions>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

export function StagingCard({ site }: { site: Site }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [pushOpen, setPushOpen] = useState(false);
  const isStaging = site.staging_of !== null && site.staging_of !== undefined;

  // Does this production site already have a staging clone?
  const sites = useQuery({
    queryKey: ["sites"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/sites");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load sites"));
      return data;
    },
    enabled: !isStaging,
  });
  const existingClone = (sites.data ?? []).find((s) => s.staging_of === site.id);

  const create = useMutation({
    mutationFn: async () => {
      const { data, error, response } = await api.POST("/api/sites/{site_id}/staging", {
        params: { path: { site_id: site.id } },
      });
      if (error || !data) {
        throw new Error(apiErrorMessage(error, `Staging failed (${response.status})`));
      }
      return data;
    },
    onSuccess: async (data) => {
      await queryClient.invalidateQueries({ queryKey: ["sites"] });
      toast.success(`Cloning to ${data.site.domain}…`);
      navigate(`/sites/${data.site.id}`, { state: { operationId: data.operation_id } });
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <FlaskConical className="h-4 w-4 text-muted-foreground" aria-hidden />
          Staging
          {isStaging && <Badge variant="outline">staging clone</Badge>}
        </CardTitle>
        <CardDescription>
          {isStaging
            ? "This is a staging copy. Test freely, then push files + database back to production."
            : "Clone this site (files + database) to a staging.<domain> copy for safe testing."}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        {isStaging ? (
          <Button onClick={() => setPushOpen(true)}>Push to production</Button>
        ) : existingClone ? (
          <p className="text-sm">
            Staging clone:{" "}
            <Link to={`/sites/${existingClone.id}`} className="font-medium hover:underline">
              {existingClone.domain}
            </Link>
          </p>
        ) : (
          <Button
            loading={create.isPending}
            disabled={site.status !== "active"}
            onClick={() => create.mutate()}
          >
            Create staging clone
          </Button>
        )}
        {pushOpen && <PushDialog site={site} onClose={() => setPushOpen(false)} />}
      </CardContent>
    </Card>
  );
}

// --- import ----------------------------------------------------------------------

async function uploadRaw(
  siteId: number,
  kind: "files" | "sql",
  file: File,
): Promise<{ upload_id: string }> {
  const params = new URLSearchParams({ kind, filename: file.name });
  const resp = await fetch(`/api/sites/${siteId}/import/upload?${params}`, {
    method: "POST",
    body: file,
    headers: { Authorization: `Bearer ${getAccessToken() ?? ""}` },
    credentials: "include",
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => null);
    throw new Error(apiErrorMessage(body, `Upload failed (${resp.status})`));
  }
  return resp.json();
}

export function ImportCard({ site }: { site: Site }) {
  const queryClient = useQueryClient();
  const [filesFile, setFilesFile] = useState<File | null>(null);
  const [sqlFile, setSqlFile] = useState<File | null>(null);
  const [targetDb, setTargetDb] = useState("");
  const [oldDomain, setOldDomain] = useState("");
  const [operationId, setOperationId] = useState<number | null>(null);

  const databases = useQuery({
    queryKey: ["databases"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/databases");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load databases"));
      return data;
    },
  });

  const start = useMutation({
    mutationFn: async () => {
      const filesUpload = filesFile ? await uploadRaw(site.id, "files", filesFile) : null;
      const sqlUpload = sqlFile ? await uploadRaw(site.id, "sql", sqlFile) : null;
      const { data, error, response } = await api.POST("/api/sites/{site_id}/import", {
        params: { path: { site_id: site.id } },
        body: {
          files_upload_id: filesUpload?.upload_id ?? null,
          sql_upload_id: sqlUpload?.upload_id ?? null,
          target_db: sqlUpload ? targetDb : null,
          old_domain: oldDomain.trim() || null,
        },
      });
      if (error || !data) {
        throw new Error(apiErrorMessage(error, `Import failed (${response.status})`));
      }
      return data;
    },
    onSuccess: (data) => setOperationId(data.operation_id),
    onError: (err) => toast.error(err.message),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Upload className="h-4 w-4 text-muted-foreground" aria-hidden />
          Import site
        </CardTitle>
        <CardDescription>
          Migrate an existing site in: upload a files archive (.tar.gz/.zip) and/or an SQL dump.
          Imports overwrite in place — take a backup first.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {operationId ? (
          <OperationProgress
            operationId={operationId}
            onFinished={async (op) => {
              await queryClient.invalidateQueries({ queryKey: ["sites"] });
              setOperationId(null);
              if (op.status === "succeeded") toast.success("Import finished");
            }}
          />
        ) : (
          <>
            <FormField label="Files archive (.tar.gz, .tgz or .zip)" htmlFor="import-files">
              <Input
                id="import-files"
                type="file"
                accept=".tar.gz,.tgz,.zip"
                onChange={(e) => setFilesFile(e.target.files?.[0] ?? null)}
              />
            </FormField>
            <FormField label="SQL dump (optional)" htmlFor="import-sql">
              <Input
                id="import-sql"
                type="file"
                accept=".sql"
                onChange={(e) => setSqlFile(e.target.files?.[0] ?? null)}
              />
            </FormField>
            {sqlFile && (
              <FormField label="Import the dump into" htmlFor="import-target-db">
                <select
                  id="import-target-db"
                  className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
                  value={targetDb}
                  onChange={(e) => setTargetDb(e.target.value)}
                >
                  <option value="">Choose a database…</option>
                  {(databases.data ?? [])
                    .filter((e) => e.database && e.database.site_id === site.id)
                    .map((e) =>
                      e.database ? (
                        <option key={e.database.id} value={e.database.name}>
                          {e.database.name}
                        </option>
                      ) : null,
                    )}
                </select>
              </FormField>
            )}
            {site.wordpress && (
              <FormField
                label="Old domain to rewrite (WordPress search-replace, optional)"
                htmlFor="import-old-domain"
              >
                <Input
                  id="import-old-domain"
                  placeholder="old-site.example"
                  value={oldDomain}
                  onChange={(e) => setOldDomain(e.target.value)}
                />
              </FormField>
            )}
            <Button
              disabled={(!filesFile && !sqlFile) || (sqlFile !== null && targetDb === "")}
              loading={start.isPending}
              onClick={() => start.mutate()}
            >
              Start import
            </Button>
          </>
        )}
      </CardContent>
    </Card>
  );
}

// --- PHP error log ----------------------------------------------------------------

export function PhpLogCard({ site }: { site: Site }) {
  const log = useQuery({
    queryKey: ["sites", site.id, "php-log"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/sites/{site_id}/logs/php", {
        params: { path: { site_id: site.id }, query: { lines: 200 } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load log"));
      return data;
    },
  });

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <div>
          <CardTitle className="flex items-center gap-2 text-base">
            <ScrollText className="h-4 w-4 text-muted-foreground" aria-hidden />
            PHP error log
          </CardTitle>
          <CardDescription>The last 200 lines of {log.data?.path ?? "the log"}.</CardDescription>
        </div>
        <Button
          variant="outline"
          size="icon"
          aria-label="Refresh log"
          onClick={() => log.refetch()}
        >
          <RefreshCcw className="h-4 w-4" />
        </Button>
      </CardHeader>
      <CardContent>
        {log.isPending ? (
          <LoadingState label="Reading log…" />
        ) : log.isError ? (
          <EmptyState title="Log unavailable" description={log.error.message} />
        ) : !log.data.exists || log.data.lines.length === 0 ? (
          <EmptyState
            title="No PHP errors logged"
            description="Either the site is healthy or nothing has hit the error log yet."
          />
        ) : (
          <pre className="max-h-96 overflow-auto rounded-md bg-muted p-3 text-xs leading-5">
            {log.data.lines.join("\n")}
          </pre>
        )}
      </CardContent>
    </Card>
  );
}

export function SiteToolsTab({ site }: { site: Site }) {
  return (
    <div className="space-y-4">
      <StagingCard site={site} />
      <ImportCard site={site} />
      <PhpLogCard site={site} />
    </div>
  );
}
