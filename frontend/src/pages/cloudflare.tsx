import { FormField } from "@/components/form-field";
import { EmptyState, ErrorState, LoadingState } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
/**
 * Cloudflare account management: list the zones on the connected account and
 * add/edit/delete their DNS records straight in Cloudflare.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Cloud, Pencil, Plus, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router";
import { toast } from "sonner";

type CfRecord = components["schemas"]["CloudflareRecordResponse"];

const CF_RECORD_TYPES = ["A", "AAAA", "CNAME", "TXT", "MX", "NS"] as const;
type CfRecordType = (typeof CF_RECORD_TYPES)[number];

const PLACEHOLDERS: Record<CfRecordType, string> = {
  A: "192.0.2.1",
  AAAA: "2001:db8::1",
  CNAME: "target.example.com",
  TXT: "v=spf1 mx ~all",
  MX: "mail.example.com",
  NS: "ns1.example.com",
};

// --- zones list ----------------------------------------------------------------------

export function CloudflareZonesPage() {
  const config = useQuery({
    queryKey: ["dns", "cloudflare", "config"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/dns/cloudflare/config");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load config"));
      return data;
    },
  });

  const zones = useQuery({
    queryKey: ["dns", "cloudflare", "zones"],
    enabled: config.data?.configured === true,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/dns/cloudflare/zones");
      if (error || !data) {
        throw new Error(apiErrorMessage(error, "Failed to load Cloudflare zones"));
      }
      return data; /* as any */
    },
  });

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <Link
          to="/dns"
          aria-label="Back to DNS"
          className="inline-flex h-9 w-9 items-center justify-center rounded-md transition-colors hover:bg-accent"
        >
          <ArrowLeft className="h-4 w-4" />
        </Link>
        <Cloud className="h-5 w-5 text-muted-foreground" aria-hidden />
        <h1 className="text-2xl font-semibold tracking-tight">Cloudflare zones</h1>
      </div>

      {/* Not configured — show setup prompt */}
      {!config.isPending && config.data?.configured === false && (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed py-16 text-center">
          <Cloud className="mb-3 h-10 w-10 text-muted-foreground/40" aria-hidden />
          <p className="text-base font-semibold">Cloudflare not connected</p>
          <p className="mt-1 max-w-sm text-sm text-muted-foreground">
            Add your Cloudflare API token in Settings to manage zones and DNS records from here.
          </p>
          <Link
            to="/settings"
            className="mt-4 inline-flex h-9 items-center justify-center gap-2 rounded-md border border-border bg-transparent px-4 text-sm font-medium transition-colors hover:bg-accent"
          >
            Go to Settings
          </Link>
        </div>
      )}

      {config.data?.configured && zones.isPending ? (
        <LoadingState label="Loading Cloudflare zones…" />
      ) : config.data?.configured && zones.isError ? (
        <ErrorState message={zones.error.message} onRetry={() => zones.refetch()} />
      ) : config.data?.configured && zones.data?.length === 0 ? (
        <EmptyState
          title="No zones in this Cloudflare account"
          description="Add your domain in the Cloudflare dashboard first, then manage its records here."
        />
      ) : config.data?.configured && zones.data ? (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Domain</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Nameservers</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {zones.data.map((zone) => (
              <TableRow key={zone.id}>
                <TableCell className="font-mono text-xs">
                  <Link
                    to={`/dns/cloudflare/${encodeURIComponent(zone.id)}`}
                    state={{ zoneName: zone.name }}
                    className="hover:underline"
                  >
                    {zone.name}
                  </Link>
                </TableCell>
                <TableCell>
                  <Badge variant={zone.status === "active" && !zone.paused ? "default" : "outline"}>
                    {zone.paused ? "paused" : zone.status}
                  </Badge>
                </TableCell>
                <TableCell className="font-mono text-xs text-muted-foreground">
                  {zone.name_servers.join(", ")}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      ) : null}
    </div>
  );
}

// --- record dialog -------------------------------------------------------------------

function CfRecordDialog({
  zoneId,
  existing,
  open,
  onClose,
}: {
  zoneId: string;
  existing: CfRecord | null;
  open: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [type, setType] = useState<CfRecordType>("A");
  const [name, setName] = useState("");
  const [content, setContent] = useState("");
  const [ttl, setTtl] = useState("1");
  const [proxied, setProxied] = useState(false);
  const [priority, setPriority] = useState("10");

  useEffect(() => {
    if (!open) return;
    setType((existing?.type as CfRecordType) ?? "A");
    setName(existing?.name ?? "");
    setContent(existing?.content ?? "");
    setTtl(String(existing?.ttl ?? 1));
    setProxied(existing?.proxied ?? false);
    setPriority(String(existing?.priority ?? 10));
  }, [open, existing]);

  const save = useMutation({
    mutationFn: async () => {
      const body = {
        type,
        name: name.trim(),
        content: content.trim(),
        ttl: Math.max(1, Number(ttl) || 1),
        proxied,
        priority: type === "MX" ? Math.max(0, Number(priority) || 10) : null,
      };
      const { error } = existing
        ? await api.PUT("/api/dns/cloudflare/zones/{cf_zone_id}/records/{record_id}", {
            params: { path: { cf_zone_id: zoneId, record_id: existing.id } },
            body,
          })
        : await api.POST("/api/dns/cloudflare/zones/{cf_zone_id}/records", {
            params: { path: { cf_zone_id: zoneId } },
            body,
          });
      if (error) throw new Error(apiErrorMessage(error, "Could not save the record"));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dns", "cloudflare"] });
      toast.success(existing ? "Record updated in Cloudflare" : "Record created in Cloudflare");
      onClose();
    },
    onError: (err) => toast.error(err.message),
  });

  const proxyable = type === "A" || type === "AAAA" || type === "CNAME";

  return (
    <Dialog open={open} onClose={onClose}>
      <DialogContent>
        <DialogTitle>{existing ? "Edit Cloudflare record" : "Add Cloudflare record"}</DialogTitle>
        <DialogDescription>
          Changes apply directly to the zone in your Cloudflare account.
        </DialogDescription>
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim() && content.trim()) save.mutate();
          }}
        >
          <div className="grid grid-cols-2 gap-3">
            <FormField label="Type" htmlFor="cf-rec-type">
              <select
                id="cf-rec-type"
                className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm"
                disabled={existing !== null}
                value={type}
                onChange={(e) => setType(e.target.value as CfRecordType)}
              >
                {CF_RECORD_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </FormField>
            <FormField label="TTL (1 = auto)" htmlFor="cf-rec-ttl">
              <Input
                id="cf-rec-ttl"
                inputMode="numeric"
                value={ttl}
                onChange={(e) => setTtl(e.target.value)}
              />
            </FormField>
          </div>
          <FormField label="Name (use @ for the domain itself)" htmlFor="cf-rec-name">
            <Input
              id="cf-rec-name"
              placeholder="www"
              autoComplete="off"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </FormField>
          <FormField label="Content" htmlFor="cf-rec-content">
            <Input
              id="cf-rec-content"
              placeholder={PLACEHOLDERS[type]}
              autoComplete="off"
              value={content}
              onChange={(e) => setContent(e.target.value)}
            />
          </FormField>
          {type === "MX" && (
            <FormField label="Priority" htmlFor="cf-rec-priority">
              <Input
                id="cf-rec-priority"
                inputMode="numeric"
                value={priority}
                onChange={(e) => setPriority(e.target.value)}
              />
            </FormField>
          )}
          {proxyable && (
            <>
              <label className="flex items-center gap-2 text-sm" htmlFor="cf-rec-proxied">
                <input
                  id="cf-rec-proxied"
                  type="checkbox"
                  checked={proxied}
                  onChange={(e) => setProxied(e.target.checked)}
                />
                Proxied through Cloudflare (orange cloud)
              </label>
              {proxied && (
                <p className="text-xs text-muted-foreground">
                  If this domain is hosted on this server, also enable "Behind Cloudflare proxy" on
                  the site's HTTPS certificate card so the origin keeps a working certificate.
                </p>
              )}
            </>
          )}
          <DialogActions>
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={!name.trim() || !content.trim()}
              loading={save.isPending}
            >
              {existing ? "Save changes" : "Add record"}
            </Button>
          </DialogActions>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// --- zone records --------------------------------------------------------------------

export function CloudflareZonePage() {
  const { cfZoneId = "" } = useParams();
  const [dialog, setDialog] = useState<{ open: boolean; existing: CfRecord | null }>({
    open: false,
    existing: null,
  });
  const [deleting, setDeleting] = useState<CfRecord | null>(null);
  const queryClient = useQueryClient();

  const records = useQuery({
    queryKey: ["dns", "cloudflare", "zones", cfZoneId, "records"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/dns/cloudflare/zones/{cf_zone_id}/records", {
        params: { path: { cf_zone_id: cfZoneId } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load records"));
      return data; /* as any */
    },
    enabled: cfZoneId.length > 0,
  });

  const del = useMutation({
    mutationFn: async (record: CfRecord) => {
      const { error } = await api.DELETE(
        "/api/dns/cloudflare/zones/{cf_zone_id}/records/{record_id}",
        { params: { path: { cf_zone_id: cfZoneId, record_id: record.id } } },
      );
      if (error) throw new Error(apiErrorMessage(error, "Could not delete the record"));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dns", "cloudflare"] });
      toast.success("Record deleted from Cloudflare");
      setDeleting(null);
    },
    onError: (err) => toast.error(err.message),
  });

  const editable = (r: CfRecord) => CF_RECORD_TYPES.includes(r.type as CfRecordType);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Link
            to="/dns/cloudflare"
            aria-label="Back to Cloudflare zones"
            className="inline-flex h-9 w-9 items-center justify-center rounded-md transition-colors hover:bg-accent"
          >
            <ArrowLeft className="h-4 w-4" />
          </Link>
          <Cloud className="h-5 w-5 text-muted-foreground" aria-hidden />
          <h1 className="font-mono text-2xl font-semibold tracking-tight">
            {records.data?.[0]?.name.split(".").slice(-2).join(".") ?? "Cloudflare zone"}
          </h1>
        </div>
        <Button size="sm" onClick={() => setDialog({ open: true, existing: null })}>
          <Plus className="h-4 w-4" aria-hidden /> Add record
        </Button>
      </div>

      {records.isPending ? (
        <LoadingState label="Loading records…" />
      ) : records.isError ? (
        <ErrorState message={records.error.message} onRetry={() => records.refetch()} />
      ) : records.data.length === 0 ? (
        <EmptyState title="No records" description="This Cloudflare zone has no DNS records yet." />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Type</TableHead>
              <TableHead>Content</TableHead>
              <TableHead>TTL</TableHead>
              <TableHead>Proxy</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {records.data.map((r) => (
              <TableRow key={r.id}>
                <TableCell className="font-mono text-xs">{r.name}</TableCell>
                <TableCell>
                  <Badge variant="outline">{r.type}</Badge>
                </TableCell>
                <TableCell className="max-w-md truncate font-mono text-xs" title={r.content}>
                  {r.type === "MX" && r.priority != null ? `${r.priority} ` : ""}
                  {r.content}
                </TableCell>
                <TableCell className="font-mono text-xs">{r.ttl === 1 ? "auto" : r.ttl}</TableCell>
                <TableCell>
                  {r.proxied != null &&
                    (r.proxied ? (
                      <Badge variant="success">proxied</Badge>
                    ) : (
                      <span className="text-xs text-muted-foreground">DNS only</span>
                    ))}
                </TableCell>
                <TableCell className="text-right">
                  {editable(r) && (
                    <>
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Edit ${r.type} ${r.name}`}
                        onClick={() => setDialog({ open: true, existing: r })}
                      >
                        <Pencil className="h-4 w-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Delete ${r.type} ${r.name}`}
                        onClick={() => setDeleting(r)}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <CfRecordDialog
        zoneId={cfZoneId}
        existing={dialog.existing}
        open={dialog.open}
        onClose={() => setDialog({ open: false, existing: null })}
      />

      <Dialog open={deleting !== null} onClose={() => setDeleting(null)}>
        <DialogContent>
          <DialogTitle>Delete this record from Cloudflare?</DialogTitle>
          <DialogDescription>
            {deleting && (
              <span className="font-mono text-xs">
                {deleting.type} {deleting.name} → {deleting.content}
              </span>
            )}
          </DialogDescription>
          <DialogActions>
            <Button variant="outline" onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              loading={del.isPending}
              onClick={() => deleting && del.mutate(deleting)}
            >
              Delete record
            </Button>
          </DialogActions>
        </DialogContent>
      </Dialog>
    </div>
  );
}
