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
import { ShowOnceDialog } from "@/pages/stack-create";
import { StackStatusBadge, isSettling } from "@/pages/stacks";
/**
 * Stack detail (v2/M4): services, endpoints, journald logs viewer, blueprint
 * day-2 actions (with confirm + show-once results), delete with
 * type-to-confirm. Polls while the reconciler is converging.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ExternalLink, Play, RefreshCw, RotateCcw, Save, Trash2 } from "lucide-react";
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
          Type the stack name to confirm.
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
                      {svc.internal_port ? `${svc.internal_port} → ${svc.host_port}` : "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Endpoints</CardTitle>
          </CardHeader>
          <CardContent>
            {data.endpoints.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No public domains — services are reachable only inside the stack.
              </p>
            ) : (
              <ul className="space-y-2 text-sm">
                {data.endpoints.map((ep) => (
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
                    <span className="font-mono text-xs text-muted-foreground">
                      → {ep.service_name}
                    </span>
                    {ep.behind_cloudflare && <Badge variant="outline">Cloudflare</Badge>}
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>

      <ActionsCard stack={data} actions={actions} />
      <StackBackupsCard stack={data} onOperation={setOperationId} />
      <LogsCard stack={data} />
      <DeleteStackDialog stack={data} open={deleteOpen} onClose={() => setDeleteOpen(false)} />
    </div>
  );
}
