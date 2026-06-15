
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { RotateCcw, Package, RefreshCw } from "lucide-react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { toast } from "sonner";

interface RollbackImage {
  ref: string;
  service: string;
  tag: string;
  created_at: string | null;
  size: string | null;
}

export function RollbackList({
  stackId,
  onOperation,
}: {
  stackId: number;
  onOperation?: (id: number) => void;
}) {
  const queryClient = useQueryClient();

  const { data, isLoading, refetch, isRefetching } = useQuery<{ images: RollbackImage[]; error?: string }>({
    queryKey: ["stacks", stackId, "rollback-images"],
    queryFn: async () => {
      // @ts-ignore
      const res = await api.GET(`/api/stacks/${stackId}/rollback-images` as any);
      return (res.data as any) ?? { images: [] };
    },
    staleTime: 30_000,
  });

  const rollback = useMutation({
    mutationFn: async ({ serviceName, imageRef }: { serviceName: string; imageRef: string }) => {
      // @ts-ignore
      const res = await api.POST(`/api/stacks/${stackId}/rollback-image` as any, {
        body: { service_name: serviceName, image_ref: imageRef },
      });
      if (res.error) throw new Error((res.error as any)?.detail ?? "Rollback failed");
      return res.data as any;
    },
    onSuccess: (data: any) => {
      toast.success("Rollback initiated — container is restarting");
      if (onOperation && data?.operation_id) onOperation(data.operation_id);
      queryClient.invalidateQueries({ queryKey: ["stacks", stackId] });
    },
    onError: (e: any) => toast.error(e.message),
  });

  const images = data?.images ?? [];

  // Group by service
  const byService = images.reduce<Record<string, RollbackImage[]>>((acc, img) => {
    (acc[img.service] ??= []).push(img);
    return acc;
  }, {});

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle className="text-base flex items-center gap-2">
              <RotateCcw className="w-4 h-4 text-destructive" />
              Rollback
            </CardTitle>
            <CardDescription className="mt-1">
              Instantly revert a service to a previously built image without triggering a rebuild.
            </CardDescription>
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => refetch()}
            disabled={isRefetching}
          >
            <RefreshCw className={`h-3.5 w-3.5 ${isRefetching ? "animate-spin" : ""}`} />
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className="space-y-2">
            {[1, 2, 3].map((i) => (
              <div key={i} className="h-10 rounded-md bg-muted animate-pulse" />
            ))}
          </div>
        ) : images.length === 0 ? (
          <div className="flex flex-col items-center justify-center rounded-xl border border-dashed py-10 text-center">
            <Package className="mb-3 h-7 w-7 text-muted-foreground/40" />
            <p className="text-sm font-medium">No rollback images available</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Images are tagged during each build. Deploy at least once to enable rollback.
            </p>
            {data?.error && (
              <p className="mt-2 text-xs text-red-400 font-mono">{data.error}</p>
            )}
          </div>
        ) : (
          <div className="space-y-4">
            {Object.entries(byService).map(([service, imgs]) => (
              <div key={service} className="space-y-2">
                <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                  {service}
                </p>
                {imgs.map((img) => (
                  <div
                    key={img.ref}
                    className="flex items-center justify-between rounded-lg border bg-muted/30 px-4 py-2.5 gap-3"
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <Badge variant="outline" className="font-mono text-xs shrink-0">
                        {img.tag}
                      </Badge>
                      {img.created_at && (
                        <span className="text-xs text-muted-foreground truncate">{img.created_at}</span>
                      )}
                      {img.size && (
                        <span className="text-xs text-muted-foreground shrink-0">{img.size}</span>
                      )}
                    </div>
                    <Button
                      variant="outline"
                      size="sm"
                      className="shrink-0 text-destructive border-destructive/30 hover:bg-destructive/10"
                      disabled={rollback.isPending}
                      onClick={() => rollback.mutate({ serviceName: img.service, imageRef: img.ref })}
                    >
                      <RotateCcw className="h-3 w-3 mr-1.5" />
                      Rollback
                    </Button>
                  </div>
                ))}
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
