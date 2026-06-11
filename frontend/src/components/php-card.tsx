import { FormField } from "@/components/form-field";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api, apiErrorMessage } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { PHP_VERSIONS } from "@/pages/sites";
/**
 * Week 11 UI: per-site PHP management — version switch (zero-downtime on the
 * backend) and editable limits (memory_limit / upload_max_filesize).
 */
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

type Site = components["schemas"]["SiteResponse"];

const PHP_SIZE = /^[1-9][0-9]{0,3}M$/;

const phpSettingsSchema = z.object({
  memory_limit: z.string().regex(PHP_SIZE, "Use a value like 256M"),
  upload_max_filesize: z.string().regex(PHP_SIZE, "Use a value like 64M"),
});

type PhpSettingsValues = z.infer<typeof phpSettingsSchema>;

export function PhpCard({ site }: { site: Site }) {
  const queryClient = useQueryClient();

  const switchVersion = useMutation({
    mutationFn: async (php_version: string) => {
      const { data, error } = await api.POST("/api/sites/{site_id}/php", {
        params: { path: { site_id: site.id } },
        body: { php_version },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "PHP switch failed"));
      return data;
    },
    onSuccess: async (data) => {
      toast.success(`Now running PHP ${data.php_version}`);
      await queryClient.invalidateQueries({ queryKey: ["sites"] });
    },
    onError: (err) => toast.error(err.message),
  });

  const form = useForm<PhpSettingsValues>({
    resolver: zodResolver(phpSettingsSchema),
    defaultValues: {
      memory_limit: site.php_memory_limit,
      upload_max_filesize: site.php_upload_max_filesize,
    },
  });

  const onSubmit = form.handleSubmit(async (values) => {
    const { data, error } = await api.PATCH("/api/sites/{site_id}/php-settings", {
      params: { path: { site_id: site.id } },
      body: values,
    });
    if (error || !data) {
      form.setError("memory_limit", {
        type: "server",
        message: apiErrorMessage(error, "Saving PHP settings failed"),
      });
      return;
    }
    toast.success("PHP settings saved");
    await queryClient.invalidateQueries({ queryKey: ["sites"] });
  });

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium">PHP</CardTitle>
        <CardDescription>
          Switching versions brings the new pool up before the old one goes away.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <label htmlFor="php-version-select" className="text-sm font-medium">
            Version
          </label>
          <select
            id="php-version-select"
            className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            value={site.php_version}
            disabled={switchVersion.isPending}
            onChange={(e) => switchVersion.mutate(e.target.value)}
          >
            {PHP_VERSIONS.map((v) => (
              <option key={v} value={v}>
                PHP {v}
              </option>
            ))}
          </select>
          {switchVersion.isPending && <p className="text-xs text-muted-foreground">Switching…</p>}
        </div>

        <form onSubmit={onSubmit} className="space-y-3" noValidate>
          <FormField
            label="Memory limit"
            htmlFor="memory_limit"
            error={form.formState.errors.memory_limit?.message}
          >
            <Input id="memory_limit" {...form.register("memory_limit")} />
          </FormField>
          <FormField
            label="Max upload size"
            htmlFor="upload_max_filesize"
            error={form.formState.errors.upload_max_filesize?.message}
          >
            <Input id="upload_max_filesize" {...form.register("upload_max_filesize")} />
          </FormField>
          <Button type="submit" size="sm" loading={form.formState.isSubmitting}>
            Save PHP settings
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
