import { FilesTab } from "@/components/files-tab";
import { OperationProgress } from "@/components/operation-progress";
import { PhpCard } from "@/components/php-card";
import { SiteBackupsTab } from "@/components/site-backups-tab";
import { SiteDatabasesTab } from "@/components/site-databases-tab";
import { SiteToolsTab } from "@/components/site-tools-tab";
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { WordPressTab } from "@/components/wordpress-tab";
import { api, apiErrorMessage } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { SiteStatusBadge } from "@/pages/sites";
/** Site detail: overview plus per-site WordPress, files, databases, and backups workflows. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Network, RefreshCw, ShieldCheck, ShieldQuestion } from "lucide-react";
import { useCallback, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router";
import { toast } from "sonner";

type Operation = components["schemas"]["OperationResponse"];
type Site = components["schemas"]["SiteResponse"];

function SslStatusCard({ site }: { site: Site }) {
  const siteId = site.id;
  const queryClient = useQueryClient();
  const ssl = useQuery({
    queryKey: ["sites", siteId, "ssl"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/sites/{site_id}/ssl", {
        params: { path: { site_id: siteId } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to check SSL"));
      return data;
    },
    staleTime: 60_000,
  });

  const renew = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/sites/{site_id}/ssl/renew", {
        params: { path: { site_id: siteId } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Certificate renewal failed"));
      return data;
    },
    onSuccess: (data) => {
      queryClient.setQueryData(["sites", siteId, "ssl"], data);
      if (data.status === "active") toast.success(`Certificate for ${data.domain} is active`);
      else toast.info(data.detail ?? "Renewal requested; the certificate is not active yet");
    },
    onError: (err) => toast.error(err.message),
  });

  const proxy = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.PATCH("/api/sites/{site_id}/cloudflare-proxy", {
        params: { path: { site_id: siteId } },
        body: { behind_cloudflare: !site.behind_cloudflare },
      });
      if (error || !data) {
        throw new Error(apiErrorMessage(error, "Could not update the proxy setting"));
      }
      return data;
    },
    onSuccess: async (data) => {
      await queryClient.invalidateQueries({ queryKey: ["sites"] });
      toast.success(
        data.behind_cloudflare
          ? 'Origin certificate enabled — set the Cloudflare SSL mode to "Full"'
          : "Switched back to Let's Encrypt certificates",
      );
    },
    onError: (err) => toast.error(err.message),
  });

  const ok = ssl.data?.status === "active" || ssl.data?.status === "origin_internal";

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium">HTTPS certificate</CardTitle>
        {ok ? (
          <ShieldCheck className="h-4 w-4 text-success" aria-hidden />
        ) : (
          <ShieldQuestion className="h-4 w-4 text-muted-foreground" aria-hidden />
        )}
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {ssl.isPending ? (
          <p className="text-muted-foreground">Checking…</p>
        ) : ssl.isError ? (
          <p className="text-muted-foreground">{ssl.error.message}</p>
        ) : ssl.data.status === "active" ? (
          <>
            <Badge variant="success">Active</Badge>
            <p className="text-muted-foreground">
              {ssl.data.issuer ?? "Unknown issuer"}
              {ssl.data.not_after &&
                ` · expires ${new Date(ssl.data.not_after).toLocaleDateString()}`}
            </p>
            <p className="text-xs text-muted-foreground">
              Certificates renew automatically before they expire.
            </p>
          </>
        ) : ssl.data.status === "origin_internal" ? (
          <>
            <Badge variant="success">Origin certificate</Badge>
            <p className="text-muted-foreground">{ssl.data.detail}</p>
          </>
        ) : (
          <>
            <Badge variant="outline">
              {ssl.data.status === "dns_unresolved" ? "DNS not pointing here" : "No certificate"}
            </Badge>
            <p className="text-muted-foreground">{ssl.data.detail}</p>
            <Button
              size="sm"
              variant="outline"
              loading={renew.isPending}
              onClick={() => renew.mutate()}
            >
              <RefreshCw className="mr-2 h-3.5 w-3.5" aria-hidden />
              Retry certificate
            </Button>
          </>
        )}
        <label className="flex items-start gap-2 pt-1 text-xs" htmlFor="cf-proxy-toggle">
          <input
            id="cf-proxy-toggle"
            type="checkbox"
            className="mt-0.5"
            checked={site.behind_cloudflare}
            disabled={proxy.isPending}
            onChange={() => proxy.mutate()}
          />
          <span className="text-muted-foreground">
            Behind Cloudflare proxy (orange cloud) — serve an internal origin certificate instead of
            Let's Encrypt
          </span>
        </label>
      </CardContent>
    </Card>
  );
}

/** Bridge to the DNS page: link the site's zone, or create it in one click. */
function DnsCard({ site }: { site: Site }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const meta = useQuery({
    queryKey: ["dns", "meta"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/dns/meta");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load DNS settings"));
      return data;
    },
    staleTime: 60_000,
  });

  const zones = useQuery({
    queryKey: ["dns", "zones"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/dns/zones");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load zones"));
      return data;
    },
    enabled: meta.data?.enabled === true,
  });

  const create = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/dns/zones", {
        body: {
          name: site.domain,
          site_id: site.id,
          point_to_server: (meta.data?.server_ip ?? "") !== "",
        },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to create the zone"));
      return data;
    },
    onSuccess: async (zone) => {
      await queryClient.invalidateQueries({ queryKey: ["dns", "zones"] });
      toast.success(`Zone ${zone.name.replace(/\.$/, "")} created`);
      navigate(`/dns/${encodeURIComponent(zone.id)}`);
    },
    onError: (err) => toast.error(err.message),
  });

  if (meta.data?.enabled === false) return null;

  const zone = zones.data?.find(
    (z) => z.name.replace(/\.$/, "").toLowerCase() === site.domain.toLowerCase(),
  );

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium">DNS</CardTitle>
        <Network className="h-4 w-4 text-muted-foreground" aria-hidden />
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {zones.isPending || meta.isPending ? (
          <p className="text-muted-foreground">Checking…</p>
        ) : zone ? (
          <>
            <p className="text-muted-foreground">This server hosts the zone for {site.domain}.</p>
            <Link
              to={`/dns/${encodeURIComponent(zone.id)}`}
              className="inline-flex h-8 items-center justify-center gap-2 whitespace-nowrap rounded-md border border-border bg-transparent px-3 text-xs font-medium transition-colors hover:bg-accent hover:text-accent-foreground"
            >
              Manage DNS records
            </Link>
          </>
        ) : (
          <>
            <p className="text-muted-foreground">
              No zone on this server — DNS is managed elsewhere (registrar, Cloudflare…). Create one
              to manage records here
              {meta.data?.server_ip ? " — apex and www will point at this server." : "."}
            </p>
            <Button
              size="sm"
              variant="outline"
              loading={create.isPending}
              onClick={() => create.mutate()}
            >
              Create DNS zone
            </Button>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function DetailsCard({ site }: { site: Site }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium">Details</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        <div className="flex justify-between gap-4">
          <span className="text-muted-foreground">Web root</span>
          <code className="break-all text-xs">{site.doc_root}</code>
        </div>
        <div className="flex justify-between gap-4">
          <span className="text-muted-foreground">Linux user</span>
          <code className="text-xs">{site.site_user}</code>
        </div>
        <div className="flex justify-between gap-4">
          <span className="text-muted-foreground">Created</span>
          <span>{new Date(site.created_at).toLocaleString()}</span>
        </div>
      </CardContent>
    </Card>
  );
}

function DeleteSiteDialog({
  open,
  onClose,
  siteId,
  domain,
}: {
  open: boolean;
  onClose: () => void;
  siteId: number;
  domain: string;
}) {
  const [confirm, setConfirm] = useState("");
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const del = useMutation({
    mutationFn: async () => {
      const { data, error, response } = await api.DELETE("/api/sites/{site_id}", {
        params: { path: { site_id: siteId } },
        body: { confirm_domain: confirm },
      });
      if (error || !data) {
        throw new Error(apiErrorMessage(error, `Delete failed (${response.status})`));
      }
      return data;
    },
    onSuccess: async (data) => {
      await queryClient.invalidateQueries({ queryKey: ["sites"] });
      toast.success(`Deleting ${domain}…`);
      onClose();
      navigate(`/sites/${siteId}`, { state: { operationId: data.operation_id } });
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <Dialog open={open} onClose={onClose}>
      <DialogContent>
        <DialogTitle>Delete {domain}?</DialogTitle>
        <DialogDescription>
          This permanently removes the vhost, PHP pool, all files under the site directory and the
          Linux user. Type the domain to confirm.
        </DialogDescription>
        <Input
          aria-label="Type the domain to confirm"
          placeholder={domain}
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          autoComplete="off"
          spellCheck={false}
        />
        <DialogActions>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={confirm.trim().toLowerCase() !== domain}
            loading={del.isPending}
            onClick={() => del.mutate()}
          >
            Delete site
          </Button>
        </DialogActions>
      </DialogContent>
    </Dialog>
  );
}

export function SiteDetailPage() {
  const params = useParams();
  const siteId = Number(params.siteId);
  const location = useLocation();
  const operationId = (location.state as { operationId?: number } | null)?.operationId;
  const [deleteOpen, setDeleteOpen] = useState(false);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const site = useQuery({
    queryKey: ["sites", siteId],
    queryFn: async () => {
      const { data, error, response } = await api.GET("/api/sites/{site_id}", {
        params: { path: { site_id: siteId } },
      });
      if (error || !data) {
        throw Object.assign(new Error(apiErrorMessage(error, "Failed to load site")), {
          status: response.status,
        });
      }
      return data;
    },
    enabled: Number.isFinite(siteId),
    retry: (count, err) => (err as { status?: number }).status !== 404 && count < 2,
  });

  const onOperationFinished = useCallback(
    async (op: Operation) => {
      await queryClient.invalidateQueries({ queryKey: ["sites"] });
      if (op.kind === "delete_site" && op.status === "succeeded") {
        toast.success(`${op.domain} deleted`);
        navigate("/sites", { replace: true });
        return;
      }
      if (op.status === "failed") toast.error(op.error ?? "Operation failed");
      else toast.success(`${op.domain} is live`);
      await site.refetch();
    },
    [navigate, queryClient, site.refetch],
  );

  if (!Number.isFinite(siteId)) return <ErrorState message="Invalid site id" />;
  if (site.isPending) return <LoadingState label="Loading site…" />;
  if (site.isError) {
    const status = (site.error as { status?: number }).status;
    if (status === 404 && operationId) {
      // Deleted (or mid-deletion): keep showing the operation until it ends.
      return (
        <div className="mx-auto max-w-lg">
          <OperationProgress operationId={operationId} onFinished={onOperationFinished} />
        </div>
      );
    }
    return <ErrorState message={site.error.message} onRetry={() => site.refetch()} />;
  }

  const s = site.data;
  const busy = s.status === "provisioning" || s.status === "deleting";

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <Link
          to="/sites"
          aria-label="Back to sites"
          className="inline-flex h-9 w-9 items-center justify-center rounded-md hover:bg-accent hover:text-accent-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
        </Link>
        <h1 className="text-2xl font-semibold tracking-tight">{s.domain}</h1>
        <SiteStatusBadge status={s.status} />
        <Badge variant="outline">PHP {s.php_version}</Badge>
      </div>

      {busy && operationId && (
        <OperationProgress operationId={operationId} onFinished={onOperationFinished} />
      )}
      {s.status === "error" && s.error_message && (
        <ErrorState message={s.error_message} onRetry={() => site.refetch()} />
      )}

      <Tabs defaultValue="overview">
        {/* Full-width segmented header: triggers share the row equally. */}
        <TabsList className="flex h-10 w-full">
          <TabsTrigger value="overview" className="flex-1">
            Overview
          </TabsTrigger>
          <TabsTrigger value="wordpress" className="flex-1">
            WordPress
          </TabsTrigger>
          <TabsTrigger value="files" className="flex-1">
            Files
          </TabsTrigger>
          <TabsTrigger value="databases" className="flex-1">
            Databases
          </TabsTrigger>
          <TabsTrigger value="backups" className="flex-1">
            Backups
          </TabsTrigger>
          <TabsTrigger value="tools" className="flex-1">
            Tools
          </TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="mt-4 space-y-4">
          {s.status === "active" ? (
            /* Balanced two-column layout: the two shorter cards (Details +
               HTTPS) stack on the left; the tall PHP card fills the right. */
            <div className="grid items-stretch gap-4 lg:grid-cols-2">
              <div className="flex flex-col gap-4">
                <DetailsCard site={s} />
                <div className="flex-1">
                  <SslStatusCard site={s} />
                </div>
              </div>
              <div className="flex flex-col gap-4">
                <PhpCard site={s} />
                <DnsCard site={s} />
              </div>
            </div>
          ) : (
            <DetailsCard site={s} />
          )}

          <Card className="border-destructive/40">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium">Danger zone</CardTitle>
              <CardDescription>
                Deleting a site removes its files, PHP pool, vhost and Linux user.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Button variant="destructive" disabled={busy} onClick={() => setDeleteOpen(true)}>
                Delete site
              </Button>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="wordpress" className="mt-4">
          <WordPressTab site={s} />
        </TabsContent>

        <TabsContent value="files" className="mt-4">
          <FilesTab site={s} />
        </TabsContent>
        <TabsContent value="databases" className="mt-4">
          <SiteDatabasesTab site={s} />
        </TabsContent>
        <TabsContent value="backups" className="mt-4">
          <SiteBackupsTab site={s} />
        </TabsContent>
        <TabsContent value="tools" className="mt-4">
          <SiteToolsTab site={s} />
        </TabsContent>
      </Tabs>

      <DeleteSiteDialog
        open={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        siteId={s.id}
        domain={s.domain}
      />
    </div>
  );
}
