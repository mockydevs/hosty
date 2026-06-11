import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, apiErrorMessage } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { cn } from "@/lib/utils";
/**
 * Live progress for a provisioning/deletion operation: polls every second
 * while running, then reports the terminal state via onFinished.
 */
import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, CircleAlert, CircleDashed, Loader2, Undo2 } from "lucide-react";
import { useEffect } from "react";

type Operation = components["schemas"]["OperationResponse"];

const STEP_ICONS = {
  pending: CircleDashed,
  running: Loader2,
  done: CheckCircle2,
  failed: CircleAlert,
  rolled_back: Undo2,
} as const;

function stepIcon(status: string) {
  return STEP_ICONS[status as keyof typeof STEP_ICONS] ?? CircleDashed;
}

export function OperationProgress({
  operationId,
  onFinished,
}: {
  operationId: number;
  onFinished?: (op: Operation) => void;
}) {
  const query = useQuery({
    queryKey: ["operations", operationId],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/operations/{operation_id}", {
        params: { path: { operation_id: operationId } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load operation"));
      return data;
    },
    refetchInterval: (q) => {
      const status = q.state.data?.status;
      return status === "succeeded" || status === "failed" ? false : 1_000;
    },
  });

  const op = query.data;
  const finished = op && (op.status === "succeeded" || op.status === "failed");

  useEffect(() => {
    if (op && finished) onFinished?.(op);
  }, [op, finished, onFinished]);

  if (!op) return null;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">
          {op.kind === "create_site" ? "Provisioning" : "Deleting"} {op.domain}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <ol className="space-y-1.5">
          {op.steps.map((step) => {
            const Icon = stepIcon(step.status);
            return (
              <li key={step.name} className="flex items-center gap-2 text-sm">
                <Icon
                  className={cn(
                    "h-4 w-4",
                    step.status === "running" && "animate-spin",
                    step.status === "done" && "text-success",
                    step.status === "failed" && "text-destructive",
                    step.status === "rolled_back" && "text-yellow-500",
                    step.status === "pending" && "text-muted-foreground",
                  )}
                  aria-hidden
                />
                <span className={cn(step.status === "pending" && "text-muted-foreground")}>
                  {step.label}
                </span>
                {step.status === "rolled_back" && (
                  <span className="text-xs text-muted-foreground">(rolled back)</span>
                )}
              </li>
            );
          })}
        </ol>
        {op.status === "failed" && op.error && (
          <p className="text-sm text-destructive" role="alert">
            {op.error}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
