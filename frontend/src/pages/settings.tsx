import { CloudflareSettingsCard } from "@/components/cloudflare-settings-card";
import { FormField } from "@/components/form-field";
import { PanelDomainCard } from "@/components/panel-domain-card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api/client";
import { ApiError, useAuth } from "@/lib/auth";
/**
 * Settings: server-level configuration (panel domain/HTTPS, Cloudflare).
 * ChangePasswordForm is defined here but rendered on the Users page.
 */
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

const changePasswordSchema = z
  .object({
    current_password: z.string().min(1, "Current password is required"),
    new_password: z.string().min(12, "At least 12 characters").max(128, "At most 128 characters"),
    confirm: z.string(),
  })
  .refine((v) => v.new_password === v.confirm, {
    message: "Passwords do not match",
    path: ["confirm"],
  });

type ChangePasswordValues = z.infer<typeof changePasswordSchema>;

export function ChangePasswordForm({ onChanged }: { onChanged?: () => void }) {
  const form = useForm<ChangePasswordValues>({
    resolver: zodResolver(changePasswordSchema),
    defaultValues: { current_password: "", new_password: "", confirm: "" },
  });

  const onSubmit = form.handleSubmit(async (values) => {
    const { error, response } = await api.POST("/api/auth/change-password", {
      body: { current_password: values.current_password, new_password: values.new_password },
    });
    if (error) {
      form.setError("current_password", {
        type: "server",
        message: new ApiError(error, response.status).message,
      });
      return;
    }
    toast.success("Password changed — please log in again");
    onChanged?.();
  });

  return (
    <form onSubmit={onSubmit} className="space-y-4" noValidate>
      <FormField
        label="Current password"
        htmlFor="current_password"
        error={form.formState.errors.current_password?.message}
      >
        <Input
          id="current_password"
          type="password"
          autoComplete="current-password"
          {...form.register("current_password")}
        />
      </FormField>
      <FormField
        label="New password"
        htmlFor="new_password"
        error={form.formState.errors.new_password?.message}
      >
        <Input
          id="new_password"
          type="password"
          autoComplete="new-password"
          {...form.register("new_password")}
        />
      </FormField>
      <FormField
        label="Confirm new password"
        htmlFor="confirm"
        error={form.formState.errors.confirm?.message}
      >
        <Input
          id="confirm"
          type="password"
          autoComplete="new-password"
          {...form.register("confirm")}
        />
      </FormField>
      <Button type="submit" loading={form.formState.isSubmitting}>
        Change password
      </Button>
    </form>
  );
}

export function SettingsPage() {
  const { user } = useAuth();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Signed in as <span className="font-medium text-foreground">{user?.username}</span> (
          {user?.role})
        </p>
      </div>

      <div className="grid items-start gap-6 lg:grid-cols-2">
        <PanelDomainCard />

        <CloudflareSettingsCard />
      </div>
    </div>
  );
}
