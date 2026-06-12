import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, apiErrorMessage } from "@/lib/api/client";
/**
 * Admin notifications on the dashboard (Phase 11d): unresolved conditions —
 * service down, disk full, cert failures, quota overruns, failed backups.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BellRing, Check, X } from "lucide-react";
import { toast } from "sonner";

const SEVERITY_VARIANT = {
  info: "secondary",
  warning: "outline",
  error: "destructive",
} as const;

export function NotificationsCard() {
  const queryClient = useQueryClient();
  const notifications = useQuery({
    queryKey: ["notifications"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/notifications", {
        params: { query: { limit: 20 } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load notifications"));
      return data;
    },
    refetchInterval: 60_000,
  });

  const markRead = useMutation({
    mutationFn: async (id: number) => {
      const { error, response } = await api.POST("/api/notifications/{notification_id}/read", {
        params: { path: { notification_id: id } },
      });
      if (error) throw new Error(apiErrorMessage(error, `Request failed (${response.status})`));
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
    onError: (err) => toast.error(err.message),
  });

  const dismiss = useMutation({
    mutationFn: async (id: number) => {
      const { error, response } = await api.DELETE("/api/notifications/{notification_id}", {
        params: { path: { notification_id: id } },
      });
      if (error) throw new Error(apiErrorMessage(error, `Request failed (${response.status})`));
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
    onError: (err) => toast.error(err.message),
  });

  const rows = notifications.data ?? [];
  if (rows.length === 0) return null;

  return (
    <section aria-label="Notifications">
      <Card className="border-amber-500/40">
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-sm font-medium">
            <BellRing className="h-4 w-4 text-amber-500" aria-hidden />
            Notifications
            <Badge variant="secondary">{rows.length}</Badge>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {rows.map((n) => (
            <div
              key={n.id}
              className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border px-3 py-2 text-sm"
            >
              <div className="flex min-w-0 items-center gap-2">
                <Badge variant={SEVERITY_VARIANT[n.severity as keyof typeof SEVERITY_VARIANT]}>
                  {n.kind.replaceAll("_", " ")}
                </Badge>
                <span className={n.read ? "text-muted-foreground" : "font-medium"}>
                  {n.message}
                </span>
              </div>
              <div className="flex items-center gap-1">
                <span className="text-xs text-muted-foreground">
                  {new Date(n.created_at).toLocaleString()}
                </span>
                {!n.read && (
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="Mark as read"
                    onClick={() => markRead.mutate(n.id)}
                  >
                    <Check className="h-4 w-4" />
                  </Button>
                )}
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label="Dismiss notification"
                  onClick={() => dismiss.mutate(n.id)}
                >
                  <X className="h-4 w-4" />
                </Button>
              </div>
            </div>
          ))}
        </CardContent>
      </Card>
    </section>
  );
}
