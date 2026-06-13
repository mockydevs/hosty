import { OperationProgress } from "@/components/operation-progress";
import { ErrorState, LoadingState } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogActions,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
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
import type { components } from "@/lib/api/schema";
import { copyToClipboard } from "@/lib/utils";
import { ShowOnceDialog } from "@/pages/stack-create";
import { StackStatusBadge, isSettling } from "@/pages/stacks";
/**
 * Stack detail (v2/M4): services, endpoints, journald logs viewer, blueprint
 * day-2 actions (with confirm + show-once results), delete with
 * type-to-confirm. Polls while the reconciler is converging.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Copy,
  Database,
  ExternalLink,
  FolderOpen,
  Globe,
  Play,
  RefreshCw,
  RotateCcw,
  Save,
  Trash2,
} from "lucide-react";
import { useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router";
import { toast } from "sonner";

type Stack = components["schemas"]["StackResponse"];
type ActionResult = components["schemas"]["StackActionResponse"];
type Backup = components["schemas"]["BackupResponse"];

export function StackBackupsCard({
  stack,
  onOperation,
}: {
  stack: Stack;
  onOperation: (operationId: number) => void;
}) {
  const queryClient = useQueryClient();
  const [restoring, setRestoring] = useState<Backup | null>(null);
  const [confirm, setConfirm] = useState("");
  const backups = useQuery({
    queryKey: ["stacks", stack.id, "backups"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/stacks/{stack_id}/backups", {
        params: { path: { stack_id: stack.id } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load backups"));
      return data;
    },
  });

  const runBackup = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/stacks/{stack_id}/backups", {
        params: { path: { stack_id: stack.id } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Backup failed to start"));
      return data;
    },
    onSuccess: async (data) => {
      onOperation(data.operation_id);
      toast.success("Stack backup started");
      await queryClient.invalidateQueries({ queryKey: ["stacks", stack.id, "backups"] });
    },
    onError: (error) => toast.error(error.message),
  });

  const restore = useMutation({
    mutationFn: async (item: Backup) => {
      const { data, error } = await api.POST("/api/stacks/{stack_id}/backups/{backup_id}/restore", {
        params: { path: { stack_id: stack.id, backup_id: item.backup_id } },
        body: { scope: "full", confirm_domain: confirm },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Restore failed to start"));
      return data;
    },
    onSuccess: (data) => {
      onOperation(data.operation_id);
      setRestoring(null);
      setConfirm("");
      toast.success("Stack restore started");
    },
    onError: (error) => toast.error(error.message),
  });

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-base">Backups</CardTitle>
        <Button
          size="sm"
          variant="outline"
          disabled={stack.status !== "ready"}
          loading={runBackup.isPending}
          onClick={() => runBackup.mutate()}
        >
          <Save className="h-3.5 w-3.5" aria-hidden /> Back up now
        </Button>
      </CardHeader>
      <CardContent>
        {backups.isPending ? (
          <LoadingState label="Loading backupsâ€¦" />
        ) : backups.isError ? (
          <ErrorState message={backups.error.message} onRetry={() => backups.refetch()} />
        ) : backups.data.length === 0 ? (
          <p className="text-sm text-muted-foreground">No backups yet.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Created</TableHead>
                <TableHead>Size</TableHead>
                <TableHead className="text-right">Action</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {backups.data.map((item) => (
                <TableRow key={item.backup_id}>
                  <TableCell className="font-mono text-xs">{item.backup_id}</TableCell>
                  <TableCell>{Math.max(1, Math.round(item.size_bytes / 1024))} KB</TableCell>
                  <TableCell className="text-right">
                    <Button size="sm" variant="outline" onClick={() => setRestoring(item)}>
                      <RotateCcw className="h-3.5 w-3.5" aria-hidden /> Restore
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
      <Dialog open={restoring !== null} onClose={() => setRestoring(null)}>
        <DialogContent>
          <DialogTitle>Restore this stack backup?</DialogTitle>
          <DialogDescription>
            Stops the stack, replaces its volumes, imports the database dump, then restarts it. Type{" "}
            <strong>{stack.name}</strong> to confirm.
          </DialogDescription>
          <Input
            aria-label="Confirm stack name for restore"
            value={confirm}
            onChange={(event) => setConfirm(event.target.value)}
          />
          <DialogActions>
            <Button variant="outline" onClick={() => setRestoring(null)}>
              Cancel
            </Button>
            <Button
              disabled={confirm.trim().toLowerCase() !== stack.name}
              loading={restore.isPending}
              onClick={() => restoring && restore.mutate(restoring)}
            >
              Restore backup
            </Button>
          </DialogActions>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function LogsCard({ stack }: { stack: Stack }) {
  const [service, setService] = useState(
    stack.services.find((s) => s.is_web)?.name ?? stack.services[0]?.name ?? "",
  );
  const logs = useQuery({
    queryKey: ["stacks", stack.id, "logs", service],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/stacks/{stack_id}/logs", {
        params: { path: { stack_id: stack.id }, query: { service, tail: 200 } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load logs"));
      return data;
    },
    enabled: service !== "",
  });

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-base">Logs</CardTitle>
        <div className="flex items-center gap-2">
          {stack.services.length > 1 && (
            <select
              aria-label="Service"
              className="flex h-8 rounded-md border border-input bg-transparent px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              value={service}
              onChange={(e) => setService(e.target.value)}
            >
              {stack.services.map((s) => (
                <option key={s.name} value={s.name}>
                  {s.name}
                </option>
              ))}
            </select>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={() => logs.refetch()}
            loading={logs.isFetching}
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden /> Refresh
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {logs.isPending ? (
          <LoadingState label="Loading logs…" />
        ) : logs.isError ? (
          <ErrorState message={logs.error.message} onRetry={() => logs.refetch()} />
        ) : (
          <pre className="max-h-96 overflow-auto rounded-md bg-muted p-3 font-mono text-xs leading-relaxed">
            {logs.data.logs || "No log output yet."}
          </pre>
        )}
      </CardContent>
    </Card>
  );
}

function StackToolsCard({ stack }: { stack: Stack }) {
  const hasAdminer = stack.services.some((service) => service.name === "adminer");
  const hasFiles = stack.services.some((service) => service.name === "files");

  const openAdminer = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/stacks/{stack_id}/adminer-session", {
        params: { path: { stack_id: stack.id } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Could not open Adminer"));
      return data.url;
    },
    onSuccess: (url) => window.open(url, "_blank", "noopener"),
    onError: (error) => toast.error(error.message),
  });

  const openFiles = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/stacks/{stack_id}/files-session", {
        params: { path: { stack_id: stack.id } },
      });
      if (error || !data) {
        throw new Error(apiErrorMessage(error, "Could not open the file manager"));
      }
      return data.url;
    },
    onSuccess: (url) => window.open(url, "_blank", "noopener"),
    onError: (error) => toast.error(error.message),
  });

  if (!hasAdminer && !hasFiles) return null;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Tools</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-wrap gap-2">
        {hasAdminer && (
          <Button
            variant="outline"
            size="sm"
            disabled={stack.status !== "ready"}
            loading={openAdminer.isPending}
            onClick={() => openAdminer.mutate()}
          >
            <Database className="h-3.5 w-3.5" aria-hidden /> Open Adminer
          </Button>
        )}
        {hasFiles && (
          <Button
            variant="outline"
            size="sm"
            disabled={stack.status !== "ready"}
            loading={openFiles.isPending}
            onClick={() => openFiles.mutate()}
          >
            <FolderOpen className="h-3.5 w-3.5" aria-hidden /> Open files
          </Button>
        )}
      </CardContent>
    </Card>
  );
}

function ActionsCard({ stack, actions }: { stack: Stack; actions: string[] }) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState<string | null>(null);
  const [showOnce, setShowOnce] = useState<Record<string, string> | null>(null);

  const run = useMutation({
    mutationFn: async (action: string): Promise<ActionResult> => {
      const { data, error, response } = await api.POST(
        "/api/stacks/{stack_id}/actions/{action_name}",
        { params: { path: { stack_id: stack.id, action_name: action } } },
      );
      if (error || !data) {
        throw new Error(apiErrorMessage(error, `Action failed (${response.status})`));
      }
      return data;
    },
    onSuccess: async (result) => {
      if (result.ok) toast.success(result.message || "Done");
      else toast.error(result.message || "Action failed");
      const secrets = result.show_once ?? {};
      if (Object.keys(secrets).length > 0) setShowOnce(secrets);
      await queryClient.invalidateQueries({ queryKey: ["stacks"] });
    },
    onError: (err) => toast.error(err.message),
    onSettled: () => setConfirming(null),
  });

  if (actions.length === 0) return null;
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Actions</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-wrap gap-2">
        {actions.map((action) => (
          <Button
            key={action}
            variant="outline"
            size="sm"
            disabled={run.isPending}
            onClick={() => setConfirming(action)}
          >
            <Play className="h-3.5 w-3.5" aria-hidden /> {action.replace(/_/g, " ")}
          </Button>
        ))}
      </CardContent>
      <Dialog open={confirming !== null} onClose={() => setConfirming(null)}>
        <DialogContent>
          <DialogTitle>Run {confirming?.replace(/_/g, " ")}?</DialogTitle>
          <DialogDescription>
            Runs against the live stack <strong>{stack.name}</strong>.
          </DialogDescription>
          <DialogActions>
            <Button variant="outline" onClick={() => setConfirming(null)}>
              Cancel
            </Button>
            <Button loading={run.isPending} onClick={() => confirming && run.mutate(confirming)}>
              Run
            </Button>
          </DialogActions>
        </DialogContent>
      </Dialog>
      <ShowOnceDialog secrets={showOnce} onClose={() => setShowOnce(null)} />
    </Card>
  );
}

function DeleteStackDialog({
  stack,
  open,
  onClose,
}: {
  stack: Stack;
  open: boolean;
  onClose: () => void;
}) {
  const [confirm, setConfirm] = useState("");
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const del = useMutation({
    mutationFn: async () => {
      const { error, response } = await api.DELETE("/api/stacks/{stack_id}", {
        params: { path: { stack_id: stack.id } },
        body: { confirm_name: confirm },
      });
      if (error) throw new Error(apiErrorMessage(error, `Delete failed (${response.status})`));
    },
    onSuccess: async () => {
      toast.success(`Deleting ${stack.name}…`);
      await queryClient.invalidateQueries({ queryKey: ["stacks"] });
      navigate("/stacks");
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <Dialog open={open} onClose={onClose}>
      <DialogContent>
        <DialogTitle>Delete {stack.name}?</DialogTitle>
        <DialogDescription>
          Stops every service and removes containers, volumes and routes. This cannot be undone.
          Type the stack name <code className="select-all rounded bg-muted px-1 font-mono text-foreground">{stack.name}</code> to confirm.
        </DialogDescription>
        <Input
          aria-label="Confirm stack name"
          placeholder={stack.name}
          autoComplete="off"
          spellCheck={false}
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
        />
        <DialogActions>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={confirm.trim().toLowerCase() !== stack.name}
            loading={del.isPending}
            onClick={() => del.mutate()}
          >
            Delete stack
          </Button>
        </DialogActions>
      </DialogContent>
    </Dialog>
  );
}

function EndpointsCard({
  stack,
  onOperation,
}: {
  stack: Stack;
  onOperation: (operationId: number) => void;
}) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [domain, setDomain] = useState("");
  const hasWeb = stack.services.some((s) => s.is_web && s.internal_port != null);

  const setDomainMut = useMutation({
    mutationFn: async (value: string | null) => {
      const { data, error } = await api.PUT("/api/stacks/{stack_id}/domain", {
        params: { path: { stack_id: stack.id } },
        body: { domain: value, behind_cloudflare: false },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Could not set the domain"));
      return data;
    },
    onSuccess: (data) => {
      toast.success("Updating domain…");
      onOperation(data.operation_id);
      setEditing(false);
      void queryClient.invalidateQueries({ queryKey: ["stack", stack.id] });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Failed to set the domain"),
  });

  const autogenerate = async () => {
    const { data, error } = await api.GET("/api/stacks/suggested-domain", {
      params: { query: { name: stack.name } },
    });
    if (error || !data) {
      toast.error(apiErrorMessage(error, "Set an apps base domain or server public IP first"));
      return;
    }
    setDomain(data.domain);
  };

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-base">Endpoints</CardTitle>
        {hasWeb && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setDomain(stack.endpoints[0]?.domain ?? "");
              setEditing(true);
            }}
          >
            {stack.endpoints.length ? "Change domain" : "Set domain"}
          </Button>
        )}
      </CardHeader>
      <CardContent>
        {stack.endpoints.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No public domains — services are reachable only inside the stack.
          </p>
        ) : (
          <ul className="space-y-2 text-sm">
            {stack.endpoints.map((ep) => (
              <li key={ep.domain} className="flex items-center gap-2">
                <a
                  href={`https://${ep.domain}`}
                  target="_blank"
                  rel="noreferrer"
                  className="flex items-center gap-1 font-medium hover:underline"
                >
                  {ep.domain}
                  <ExternalLink className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
                </a>
                <span className="font-mono text-xs text-muted-foreground">→ {ep.service_name}</span>
                {ep.behind_cloudflare && <Badge variant="outline">Cloudflare</Badge>}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
      <Dialog open={editing} onClose={() => setEditing(false)}>
        <DialogContent>
          <DialogTitle>{stack.endpoints.length ? "Change domain" : "Set domain"}</DialogTitle>
          <DialogDescription>
            Point a public domain at this stack's web service. Leave blank to auto-generate one
            (wildcard base, or sslip.io off the server IP).
          </DialogDescription>
          <div className="flex items-center gap-2">
            <Input
              className="flex-1"
              value={domain}
              placeholder="app.example.com"
              autoComplete="off"
              spellCheck={false}
              onChange={(e) => setDomain(e.target.value)}
            />
            <Button type="button" variant="outline" onClick={autogenerate}>
              Autogenerate
            </Button>
          </div>
          <DialogActions>
            <Button variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
            <Button
              loading={setDomainMut.isPending}
              onClick={() => setDomainMut.mutate(domain.trim() || null)}
            >
              Save
            </Button>
          </DialogActions>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

type ConnectionLink = components["schemas"]["ConnectionLinkResponse"];

function ConnRow({ label, uri }: { label: string; uri: string }) {
  const copy = async () => {
    try {
      await copyToClipboard(uri);
      toast.success("Connection string copied");
    } catch {
      toast.error("Copy failed — select the text manually");
    }
  };
  return (
    <div className="space-y-1">
      <p className="text-xs text-muted-foreground">{label}</p>
      <div className="flex items-center gap-2">
        <code className="flex-1 truncate rounded-md border border-border bg-muted/40 px-2 py-1.5 font-mono text-xs">
          {uri}
        </code>
        <Button
          variant="ghost"
          size="icon"
          aria-label={`Copy ${label} connection string`}
          onClick={copy}
        >
          <Copy className="h-3.5 w-3.5" />
        </Button>
      </div>
    </div>
  );
}

function EnvVarsCard({
  stack,
  onOperation,
}: {
  stack: Stack;
  onOperation: (operationId: number) => void;
}) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [envVars, setEnvVars] = useState("");

  const updateEnv = useMutation({
    mutationFn: async (envString: string) => {
      const parsedEnv: Record<string, string> = {};
      envString.split("\n").forEach(line => {
        const match = line.match(/^([^=]+)=(.*)$/);
        if (match && match[1] && match[2] !== undefined) {
          parsedEnv[match[1].trim()] = match[2].trim();
        }
      });
      const { data, error } = await api.PUT("/api/stacks/{stack_id}/env" as any, {
        params: { path: { stack_id: stack.id } },
        body: { env: parsedEnv },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Could not update environment variables"));
      return data;
    },
    onSuccess: (data) => {
      toast.success("Environment variables updated. Restarting stack...");
      onOperation(data.operation_id);
      setEditing(false);
      void queryClient.invalidateQueries({ queryKey: ["stack", stack.id] });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Failed to update env vars"),
  });

  const envObj = (stack as any).inputs?.env || {};
  const currentEnvString = Object.entries(envObj).map(([k, v]) => `${k}=${v}`).join("\n");

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-base">Environment Variables</CardTitle>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            setEnvVars(currentEnvString);
            setEditing(true);
          }}
        >
          Edit
        </Button>
      </CardHeader>
      <CardContent>
        {Object.keys(envObj).length === 0 ? (
          <p className="text-sm text-muted-foreground">No environment variables defined.</p>
        ) : (
          <div className="rounded-md border bg-muted/40 p-3 max-h-48 overflow-y-auto">
            {Object.entries(envObj).map(([key, value]) => (
              <div key={key} className="flex gap-2 font-mono text-xs mb-1 last:mb-0">
                <span className="font-semibold text-primary/80">{key}</span>
                <span className="text-muted-foreground">=</span>
                <span className="truncate flex-1">{String(value)}</span>
              </div>
            ))}
          </div>
        )}
      </CardContent>
      <Dialog open={editing} onClose={() => setEditing(false)}>
        <DialogContent className="max-w-xl">
          <DialogTitle>Edit Environment Variables</DialogTitle>
          <DialogDescription>
            Variables must be in <code>KEY=value</code> format, one per line. They will be applied on the next stack restart.
          </DialogDescription>
          <div className="py-2">
            <textarea
              className="flex min-h-[200px] w-full rounded-md border border-input bg-background px-3 py-2 text-sm font-mono ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
              placeholder="PORT=3000&#10;DATABASE_URL=postgres://..."
              value={envVars}
              onChange={(e) => setEnvVars(e.target.value)}
              spellCheck={false}
            />
          </div>
          <DialogActions>
            <Button variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
            <Button
              loading={updateEnv.isPending}
              onClick={() => updateEnv.mutate(envVars)}
            >
              Save & Restart
            </Button>
          </DialogActions>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function ConnectionsCard({
  stack,
  onOperation,
}: {
  stack: Stack;
  onOperation: (operationId: number) => void;
}) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState<ConnectionLink | null>(null);

  const links = useQuery({
    queryKey: ["stack", stack.id, "connections"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/stacks/{stack_id}/connections", {
        params: { path: { stack_id: stack.id } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load connections"));
      return data;
    },
  });

  const expose = useMutation({
    mutationFn: async ({ service, exposed }: { service: string; exposed: boolean }) => {
      const { data, error } = await api.PUT(
        "/api/stacks/{stack_id}/services/{service_name}/expose" as any,
        { params: { path: { stack_id: stack.id, service_name: service } }, body: { exposed } },
      );
      if (error || !data) throw new Error(apiErrorMessage(error, "Could not change exposure"));
      return data;
    },
    onSuccess: async (data) => {
      setConfirming(null);
      onOperation(data.operation_id);
      toast.success("Updating exposure…");
      await queryClient.invalidateQueries({ queryKey: ["stack", stack.id] });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Failed"),
  });

  // Only databases with connection metadata produce links.
  if (links.isPending || links.isError || (links.data?.length ?? 0) === 0) return null;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-base">
          <Database className="h-4 w-4 text-muted-foreground" aria-hidden /> Connection
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-5">
        {links.data?.map((link) => (
          <div key={link.service} className="space-y-3">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <span className="font-medium text-sm">{link.service}</span>
                {link.exposed ? (
                  <Badge variant="success">Public</Badge>
                ) : (
                  <Badge variant="secondary">Internal only</Badge>
                )}
              </div>
              <Button
                variant={link.exposed ? "outline" : "default"}
                size="sm"
                loading={expose.isPending}
                onClick={() =>
                  link.exposed
                    ? expose.mutate({ service: link.service, exposed: false })
                    : setConfirming(link)
                }
              >
                <Globe className="h-3.5 w-3.5" aria-hidden />
                {link.exposed ? "Make private" : "Expose publicly"}
              </Button>
            </div>
            <ConnRow label="From another stack (internal network)" uri={link.internal_uri} />
            <ConnRow label="From code on this server (loopback)" uri={link.host_uri} />
            {link.public_uri && <ConnRow label="From anywhere (public)" uri={link.public_uri} />}
          </div>
        ))}
      </CardContent>

      <Dialog open={confirming !== null} onClose={() => setConfirming(null)}>
        <DialogContent>
          <DialogTitle>Expose this database to the internet?</DialogTitle>
          <DialogDescription>
            The port will bind your server's public IP — anyone who can reach it with the password
            can connect. Make sure the password is strong. You can make it private again at any
            time.
          </DialogDescription>
          <DialogActions>
            <Button variant="ghost" onClick={() => setConfirming(null)}>
              Cancel
            </Button>
            <Button
              loading={expose.isPending}
              onClick={() =>
                confirming && expose.mutate({ service: confirming.service, exposed: true })
              }
            >
              Expose publicly
            </Button>
          </DialogActions>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

export function StackDetailPage() {
  const { stackId } = useParams();
  const location = useLocation();
  const queryClient = useQueryClient();
  const [operationId, setOperationId] = useState<number | null>(
    (location.state as { operationId?: number } | null)?.operationId ?? null,
  );
  const [deleteOpen, setDeleteOpen] = useState(false);

  const id = Number(stackId);
  const stack = useQuery({
    queryKey: ["stacks", id],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/stacks/{stack_id}", {
        params: { path: { stack_id: id } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load stack"));
      return data;
    },
    enabled: Number.isInteger(id),
    refetchInterval: (q) => (q.state.data && isSettling(q.state.data) ? 3_000 : false),
  });

  const blueprints = useQuery({
    queryKey: ["stack-blueprints"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/stacks/blueprints");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load blueprints"));
      return data;
    },
  });

  if (stack.isPending) return <LoadingState label="Loading stack…" />;
  if (stack.isError) {
    return <ErrorState message={stack.error.message} onRetry={() => stack.refetch()} />;
  }
  const data = stack.data;
  const actions = blueprints.data?.find((bp) => bp.id === data.blueprint_id)?.actions ?? [];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Link
            to="/stacks"
            aria-label="Back to stacks"
            className="inline-flex h-9 w-9 items-center justify-center rounded-md hover:bg-accent"
          >
            <ArrowLeft className="h-4 w-4" />
          </Link>
          <h1 className="text-2xl font-semibold tracking-tight">{data.name}</h1>
          <StackStatusBadge stack={data} />
          <Badge variant="outline">{data.blueprint_id}</Badge>
        </div>
        <Button variant="destructive" size="sm" onClick={() => setDeleteOpen(true)}>
          <Trash2 className="h-4 w-4" aria-hidden /> Delete
        </Button>
      </div>

      {data.error_message && (
        <p className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          {data.error_message}
        </p>
      )}

      {operationId !== null && (
        <OperationProgress
          operationId={operationId}
          onFinished={async (op) => {
            await queryClient.invalidateQueries({ queryKey: ["stacks"] });
            if (op.status === "succeeded") setOperationId(null);
          }}
        />
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Services</CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Service</TableHead>
                  <TableHead>Image</TableHead>
                  <TableHead>Port</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.services.map((svc) => (
                  <TableRow key={svc.name}>
                    <TableCell className="font-medium">
                      {svc.name}
                      {svc.is_web && (
                        <Badge variant="outline" className="ml-2">
                          web
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell className="max-w-56 truncate font-mono text-xs text-muted-foreground">
                      {svc.image}
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {svc.internal_port ? `${svc.internal_port}` : "-"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>

        <EndpointsCard stack={data} onOperation={setOperationId} />
      </div>

      <ConnectionsCard stack={data} onOperation={setOperationId} />
      <EnvVarsCard stack={data} onOperation={setOperationId} />
      <ActionsCard stack={data} actions={actions} />
      <StackToolsCard stack={data} />
      <StackBackupsCard stack={data} onOperation={setOperationId} />
      <LogsCard stack={data} />
      <DeleteStackDialog stack={data} open={deleteOpen} onClose={() => setDeleteOpen(false)} />
    </div>
  );
}
