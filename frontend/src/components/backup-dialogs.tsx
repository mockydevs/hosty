import { FormField } from "@/components/form-field";
import { OperationProgress } from "@/components/operation-progress";
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
/** Backup dialogs: S3 target, schedule/scope editor, restore wizard, delete, progress. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";

type BackupEntry = components["schemas"]["BackupResponse"];

// --- live progress for a started backup/restore -------------------------------------

export function OperationDialog({
  title,
  operationId,
  onClose,
}: {
  title: string;
  operationId: number | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  return (
    <Dialog open={operationId !== null} onClose={onClose}>
      <DialogContent>
        <DialogTitle>{title}</DialogTitle>
        {operationId !== null && (
          <OperationProgress
            operationId={operationId}
            onFinished={() => queryClient.invalidateQueries({ queryKey: ["backups"] })}
          />
        )}
        <DialogActions>
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
        </DialogActions>
      </DialogContent>
    </Dialog>
  );
}

// --- S3 target configuration ----------------------------------------------------------

interface S3ConfigForm {
  endpoint: string;
  bucket: string;
  region: string;
  prefix: string;
  access_key: string;
  secret_key: string;
}

export function S3ConfigDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [hasSecret, setHasSecret] = useState(false);
  const form = useForm<S3ConfigForm>({
    defaultValues: {
      endpoint: "",
      bucket: "",
      region: "",
      prefix: "hosty",
      access_key: "",
      secret_key: "",
    },
  });

  useQuery({
    queryKey: ["backups", "s3-config"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/backups/s3-config");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load S3 settings"));
      setHasSecret(data.has_secret);
      form.reset({
        endpoint: data.endpoint,
        bucket: data.bucket,
        region: data.region,
        prefix: data.prefix,
        access_key: data.access_key,
        secret_key: "",
      });
      return data;
    },
    enabled: open,
  });

  const save = useMutation({
    mutationFn: async (values: S3ConfigForm) => {
      const { data, error } = await api.PUT("/api/backups/s3-config", {
        body: {
          endpoint: values.endpoint,
          bucket: values.bucket,
          region: values.region,
          prefix: values.prefix,
          access_key: values.access_key,
          secret_key: values.secret_key || null, // blank keeps the stored secret
        },
      });
      if (error || !data) {
        throw new Error(apiErrorMessage(error, "Could not save the S3 configuration"));
      }
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["backups"] });
      toast.success("S3 configuration verified and saved");
      onClose();
    },
    onError: (err) => form.setError("root", { message: err.message }),
  });

  const disable = useMutation({
    mutationFn: async () => {
      const { error } = await api.DELETE("/api/backups/s3-config");
      if (error) throw new Error(apiErrorMessage(error, "Could not remove the S3 configuration"));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["backups"] });
      toast.success("S3 configuration removed");
      onClose();
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <Dialog open={open} onClose={onClose}>
      <DialogContent>
        <DialogTitle>S3 backup target</DialogTitle>
        <DialogDescription>
          Works with Amazon S3 and any compatible service (MinIO, Backblaze B2, R2…). The
          credentials are verified against the bucket before saving; the secret key is stored
          encrypted.
        </DialogDescription>
        <form
          className="flex flex-col gap-3"
          onSubmit={form.handleSubmit((values) => save.mutate(values))}
        >
          <FormField label="Endpoint URL" htmlFor="s3-endpoint">
            <Input
              id="s3-endpoint"
              placeholder="https://s3.eu-central-1.amazonaws.com"
              autoComplete="off"
              {...form.register("endpoint", { required: true })}
            />
          </FormField>
          <div className="grid grid-cols-2 gap-3">
            <FormField label="Bucket" htmlFor="s3-bucket">
              <Input
                id="s3-bucket"
                placeholder="my-backups"
                autoComplete="off"
                {...form.register("bucket", { required: true })}
              />
            </FormField>
            <FormField label="Region (optional)" htmlFor="s3-region">
              <Input
                id="s3-region"
                placeholder="eu-central-1"
                autoComplete="off"
                {...form.register("region")}
              />
            </FormField>
          </div>
          <FormField label="Access key ID" htmlFor="s3-access-key">
            <Input
              id="s3-access-key"
              autoComplete="off"
              {...form.register("access_key", { required: true })}
            />
          </FormField>
          <FormField label="Secret access key" htmlFor="s3-secret-key">
            <Input
              id="s3-secret-key"
              type="password"
              autoComplete="new-password"
              placeholder={hasSecret ? "(unchanged — leave blank to keep)" : ""}
              {...form.register("secret_key")}
            />
          </FormField>
          <FormField label="Key prefix" htmlFor="s3-prefix">
            <Input id="s3-prefix" autoComplete="off" {...form.register("prefix")} />
          </FormField>
          {form.formState.errors.root && (
            <p className="text-sm text-destructive" role="alert">
              {form.formState.errors.root.message}
            </p>
          )}
          <DialogActions>
            {hasSecret && (
              <Button
                type="button"
                variant="destructive"
                className="mr-auto"
                loading={disable.isPending}
                onClick={() => disable.mutate()}
              >
                Remove
              </Button>
            )}
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" loading={save.isPending}>
              Verify &amp; save
            </Button>
          </DialogActions>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// --- schedule + scope editor -----------------------------------------------------------

interface ScheduleForm {
  enabled: boolean;
  frequency: "daily" | "weekly";
  hour: number;
  retention: number;
  include_files: boolean;
  include_databases: boolean;
  s3_mirror: boolean;
}

export function ScheduleDialog({
  site,
  s3Enabled,
  onClose,
}: {
  site: { id: number; domain: string } | null;
  s3Enabled: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const form = useForm<ScheduleForm>({
    defaultValues: {
      enabled: false,
      frequency: "daily",
      hour: 3,
      retention: 7,
      include_files: true,
      include_databases: true,
      s3_mirror: true,
    },
  });

  useQuery({
    queryKey: ["backups", "schedule", site?.id],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/sites/{site_id}/backup-schedule", {
        params: { path: { site_id: site?.id ?? 0 } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load the schedule"));
      form.reset({
        enabled: data.enabled,
        frequency: data.frequency,
        hour: data.hour,
        retention: data.retention,
        include_files: data.include_files,
        include_databases: data.include_databases,
        s3_mirror: data.s3_mirror,
      });
      return data;
    },
    enabled: site !== null,
  });

  const save = useMutation({
    mutationFn: async (values: ScheduleForm) => {
      if (!values.include_files && !values.include_databases) {
        throw new Error("A backup must include files, databases, or both");
      }
      const { error } = await api.PUT("/api/sites/{site_id}/backup-schedule", {
        params: { path: { site_id: site?.id ?? 0 } },
        body: {
          enabled: values.enabled,
          frequency: values.frequency,
          hour: Number(values.hour),
          retention: Number(values.retention),
          include_files: values.include_files,
          include_databases: values.include_databases,
          s3_mirror: values.s3_mirror,
        },
      });
      if (error) throw new Error(apiErrorMessage(error, "Could not save the schedule"));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["backups"] });
      toast.success(`Backup settings for ${site?.domain} saved`);
      onClose();
    },
    onError: (err) => form.setError("root", { message: err.message }),
  });

  return (
    <Dialog open={site !== null} onClose={onClose}>
      <DialogContent>
        <DialogTitle>Backup settings — {site?.domain}</DialogTitle>
        <DialogDescription>
          Applies to scheduled and manual backups. The most recent N copies are kept locally.
        </DialogDescription>
        <form
          className="flex flex-col gap-3"
          onSubmit={form.handleSubmit((values) => save.mutate(values))}
        >
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-sm font-medium">What to back up</legend>
            <label className="flex items-center gap-2 text-sm" htmlFor="scope-files">
              <input id="scope-files" type="checkbox" {...form.register("include_files")} />
              Site files
            </label>
            <label className="flex items-center gap-2 text-sm" htmlFor="scope-databases">
              <input id="scope-databases" type="checkbox" {...form.register("include_databases")} />
              Databases
            </label>
            <label className="flex items-center gap-2 text-sm" htmlFor="scope-s3">
              <input
                id="scope-s3"
                type="checkbox"
                disabled={!s3Enabled}
                {...form.register("s3_mirror")}
              />
              Mirror to S3 {s3Enabled ? "" : "(configure an S3 target first)"}
            </label>
          </fieldset>
          <label className="flex items-center gap-2 text-sm" htmlFor="sched-enabled">
            <input id="sched-enabled" type="checkbox" {...form.register("enabled")} />
            Enable scheduled backups
          </label>
          <div className="grid grid-cols-3 gap-3">
            <FormField label="Frequency" htmlFor="sched-frequency">
              <select
                id="sched-frequency"
                className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm"
                {...form.register("frequency")}
              >
                <option value="daily">Daily</option>
                <option value="weekly">Weekly (Mon)</option>
              </select>
            </FormField>
            <FormField label="At hour (UTC)" htmlFor="sched-hour">
              <Input
                id="sched-hour"
                type="number"
                min={0}
                max={23}
                {...form.register("hour", { min: 0, max: 23 })}
              />
            </FormField>
            <FormField label="Keep last" htmlFor="sched-retention">
              <Input
                id="sched-retention"
                type="number"
                min={1}
                max={60}
                {...form.register("retention", { min: 1, max: 60 })}
              />
            </FormField>
          </div>
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
              Save settings
            </Button>
          </DialogActions>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// --- restore wizard --------------------------------------------------------------------

const SCOPES = [
  { value: "full", label: "Everything (files + databases)" },
  { value: "files", label: "Files only" },
  { value: "db", label: "Databases only" },
] as const;

export function RestoreDialog({
  target,
  siteId,
  onStarted,
  onClose,
}: {
  target: BackupEntry | null;
  siteId: number | null;
  onStarted: (operationId: number) => void;
  onClose: () => void;
}) {
  const [scope, setScope] = useState<"full" | "files" | "db">("full");
  const [confirm, setConfirm] = useState("");

  const restore = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/sites/{site_id}/backups/{backup_id}/restore", {
        params: {
          path: { site_id: siteId ?? 0, backup_id: target?.backup_id ?? "" },
        },
        body: { scope, confirm_domain: confirm },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Could not start the restore"));
      return data;
    },
    onSuccess: (data) => {
      setConfirm("");
      onClose();
      onStarted(data.operation_id);
    },
    onError: (err) => toast.error(err.message),
  });

  const matches = confirm.trim().toLowerCase() === target?.domain;

  return (
    <Dialog open={target !== null} onClose={onClose}>
      <DialogContent>
        <DialogTitle>Restore {target?.domain}</DialogTitle>
        <DialogDescription>
          Restores backup <span className="font-mono">{target?.backup_id}</span> over the current
          site. This overwrites data and cannot be undone.
        </DialogDescription>
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1 text-sm font-medium">What to restore</legend>
          {SCOPES.map((option) => (
            <label key={option.value} className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name="restore-scope"
                value={option.value}
                checked={scope === option.value}
                onChange={() => setScope(option.value)}
              />
              {option.label}
            </label>
          ))}
        </fieldset>
        <FormField
          label={`Type the domain (${target?.domain}) to confirm`}
          htmlFor="restore-confirm"
        >
          <Input
            id="restore-confirm"
            autoComplete="off"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            placeholder={target?.domain}
          />
        </FormField>
        <DialogActions>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={!matches}
            loading={restore.isPending}
            onClick={() => restore.mutate()}
          >
            Restore backup
          </Button>
        </DialogActions>
      </DialogContent>
    </Dialog>
  );
}

// --- delete --------------------------------------------------------------------------

export function DeleteBackupDialog({
  target,
  siteId,
  onClose,
}: {
  target: BackupEntry | null;
  siteId: number | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [confirm, setConfirm] = useState("");

  const destroy = useMutation({
    mutationFn: async () => {
      const { error } = await api.DELETE("/api/sites/{site_id}/backups/{backup_id}", {
        params: { path: { site_id: siteId ?? 0, backup_id: target?.backup_id ?? "" } },
        body: { confirm_id: confirm },
      });
      if (error) throw new Error(apiErrorMessage(error, "Could not delete the backup"));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["backups"] });
      toast.success("Backup deleted");
      setConfirm("");
      onClose();
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <Dialog open={target !== null} onClose={onClose}>
      <DialogContent>
        <DialogTitle>Delete backup</DialogTitle>
        <DialogDescription>
          Deletes <span className="font-mono">{target?.backup_id}</span> of {target?.domain} from
          local storage{target?.s3 ? " (the S3 copy is kept)" : ""}. Type the backup id to confirm.
        </DialogDescription>
        <FormField label="Backup id" htmlFor="delete-backup-confirm">
          <Input
            id="delete-backup-confirm"
            autoComplete="off"
            className="font-mono"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            placeholder={target?.backup_id}
          />
        </FormField>
        <DialogActions>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={confirm.trim() !== target?.backup_id}
            loading={destroy.isPending}
            onClick={() => destroy.mutate()}
          >
            Delete backup
          </Button>
        </DialogActions>
      </DialogContent>
    </Dialog>
  );
}
