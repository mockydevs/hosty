import { FormField } from "@/components/form-field";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api, apiErrorMessage } from "@/lib/api/client";
/**
 * Admin notifications webhook (Phase 11d): every new panel notification is
 * POSTed as JSON to this URL (best-effort).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { toast } from "sonner";

export function NotificationWebhookCard() {
  const queryClient = useQueryClient();
  const [url, setUrl] = useState("");
  const [error, setError] = useState<string | null>(null);

  const config = useQuery({
    queryKey: ["notifications", "webhook"],
    queryFn: async () => {
      const { data, error: apiError } = await api.GET("/api/notifications/webhook");
      if (apiError || !data) throw new Error(apiErrorMessage(apiError, "Failed to load webhook"));
      return data;
    },
  });

  useEffect(() => {
    if (config.data?.url) setUrl(config.data.url);
  }, [config.data]);

  const save = useMutation({
    mutationFn: async () => {
      const { error: apiError, response } = await api.PUT("/api/notifications/webhook", {
        body: { url: url.trim() },
      });
      if (apiError) throw new Error(apiErrorMessage(apiError, `Save failed (${response.status})`));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["notifications", "webhook"] });
      setError(null);
      toast.success("Webhook saved");
    },
    onError: (err) => setError(err.message),
  });

  const remove = useMutation({
    mutationFn: async () => {
      const { error: apiError } = await api.DELETE("/api/notifications/webhook");
      if (apiError) {
        throw new Error(apiErrorMessage(apiError, "Failed to remove the webhook"));
      }
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["notifications", "webhook"] });
      setUrl("");
      setError(null);
      toast.success("Webhook removed");
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Notification webhook</CardTitle>
        <CardDescription>
          POSTs every new panel notification (service down, disk full, backup failed…) as JSON —
          point it at Slack-compatible receivers or your own endpoint.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
          noValidate
        >
          <FormField label="Webhook URL" htmlFor="webhook-url" error={error ?? undefined}>
            <Input
              id="webhook-url"
              type="url"
              placeholder="https://hooks.example.com/notify"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
            />
          </FormField>
          <div className="flex gap-2">
            <Button type="submit" disabled={url.trim().length === 0} loading={save.isPending}>
              Save webhook
            </Button>
            {config.data?.configured && (
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
