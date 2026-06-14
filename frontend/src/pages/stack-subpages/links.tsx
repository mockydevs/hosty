import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { toast } from "sonner";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Check,
  Copy,
  Database,
  Eye,
  EyeOff,
  ExternalLink,
  Globe,
  Lock,
  Server,
  Wifi,
  WifiOff,
} from "lucide-react";

// ── Helpers ────────────────────────────────────────────────────────────────

function maskCredentials(uri: string): string {
  return uri.replace(/:([^:@/]+)@/, ":••••••••@");
}

function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard.writeText(value);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };
  return (
    <Button variant="ghost" size="icon" className="h-7 w-7 shrink-0" onClick={copy}>
      {copied ? <Check className="h-3.5 w-3.5 text-green-500" /> : <Copy className="h-3.5 w-3.5" />}
    </Button>
  );
}

// ── Single URI row ──────────────────────────────────────────────────────────

function ConnectionRow({
  label,
  icon,
  uri,
  dimmed = false,
}: {
  label: string;
  icon: React.ReactNode;
  uri: string;
  dimmed?: boolean;
}) {
  const [revealed, setRevealed] = useState(false);
  const hasCredentials = uri.includes("@");
  const display = hasCredentials && !revealed ? maskCredentials(uri) : uri;

  return (
    <div className={`flex items-center gap-2 rounded-lg border px-3 py-2 ${dimmed ? "opacity-40" : ""}`}>
      <span className="shrink-0 text-muted-foreground">{icon}</span>
      <span className="w-16 shrink-0 text-xs font-medium text-muted-foreground">{label}</span>
      <code className="min-w-0 flex-1 truncate rounded bg-muted px-2 py-0.5 font-mono text-xs">
        {display}
      </code>
      {hasCredentials && (
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 shrink-0"
          onClick={() => setRevealed((r) => !r)}
        >
          {revealed ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
        </Button>
      )}
      <CopyButton value={uri} />
    </div>
  );
}

// ── DB connection card ──────────────────────────────────────────────────────

type Connection = {
  service: string;
  scheme: string;
  exposed: boolean;
  internal_uri: string;
  host_uri: string;
  public_uri: string | null;
};

function ConnectionCard({
  conn,
  stackId,
}: {
  conn: Connection;
  stackId: number;
}) {
  const queryClient = useQueryClient();

  const expose = useMutation({
    mutationFn: async (exposed: boolean) => {
      // @ts-ignore
      const res = await api.PUT(
        `/api/stacks/${stackId}/services/${conn.service}/expose` as any,
        { body: { exposed } },
      );
      if (res.error) throw new Error("Failed to update exposure");
      return res.data;
    },
    onSuccess: (_, exposed) => {
      queryClient.invalidateQueries({ queryKey: ["stacks", stackId, "connections"] });
      queryClient.invalidateQueries({ queryKey: ["stacks", stackId] });
      toast.success(exposed ? "Service exposed publicly" : "Service is now private");
    },
    onError: () => toast.error("Failed to update service exposure"),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Database className="h-4 w-4 text-muted-foreground" />
            <CardTitle className="text-sm font-semibold">{conn.service}</CardTitle>
            <Badge variant="secondary" className="font-mono text-xs">
              {conn.scheme}
            </Badge>
          </div>
          <div className="flex items-center gap-2">
            {conn.exposed ? (
              <Wifi className="h-3.5 w-3.5 text-green-500" />
            ) : (
              <WifiOff className="h-3.5 w-3.5 text-muted-foreground" />
            )}
            <span className="text-xs text-muted-foreground">
              {conn.exposed ? "Publicly exposed" : "Private only"}
            </span>
            <Switch
              checked={conn.exposed}
              disabled={expose.isPending}
              onCheckedChange={(v) => expose.mutate(v)}
            />
          </div>
        </div>
        <CardDescription className="text-xs">
          Connection strings for this database service.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        {/* Internal — container-to-container on the same stack network */}
        <ConnectionRow
          label="Internal"
          icon={<Lock className="h-3.5 w-3.5" />}
          uri={conn.internal_uri}
        />

        {/* Private — loopback from the host / other stacks on the same server */}
        <ConnectionRow
          label="Private"
          icon={<Server className="h-3.5 w-3.5" />}
          uri={conn.host_uri}
        />

        {/* Public — only available when exposed */}
        {conn.public_uri ? (
          <ConnectionRow
            label="Public"
            icon={<Globe className="h-3.5 w-3.5 text-green-500" />}
            uri={conn.public_uri}
          />
        ) : (
          <ConnectionRow
            label="Public"
            icon={<Globe className="h-3.5 w-3.5" />}
            uri={`${conn.scheme}://user:••••••••@<server-ip>/<db> — enable exposure above`}
            dimmed
          />
        )}
      </CardContent>
    </Card>
  );
}

// ── Web endpoint row ────────────────────────────────────────────────────────

type Endpoint = {
  domain: string;
  service_name: string;
};

function EndpointRow({ endpoint }: { endpoint: Endpoint }) {
  const url = endpoint.domain.startsWith("http")
    ? endpoint.domain
    : `https://${endpoint.domain}`;

  return (
    <div className="flex items-center gap-3 rounded-lg border px-3 py-2.5">
      <Globe className="h-4 w-4 shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <p className="truncate font-mono text-sm">{endpoint.domain}</p>
        <p className="text-xs text-muted-foreground">{endpoint.service_name}</p>
      </div>
      <CopyButton value={url} />
      <Button variant="ghost" size="icon" className="h-7 w-7" asChild>
        <a href={url} target="_blank" rel="noopener noreferrer">
          <ExternalLink className="h-3.5 w-3.5" />
        </a>
      </Button>
    </div>
  );
}

// ── Main tab ────────────────────────────────────────────────────────────────

export function LinksTab({
  stackId,
  endpoints,
}: {
  stackId: number;
  endpoints: Endpoint[];
}) {
  const { data: connections, isLoading } = useQuery<Connection[]>({
    queryKey: ["stacks", stackId, "connections"],
    queryFn: async () => {
      // @ts-ignore
      const res = await api.GET(`/api/stacks/${stackId}/connections` as any);
      return (res.data as Connection[]) ?? [];
    },
  });

  const hasEndpoints = endpoints.length > 0;
  const hasConnections = connections && connections.length > 0;

  return (
    <div className="space-y-6">
      {/* Web endpoints */}
      {hasEndpoints && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-semibold">Web Endpoints</CardTitle>
            <CardDescription className="text-xs">
              Public HTTPS domains routed to your application.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            {endpoints.map((ep) => (
              <EndpointRow key={ep.domain} endpoint={ep} />
            ))}
          </CardContent>
        </Card>
      )}

      {/* DB connections */}
      {isLoading && (
        <Card>
          <CardHeader className="pb-3">
            <Skeleton className="h-4 w-40" />
          </CardHeader>
          <CardContent className="space-y-2">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </CardContent>
        </Card>
      )}

      {hasConnections &&
        connections.map((conn) => (
          <ConnectionCard key={conn.service} conn={conn} stackId={stackId} />
        ))}

      {!isLoading && !hasEndpoints && !hasConnections && (
        <Card>
          <CardContent className="flex flex-col items-center justify-center py-12 text-center">
            <Globe className="mb-3 h-8 w-8 text-muted-foreground/40" />
            <p className="text-sm font-medium">No links yet</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Deploy the stack to generate web endpoints and connection strings.
            </p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
