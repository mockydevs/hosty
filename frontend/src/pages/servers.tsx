import { FormField } from "@/components/form-field";
import { ErrorState, LoadingState } from "@/components/states";
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
import { Label } from "@/components/ui/label";
import { api, apiErrorMessage } from "@/lib/api/client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  Cpu,
  HardDrive,
  Laptop,
  MemoryStick,
  Plus,
  RefreshCw,
  Server,
  Trash2,
  XCircle,
} from "lucide-react";
import type React from "react";
import { useState } from "react";
import { toast } from "sonner";

type ServerRecord = {
  id: number;
  name: string;
  hostname: string;
  port: number;
  ssh_user: string;
  ssh_key_id: number | null;
  is_localhost: boolean;
  status: string;
  error_message: string | null;
  os_info: string | null;
  cpu_count: number | null;
  memory_mb: number | null;
  disk_free_gb: number | null;
  podman_version: string | null;
  created_at: string;
  last_checked_at: string | null;
};

type SshKeyRecord = { id: number; name: string; created_at: string };

function statusBadge(status: string) {
  if (status === "connected")
    return <Badge variant="success" className="gap-1"><CheckCircle2 className="h-3 w-3" />Connected</Badge>;
  if (status === "error")
    return <Badge variant="destructive" className="gap-1"><XCircle className="h-3 w-3" />Error</Badge>;
  return <Badge variant="outline" className="gap-1 text-muted-foreground">Pending</Badge>;
}

