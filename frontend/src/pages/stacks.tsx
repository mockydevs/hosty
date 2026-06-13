import { EmptyState, ErrorState, LoadingState } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, apiErrorMessage } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
/**
 * Stacks list (v2/M4): every blueprint-deployed workload in one table —
 * status pills driven by the reconciler's generation/observed_generation
 * projection, not optimistic UI.
 */
import { useQuery } from "@tanstack/react-query";
import { Boxes, Plus, Search } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

type Stack = components["schemas"]["StackResponse"];

const STATUS_VARIANTS: Record<string, "success" | "secondary" | "destructive" | "outline"> = {
  ready: "success",
  converging: "secondary",
  deleting: "secondary",
  suspended: "outline",
  degraded: "destructive",
  error: "destructive",
};

/** The pill shows convergence truth: a stack whose spec generation is ahead
 * of what the reconciler last converged is "converging" regardless of the
 * cached status. */
export function stackDisplayStatus(stack: Stack): string {
  if (stack.status === "ready" && stack.observed_generation !== stack.generation) {
    return "converging";
  }
  return stack.status;
}

export function StackStatusBadge({ stack }: { stack: Stack }) {
  const status = stackDisplayStatus(stack);
  return <Badge variant={STATUS_VARIANTS[status] ?? "outline"}>{status}</Badge>;
}

export function isSettling(stack: Stack): boolean {
  return stackDisplayStatus(stack) === "converging" || stack.status === "deleting";
}

export function StacksPage() {
  const [search, setSearch] = useState("");
  const navigate = useNavigate();

  const stacks = useQuery({
    queryKey: ["stacks"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/stacks");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load stacks"));
      return data;
    },
    refetchInterval: (q) => (q.state.data?.some(isSettling) ? 3_000 : false),
  });

  const term = search.trim().toLowerCase();
  const filtered: Stack[] = (stacks.data ?? []).filter(
    (s) => s.name.includes(term) || s.endpoints.some((e) => e.domain.includes(term)),
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">Deployments</h1>
        <Button onClick={() => navigate("/stacks/new")}>
          <Plus className="h-4 w-4" aria-hidden /> New deployment
        </Button>
      </div>

      <div className="relative max-w-sm">
        <Search
          className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
          aria-hidden
        />
        <Input
          aria-label="Search deployments"
          placeholder="Search names and domains…"
          className="pl-9"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {stacks.isPending ? (
        <LoadingState label="Loading deployments…" />
      ) : stacks.isError ? (
        <ErrorState message={stacks.error.message} onRetry={() => stacks.refetch()} />
      ) : filtered.length === 0 ? (
        <EmptyState
          title={search ? "No deployments match your search" : "No deployments yet"}
          description={
            search ? "Try a different name or domain." : "Deploy from Git, an image, or a template."
          }
        >
          {!search && (
            <Button className="mt-2" onClick={() => navigate("/stacks/new")}>
              <Plus className="h-4 w-4" aria-hidden /> New deployment
            </Button>
          )}
        </EmptyState>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Blueprint</TableHead>
              <TableHead className="hidden md:table-cell">Domains</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filtered.map((stack) => (
              <TableRow key={stack.id}>
                <TableCell>
                  <Link
                    to={`/stacks/${stack.id}`}
                    className="flex items-center gap-2 font-medium hover:underline"
                  >
                    <Boxes className="h-4 w-4 text-muted-foreground" aria-hidden />
                    {stack.name}
                  </Link>
                </TableCell>
                <TableCell>
                  <StackStatusBadge stack={stack} />
                </TableCell>
                <TableCell>
                  <Badge variant="outline">{stack.blueprint_id}</Badge>
                </TableCell>
                <TableCell className="hidden font-mono text-xs text-muted-foreground md:table-cell">
                  {stack.endpoints.map((e) => e.domain).join(", ") || "—"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
