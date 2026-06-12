import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

import { api, apiErrorMessage } from "@/lib/api/client";
/**
 * Admin notifications on the dashboard (Phase 11d): unresolved conditions —
 * service down, disk full, cert failures, quota overruns, failed backups.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BellRing, Check, X, Info, AlertTriangle, AlertCircle, Clock } from "lucide-react";
import { toast } from "sonner";
import { cn } from "@/lib/utils";

const SEVERITY_CONFIG = {
  info: { icon: Info, colorClass: "text-blue-500", bgClass: "bg-blue-500/10" },
  warning: { icon: AlertTriangle, colorClass: "text-amber-500", bgClass: "bg-amber-500/10" },
  error: { icon: AlertCircle, colorClass: "text-red-500", bgClass: "bg-red-500/10" },
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
    <section aria-label="Notifications" className="mb-8">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="flex items-center gap-2 text-lg font-semibold tracking-tight">
          <BellRing className="h-5 w-5 text-amber-500" aria-hidden />
          Notifications
          <Badge variant="secondary" className="rounded-full px-2 py-0.5 text-xs font-normal">
            {rows.length}
          </Badge>
        </h2>
      </div>
      <div className="grid gap-3">
        {rows.map((n) => {
          const config = SEVERITY_CONFIG[n.severity as keyof typeof SEVERITY_CONFIG] || SEVERITY_CONFIG.info;
          const Icon = config.icon;
          return (
            <div
              key={n.id}
              className={cn(
                "group relative flex gap-4 rounded-xl border p-4 transition-all hover:shadow-md",
                n.read ? "bg-card/50 border-border/50 opacity-70" : "bg-card border-border shadow-sm",
                !n.read && "hover:border-primary/30"
              )}
            >
              <div
                className={cn(
                  "mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full",
                  config.bgClass,
                  config.colorClass
                )}
              >
                <Icon className="h-4 w-4" />
              </div>

              <div className="grid flex-1 gap-1">
                <div className="flex items-center justify-between gap-2">
                  <p
                    className={cn(
                      "text-sm font-semibold leading-none tracking-tight",
                      n.read ? "text-muted-foreground" : "text-foreground"
                    )}
                  >
                    {n.kind.replaceAll("_", " ")}
                  </p>
                  <span className="flex items-center gap-1 text-xs text-muted-foreground">
                    <Clock className="h-3 w-3" />
                    {new Date(n.created_at).toLocaleString(undefined, {
                      month: "short",
                      day: "numeric",
                      hour: "numeric",
                      minute: "2-digit",
                    })}
                  </span>
                </div>
                <p
                  className={cn(
                    "text-sm",
                    n.read ? "text-muted-foreground/80" : "text-muted-foreground"
                  )}
                >
                  {n.message}
                </p>
              </div>

              <div className="flex shrink-0 items-center gap-1 opacity-100 sm:opacity-0 sm:transition-opacity sm:group-hover:opacity-100">
                {!n.read && (
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 text-muted-foreground hover:bg-primary/10 hover:text-primary"
                    aria-label="Mark as read"
                    onClick={() => markRead.mutate(n.id)}
                  >
                    <Check className="h-4 w-4" />
                  </Button>
                )}
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                  aria-label="Dismiss notification"
                  onClick={() => dismiss.mutate(n.id)}
                >
                  <X className="h-4 w-4" />
                </Button>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