function fmtMemory(mb: number | null): string {
  if (mb === null) return "—";
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`;
  return `${mb} MB`;
}

function ServerCard({
  server,
  onValidate,
  onDelete,
  validating,
}: {
  server: ServerRecord;
  onValidate: () => void;
  onDelete: () => void;
  validating: boolean;
}) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            {server.is_localhost ? (
              <Laptop className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
            ) : (
              <Server className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
            )}
            <div className="min-w-0">
              <CardTitle className="text-base truncate">{server.name}</CardTitle>
              <CardDescription className="font-mono text-xs truncate">
                {server.is_localhost ? "localhost" : `${server.ssh_user}@${server.hostname}:${server.port}`}
              </CardDescription>
            </div>
          </div>
          {statusBadge(server.status)}
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {server.os_info && (
          <p className="text-xs text-muted-foreground">{server.os_info}</p>
        )}

        {server.status === "connected" && (
          <div className="grid grid-cols-3 gap-2 text-xs">
            <div className="flex items-center gap-1 text-muted-foreground">
              <Cpu className="h-3 w-3" aria-hidden />
              <span>{server.cpu_count ?? "—"} vCPU</span>
            </div>
            <div className="flex items-center gap-1 text-muted-foreground">
              <MemoryStick className="h-3 w-3" aria-hidden />
              <span>{fmtMemory(server.memory_mb)}</span>
            </div>
            <div className="flex items-center gap-1 text-muted-foreground">
              <HardDrive className="h-3 w-3" aria-hidden />
              <span>{server.disk_free_gb !== null ? `${server.disk_free_gb} GB free` : "—"}</span>
            </div>
          </div>
        )}

        {server.podman_version && (
          <p className="text-xs text-muted-foreground font-mono">
            Podman {server.podman_version}
          </p>
        )}

        {server.status === "error" && server.error_message && (
          <p className="text-xs text-destructive break-words">{server.error_message}</p>
        )}

        {server.last_checked_at && (
          <p className="text-xs text-muted-foreground/60">
            Checked {new Date(server.last_checked_at).toLocaleString()}
          </p>
        )}

        <div className="flex gap-2 pt-1">
          <Button
            variant="outline"
            size="sm"
            className="flex-1 gap-1 text-xs"
            loading={validating}
            onClick={onValidate}
          >
            <RefreshCw className="h-3 w-3" aria-hidden />
            Validate
          </Button>
          {!server.is_localhost && (
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8 text-destructive opacity-80 hover:opacity-100"
              onClick={onDelete}
            >
              <Trash2 className="h-3.5 w-3.5" aria-hidden />
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function AddServerDialog({
  open,
  onClose,
  sshKeys,
}: {
  open: boolean;
  onClose: () => void;
  sshKeys: SshKeyRecord[];
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [hostname, setHostname] = useState("");
  const [port, setPort] = useState("22");
  const [sshUser, setSshUser] = useState("root");
  const [sshKeyId, setSshKeyId] = useState<string>("");
  const [isLocalhost, setIsLocalhost] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setName("");
    setHostname("");
    setPort("22");
    setSshUser("root");
    setSshKeyId("");
    setIsLocalhost(false);
    setError(null);
  };

  const create = useMutation({
    mutationFn: async () => {
      const body = {
        name,
        hostname: isLocalhost ? "localhost" : hostname,
        port: parseInt(port, 10) || 22,
        ssh_user: sshUser,
        ssh_key_id: sshKeyId ? parseInt(sshKeyId, 10) : null,
        is_localhost: isLocalhost,
      };
      const { data, error: apiError, response } = await (api as any).POST("/api/servers", {
        body,
      });
      if (apiError || !data)
        throw new Error(apiErrorMessage(apiError, `Failed (${response?.status})`));
      return data as ServerRecord;
    },
    onSuccess: async () => {
      toast.success("Server added");
      reset();
      onClose();
      await queryClient.invalidateQueries({ queryKey: ["servers"] });
    },
    onError: (err) => setError(err instanceof Error ? err.message : "Unknown error"),
  });

  const canSubmit = name.trim() && (isLocalhost || (hostname.trim() && sshKeyId));

  return (
    <Dialog
      open={open}
      onClose={() => {
        reset();
        onClose();
      }}
    >
      <DialogContent className="sm:max-w-lg">
        <DialogTitle>Add Server</DialogTitle>
        <DialogDescription>
          Connect a remote VPS or register this machine as a deployment target.
        </DialogDescription>

        <form
          className="space-y-4"
          onSubmit={(e: React.FormEvent) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          {/* Localhost toggle */}
          <label className="flex items-center gap-3 rounded-md border px-3 py-2.5 cursor-pointer hover:bg-muted/50 transition-colors">
            <input
              type="checkbox"
              checked={isLocalhost}
              onChange={(e) => setIsLocalhost(e.target.checked)}
              className="h-4 w-4 rounded border-input accent-primary"
            />
            <div>
              <p className="text-sm font-medium leading-none">This is the panel's own machine</p>
              <p className="text-xs text-muted-foreground mt-0.5">No SSH needed — uses local Podman socket</p>
            </div>
          </label>

          <FormField label="Display Name" htmlFor="srv-name" error={error ?? undefined}>
            <Input
              id="srv-name"
              placeholder="e.g. production-us-east"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </FormField>

          {!isLocalhost && (
            <>
              <div className="grid grid-cols-3 gap-3">
                <div className="col-span-2">
                  <FormField label="Hostname / IP" htmlFor="srv-hostname">
                    <Input
                      id="srv-hostname"
                      placeholder="192.168.1.100 or server.example.com"
                      value={hostname}
                      onChange={(e) => setHostname(e.target.value)}
                    />
                  </FormField>
                </div>
                <FormField label="Port" htmlFor="srv-port">
                  <Input
                    id="srv-port"
                    type="number"
                    min={1}
                    max={65535}
                    value={port}
                    onChange={(e) => setPort(e.target.value)}
                  />
                </FormField>
              </div>

              <FormField label="SSH User" htmlFor="srv-user">
                <Input
                  id="srv-user"
                  placeholder="root"
                  value={sshUser}
                  onChange={(e) => setSshUser(e.target.value)}
                />
              </FormField>

              <div className="space-y-1.5">
                <Label htmlFor="srv-key">SSH Key</Label>
                {sshKeys.length === 0 ? (
                  <p className="text-xs text-muted-foreground">
                    No SSH keys found. Add one on the{" "}
                    <a href="/security" className="underline">Security</a> page first.
                  </p>
                ) : (
                  <select
                    id="srv-key"
                    value={sshKeyId}
                    onChange={(e) => setSshKeyId(e.target.value)}
                    className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                  >
                    <option value="">— select a key —</option>
                    {sshKeys.map((k) => (
                      <option key={k.id} value={k.id}>
                        {k.name}
                      </option>
                    ))}
                  </select>
                )}
              </div>
            </>
          )}

          <DialogActions>
            <Button
              variant="outline"
              type="button"
              onClick={() => {
                reset();
                onClose();
              }}
            >
              Cancel
            </Button>
            <Button type="submit" loading={create.isPending} disabled={!canSubmit}>
              Add Server
            </Button>
          </DialogActions>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function ServersPage() {
  const queryClient = useQueryClient();
  const [addOpen, setAddOpen] = useState(false);

  const servers = useQuery({
    queryKey: ["servers"],
    queryFn: async () => {
      const { data, error } = await (api as any).GET("/api/servers");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load servers"));
      return data as ServerRecord[];
    },
  });

  const sshKeys = useQuery({
    queryKey: ["security", "keys"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/security/keys");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load SSH keys"));
      return data as SshKeyRecord[];
    },
  });

  const [validatingId, setValidatingId] = useState<number | null>(null);
  const validate = useMutation({
    mutationFn: async (id: number) => {
      setValidatingId(id);
      const { data, error } = await (api as any).POST(`/api/servers/${id}/validate`);
      if (error || !data) throw new Error(apiErrorMessage(error, "Validation failed"));
      return data as ServerRecord;
    },
    onSuccess: async (data) => {
      if (data.status === "connected") {
        toast.success(`${data.name}: connected`);
      } else {
        toast.error(`${data.name}: ${data.error_message ?? "connection failed"}`);
      }
      await queryClient.invalidateQueries({ queryKey: ["servers"] });
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "Validation failed"),
    onSettled: () => setValidatingId(null),
  });

  const deleteServer = useMutation({
    mutationFn: async (id: number) => {
      const { error } = await (api as any).DELETE(`/api/servers/${id}`);
      if (error) throw new Error(apiErrorMessage(error, "Delete failed"));
    },
    onSuccess: async () => {
      toast.success("Server removed");
      await queryClient.invalidateQueries({ queryKey: ["servers"] });
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "Delete failed"),
  });

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Servers</h1>
          <p className="text-sm text-muted-foreground">
            Deployment targets — the panel orchestrates containers on each connected server.
          </p>
        </div>
        <Button onClick={() => setAddOpen(true)}>
          <Plus className="h-4 w-4" aria-hidden /> Add Server
        </Button>
      </div>

      {servers.isPending ? (
        <LoadingState />
      ) : servers.isError ? (
        <ErrorState message={servers.error.message} onRetry={() => servers.refetch()} />
      ) : servers.data.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-12 text-center">
            <Server className="mb-4 h-12 w-12 text-muted-foreground/50" aria-hidden />
            <h2 className="text-lg font-semibold">No servers yet</h2>
            <p className="mb-4 text-sm text-muted-foreground max-w-sm">
              Add your first server to start deploying containers.
            </p>
            <Button onClick={() => setAddOpen(true)}>
              <Plus className="h-4 w-4" aria-hidden /> Add Server
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {servers.data.map((s) => (
            <ServerCard
              key={s.id}
              server={s}
              validating={validatingId === s.id}
              onValidate={() => validate.mutate(s.id)}
              onDelete={() => {
                if (confirm(`Remove server "${s.name}"?`)) {
                  deleteServer.mutate(s.id);
                }
              }}
            />
          ))}
        </div>
      )}

      <AddServerDialog
        open={addOpen}
        onClose={() => setAddOpen(false)}
        sshKeys={sshKeys.data ?? []}
      />
    </div>
  );
}
