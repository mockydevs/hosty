import { FormField } from "@/components/form-field";
import { OperationProgress } from "@/components/operation-progress";
import { ErrorState, LoadingState } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api, apiErrorMessage } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
/**
 * Week 13 UI: WordPress tab — install wizard when absent; status card with
 * version / update / plugin / theme info and management actions when present.
 */
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, KeyRound, RefreshCw, Wrench } from "lucide-react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

type Site = components["schemas"]["SiteResponse"];

const LOCALES = [
  ["en_US", "English (US)"],
  ["en_GB", "English (UK)"],
  ["de_DE", "Deutsch"],
  ["fr_FR", "Français"],
  ["es_ES", "Español"],
  ["it_IT", "Italiano"],
  ["pt_BR", "Português (BR)"],
  ["nl_NL", "Nederlands"],
  ["sw", "Kiswahili"],
  ["ja", "日本語"],
] as const;

const installSchema = z.object({
  title: z.string().min(1, "Site title is required").max(200),
  admin_user: z
    .string()
    .min(3, "At least 3 characters")
    .max(60)
    .regex(/^[a-zA-Z0-9_.\-@ ]+$/, "Letters, digits, _ . - @ and spaces only"),
  admin_password: z.string().min(12, "At least 12 characters").max(128),
  admin_email: z.string().regex(/^[^@\s]+@[^@\s]+\.[^@\s]+$/, "Enter a valid email"),
  locale: z.string(),
  version: z
    .string()
    .regex(/^(latest|[0-9]+\.[0-9]+(\.[0-9]+)?)$/, 'Use "latest" or a version like 6.5.1'),
});

type InstallValues = z.infer<typeof installSchema>;

