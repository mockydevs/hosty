import { OperationProgress } from "@/components/operation-progress";
import { PhpCard } from "@/components/php-card";
import { EmptyState, ErrorState, LoadingState } from "@/components/states";
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
/**
 * Site detail: Overview (info + SSL status + danger zone) with stub tabs for
 * Files / Databases / Backups arriving in later phases. Shows live operation
 * progress while the site is provisioning or being deleted.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ShieldCheck, ShieldQuestion } from "lucide-react";
import { useCallback, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router";
import { toast } from "sonner";

type Operation = components["schemas"]["OperationResponse"];

function SslStatusCard({ siteId }: { siteId: number }) {
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

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium">HTTPS certificate</CardTitle>
        {ssl.data?.status === "active" ? (
          <ShieldCheck className="h-4 w-4 text-success" aria-hidden />
        ) : (
          <ShieldQuestion className="h-4 w-4 text-muted-foreground" aria-hidden />
        )}
      </CardHeader>
      <CardContent className="space-y-1 text-sm">
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
          </>
        ) : (
          <>
            <Badge variant="outline">
              {ssl.data.status === "dns_unresolved" ? "DNS not pointing here" : "No certificate"}
            </Badge>
            <p className="text-muted-foreground">{ssl.data.detail}</p>
          </>
        )}
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

function PhaseStub({ title, phase }: { title: string; phase: string }) {
  return <EmptyState title={`${title} arrive in ${phase}`} description="Stay tuned." />;
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
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="wordpress">WordPress</TabsTrigger>
          <TabsTrigger value="files">Files</TabsTrigger>
          <TabsTrigger value="databases">Databases</TabsTrigger>
          <TabsTrigger value="backups">Backups</TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium">Details</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-sm">
                <div className="flex justify-between gap-4">
                  <span className="text-muted-foreground">Web root</span>
                  <code className="break-all text-xs">{s.doc_root}</code>
                </div>
                <div className="flex justify-between gap-4">
                  <span className="text-muted-foreground">Linux user</span>
                  <code className="text-xs">{s.site_user}</code>
                </div>
                <div className="flex justify-between gap-4">
                  <span className="text-muted-foreground">Created</span>
                  <span>{new Date(s.created_at).toLocaleString()}</span>
                </div>
              </CardContent>
            </Card>
            {s.status === "active" && <SslStatusCard siteId={s.id} />}
            {s.status === "active" && <PhpCard site={s} />}
          </div>

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

        <TabsContent value="wordpress">
          <WordPressTab site={s} />
        </TabsContent>

        <TabsContent value="files">
          <PhaseStub title="File management" phase="Phase 6" />
        </TabsContent>
        <TabsContent value="databases">
          <PhaseStub title="Databases" phase="Phase 5" />
        </TabsContent>
        <TabsContent value="backups">
          <PhaseStub title="Backups" phase="Phase 8" />
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
