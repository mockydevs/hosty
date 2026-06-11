import {
  CreateZoneDialog,
  DeleteRecordDialog,
  DeleteZoneDialog,
  RecordDialog,
} from "@/components/dns-dialogs";
import { EmptyState, ErrorState, LoadingState } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
 * DNS (Phase 7): PowerDNS zones and records, one-click templates, and
 * one-click "push all records to Cloudflare".
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Cloud,
  CloudUpload,
  Globe2,
  Mail,
  Pencil,
  Plus,
  Server,
  Trash2,
} from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router";
import { toast } from "sonner";

type RRSet = components["schemas"]["RRSetResponse"];

function useDnsMeta() {
  return useQuery({
    queryKey: ["dns", "meta"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/dns/meta");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load DNS settings"));
      return data;
    },
  });
}

// --- zone list ---------------------------------------------------------------------

export function DnsPage() {
  const meta = useDnsMeta();
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);

  const zones = useQuery({
    queryKey: ["dns", "zones"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/dns/zones");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load zones"));
      return data;
    },
  });

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">DNS</h1>
        <div className="flex flex-wrap gap-2">
          {meta.data?.cloudflare_enabled && (
            <Link
              to="/dns/cloudflare"
              className="inline-flex h-9 items-center justify-center gap-2 whitespace-nowrap rounded-md border border-border bg-transparent px-4 py-2 text-sm font-medium transition-colors hover:bg-accent hover:text-accent-foreground"
            >
              <Cloud className="h-4 w-4" aria-hidden /> Cloudflare zones
            </Link>
          )}
          <Button onClick={() => setCreating(true)}>
            <Plus className="h-4 w-4" aria-hidden /> New zone
          </Button>
        </div>
      </div>

      {zones.isPending ? (
        <LoadingState label="Loading zones…" />
      ) : zones.isError ? (
        <ErrorState message={zones.error.message} onRetry={() => zones.refetch()} />
      ) : zones.data.length === 0 ? (
        <EmptyState
          title="No DNS zones yet"
          description="Create a zone to manage records for a domain on this server."
        />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Zone</TableHead>
              <TableHead>Kind</TableHead>
              <TableHead>Serial</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {zones.data.map((zone) => (
              <TableRow key={zone.id}>
                <TableCell className="font-mono text-xs">
                  <Link
                    to={`/dns/${encodeURIComponent(zone.id)}`}
                    className="flex items-center gap-2 hover:underline"
                  >
                    <Globe2 className="h-4 w-4 text-muted-foreground" aria-hidden />
                    {zone.name.replace(/\.$/, "")}
                  </Link>
                </TableCell>
                <TableCell>
                  <Badge variant="outline">{zone.kind}</Badge>
                </TableCell>
                <TableCell className="font-mono text-xs">{zone.serial}</TableCell>
                <TableCell className="text-right">
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={`Delete ${zone.name}`}
                    onClick={() => setDeleting(zone.id)}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <CreateZoneDialog
        open={creating}
        onClose={() => setCreating(false)}
        serverIp={meta.data?.server_ip ?? ""}
      />
      <DeleteZoneDialog zone={deleting} onClose={() => setDeleting(null)} />
    </div>
  );
}

// --- zone detail / record editor ---------------------------------------------------

function relativeName(fqdn: string, zone: string): string {
  if (fqdn === zone) return "@";
  return fqdn.endsWith(`.${zone}`) ? fqdn.slice(0, -zone.length - 1) : fqdn.replace(/\.$/, "");
}

function TemplateButton({
  zoneId,
  template,
  label,
  icon: Icon,
}: {
  zoneId: string;
  template: string;
  label: string;
  icon: typeof Server;
}) {
  const queryClient = useQueryClient();
  const apply = useMutation({
    mutationFn: async () => {
      const { error } = await api.POST("/api/dns/zones/{zone_id}/templates/{template}", {
        params: { path: { zone_id: zoneId, template } },
      });
      if (error) throw new Error(apiErrorMessage(error, "Template failed"));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dns"] });
      toast.success(`Applied: ${label}`);
    },
    onError: (err) => toast.error(err.message),
  });
  return (
    <Button variant="outline" size="sm" loading={apply.isPending} onClick={() => apply.mutate()}>
      <Icon className="h-4 w-4" aria-hidden /> {label}
    </Button>
  );
}

function CloudflarePushButton({ zoneId }: { zoneId: string }) {
  const push = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/dns/zones/{zone_id}/push/cloudflare", {
        params: { path: { zone_id: zoneId } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Cloudflare push failed"));
      return data;
    },
    onSuccess: (result) => {
      toast.success(
        `Cloudflare: ${result.created} created, ${result.updated} updated, ${result.skipped} unchanged`,
      );
      if (result.errors.length > 0) {
        toast.warning(`${result.errors.length} record(s) not pushed — ${result.errors[0]}`);
      }
    },
    onError: (err) => toast.error(err.message),
  });
  return (
    <Button size="sm" loading={push.isPending} onClick={() => push.mutate()}>
      <CloudUpload className="h-4 w-4" aria-hidden /> Push to Cloudflare
    </Button>
  );
}

export function DnsZonePage() {
  const { zoneId = "" } = useParams();
  const meta = useDnsMeta();
  const [recordDialog, setRecordDialog] = useState<{ open: boolean; existing: RRSet | null }>({
    open: false,
    existing: null,
  });
  const [deletingRecord, setDeletingRecord] = useState<{ name: string; type: string } | null>(null);
  const [deletingZone, setDeletingZone] = useState<string | null>(null);

  const zone = useQuery({
    queryKey: ["dns", "zone", zoneId],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/dns/zones/{zone_id}", {
        params: { path: { zone_id: zoneId } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load the zone"));
      return data;
    },
    enabled: zoneId.length > 0,
  });

  if (zone.isPending) return <LoadingState label="Loading zone…" />;
  if (zone.isError)
    return <ErrorState message={zone.error.message} onRetry={() => zone.refetch()} />;

  const zoneName = zone.data.name; // canonical, with trailing dot
  const bare = zoneName.replace(/\.$/, "");

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Link
            to="/dns"
            aria-label="Back to zones"
            className="inline-flex h-9 w-9 items-center justify-center rounded-md transition-colors hover:bg-accent"
          >
            <ArrowLeft className="h-4 w-4" />
          </Link>
          <h1 className="font-mono text-2xl font-semibold tracking-tight">{bare}</h1>
          <Badge variant="outline">serial {zone.data.serial}</Badge>
        </div>
        <div className="flex flex-wrap gap-2">
          {meta.data?.cloudflare_enabled && <CloudflarePushButton zoneId={zoneId} />}
          <Button size="sm" onClick={() => setRecordDialog({ open: true, existing: null })}>
            <Plus className="h-4 w-4" aria-hidden /> Add record
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        {meta.data?.server_ip && (
          <TemplateButton
            zoneId={zoneId}
            template="point-to-server"
            label="Point to this server"
            icon={Server}
          />
        )}
        <TemplateButton
          zoneId={zoneId}
          template="external-mail"
          label="SPF/DMARC (external mail)"
          icon={Mail}
        />
        <TemplateButton
          zoneId={zoneId}
          template="google-workspace"
          label="Google Workspace MX"
          icon={Mail}
        />
        <Button
          variant="outline"
          size="sm"
          className="ml-auto text-destructive"
          onClick={() => setDeletingZone(zoneId)}
        >
          <Trash2 className="h-4 w-4" aria-hidden /> Delete zone
        </Button>
      </div>

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Name</TableHead>
            <TableHead>Type</TableHead>
            <TableHead>TTL</TableHead>
            <TableHead>Values</TableHead>
            <TableHead className="text-right">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {zone.data.rrsets.map((rrset) => {
            const isSoa = rrset.type === "SOA";
            const isApexNs = rrset.type === "NS" && rrset.name === zoneName;
            return (
              <TableRow key={`${rrset.name}|${rrset.type}`}>
                <TableCell className="font-mono text-xs">
                  {relativeName(rrset.name, zoneName)}
                </TableCell>
                <TableCell>
                  <Badge variant={isSoa ? "secondary" : "outline"}>{rrset.type}</Badge>
                </TableCell>
                <TableCell className="font-mono text-xs">{rrset.ttl}</TableCell>
                <TableCell className="max-w-md font-mono text-xs">
                  {rrset.records.map((value) => (
                    <div key={value} className="truncate" title={value}>
                      {value}
                    </div>
                  ))}
                </TableCell>
                <TableCell className="text-right">
                  {!isSoa && (
                    <>
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Edit ${rrset.type} ${rrset.name}`}
                        onClick={() => setRecordDialog({ open: true, existing: rrset })}
                      >
                        <Pencil className="h-4 w-4" />
                      </Button>
                      {!isApexNs && (
                        <Button
                          variant="ghost"
                          size="icon"
                          aria-label={`Delete ${rrset.type} ${rrset.name}`}
                          onClick={() => setDeletingRecord({ name: rrset.name, type: rrset.type })}
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      )}
                    </>
                  )}
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>

      <RecordDialog
        zoneId={zoneId}
        defaultTtl={meta.data?.default_ttl ?? 3600}
        existing={recordDialog.existing}
        open={recordDialog.open}
        onClose={() => setRecordDialog({ open: false, existing: null })}
      />
      <DeleteRecordDialog
        zoneId={zoneId}
        target={deletingRecord}
        onClose={() => setDeletingRecord(null)}
      />
      <DeleteZoneDialog zone={deletingZone} onClose={() => setDeletingZone(null)} />
    </div>
  );
}