function InstallWizard({ site, onStarted }: { site: Site; onStarted: (opId: number) => void }) {
  const form = useForm<InstallValues>({
    resolver: zodResolver(installSchema),
    defaultValues: {
      title: "",
      admin_user: "",
      admin_password: "",
      admin_email: "",
      locale: "en_US",
      version: "latest",
    },
  });

  const onSubmit = form.handleSubmit(async (values) => {
    const { data, error, response } = await api.POST("/api/sites/{site_id}/wordpress", {
      params: { path: { site_id: site.id } },
      body: values,
    });
    if (error || !data) {
      form.setError("title", {
        type: "server",
        message: apiErrorMessage(error, `Install failed (${response.status})`),
      });
      return;
    }
    toast.success("Installing WordPress…");
    onStarted(data.operation_id);
  });

  return (
    <Card className="max-w-lg">
      <CardHeader>
        <CardTitle className="text-base">Install WordPress</CardTitle>
        <CardDescription>
          One click: database, wp-config and admin account — installed as the site user, never root.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <FormField
            label="Site title"
            htmlFor="wp-title"
            error={form.formState.errors.title?.message}
          >
            <Input id="wp-title" {...form.register("title")} />
          </FormField>
          <div className="grid gap-4 sm:grid-cols-2">
            <FormField
              label="Admin username"
              htmlFor="wp-admin"
              error={form.formState.errors.admin_user?.message}
            >
              <Input id="wp-admin" autoComplete="off" {...form.register("admin_user")} />
            </FormField>
            <FormField
              label="Admin password"
              htmlFor="wp-password"
              error={form.formState.errors.admin_password?.message}
            >
              <Input
                id="wp-password"
                type="password"
                autoComplete="new-password"
                {...form.register("admin_password")}
              />
            </FormField>
          </div>
          <FormField
            label="Admin email"
            htmlFor="wp-email"
            error={form.formState.errors.admin_email?.message}
          >
            <Input id="wp-email" type="email" {...form.register("admin_email")} />
          </FormField>
          <div className="grid gap-4 sm:grid-cols-2">
            <FormField
              label="Language"
              htmlFor="wp-locale"
              error={form.formState.errors.locale?.message}
            >
              <select
                id="wp-locale"
                className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                {...form.register("locale")}
              >
                {LOCALES.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </FormField>
            <FormField
              label="Version"
              htmlFor="wp-version"
              error={form.formState.errors.version?.message}
            >
              <Input id="wp-version" placeholder="latest" {...form.register("version")} />
            </FormField>
          </div>
          <Button type="submit" loading={form.formState.isSubmitting}>
            Install WordPress
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

function ActionButton({
  site,
  action,
  label,
  icon: Icon,
  onUrl,
}: {
  site: Site;
  action: string;
  label: string;
  icon: typeof Wrench;
  onUrl?: (url: string) => void;
}) {
  const queryClient = useQueryClient();
  const run = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/sites/{site_id}/wordpress/actions/{action}", {
        params: { path: { site_id: site.id, action } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, `${label} failed`));
      return data;
    },
    onSuccess: async (data) => {
      if (data.url && onUrl) {
        onUrl(data.url);
      } else {
        toast.success(`${label} done`);
      }
      await queryClient.invalidateQueries({ queryKey: ["sites", site.id, "wordpress"] });
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <Button variant="outline" size="sm" loading={run.isPending} onClick={() => run.mutate()}>
      <Icon className="h-4 w-4" aria-hidden /> {label}
    </Button>
  );
}

export function WordPressTab({ site }: { site: Site }) {
  const [operationId, setOperationId] = useState<number | null>(null);
  const [loginUrl, setLoginUrl] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const wp = useQuery({
    queryKey: ["sites", site.id, "wordpress"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/sites/{site_id}/wordpress", {
        params: { path: { site_id: site.id } },
      });
      if (error || !data)
        throw new Error(apiErrorMessage(error, "Failed to load WordPress status"));
      return data;
    },
    enabled: site.status === "active" && operationId === null,
    staleTime: 30_000,
  });

  if (site.status !== "active") {
    return <p className="text-sm text-muted-foreground">Available once the site is active.</p>;
  }

  if (operationId !== null) {
    return (
      <div className="max-w-lg">
        <OperationProgress
          operationId={operationId}
          onFinished={async (op) => {
            if (op.status === "succeeded") toast.success("WordPress installed");
            await queryClient.invalidateQueries({ queryKey: ["sites", site.id, "wordpress"] });
            await queryClient.invalidateQueries({ queryKey: ["sites"] });
            setOperationId(null);
          }}
        />
      </div>
    );
  }

  if (wp.isPending) return <LoadingState label="Checking WordPress…" />;
  if (wp.isError) return <ErrorState message={wp.error.message} onRetry={() => wp.refetch()} />;

  if (!wp.data.installed) {
    return <InstallWizard site={site} onStarted={setOperationId} />;
  }

  const status = wp.data;
  return (
    <div className="space-y-4">
      <Card className="max-w-lg">
        <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
          <CardTitle className="text-base">WordPress {status.version ?? ""}</CardTitle>
          {status.update_available ? (
            <Badge variant="secondary">Update {status.update_available} available</Badge>
          ) : (
            <Badge variant="success">Up to date</Badge>
          )}
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">
            {status.plugin_count ?? "?"} plugins · {status.theme_count ?? "?"} themes
          </p>
          <div className="flex flex-wrap gap-2">
            <ActionButton site={site} action="update_core" label="Update core" icon={RefreshCw} />
            <ActionButton
              site={site}
              action="maintenance_on"
              label="Maintenance on"
              icon={Wrench}
            />
            <ActionButton
              site={site}
              action="maintenance_off"
              label="Maintenance off"
              icon={Wrench}
            />
            <ActionButton
              site={site}
              action="shuffle_salts"
              label="Regenerate salts"
              icon={KeyRound}
            />
            <ActionButton
              site={site}
              action="login_link"
              label="Admin login link"
              icon={ExternalLink}
              onUrl={setLoginUrl}
            />
          </div>
          {loginUrl && (
            <p className="break-all text-sm">
              One-time login:{" "}
              <a href={loginUrl} target="_blank" rel="noreferrer" className="font-medium underline">
                {loginUrl}
              </a>
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
