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
import { Label } from "@/components/ui/label";
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

import { AdvancedSettings, HealthCheckCard } from "./stack-subpages/advanced";
import { GitSourceSettings } from "./stack-subpages/git-source";
import { ServersList } from "./stack-subpages/servers";
import { ScheduledTasksList } from "./stack-subpages/scheduled-tasks";
import { WebhooksConfig } from "./stack-subpages/webhooks";
import { PreviewDeploymentsConfig } from "./stack-subpages/preview-deployments";
import { RollbackList } from "./stack-subpages/rollback";
import { ResourceLimitsConfig } from "./stack-subpages/resource-limits";
import { MetricsView } from "./stack-subpages/metrics";
import { TagsConfig } from "./stack-subpages/tags";
import { DeploymentsTab } from "./stack-subpages/deployments";
import { TerminalTab } from "./stack-subpages/terminal-tab";
import { LinksTab } from "./stack-subpages/links";

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
  Terminal,
  Trash2,
} from "lucide-react";
import { useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router";
import { toast } from "sonner";

type Stack = components["schemas"]["StackResponse"];
type ActionResult = components["schemas"]["StackActionResponse"];
type Backup = components["schemas"]["BackupResponse"];

// ─── Sub-nav groups (matches Coolify sidebar order exactly) ──────────────────
const SUB_NAV_GROUPS: string[][] = [
  ["General", "Advanced"],
  ["Environment Variables", "Persistent Storage"],
  ["Git Source", "Servers"],
  ["Scheduled Tasks", "Webhooks", "Preview Deployments"],
  ["Rollback", "Resource Limits", "Resource Operations", "Metrics", "Tags"],
  ["Danger Zone"],
];

// ─── GeneralConfigForm ────────────────────────────────────────────────────────
function GeneralConfigForm({ stack }: { stack: Stack }) {
  const queryClient = useQueryClient();
  const inputs = (stack as any).inputs || {};
  const isGit = stack.blueprint_id === "git";

  const [branch, setBranch] = useState<string>(inputs.branch ?? "main");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    setSaving(true);
    try {
      const { error } = await (api as any).PATCH(`/api/stacks/${stack.id}/config`, {
        body: { branch },
      });
      if (error) {
        toast.error(apiErrorMessage(error, "Failed to save configuration"));
        return;
      }
      toast.success("Configuration saved.");
      setDirty(false);
      await queryClient.invalidateQueries({ queryKey: ["stacks", stack.id] });
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">General Configuration</CardTitle>
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>Deployment Name</Label>
            <Input value={stack.name} disabled />
            <p className="text-xs text-muted-foreground">
              Name cannot be changed after creation.
            </p>
          </div>
          <div className="space-y-1.5">
            <Label>Blueprint</Label>
            <Input value={stack.blueprint_id} disabled className="font-mono" />
          </div>
        </div>

        {isGit && (
          <>
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label>Repository</Label>
                <Input
                  value={inputs.repo ?? ""}
                  disabled
                  className="font-mono text-xs"
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="gen-branch">Branch</Label>
                <Input
                  id="gen-branch"
                  value={branch}
                  onChange={(e) => {
                    setBranch(e.target.value);
                    setDirty(true);
                  }}
                  spellCheck={false}
                />
              </div>
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label>Build Method</Label>
                <Input value={inputs.build_method ?? "nixpacks"} disabled />
              </div>
              <div className="space-y-1.5">
                <Label>Port</Label>
                <Input value={String(inputs.internal_port ?? 3000)} disabled />
              </div>
            </div>
          </>
        )}

        {!isGit && inputs.image && (
          <div className="space-y-1.5">
            <Label>Image</Label>
            <Input value={inputs.image ?? ""} disabled className="font-mono text-xs" />
          </div>
        )}

        {dirty && (
          <div className="flex items-center gap-3 pt-1">
            <Button size="sm" onClick={handleSave} loading={saving}>
              <Save className="h-3.5 w-3.5 mr-2" />
              Save Configuration
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setBranch(inputs.branch ?? "main");
                setDirty(false);
              }}
            >
              Discard
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ─── StackBackupsCard ─────────────────────────────────────────────────────────
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
          <LoadingState label="Loading backups…" />
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

// ─── LogsCard ─────────────────────────────────────────────────────────────────
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

// ─── StackToolsCard ───────────────────────────────────────────────────────────
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

// ─── ActionsCard ──────────────────────────────────────────────────────────────
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

// ─── DeleteStackDialog ────────────────────────────────────────────────────────
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
          Type the stack name{" "}
          <code className="select-all rounded bg-muted px-1 font-mono text-foreground">
            {stack.name}
          </code>{" "}
          to confirm.
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

// ─── PostStartCommandCard ─────────────────────────────────────────────────────
function PostStartCommandCard({
  stack,
  onOperation,
}: {
  stack: Stack;
  onOperation: (id: number) => void;
}) {
  const queryClient = useQueryClient();
  const portServices = stack.services.filter((s) => s.internal_port != null);
  const [commands, setCommands] = useState<Record<string, string>>(
    Object.fromEntries(portServices.map((s) => [s.name, (s as any).post_start_command ?? ""])),
  );

  const save = useMutation({
    mutationFn: async (serviceName: string) => {
      const { data, error } = await api.PUT("/api/stacks/{stack_id}/post-start-command" as any, {
        params: { path: { stack_id: stack.id } },
        body: { service_name: serviceName, command: commands[serviceName] ?? "" },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Could not save command"));
      return data as any;
    },
    onSuccess: (data, serviceName) => {
      toast.success(`Post-start command saved for "${serviceName}"`);
      onOperation(data.operation_id);
      void queryClient.invalidateQueries({ queryKey: ["stack", stack.id] });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Failed to save command"),
  });

  if (portServices.length === 0) return null;

  return (
    <Card className="mt-4">
      <CardHeader>
        <CardTitle className="text-base">Post-start Commands</CardTitle>
        <p className="text-sm text-muted-foreground">
          Run a command inside the container after it starts — ideal for database migrations, cache
          warming, or seed scripts.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {portServices.map((svc) => (
          <div key={svc.name} className="space-y-1">
            <p className="text-xs font-medium text-muted-foreground">
              Command for <span className="font-mono text-foreground">{svc.name}</span>
            </p>
            <div className="flex gap-2">
              <Input
                className="flex-1 font-mono text-sm"
                placeholder="php artisan migrate --force"
                value={commands[svc.name] ?? ""}
                onChange={(e) => setCommands((prev) => ({ ...prev, [svc.name]: e.target.value }))}
                spellCheck={false}
              />
              <Button
                size="sm"
                loading={save.isPending && save.variables === svc.name}
                onClick={() => save.mutate(svc.name)}
              >
                Save
              </Button>
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

// ─── EndpointsCard ────────────────────────────────────────────────────────────
function ServiceDomainRow({
  stack,
  serviceName,
  currentDomain,
  onOperation,
}: {
  stack: Stack;
  serviceName: string;
  currentDomain: string | undefined;
  onOperation: (operationId: number) => void;
}) {
  const queryClient = useQueryClient();
  const [domain, setDomain] = useState(currentDomain ?? "");

  const setDomainMut = useMutation({
    mutationFn: async (value: string | null) => {
      const { data, error } = await api.PUT("/api/stacks/{stack_id}/domain", {
        params: { path: { stack_id: stack.id } },
        body: { domain: value, behind_cloudflare: false, service_name: serviceName },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Could not set the domain"));
      return data;
    },
    onSuccess: (data) => {
      toast.success("Updating domain…");
      onOperation(data.operation_id);
      void queryClient.invalidateQueries({ queryKey: ["stack", stack.id] });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Failed to set the domain"),
  });

  const autogenerate = async () => {
    const slug = `${stack.name}-${serviceName}`;
    const { data, error } = await api.GET("/api/stacks/suggested-domain", {
      params: { query: { name: slug } },
    });
    if (error || !data) {
      toast.error(apiErrorMessage(error, "Set an apps base domain or server public IP first"));
      return;
    }
    setDomain(data.domain);
  };

  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-muted-foreground">
        Domain for <span className="font-mono text-foreground">{serviceName}</span>
      </p>
      <div className="flex items-center gap-2">
        <Input
          className="flex-1 font-mono text-sm"
          value={domain}
          placeholder="app.example.com"
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => setDomain(e.target.value)}
        />
        <Button type="button" variant="outline" size="sm" onClick={autogenerate}>
          Generate
        </Button>
        <Button
          size="sm"
          loading={setDomainMut.isPending}
          onClick={() => setDomainMut.mutate(domain.trim() || null)}
        >
          {currentDomain ? "Update" : "Set"}
        </Button>
      </div>
      {currentDomain && (
        <a
          href={`https://${currentDomain}`}
          target="_blank"
          rel="noreferrer"
          className="flex items-center gap-1 text-xs text-primary hover:underline"
        >
          <ExternalLink className="h-3 w-3" />
          {currentDomain}
        </a>
      )}
    </div>
  );
}

function EndpointsCard({
  stack,
  onOperation,
}: {
  stack: Stack;
  onOperation: (operationId: number) => void;
}) {
  const portServices = stack.services.filter((s) => s.internal_port != null);

  if (portServices.length === 0) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Endpoints</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">No services expose a port.</p>
        </CardContent>
      </Card>
    );
  }

  const endpointByService = Object.fromEntries(
    stack.endpoints.map((ep) => [ep.service_name, ep.domain])
  );

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Endpoints</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {portServices.map((svc) => (
          <ServiceDomainRow
            key={svc.name}
            stack={stack}
            serviceName={svc.name}
            currentDomain={endpointByService[svc.name]}
            onOperation={onOperation}
          />
        ))}
      </CardContent>
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

// ─── EnvVarsCard ──────────────────────────────────────────────────────────────
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
      envString.split("\n").forEach((line) => {
        const match = line.match(/^([^=]+)=(.*)$/);
        if (match && match[1] && match[2] !== undefined) {
          parsedEnv[match[1].trim()] = match[2].trim();
        }
      });
      const { data, error } = await api.PUT("/api/stacks/{stack_id}/env" as any, {
        params: { path: { stack_id: stack.id } },
        body: { env: parsedEnv },
      });
      if (error || !data)
        throw new Error(apiErrorMessage(error, "Could not update environment variables"));
      return data;
    },
    onSuccess: (data) => {
      toast.success("Environment variables updated. Restarting stack…");
      // Show any advisory warnings (e.g. PORT mismatch)
      for (const w of (data as any).warnings ?? []) {
        toast.warning(w);
      }
      onOperation(data.operation_id);
      setEditing(false);
      void queryClient.invalidateQueries({ queryKey: ["stack", stack.id] });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Failed to update env vars"),
  });

  const envObj = (stack as any).inputs?.env || {};
  const currentEnvString = Object.entries(envObj)
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");

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
          <div className="rounded-md border bg-muted/40 p-3 max-h-96 overflow-y-auto">
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
            Variables must be in <code>KEY=value</code> format, one per line. They will be applied
            on the next stack restart.
          </DialogDescription>
          <div className="py-2">
            <textarea
              className="flex min-h-[400px] w-full rounded-md border border-input bg-background px-3 py-2 text-sm font-mono ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
              placeholder={"PORT=3000\nDATABASE_URL=postgres://..."}
              value={envVars}
              onChange={(e) => setEnvVars(e.target.value)}
              spellCheck={false}
            />
          </div>
          <DialogActions>
            <Button variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
            <Button loading={updateEnv.isPending} onClick={() => updateEnv.mutate(envVars)}>
              Save & Restart
            </Button>
          </DialogActions>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

// ─── ConnectionsCard ──────────────────────────────────────────────────────────
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
        {
          params: { path: { stack_id: stack.id, service_name: service } },
          body: { exposed },
        },
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
            can connect. Make sure the password is strong. You can make it private again at any time.
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

// ─── ServicesCard ─────────────────────────────────────────────────────────────
function ServicesCard({ stack }: { stack: Stack }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Containers</CardTitle>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Service</TableHead>
              <TableHead>Image</TableHead>
              <TableHead>Port</TableHead>
              <TableHead className="w-10" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {stack.services.map((svc) => (
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
                <TableCell>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    title={`Open terminal for ${svc.name}`}
                    disabled={stack.status !== "ready"}
                    onClick={() =>
                      window.open(
                        `/stacks/${stack.id}/terminal/${svc.name}`,
                        `terminal-${stack.id}-${svc.name}`,
                        "width=960,height=600,noopener,noreferrer",
                      )
                    }
                  >
                    <Terminal className="h-3.5 w-3.5" aria-hidden />
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

// ─── StackDetailPage ──────────────────────────────────────────────────────────
export function StackDetailPage() {
  const { stackId } = useParams();
  const location = useLocation();
  const queryClient = useQueryClient();
  const initialOpId = (location.state as { operationId?: number } | null)?.operationId ?? null;
  const [operationId, setOperationId] = useState<number | null>(initialOpId);
  const [deleteOpen, setDeleteOpen] = useState(false);
  // Auto-switch to Deployments tab when arriving from a deploy action
  const [activeTab, setActiveTab] = useState(initialOpId != null ? "Deployments" : "Configuration");
  const [activeSubTab, setActiveSubTab] = useState("General");

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

  // Redeploy — calls the blueprint "rebuild" action (available for git blueprint)
  const redeploy = useMutation({
    mutationFn: async () => {
      const { data: result, error, response } = await api.POST(
        "/api/stacks/{stack_id}/actions/{action_name}",
        { params: { path: { stack_id: id, action_name: "rebuild" } } },
      );
      if (error || !result)
        throw new Error(apiErrorMessage(error, `Redeploy failed (${response.status})`));
      return result;
    },
    onSuccess: (result) => {
      if (result.ok) toast.success(result.message || "Redeploying…");
      else toast.error(result.message || "Redeploy failed");
      void queryClient.invalidateQueries({ queryKey: ["stacks"] });
    },
    onError: (err) => toast.error(err.message),
  });

  if (stack.isPending) return <LoadingState label="Loading stack…" />;
  if (stack.isError) {
    return <ErrorState message={stack.error.message} onRetry={() => stack.refetch()} />;
  }

  const data = stack.data;
  const blueprintActions =
    blueprints.data?.find((bp) => bp.id === data.blueprint_id)?.actions ?? [];
  const canRedeploy = blueprintActions.includes("rebuild");
  const inputs = (data as any).inputs || {};
  const isGit = data.blueprint_id === "git";

  const TABS = ["Configuration", "Deployments", "Logs", "Terminal", "Links"];

  return (
    <div>
      {/* ── Heading (matches Coolify heading.blade.php) ── */}
      <div className="border-b border-border pb-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          {/* Left: back + name + metadata */}
          <div className="flex items-start gap-3">
            <Link
              to="/stacks"
              aria-label="Back to projects"
              className="mt-1 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md hover:bg-accent transition-colors"
            >
              <ArrowLeft className="h-4 w-4" />
            </Link>
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="text-xl font-bold tracking-tight">{data.name}</h1>
                <StackStatusBadge stack={data} />
                <Badge variant="outline" className="font-mono text-xs">
                  {data.blueprint_id}
                </Badge>
              </div>

              {/* Git repo:branch subtitle */}
              {isGit && inputs.repo && (
                <p className="mt-1 text-xs font-mono text-muted-foreground">
                  {inputs.repo.replace(/^https?:\/\//, "")}
                  {" : "}
                  <span className="text-foreground/70">{inputs.branch ?? "main"}</span>
                </p>
              )}

              {/* Domain link */}
              {data.endpoints[0]?.domain && (
                <div className="mt-1 flex items-center gap-1">
                  <Globe className="h-3 w-3 text-muted-foreground" />
                  <a
                    href={`https://${data.endpoints[0].domain}`}
                    target="_blank"
                    rel="noreferrer"
                    className="text-xs text-primary hover:underline"
                  >
                    {data.endpoints[0].domain}
                  </a>
                  <ExternalLink className="h-3 w-3 text-muted-foreground" />
                </div>
              )}
            </div>
          </div>

          {/* Right: action buttons */}
          {canRedeploy && (
            <div className="flex items-center gap-2 shrink-0">
              <Button
                size="sm"
                loading={redeploy.isPending}
                disabled={
                  data.status === "deleting" ||
                  data.status === "converging" ||
                  redeploy.isPending
                }
                onClick={() => redeploy.mutate()}
              >
                <RefreshCw className="h-3.5 w-3.5 mr-2" />
                Redeploy
              </Button>
            </div>
          )}
        </div>
      </div>

      {/* ── Horizontal tab bar ── */}
      <div className="flex border-b border-border">
        {TABS.map((tab) => (
          <button
            key={tab}
            className={[
              "px-5 py-3 text-sm border-b-2 transition-colors",
              activeTab === tab
                ? "border-primary text-foreground font-medium"
                : "border-transparent text-muted-foreground hover:text-foreground",
            ].join(" ")}
            onClick={() => setActiveTab(tab)}
          >
            {tab}
          </button>
        ))}
      </div>

      {/* ── Banners ── */}
      {(data.error_message || operationId !== null) && (
        <div className="mt-4 space-y-3 px-0">
          {data.error_message && (
            <p className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
              {data.error_message}
            </p>
          )}
          {operationId !== null && activeTab !== "Deployments" && (
            <OperationProgress
              operationId={operationId}
              onFinished={async (op) => {
                await queryClient.invalidateQueries({ queryKey: ["stacks"] });
                await queryClient.invalidateQueries({ queryKey: ["stacks", id, "operations"] });
                if (op.status === "succeeded") setOperationId(null);
              }}
            />
          )}
        </div>
      )}

      {/* ── Configuration tab: sub-nav + content ── */}
      {activeTab === "Configuration" && (
        <>
        {data.generation !== data.observed_generation && (
          <div className="mb-4 flex items-center justify-between gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-700 dark:text-amber-400">
            <div className="flex items-center gap-2">
              <svg className="h-4 w-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" /></svg>
              <span>Configuration changed — click <strong>Redeploy</strong> to apply the changes.</span>
            </div>
            {canRedeploy && (
              <button
                className="shrink-0 rounded-md bg-amber-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-600 disabled:opacity-50"
                disabled={redeploy.isPending || data.status === "converging"}
                onClick={() => redeploy.mutate()}
              >
                {redeploy.isPending ? "Redeploying…" : "Redeploy now"}
              </button>
            )}
          </div>
        )}
        <div className="flex min-h-[600px]">
          {/* Left sub-nav (matches Coolify sub-menu-wrapper exactly) */}
          <nav className="w-52 shrink-0 border-r border-border pt-5 pr-2">
            {SUB_NAV_GROUPS.map((group, groupIdx) => (
              <div key={groupIdx}>
                {groupIdx > 0 && <div className="my-2 border-t border-border/60" />}
                <div className="space-y-0.5">
                  {group.map((sub) => {
                    const isActive = activeSubTab === sub;
                    const isDanger = sub === "Danger Zone";
                    return (
                      <button
                        key={sub}
                        className={[
                          "flex w-full items-center rounded-md px-3 py-2 text-sm transition-colors text-left",
                          isActive
                            ? isDanger
                              ? "bg-destructive/10 text-destructive font-medium"
                              : "bg-accent text-foreground font-medium"
                            : isDanger
                              ? "text-destructive/70 hover:bg-destructive/10 hover:text-destructive"
                              : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                        ].join(" ")}
                        onClick={() => setActiveSubTab(sub)}
                      >
                        {sub}
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
          </nav>

          {/* Right content panel */}
          <div className="flex-1 min-w-0 pl-8 pt-5 space-y-6">
            {/* General */}
            {activeSubTab === "General" && (
              <>
                <GeneralConfigForm stack={data} />
                <EndpointsCard stack={data} onOperation={setOperationId} />
                <ServicesCard stack={data} />
                <ConnectionsCard stack={data} onOperation={setOperationId} />
              </>
            )}

            {/* Environment Variables */}
            {activeSubTab === "Environment Variables" && (
              <EnvVarsCard stack={data} onOperation={setOperationId} />
            )}

            {/* Persistent Storage */}
            {activeSubTab === "Persistent Storage" && (
              <StackBackupsCard stack={data} onOperation={setOperationId} />
            )}

            {/* Resource Operations */}
            {activeSubTab === "Resource Operations" && (
              <ActionsCard stack={data} actions={blueprintActions} />
            )}

            {/* Advanced */}
            {activeSubTab === "Advanced" && (
              <>
                <AdvancedSettings stackId={Number(id)} inputs={data.inputs} />
                <PostStartCommandCard stack={data} onOperation={setOperationId} />
                <HealthCheckCard stackId={Number(id)} services={(data.services ?? []) as any} />
              </>
            )}

            {/* Git Source */}
            {activeSubTab === "Git Source" && (
              <GitSourceSettings stackId={Number(id)} inputs={data.inputs} />
            )}

            {/* Servers */}
            {activeSubTab === "Servers" && <ServersList />}

            {/* Scheduled Tasks */}
            {activeSubTab === "Scheduled Tasks" && (
              <ScheduledTasksList stackId={Number(id)} />
            )}

            {/* Webhooks */}
            {activeSubTab === "Webhooks" && <WebhooksConfig stackId={Number(id)} />}

            {/* Preview Deployments */}
            {activeSubTab === "Preview Deployments" && <PreviewDeploymentsConfig />}

            {/* Rollback */}
            {activeSubTab === "Rollback" && <RollbackList stackId={Number(id)} onOperation={setOperationId} />}

            {/* Resource Limits */}
            {activeSubTab === "Resource Limits" && (
              <ResourceLimitsConfig stackId={Number(id)} inputs={data.inputs} />
            )}

            {/* Metrics */}
            {activeSubTab === "Metrics" && <MetricsView stackId={Number(id)} />}

            {/* Tags */}
            {activeSubTab === "Tags" && <TagsConfig stackId={Number(id)} />}

            {/* Danger Zone */}
            {activeSubTab === "Danger Zone" && (
              <>
                <StackToolsCard stack={data} />
                <Card className="border-destructive/50">
                  <CardHeader>
                    <CardTitle className="text-destructive text-base">Delete Stack</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <p className="text-sm text-muted-foreground mb-4">
                      Once you delete this stack, there is no going back. All containers, volumes,
                      and routes will be permanently removed.
                    </p>
                    <Button variant="destructive" onClick={() => setDeleteOpen(true)}>
                      <Trash2 className="h-4 w-4 mr-2" aria-hidden /> Delete {data.name}
                    </Button>
                  </CardContent>
                </Card>
              </>
            )}
          </div>
        </div>
        </>
      )}

      {/* ── Other tabs ── */}
      {activeTab === "Logs" && (
        <div className="mt-5">
          <LogsCard stack={data} />
        </div>
      )}

      {activeTab === "Deployments" && (
        <div className="mt-5">
          <DeploymentsTab stackId={Number(id)} activeOperationId={operationId} />
        </div>
      )}

      {activeTab === "Terminal" && (
        <div className="mt-5">
          <TerminalTab />
        </div>
      )}

      {activeTab === "Links" && (
        <div className="mt-5">
          <LinksTab stackId={id} endpoints={data.endpoints} />
        </div>
      )}

      <DeleteStackDialog stack={data} open={deleteOpen} onClose={() => setDeleteOpen(false)} />
    </div>
  );
}
