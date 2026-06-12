import { FormField } from "@/components/form-field";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api, apiErrorMessage } from "@/lib/api/client";
/**
 * Panel domain & HTTPS: point a domain at the server, Caddy obtains the
 * certificate, secure cookies are enabled, and the change persists.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Lock } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

export function PanelDomainCard() {
  const queryClient = useQueryClient();
  const [domain, setDomain] = useState("");
  const [dnsError, setDnsError] = useState<string | null>(null);

  const config = useQuery({
    queryKey: ["system", "panel-domain"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/system/panel-domain");
      if (error || !data) {
        throw new Error(apiErrorMessage(error, "Failed to load the panel domain"));
      }
      return data;
    },
  });

  const save = useMutation({
    mutationFn: async (force: boolean) => {
      const { data, error, response } = await api.PUT("/api/system/panel-domain", {
        body: { domain: domain.trim(), force },
      });
      if (error || !data) {
        throw Object.assign(new Error(apiErrorMessage(error, "Could not set the panel domain")), {
          status: response.status,
        });
      }
      return data;
    },
    onSuccess: async (data) => {
      setDomain("");
      setDnsError(null);
      await queryClient.invalidateQueries({ queryKey: ["system", "panel-domain"] });
      toast.success(`Panel available at ${data.url} — log in there to stay signed in`);
    },
    onError: (err) => {
      // DNS-mismatch conflicts get an inline "enable anyway" path.
      if ((err as { status?: number }).status === 409) setDnsError(err.message);
      else toast.error(err.message);
    },
  });

  const remove = useMutation({
    mutationFn: async () => {
      const { error } = await api.DELETE("/api/system/panel-domain");
      if (error) throw new Error(apiErrorMessage(error, "Could not remove the panel domain"));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["system", "panel-domain"] });
      toast.success("Panel domain removed — sessions work over HTTP again");
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Lock className="h-4 w-4 text-muted-foreground" aria-hidden /> Panel domain (HTTPS)
        </CardTitle>
        <CardDescription>
          Serve this panel on a domain. Caddy obtains the certificate automatically and session
          cookies become HTTPS-only. Point an A record at this server first.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {config.isPending ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : config.isError ? (
          <p className="text-sm text-muted-foreground">{config.error.message}</p>
        ) : config.data.domain ? (
          <div className="space-y-3 text-sm">
            <div className="flex items-center gap-2">
              <Badge variant="success">HTTPS</Badge>
              <a
                href={config.data.url ?? "#"}
                target="_blank"
                rel="noreferrer"
                className="font-mono text-xs hover:underline"
              >
                {config.data.url}
              </a>
            </div>
            <Button variant="outline" loading={remove.isPending} onClick={() => remove.mutate()}>
              Remove domain
            </Button>
          </div>
        ) : (
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault();
              if (domain.trim()) save.mutate(false);
            }}
          >
            <FormField label="Domain" htmlFor="panel-domain">
              <Input
                id="panel-domain"
                placeholder="panel.example.com"
                autoComplete="off"
                spellCheck={false}
                value={domain}
                onChange={(e) => {
                  setDomain(e.target.value);
                  setDnsError(null);
                }}
              />
            </FormField>
            {dnsError && (
              <div className="space-y-2 rounded-md border border-border p-3 text-xs">
                <p className="text-muted-foreground">{dnsError}</p>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  loading={save.isPending}
                  onClick={() => save.mutate(true)}
                >
                  Enable anyway (DNS still propagating)
                </Button>
              </div>
            )}
            <Button type="submit" disabled={!domain.trim()} loading={save.isPending}>
              Enable HTTPS
            </Button>
          </form>
        )}
      </CardContent>
    </Card>
  );
}
