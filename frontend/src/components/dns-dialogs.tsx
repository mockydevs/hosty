import { FormField } from "@/components/form-field";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogActions,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { api, apiErrorMessage } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
/** DNS dialogs: create/delete zone, add/edit record, delete record. */
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { z } from "zod";

type RRSet = components["schemas"]["RRSetResponse"];

export const RECORD_TYPES = ["A", "AAAA", "CNAME", "MX", "TXT", "NS", "SRV", "CAA"] as const;

const PLACEHOLDERS: Record<(typeof RECORD_TYPES)[number], string> = {
  A: "192.0.2.1",
  AAAA: "2001:db8::1",
  CNAME: "target.example.com",
  MX: "10 mail.example.com",
  TXT: "v=spf1 mx ~all",
  NS: "ns1.example.com",
  SRV: "10 5 5060 sip.example.com",
  CAA: '0 issue "letsencrypt.org"',
};

// --- create zone -------------------------------------------------------------------

const createZoneSchema = z.object({
  name: z
    .string()
    .min(3, "Zone name is required")
    .regex(/^[a-zA-Z0-9.-]+\.?$/, "Enter a domain like example.com"),
  point_to_server: z.boolean(),
});
type CreateZoneForm = z.infer<typeof createZoneSchema>;

export function CreateZoneDialog({
  open,
  onClose,
  serverIp,
}: {
  open: boolean;
  onClose: () => void;
  serverIp: string;
}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const form = useForm<CreateZoneForm>({
    resolver: zodResolver(createZoneSchema),
    defaultValues: { name: "", point_to_server: false },
  });

  const create = useMutation({
    mutationFn: async (body: CreateZoneForm) => {
      const { data, error } = await api.POST("/api/dns/zones", { body });
      if (error || !data) throw new Error(apiErrorMessage(error, "Could not create the zone"));
      return data;
    },
    onSuccess: async (zone) => {
      await queryClient.invalidateQueries({ queryKey: ["dns"] });
      toast.success(`Zone ${zone.name} created`);
      form.reset();
      onClose();
      navigate(`/dns/${encodeURIComponent(zone.id)}`);
    },
    onError: (err) => form.setError("root", { message: err.message }),
  });

  return (
    <Dialog open={open} onClose={onClose}>
      <DialogContent>
        <DialogTitle>New DNS zone</DialogTitle>
        <DialogDescription>
          Creates the zone in PowerDNS with default nameservers (NS records).
        </DialogDescription>
        <form
          className="flex flex-col gap-3"
          onSubmit={form.handleSubmit((values) => create.mutate(values))}
        >
          <FormField label="Domain" htmlFor="zone-name" error={form.formState.errors.name?.message}>
            <Input
              id="zone-name"
              placeholder="example.com"
              autoComplete="off"
              {...form.register("name")}
            />
          </FormField>
          <label className="flex items-center gap-2 text-sm" htmlFor="zone-point">
            <input
              id="zone-point"
              type="checkbox"
              disabled={!serverIp}
              {...form.register("point_to_server")}
            />
            Point it at this server{" "}
            {serverIp ? `(A @ → ${serverIp}, CNAME www)` : "(set HOSTY_PUBLIC_IP to enable)"}
          </label>
          {form.formState.errors.root && (
            <p className="text-sm text-destructive" role="alert">
              {form.formState.errors.root.message}
            </p>
          )}
          <DialogActions>
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" loading={create.isPending}>
              Create zone
            </Button>
          </DialogActions>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// --- delete zone -------------------------------------------------------------------

export function DeleteZoneDialog({
  zone,
  onClose,
}: {
  zone: string | null; // zone id, e.g. "example.com."
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const form = useForm<{ confirm: string }>({ defaultValues: { confirm: "" } });
  const bare = (zone ?? "").replace(/\.$/, "");

  const destroy = useMutation({
    mutationFn: async (confirm: string) => {
      const { error } = await api.DELETE("/api/dns/zones/{zone_id}", {
        params: { path: { zone_id: zone ?? "" } },
        body: { confirm_name: confirm },
      });
      if (error) throw new Error(apiErrorMessage(error, "Could not delete the zone"));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dns"] });
      toast.success(`Zone ${bare} deleted`);
      form.reset();
      onClose();
      navigate("/dns");
    },
    onError: (err) => form.setError("root", { message: err.message }),
  });

  return (
    <Dialog open={zone !== null} onClose={onClose}>
      <DialogContent>
        <DialogTitle>Delete zone {bare}</DialogTitle>
        <DialogDescription>
          All records in this zone are removed from PowerDNS. This cannot be undone. Type the zone
          name to confirm.
        </DialogDescription>
        <form
          className="flex flex-col gap-3"
          onSubmit={form.handleSubmit(({ confirm }) => destroy.mutate(confirm))}
        >
          <FormField
            label="Zone name"
            htmlFor="zone-confirm"
            error={form.formState.errors.root?.message}
          >
            <Input
              id="zone-confirm"
              placeholder={bare}
              autoComplete="off"
              {...form.register("confirm")}
            />
          </FormField>
          <DialogActions>
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant="destructive" loading={destroy.isPending}>
              Delete zone
            </Button>
          </DialogActions>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// --- add / edit record -------------------------------------------------------------

const recordSchema = z.object({
  name: z.string().max(254),
  type: z.enum(RECORD_TYPES),
  ttl: z.coerce.number().int().min(60, "Min TTL is 60s").max(604800, "Max TTL is 7 days"),
  values: z.string().trim().min(1, "At least one value (one per line)"),
});
type RecordForm = z.infer<typeof recordSchema>;

export function RecordDialog({
  zoneId,
  defaultTtl,
  existing,
  open,
  onClose,
}: {
  zoneId: string;
  defaultTtl: number;
  existing: RRSet | null; // editing when set
  open: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const form = useForm<RecordForm>({
    resolver: zodResolver(recordSchema),
    values: {
      name: existing?.name ?? "",
      type: (existing?.type as RecordForm["type"]) ?? "A",
      ttl: existing?.ttl || defaultTtl,
      values: existing ? existing.records.join("\n") : "",
    },
  });
  const selectedType = form.watch("type");

  const save = useMutation({
    mutationFn: async (values: RecordForm) => {
      const records = values.values
        .split("\n")
        .map((v) => v.trim())
        .filter(Boolean);
      const { error } = await api.PUT("/api/dns/zones/{zone_id}/records", {
        params: { path: { zone_id: zoneId } },
        body: {
          name: values.name || "@",
          type: values.type,
          ttl: values.ttl,
          records,
        },
      });
      if (error) throw new Error(apiErrorMessage(error, "Could not save the record"));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dns"] });
      toast.success(existing ? "Record updated" : "Record added");
      form.reset();
      onClose();
    },
    onError: (err) => form.setError("root", { message: err.message }),
  });

  return (
    <Dialog open={open} onClose={onClose}>
      <DialogContent>
        <DialogTitle>{existing ? "Edit record" : "Add record"}</DialogTitle>
        <form
          className="flex flex-col gap-3"
          onSubmit={form.handleSubmit((values) => save.mutate(values))}
        >
          <div className="grid grid-cols-2 gap-3">
            <FormField label="Name" htmlFor="rec-name" error={form.formState.errors.name?.message}>
              <Input
                id="rec-name"
                placeholder="@ or www"
                autoComplete="off"
                disabled={existing !== null}
                {...form.register("name")}
              />
            </FormField>
            <FormField label="Type" htmlFor="rec-type" error={form.formState.errors.type?.message}>
              <select
                id="rec-type"
                className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm"
                disabled={existing !== null}
                {...form.register("type")}
              >
                {RECORD_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </FormField>
          </div>
          <FormField
            label="TTL (seconds)"
            htmlFor="rec-ttl"
            error={form.formState.errors.ttl?.message}
          >
            <Input id="rec-ttl" type="number" {...form.register("ttl")} />
          </FormField>
          <FormField
            label="Values (one per line)"
            htmlFor="rec-values"
            error={form.formState.errors.values?.message}
          >
            <textarea
              id="rec-values"
              rows={3}
              placeholder={PLACEHOLDERS[selectedType]}
              className="flex w-full rounded-md border border-input bg-transparent px-3 py-2 font-mono text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              {...form.register("values")}
            />
          </FormField>
          {form.formState.errors.root && (
            <p className="text-sm text-destructive" role="alert">
              {form.formState.errors.root.message}
            </p>
          )}
          <DialogActions>
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" loading={save.isPending}>
              {existing ? "Save changes" : "Add record"}
            </Button>
          </DialogActions>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// --- delete record -----------------------------------------------------------------

export function DeleteRecordDialog({
  zoneId,
  target,
  onClose,
}: {
  zoneId: string;
  target: { name: string; type: string } | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();

  const destroy = useMutation({
    mutationFn: async (t: { name: string; type: string }) => {
      const { error } = await api.DELETE("/api/dns/zones/{zone_id}/records", {
        params: { path: { zone_id: zoneId } },
        body: { name: t.name, type: t.type },
      });
      if (error) throw new Error(apiErrorMessage(error, "Could not delete the record"));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dns"] });
      toast.success("Record deleted");
      onClose();
    },
    onError: (err) => {
      toast.error(err.message);
      onClose();
    },
  });

  return (
    <Dialog open={target !== null} onClose={onClose}>
      <DialogContent>
        <DialogTitle>Delete record</DialogTitle>
        <DialogDescription>
          Remove the {target?.type} record set for <span className="font-mono">{target?.name}</span>
          ?
        </DialogDescription>
        <DialogActions>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            loading={destroy.isPending}
            onClick={() => target && destroy.mutate(target)}
          >
            Delete
          </Button>
        </DialogActions>
      </DialogContent>
    </Dialog>
  );
}
