import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { cn } from "@/lib/utils";
import {
  CheckCircle2,
  CircleAlert,
  CircleDashed,
  Clock,
  Loader2,
  Undo2,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";

// ── Types ────────────────────────────────────────────────────────────────────

type Step = { name: string; label: string; status: string };
type StackOp = {
  id: number;
  kind: string;
  domain: string;
  status: string;
  steps: Step[];
  error: string | null;
  log_lines: string;
  created_at: string;
  finished_at: string | null;
};

// ── Helpers ──────────────────────────────────────────────────────────────────

const KIND_LABELS: Record<string, string> = {
  create_stack: "Deploy",
  delete_stack: "Remove",
  converge_stack: "Redeploy",
  webhook_rebuild: "Webhook rebuild",
  backup_stack: "Backup",
  restore_stack: "Restore",
};

function kindLabel(kind: string) {
  return KIND_LABELS[kind] ?? kind.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

function relativeTime(iso: string) {
  const diff = Date.now() - new Date(iso).getTime();
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return new Date(iso).toLocaleDateString();
}

function duration(start: string, end: string | null) {
  if (!end) return null;
  const ms = new Date(end).getTime() - new Date(start).getTime();
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

// ── Status dot ───────────────────────────────────────────────────────────────

function StatusDot({ status }: { status: string }) {
  if (status === "running" || status === "pending") {
    return (
      <span className="relative flex h-3 w-3 shrink-0">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-blue-400 opacity-75" />
        <span className="relative inline-flex h-3 w-3 rounded-full bg-blue-500" />
      </span>
    );
  }
  if (status === "succeeded") {
    return <span className="h-3 w-3 shrink-0 rounded-full bg-emerald-500" />;
  }
  if (status === "failed") {
    return <span className="h-3 w-3 shrink-0 rounded-full bg-red-500" />;
  }
  return <span className="h-3 w-3 shrink-0 rounded-full bg-muted-foreground/40" />;
}

// ── Step row ─────────────────────────────────────────────────────────────────

const STEP_ICONS = {
  pending: CircleDashed,
  running: Loader2,
  done: CheckCircle2,
  failed: CircleAlert,
  rolled_back: Undo2,
} as const;

function StepRow({ step }: { step: Step }) {
  const Icon = STEP_ICONS[step.status as keyof typeof STEP_ICONS] ?? CircleDashed;
  return (
    <li className="flex items-center gap-2.5 py-1 text-sm">
      <Icon
        className={cn(
          "h-4 w-4 shrink-0",
          step.status === "running" && "animate-spin text-blue-400",
          step.status === "done" && "text-emerald-500",
          step.status === "failed" && "text-red-500",
          step.status === "rolled_back" && "text-yellow-500",
          step.status === "pending" && "text-muted-foreground/50",
        )}
        aria-hidden
      />
      <span
        className={cn(
          step.status === "pending" && "text-muted-foreground",
          step.status === "done" && "text-foreground",
          step.status === "running" && "font-medium text-foreground",
          step.status === "failed" && "text-red-500",
        )}
      >
        {step.label}
      </span>
      {step.status === "rolled_back" && (
        <span className="text-xs text-muted-foreground">(rolled back)</span>
      )}
    </li>
  );
}

// ── Terminal log ─────────────────────────────────────────────────────────────

function colorLine(line: string): { cls: string; text: string } {
  if (line.includes("✗") || line.toLowerCase().includes("error") || line.toLowerCase().includes("failed")) {
    return { cls: "text-red-400", text: line };
  }
  if (line.includes("✓") || line.includes("Done —")) {
    return { cls: "text-emerald-400", text: line };
  }
  if (line.includes("→") || line.includes("Starting")) {
    return { cls: "text-blue-300", text: line };
  }
  return { cls: "text-zinc-300", text: line };
}

function LogTerminal({ logLines, isRunning }: { logLines: string; isRunning: boolean }) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const lines = logLines ? logLines.split("\n").filter(Boolean) : [];

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logLines]);

  if (lines.length === 0 && !isRunning) return null;

  return (
    <div className="border-t border-border/60 bg-zinc-950 rounded-b-xl">
      <div className="h-56 overflow-y-auto px-4 py-3 font-mono text-xs leading-5 scrollbar-thin scrollbar-thumb-zinc-700">
        {lines.length === 0 ? (
          <span className="text-zinc-500 animate-pulse">Waiting for output...</span>
        ) : (
          lines.map((line, i) => {
            const { cls, text } = colorLine(line);
            return (
              <div key={i} className={cls}>
                {text}
              </div>
            );
          })
        )}
        {isRunning && (
          <div className="mt-1 flex items-center gap-1.5 text-zinc-500">
            <Loader2 className="h-3 w-3 animate-spin" />
            <span>Running…</span>
          </div>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}

// ── Single operation card ────────────────────────────────────────────────────

function OpCard({ op, live = false }: { op: StackOp; live?: boolean }) {
  const isRunning = op.status === "running" || op.status === "pending";
  const dur = duration(op.created_at, op.finished_at);

  return (
    <div
      className={cn(
        "rounded-xl border bg-card",
        live && "border-blue-500/40 shadow-[0_0_0_1px_theme(colors.blue.500/0.15)]",
      )}
    >
      {/* Header row */}
      <div className="flex items-center gap-3 px-4 py-3">
        <StatusDot status={op.status} />

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-semibold text-sm">{kindLabel(op.kind)}</span>
            {live && (
              <Badge variant="outline" className="border-blue-500/50 text-blue-400 text-[10px] px-1.5 py-0">
                LIVE
              </Badge>
            )}
            <span className="text-xs text-muted-foreground truncate">{op.domain}</span>
          </div>
          <div className="flex items-center gap-2 mt-0.5 text-xs text-muted-foreground">
            <Clock className="h-3 w-3" />
            <span>{relativeTime(op.created_at)}</span>
            {dur && <span>· {dur}</span>}
          </div>
        </div>

        <Badge
          variant={
            op.status === "succeeded"
              ? "default"
              : op.status === "failed"
                ? "destructive"
                : "secondary"
          }
          className={cn(
            "text-xs shrink-0",
            op.status === "succeeded" && "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
            isRunning && "bg-blue-500/20 text-blue-400 border-blue-500/30",
          )}
        >
          {op.status}
        </Badge>
      </div>

      {/* Steps (always shown for live, collapsible feel but always visible) */}
      {op.steps.length > 0 && (
        <div className="border-t border-border/60 px-4 pb-3 pt-2">
          <ol className="space-y-0.5">
            {op.steps.map((step) => (
              <StepRow key={step.name} step={step} />
            ))}
          </ol>
        </div>
      )}

      {/* Terminal log — shown whenever there are lines, or while live+running */}
      {(op.log_lines || (live && isRunning)) && (
        <LogTerminal logLines={op.log_lines} isRunning={isRunning} />
      )}

      {/* Error message for failed ops with no log output (timed out / orphaned) */}
      {op.status === "failed" && op.error && !op.log_lines && (
        <div className="border-t border-red-500/20 bg-red-950/40 px-4 py-2.5 rounded-b-xl">
          <p className="text-xs text-red-400 font-mono break-all">{op.error}</p>
        </div>
      )}
    </div>
  );
}

// ── Main tab ─────────────────────────────────────────────────────────────────

export function DeploymentsTab({
  stackId,
  activeOperationId,
}: {
  stackId: number;
  activeOperationId?: number | null;
}) {
  const { data: ops = [], isLoading } = useQuery<StackOp[]>({
    queryKey: ["stacks", stackId, "operations"],
    queryFn: async () => {
      // @ts-ignore
      const res = await api.GET(`/api/stacks/${stackId}/operations` as any);
      return res.data ?? [];
    },
    refetchInterval: (q) => {
      const list = q.state.data ?? [];
      const hasRunning = list.some(
        (op: StackOp) => op.status === "running" || op.status === "pending",
      );
      return hasRunning ? 2_000 : false;
    },
  });

  if (isLoading) {
    return (
      <div className="space-y-3">
        {[1, 2, 3].map((i) => (
          <div key={i} className="h-20 rounded-xl border bg-card animate-pulse" />
        ))}
      </div>
    );
  }

  if (ops.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center rounded-xl border border-dashed py-16 text-center">
        <Clock className="mb-3 h-8 w-8 text-muted-foreground/40" />
        <p className="text-sm font-medium">No deployments yet</p>
        <p className="mt-1 text-xs text-muted-foreground">
          Deployment history will appear here after the first deploy.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {ops.map((op) => (
        <OpCard
          key={op.id}
          op={op}
          live={activeOperationId != null && op.id === activeOperationId}
        />
      ))}
    </div>
  );
}
