
import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { toast } from "sonner";
import { Activity } from "lucide-react";

// ── Advanced settings (auto deploy, force rebuild) ────────────────────────────

export function AdvancedSettings({ stackId, inputs }: { stackId: number, inputs: any }) {
  const queryClient = useQueryClient();
  const [autoDeploy, setAutoDeploy] = useState(inputs.auto_deploy || false);
  const [forceRebuild, setForceRebuild] = useState(inputs.force_rebuild || false);

  const save = useMutation({
    mutationFn: async () => {
      // @ts-ignore
      const res = await api.PATCH(`/api/stacks/${stackId}/config`, { body: { auto_deploy: autoDeploy, force_rebuild: forceRebuild } });
      if (res.error) throw new Error("Failed to save");
      return res.data as any;
    },
    onSuccess: () => {
      toast.success("Settings saved");
      queryClient.invalidateQueries({ queryKey: ["stacks", stackId] });
    }
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Advanced Settings</CardTitle>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="flex items-center justify-between">
          <div className="space-y-0.5">
            <Label>Auto Deploy</Label>
            <p className="text-sm text-muted-foreground">Automatically deploy when new commits are pushed.</p>
          </div>
          <input type="checkbox" checked={autoDeploy} onChange={(e) => setAutoDeploy(e.target.checked)} />
        </div>
        <div className="flex items-center justify-between">
          <div className="space-y-0.5">
            <Label>Force Rebuild</Label>
            <p className="text-sm text-muted-foreground">Ignore cache and rebuild from scratch on next deployment.</p>
          </div>
          <input type="checkbox" checked={forceRebuild} onChange={(e) => setForceRebuild(e.target.checked)} />
        </div>
        <Button onClick={() => save.mutate()}>Save Advanced Settings</Button>
      </CardContent>
    </Card>
  );
}

// ── Health check card ─────────────────────────────────────────────────────────

interface ServiceRow {
  name: string;
  internal_port: number | null;
  health_check_enabled: boolean;
  health_check_path: string | null;
  health_check_port: number | null;
  health_check_interval: number;
  health_check_retries: number;
  health_check_start_period: number;
  health_check_timeout: number;
}

function ServiceHealthRow({
  stackId,
  svc,
}: {
  stackId: number;
  svc: ServiceRow;
}) {
  const queryClient = useQueryClient();
  const [enabled, setEnabled] = useState(svc.health_check_enabled);
  const [path, setPath] = useState(svc.health_check_path ?? "/health");
  const [port, setPort] = useState<string>(svc.health_check_port ? String(svc.health_check_port) : "");
  const [interval, setInterval] = useState(String(svc.health_check_interval));
  const [retries, setRetries] = useState(String(svc.health_check_retries));
  const [startPeriod, setStartPeriod] = useState(String(svc.health_check_start_period));
  const [timeout, setTimeout] = useState(String(svc.health_check_timeout));

  const save = useMutation({
    mutationFn: async () => {
      // @ts-ignore
      const res = await api.PUT(`/api/stacks/${stackId}/health-check` as any, {
        body: {
          service_name: svc.name,
          enabled,
          path: path || "/health",
          port: port ? Number(port) : null,
          interval: Number(interval) || 10,
          retries: Number(retries) || 3,
          start_period: Number(startPeriod) || 30,
          timeout: Number(timeout) || 5,
        },
      });
      if (res.error) throw new Error("Failed to save");
      return res.data as any;
    },
    onSuccess: () => {
      toast.success(`Health check saved for ${svc.name}`);
      queryClient.invalidateQueries({ queryKey: ["stacks", stackId] });
    },
    onError: (e: any) => toast.error(e.message),
  });

  return (
    <div className="rounded-lg border bg-muted/30 p-4 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm font-medium">{svc.name}</p>
          {svc.internal_port && (
            <p className="text-xs text-muted-foreground">Port {svc.internal_port}</p>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Label className="text-xs">Enabled</Label>
          <Switch checked={enabled} onCheckedChange={setEnabled} />
        </div>
      </div>

      {enabled && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          <div className="col-span-2 sm:col-span-1 grid gap-1.5">
            <Label className="text-xs">Path</Label>
            <Input className="h-8 text-sm" value={path} onChange={(e) => setPath(e.target.value)} placeholder="/health" />
          </div>
          <div className="grid gap-1.5">
            <Label className="text-xs">Port override</Label>
            <Input className="h-8 text-sm" type="number" value={port} onChange={(e) => setPort(e.target.value)} placeholder="default" />
          </div>
          <div className="grid gap-1.5">
            <Label className="text-xs">Interval (s)</Label>
            <Input className="h-8 text-sm" type="number" value={interval} onChange={(e) => setInterval(e.target.value)} />
          </div>
          <div className="grid gap-1.5">
            <Label className="text-xs">Retries</Label>
            <Input className="h-8 text-sm" type="number" value={retries} onChange={(e) => setRetries(e.target.value)} />
          </div>
          <div className="grid gap-1.5">
            <Label className="text-xs">Start period (s)</Label>
            <Input className="h-8 text-sm" type="number" value={startPeriod} onChange={(e) => setStartPeriod(e.target.value)} />
          </div>
          <div className="grid gap-1.5">
            <Label className="text-xs">Timeout (s)</Label>
            <Input className="h-8 text-sm" type="number" value={timeout} onChange={(e) => setTimeout(e.target.value)} />
          </div>
        </div>
      )}

      <Button
        size="sm"
        variant="outline"
        onClick={() => save.mutate()}
        disabled={save.isPending || svc.internal_port === null}
      >
        {save.isPending ? "Saving…" : "Save"}
      </Button>
      {svc.internal_port === null && (
        <p className="text-xs text-muted-foreground">Health checks require a service with an internal port.</p>
      )}
    </div>
  );
}

export function HealthCheckCard({
  stackId,
  services,
}: {
  stackId: number;
  services: ServiceRow[];
}) {
  const webServices = services.filter((s) => s.internal_port !== null);
  if (webServices.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <Activity className="h-4 w-4" />
          HTTP Health Checks
        </CardTitle>
        <CardDescription>
          Podman monitors each service via an HTTP probe. Unhealthy containers are restarted automatically.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {webServices.map((svc) => (
          <ServiceHealthRow key={svc.name} stackId={stackId} svc={svc} />
        ))}
      </CardContent>
    </Card>
  );
}
