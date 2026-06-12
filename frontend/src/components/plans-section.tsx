import { FormField } from "@/components/form-field";
import { EmptyState, ErrorState, LoadingState } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogActions,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
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
 * Plans (Phase 11d, admin-only): named quota bundles assignable to clients.
 * Explicit per-user values always override the plan's defaults.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Package, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

export type Plan = components["schemas"]["PlanResponse"];

function fmt(value: number | null | undefined, suffix = ""): string {
  return value === null || value === undefined ? "∞" : `${value}${suffix}`;
}

const EMPTY_FORM = {
  name: "",
  max_sites: "",
  max_databases: "",
  max_disk_mb: "",
  cpu_quota_percent: "",
  memory_max_mb: "",
};

type PlanForm = typeof EMPTY_FORM;

function toBody(form: PlanForm) {
  const num = (v: string) => (v === "" ? null : Number(v));
  return {
    name: form.name.trim(),
    max_sites: num(form.max_sites),
    max_databases: num(form.max_databases),
    max_disk_mb: num(form.max_disk_mb),
    cpu_quota_percent: num(form.cpu_quota_percent),
    memory_max_mb: num(form.memory_max_mb),
  };
}

function PlanDialog({
  open,
  plan,
  onClose,
}: {
  open: boolean;
  plan: Plan | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<PlanForm>(
    plan
      ? {
          name: plan.name,
          max_sites: plan.max_sites?.toString() ?? "",
          max_databases: plan.max_databases?.toString() ?? "",
          max_disk_mb: plan.max_disk_mb?.toString() ?? "",
          cpu_quota_percent: plan.cpu_quota_percent?.toString() ?? "",
          memory_max_mb: plan.memory_max_mb?.toString() ?? "",
        }
      : EMPTY_FORM,
  );
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: async () => {
      const body = toBody(form);
      const result = plan
        ? await api.PUT("/api/plans/{plan_id}", {
            params: { path: { plan_id: plan.id } },
            body,
          })
        : await api.POST("/api/plans", { body });
      if (result.error) {
        throw new Error(apiErrorMessage(result.error, `Save failed (${result.response.status})`));
      }
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["plans"] });
      await queryClient.invalidateQueries({ queryKey: ["users"] });
      toast.success(plan ? "Plan updated" : "Plan created");
      onClose();
    },
    onError: (err) => setError(err.message),
  });

  const set = (key: keyof PlanForm) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [key]: e.target.value }));

  return (
    <Dialog open={open} onClose={onClose}>
      <DialogContent>
        <DialogTitle>{plan ? `Edit plan ${plan.name}` : "New plan"}</DialogTitle>
        <DialogDescription>
          Leave a field blank for unlimited. Per-user overrides always win over the plan.
        </DialogDescription>
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
          noValidate
        >
          <FormField label="Name" htmlFor="plan-name" error={error ?? undefined}>
            <Input
              id="plan-name"
              placeholder="Starter"
              value={form.name}
              onChange={set("name")}
            />
          </FormField>
          <div className="grid grid-cols-2 gap-4">
            <FormField label="Max sites" htmlFor="plan-max-sites">
              <Input
                id="plan-max-sites"
                type="number"
                min={0}
                placeholder="Unlimited"
                value={form.max_sites}
                onChange={set("max_sites")}
              />
            </FormField>
            <FormField label="Max databases" htmlFor="plan-max-databases">
              <Input
                id="plan-max-databases"
                type="number"
                min={0}
                placeholder="Unlimited"
                value={form.max_databases}
                onChange={set("max_databases")}
              />
            </FormField>
            <FormField label="Disk (MB)" htmlFor="plan-max-disk">
              <Input
                id="plan-max-disk"
                type="number"
                min={1}
                placeholder="Unlimited"
                value={form.max_disk_mb}
                onChange={set("max_disk_mb")}
              />
            </FormField>
            <FormField label="CPU quota (%)" htmlFor="plan-cpu">
              <Input
                id="plan-cpu"
                type="number"
                min={1}
                max={1600}
                placeholder="Unlimited"
                value={form.cpu_quota_percent}
                onChange={set("cpu_quota_percent")}
              />
            </FormField>
            <FormField label="Memory (MB)" htmlFor="plan-memory">
              <Input
                id="plan-memory"
                type="number"
                min={16}
                placeholder="Unlimited"
                value={form.memory_max_mb}
                onChange={set("memory_max_mb")}
              />
            </FormField>
          </div>
          <DialogActions>
            <Button variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={form.name.trim().length === 0} loading={save.isPending}>
              {plan ? "Save plan" : "Create plan"}
            </Button>
          </DialogActions>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function PlansSection() {
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [editPlan, setEditPlan] = useState<Plan | null>(null);

  const plans = useQuery({
    queryKey: ["plans"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/plans");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load plans"));
      return data;
    },
  });

  const del = useMutation({
    mutationFn: async (plan: Plan) => {
      const { error, response } = await api.DELETE("/api/plans/{plan_id}", {
        params: { path: { plan_id: plan.id } },
      });
      if (error) throw new Error(apiErrorMessage(error, `Delete failed (${response.status})`));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["plans"] });
      toast.success("Plan deleted");
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <section aria-label="Plans" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-lg font-semibold tracking-tight">
            <Package className="h-4 w-4 text-muted-foreground" aria-hidden /> Plans
          </h2>
          <p className="text-sm text-muted-foreground">
            Named quota bundles for clients — assign them when creating or editing an account.
          </p>
        </div>
        <Button variant="outline" onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" aria-hidden /> New plan
        </Button>
      </div>

      {plans.isPending ? (
        <LoadingState label="Loading plans…" />
      ) : plans.isError ? (
        <ErrorState message={plans.error.message} onRetry={() => plans.refetch()} />
      ) : plans.data.length === 0 ? (
        <EmptyState
          title="No plans yet"
          description="Create a bundle like “Starter: 1 site, 1 DB, 1 GB” instead of setting raw numbers per client."
        />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Plan</TableHead>
              <TableHead>Sites</TableHead>
              <TableHead>DBs</TableHead>
              <TableHead className="hidden sm:table-cell">Disk</TableHead>
              <TableHead className="hidden sm:table-cell">CPU</TableHead>
              <TableHead className="hidden sm:table-cell">Memory</TableHead>
              <TableHead>Clients</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {plans.data.map((p) => (
              <TableRow key={p.id}>
                <TableCell className="font-medium">{p.name}</TableCell>
                <TableCell>{fmt(p.max_sites)}</TableCell>
                <TableCell>{fmt(p.max_databases)}</TableCell>
                <TableCell className="hidden sm:table-cell">{fmt(p.max_disk_mb, " MB")}</TableCell>
                <TableCell className="hidden sm:table-cell">
                  {fmt(p.cpu_quota_percent, "%")}
                </TableCell>
                <TableCell className="hidden sm:table-cell">
                  {fmt(p.memory_max_mb, " MB")}
                </TableCell>
                <TableCell>
                  <Badge variant="secondary">{p.user_count}</Badge>
                </TableCell>
                <TableCell>
                  <div className="flex items-center justify-end gap-1">
                    <Button variant="outline" size="sm" onClick={() => setEditPlan(p)}>
                      Edit
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label={`Delete plan ${p.name}`}
                      disabled={p.user_count > 0}
                      onClick={() => del.mutate(p)}
                    >
                      <Trash2 className="h-4 w-4 text-destructive" />
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {createOpen && <PlanDialog open onClose={() => setCreateOpen(false)} plan={null} />}
      {editPlan && <PlanDialog open plan={editPlan} onClose={() => setEditPlan(null)} />}
    </section>
  );
}
