import { FormField } from "@/components/form-field";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api, apiErrorMessage } from "@/lib/api/client";
/**
 * Apps base domain: the wildcard root used to auto-generate stack domains
 * (`<stack>.<base>`). Point `*.<base>` at this server once. Without it, stacks
 * fall back to a zero-config sslip.io name off the server's public IP.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Globe } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

export function AppsDomainCard() {
  const queryClient = useQueryClient();
  const [base, setBase] = useState("");

  const config = useQuery({
    queryKey: ["system", "apps-base-domain"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/system/apps-base-domain");
      if (error || !data) {
        throw new Error(apiErrorMessage(error, "Failed to load the apps base domain"));
      }
      return data;
    },
  });

  const save = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.PUT("/api/system/apps-base-domain", {
        body: { base_domain: base.trim() },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Could not set the base domain"));
      return data;
    },
    onSuccess: async () => {
      setBase("");
      await queryClient.invalidateQueries({ queryKey: ["system", "apps-base-domain"] });
      toast.success("Apps base domain saved — new web stacks get a subdomain automatically");
    },
    onError: (err) => toast.error(err.message),
  });

  const remove = useMutation({
    mutationFn: async () => {
      const { error } = await api.DELETE("/api/system/apps-base-domain");
      if (error) throw new Error(apiErrorMessage(error, "Could not remove the base domain"));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["system", "apps-base-domain"] });
      toast.success("Base domain removed — stacks fall back to sslip.io");
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Globe className="h-4 w-4 text-muted-foreground" aria-hidden /> Apps base domain
        </CardTitle>
        <CardDescription>
          Auto-generate stack domains as <code>&lt;stack&gt;.&lt;base&gt;</code>. Point a wildcard
          record <code>*.&lt;base&gt;</code> at this server. Without it, stacks fall back to a
          zero-config sslip.io name off the server's public IP.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {config.isPending ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : config.isError ? (
          <p className="text-sm text-muted-foreground">{config.error.message}</p>
        ) : config.data.base_domain ? (
          <div className="space-y-3 text-sm">
            <div className="flex items-center gap-2">
              <Badge variant="secondary">Wildcard</Badge>
              <span className="font-mono text-xs">*.{config.data.base_domain}</span>
            </div>
            <Button variant="outline" loading={remove.isPending} onClick={() => remove.mutate()}>
              Remove base domain
            </Button>
          </div>
        ) : (
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault();
              if (base.trim()) save.mutate();
            }}
          >
            <FormField label="Base domain" htmlFor="apps-base-domain">
              <Input
                id="apps-base-domain"
                placeholder="apps.example.com"
                autoComplete="off"
                spellCheck={false}
                value={base}
                onChange={(e) => setBase(e.target.value)}
              />
            </FormField>
            {config.data.sslip_fallback_ip ? (
              <p className="text-xs text-muted-foreground">
                Fallback today: <code>&lt;stack&gt;.{config.data.sslip_fallback_ip}.sslip.io</code>
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">
                No public IP configured — set one to enable the sslip.io fallback.
              </p>
            )}
            <Button type="submit" disabled={!base.trim()} loading={save.isPending}>
              Save base domain
            </Button>
          </form>
        )}
      </CardContent>
    </Card>
  );
}
