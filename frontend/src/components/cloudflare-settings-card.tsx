import { FormField } from "@/components/form-field";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api, apiErrorMessage } from "@/lib/api/client";
/**
 * Cloudflare API token management: verify against Cloudflare on save, stored
 * encrypted in the panel DB. Enables zone push + record management.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Cloud } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

export function CloudflareSettingsCard() {
  const queryClient = useQueryClient();
  const [token, setToken] = useState("");

  const config = useQuery({
    queryKey: ["dns", "cloudflare", "config"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/dns/cloudflare/config");
      if (error || !data) {
        throw new Error(apiErrorMessage(error, "Failed to load Cloudflare settings"));
      }
      return data;
    },
  });

  const save = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.PUT("/api/dns/cloudflare/config", {
        body: { api_token: token.trim() },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Token verification failed"));
      return data;
    },
    onSuccess: async () => {
      setToken("");
      await queryClient.invalidateQueries({ queryKey: ["dns"] });
      toast.success("Cloudflare token verified and saved");
    },
    onError: (err) => toast.error(err.message),
  });

  const remove = useMutation({
    mutationFn: async () => {
      const { error } = await api.DELETE("/api/dns/cloudflare/config");
      if (error) throw new Error(apiErrorMessage(error, "Could not remove the token"));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dns"] });
      toast.success("Cloudflare token removed");
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Cloud className="h-4 w-4 text-muted-foreground" aria-hidden /> Cloudflare
        </CardTitle>
        <CardDescription>
          An API token (with Zone.DNS edit permission) enables pushing zones to Cloudflare and
          managing the DNS records of domains in your Cloudflare account.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {config.isPending ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : config.isError ? (
          <p className="text-sm text-muted-foreground">{config.error.message}</p>
        ) : (
          <div className="flex items-center gap-2 text-sm">
            {config.data.configured ? (
              <>
                <Badge variant="success">Connected</Badge>
                <span className="text-muted-foreground">
                  {config.data.source === "env"
                    ? "token from server environment"
                    : "token stored encrypted in the panel"}
                </span>
              </>
            ) : (
              <Badge variant="outline">Not configured</Badge>
            )}
          </div>
        )}

        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (token.trim().length >= 10) save.mutate();
          }}
        >
          <FormField label="API token" htmlFor="cf-token">
            <Input
              id="cf-token"
              type="password"
              placeholder={config.data?.configured ? "Replace token…" : "Paste your token"}
              autoComplete="off"
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
          </FormField>
          <div className="flex gap-2">
            <Button type="submit" disabled={token.trim().length < 10} loading={save.isPending}>
              Verify &amp; save
            </Button>
            {config.data?.configured && config.data.source === "db" && (
              <Button
                type="button"
                variant="outline"
                loading={remove.isPending}
                onClick={() => remove.mutate()}
              >
                Remove
              </Button>
            )}
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
