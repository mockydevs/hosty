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
import { useAuth } from "@/lib/auth";
/**
 * Sites list: searchable table with status/PHP badges, plus the create-site
 * wizard (domain validation incl. punycode via the URL parser).
 */
import { zodResolver } from "@hookform/resolvers/zod";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Globe, Plus, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { useForm } from "react-hook-form";
import { Link, useNavigate } from "react-router";
import { toast } from "sonner";
import { z } from "zod";

type Site = components["schemas"]["SiteResponse"];

export const PHP_VERSIONS = ["8.2", "8.3", "8.4"] as const;

/** Browser URL parser handles unicode → punycode (bücher.example → xn--…). */
export function normalizeDomain(raw: string): string | null {
  const input = raw.trim().toLowerCase().replace(/\.$/, "");
  if (!input || input.includes("/") || input.includes(":") || input.includes(" ")) return null;
  let hostname: string;
  try {
    hostname = new URL(`http://${input}`).hostname;
  } catch {
    return null;
  }
  if (!hostname.includes(".")) return null;
  const label = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
  if (!hostname.split(".").every((l) => label.test(l))) return null;
  return hostname;
}

const createSiteSchema = z.object({
  domain: z
    .string()
    .min(1, "Domain is required")
    .refine((v) => normalizeDomain(v) !== null, "Enter a valid domain like example.com"),
  php_version: z.enum(PHP_VERSIONS),
  create_dns_zone: z.boolean(),
});

type CreateSiteValues = z.infer<typeof createSiteSchema>;

export function CreateSiteDialog({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const form = useForm<CreateSiteValues>({
    resolver: zodResolver(createSiteSchema),
    defaultValues: { domain: "", php_version: "8.3", create_dns_zone: false },
  });

  const domainValue = form.watch("domain");
  const punycode = useMemo(() => {
    const normalized = normalizeDomain(domainValue ?? "");
    return normalized && normalized !== domainValue.trim().toLowerCase() ? normalized : null;
  }, [domainValue]);

  const onSubmit = form.handleSubmit(async (values) => {
    const domain = normalizeDomain(values.domain);
    if (!domain) return;
    const { data, error, response } = await api.POST("/api/sites", {
      body: {
        domain,
        php_version: values.php_version,
        create_dns_zone: isAdmin && values.create_dns_zone,
      },
    });
    if (error || !data) {
      form.setError("domain", {
        type: "server",
        message: apiErrorMessage(error, `Create failed (${response.status})`),
      });
      return;
    }
    await queryClient.invalidateQueries({ queryKey: ["sites"] });
    toast.success(`Provisioning ${data.site.domain}…`);
    form.reset();
    onClose();
    navigate(`/sites/${data.site.id}`, { state: { operationId: data.operation_id } });
  });

  return (
    <Dialog open={open} onClose={onClose}>
      <DialogContent>
        <DialogTitle>New site</DialogTitle>
        <DialogDescription>
          Creates an isolated Linux user, web root, PHP-FPM pool and Caddy vhost with automatic
          HTTPS.
        </DialogDescription>
        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <FormField label="Domain" htmlFor="domain" error={form.formState.errors.domain?.message}>
            <Input
              id="domain"
              placeholder="example.com"
              autoComplete="off"
              spellCheck={false}
              {...form.register("domain")}
            />
            {punycode && (
              <p className="text-xs text-muted-foreground">
                Will be created as <code>{punycode}</code>
              </p>
            )}
          </FormField>
          <FormField
            label="PHP version"
            htmlFor="php_version"
            error={form.formState.errors.php_version?.message}
          >
            <select
              id="php_version"
              className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              {...form.register("php_version")}
            >
              {PHP_VERSIONS.map((v) => (
                <option key={v} value={v}>
                  PHP {v}
                </option>
              ))}
            </select>
          </FormField>
          {isAdmin && (
            <label className="flex items-start gap-2 text-sm" htmlFor="create_dns_zone">
              <input
                id="create_dns_zone"
                type="checkbox"
                className="mt-0.5"
                {...form.register("create_dns_zone")}
              />
              <span className="text-muted-foreground">
                Also create a DNS zone (SOA, NS and, when the server IP is configured, A/www records
                pointing here)
              </span>
            </label>
          )}
          <DialogActions>
            <Button variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" loading={form.formState.isSubmitting}>
              Create site
            </Button>
          </DialogActions>
        </form>
      </DialogContent>
    </Dialog>
  );
}

const STATUS_VARIANTS: Record<string, "success" | "secondary" | "destructive" | "outline"> = {
  active: "success",
  provisioning: "secondary",
  deleting: "secondary",
  error: "destructive",
};

export function SiteStatusBadge({ status }: { status: string }) {
  return <Badge variant={STATUS_VARIANTS[status] ?? "outline"}>{status}</Badge>;
}

export function SitesPage() {
  const [search, setSearch] = useState("");
  const [createOpen, setCreateOpen] = useState(false);

  const sites = useQuery({
    queryKey: ["sites"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/sites");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load sites"));
      return data;
    },
    refetchInterval: (q) =>
      q.state.data?.some((s) => s.status === "provisioning" || s.status === "deleting")
        ? 3_000
        : false,
  });

  const filtered: Site[] = (sites.data ?? []).filter((s) =>
    s.domain.includes(search.trim().toLowerCase()),
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">Sites</h1>
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" aria-hidden /> New site
        </Button>
      </div>

      <div className="relative max-w-sm">
        <Search
          className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
          aria-hidden
        />
        <Input
          aria-label="Search sites"
          placeholder="Search domains…"
          className="pl-9"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {sites.isPending ? (
        <LoadingState label="Loading sites…" />
      ) : sites.isError ? (
        <ErrorState message={sites.error.message} onRetry={() => sites.refetch()} />
      ) : filtered.length === 0 ? (
        <EmptyState
          title={search ? "No sites match your search" : "No sites yet"}
          description={
            search
              ? "Try a different domain."
              : "Create your first site to get a live HTTPS domain on this server."
          }
        >
          {!search && (
            <Button className="mt-2" onClick={() => setCreateOpen(true)}>
              <Plus className="h-4 w-4" aria-hidden /> New site
            </Button>
          )}
        </EmptyState>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Domain</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>PHP</TableHead>
              <TableHead className="hidden md:table-cell">Site user</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filtered.map((site) => (
              <TableRow key={site.id}>
                <TableCell>
                  <Link
                    to={`/sites/${site.id}`}
                    className="flex items-center gap-2 font-medium hover:underline"
                  >
                    <Globe className="h-4 w-4 text-muted-foreground" aria-hidden />
                    {site.domain}
                  </Link>
                </TableCell>
                <TableCell>
                  <SiteStatusBadge status={site.status} />
                </TableCell>
                <TableCell>
                  <Badge variant="outline">PHP {site.php_version}</Badge>
                </TableCell>
                <TableCell className="hidden font-mono text-xs text-muted-foreground md:table-cell">
                  {site.site_user}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <CreateSiteDialog open={createOpen} onClose={() => setCreateOpen(false)} />
    </div>
  );
}
