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
/**
 * Database dialogs shared by the global page and the site tab:
 * create (per site), show-once credentials, delete with type-to-confirm.
 */
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Copy } from "lucide-react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

export type Credentials = components["schemas"]["CredentialsResponse"];

const createSchema = z.object({
  name: z
    .string()
    .min(2, "At least 2 characters")
    .max(64)
    .regex(/^[a-z][a-z0-9_]{1,63}$/, "Lowercase letters, digits and _ — must start with a letter"),
});

type CreateValues = z.infer<typeof createSchema>;

export function CreateDatabaseDialog({
  siteId,
  open,
  onClose,
  onCreated,
}: {
  siteId: number;
  open: boolean;
  onClose: () => void;
  onCreated: (creds: Credentials) => void;
}) {
  const queryClient = useQueryClient();
  const form = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: { name: "" },
  });

  const onSubmit = form.handleSubmit(async (values) => {
    const { data, error, response } = await api.POST("/api/databases/sites/{site_id}", {
      params: { path: { site_id: siteId } },
      body: { name: values.name },
    });
    if (error || !data) {
      form.setError("name", {
        type: "server",
        message: apiErrorMessage(error, `Create failed (${response.status})`),
      });
      return;
    }
    await queryClient.invalidateQueries({ queryKey: ["databases"] });
    form.reset();
    onClose();
    onCreated(data);
  });

  return (
    <Dialog open={open} onClose={onClose}>
      <DialogContent>
        <DialogTitle>New database</DialogTitle>
        <DialogDescription>
          Creates a database and a same-named user with privileges on it only.
        </DialogDescription>
        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <FormField
            label="Database name"
            htmlFor="db-name"
            error={form.formState.errors.name?.message}
          >
            <Input
              id="db-name"
              placeholder="shop_db"
              autoComplete="off"
              spellCheck={false}
              {...form.register("name")}
            />
          </FormField>
          <DialogActions>
            <Button variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" loading={form.formState.isSubmitting}>
              Create database
            </Button>
          </DialogActions>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function CredentialsDialog({
  creds,
  onClose,
}: {
  creds: Credentials | null;
  onClose: () => void;
}) {
  const copy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      toast.success("Copied");
    } catch {
      toast.error("Copy failed — select the text manually");
    }
  };

  return (
    <Dialog open={creds !== null} onClose={onClose}>
      <DialogContent>
        <DialogTitle>Database credentials</DialogTitle>
        <DialogDescription>
          Save the password now — it is shown only this once and the panel stores just a hash.
        </DialogDescription>
        {creds && (
          <dl className="space-y-2 text-sm">
            {(
              [
                ["Database", creds.database.name],
                ["User", creds.database.db_user],
                ["Password", creds.password],
                ["Host", "localhost"],
              ] as const
            ).map(([label, value]) => (
              <div key={label} className="flex items-center justify-between gap-2">
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="flex items-center gap-1 font-mono text-xs">
                  <span className="break-all">{value}</span>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={`Copy ${label}`}
                    onClick={() => copy(value)}
                  >
                    <Copy className="h-3.5 w-3.5" />
                  </Button>
                </dd>
              </div>
            ))}
          </dl>
        )}
        <DialogActions>
          <Button onClick={onClose}>I saved it</Button>
        </DialogActions>
      </DialogContent>
    </Dialog>
  );
}

export function DeleteDatabaseDialog({
  target,
  onClose,
}: {
  target: { id: number; name: string } | null;
  onClose: () => void;
}) {
  const [confirm, setConfirm] = useState("");
  const queryClient = useQueryClient();

  const del = useMutation({
    mutationFn: async () => {
      if (!target) return;
      const { error, response } = await api.DELETE("/api/databases/{database_id}", {
        params: { path: { database_id: target.id } },
        body: { confirm_name: confirm },
      });
      if (error) throw new Error(apiErrorMessage(error, `Delete failed (${response.status})`));
    },
    onSuccess: async () => {
      toast.success(`${target?.name} deleted`);
      await queryClient.invalidateQueries({ queryKey: ["databases"] });
      setConfirm("");
      onClose();
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <Dialog open={target !== null} onClose={onClose}>
      <DialogContent>
        <DialogTitle>Delete {target?.name}?</DialogTitle>
        <DialogDescription>
          This permanently drops the database and its user. Type the database name to confirm.
        </DialogDescription>
        <Input
          aria-label="Type the database name to confirm"
          placeholder={target?.name}
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          autoComplete="off"
          spellCheck={false}
        />
        <DialogActions>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={confirm.trim() !== target?.name}
            loading={del.isPending}
            onClick={() => del.mutate()}
          >
            Delete database
          </Button>
        </DialogActions>
      </DialogContent>
    </Dialog>
  );
}
