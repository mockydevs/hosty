import { FormField } from "@/components/form-field";
import { LoadingState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api/client";
import { ApiError, useAuth } from "@/lib/auth";
/**
 * Login page. On first boot (no users yet) it switches to the setup form
 * that creates the admin account, then logs straight in.
 */
import { zodResolver } from "@hookform/resolvers/zod";
import { useQuery } from "@tanstack/react-query";
import { Server } from "lucide-react";
import { useForm } from "react-hook-form";
import { Navigate, useLocation, useNavigate } from "react-router";
import { toast } from "sonner";
import { z } from "zod";

const loginSchema = z.object({
  username: z.string().min(1, "Username is required"),
  password: z.string().min(1, "Password is required"),
});

const setupSchema = z
  .object({
    username: z
      .string()
      .min(3, "At least 3 characters")
      .max(32, "At most 32 characters")
      .regex(/^[a-zA-Z][a-zA-Z0-9_.-]*$/, "Start with a letter; letters, digits, _ . - only"),
    password: z.string().min(12, "At least 12 characters").max(128, "At most 128 characters"),
    confirm: z.string(),
  })
  .refine((v) => v.password === v.confirm, {
    message: "Passwords do not match",
    path: ["confirm"],
  });

type LoginValues = z.infer<typeof loginSchema>;
type SetupValues = z.infer<typeof setupSchema>;

function LoginForm() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from ?? "/";

  const form = useForm<LoginValues>({
    resolver: zodResolver(loginSchema),
    defaultValues: { username: "", password: "" },
  });

  const onSubmit = form.handleSubmit(async (values) => {
    try {
      await login(values.username, values.password);
      navigate(from, { replace: true });
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Login failed";
      form.setError("password", { type: "server", message });
    }
  });

  return (
    <form onSubmit={onSubmit} className="space-y-4" noValidate>
      <FormField
        label="Username"
        htmlFor="username"
        error={form.formState.errors.username?.message}
      >
        <Input id="username" autoComplete="username" autoFocus {...form.register("username")} />
      </FormField>
      <FormField
        label="Password"
        htmlFor="password"
        error={form.formState.errors.password?.message}
      >
        <Input
          id="password"
          type="password"
          autoComplete="current-password"
          {...form.register("password")}
        />
      </FormField>
      <Button type="submit" className="w-full" loading={form.formState.isSubmitting}>
        Log in
      </Button>
    </form>
  );
}

function SetupForm({ onDone }: { onDone: () => void }) {
  const { login } = useAuth();
  const navigate = useNavigate();

  const form = useForm<SetupValues>({
    resolver: zodResolver(setupSchema),
    defaultValues: { username: "", password: "", confirm: "" },
  });

  const onSubmit = form.handleSubmit(async (values) => {
    const { error, response } = await api.POST("/api/auth/setup", {
      body: { username: values.username, password: values.password },
    });
    if (error) {
      form.setError("username", {
        type: "server",
        message: new ApiError(error, response.status).message,
      });
      return;
    }
    toast.success("Admin account created");
    onDone();
    try {
      await login(values.username, values.password);
      navigate("/", { replace: true });
    } catch {
      // Account exists; let them log in manually.
    }
  });

  return (
    <form onSubmit={onSubmit} className="space-y-4" noValidate>
      <FormField
        label="Admin username"
        htmlFor="username"
        error={form.formState.errors.username?.message}
      >
        <Input id="username" autoComplete="username" autoFocus {...form.register("username")} />
      </FormField>
      <FormField
        label="Password"
        htmlFor="password"
        error={form.formState.errors.password?.message}
      >
        <Input
          id="password"
          type="password"
          autoComplete="new-password"
          {...form.register("password")}
        />
      </FormField>
      <FormField
        label="Confirm password"
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
      <Button type="submit" className="w-full" loading={form.formState.isSubmitting}>
        Create admin account
      </Button>
    </form>
  );
}

export function LoginPage() {
  const { status } = useAuth();
  const setup = useQuery({
    queryKey: ["auth", "setup"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/auth/setup");
      if (error || !data) throw new Error("Failed to check setup status");
      return data;
    },
    staleTime: Number.POSITIVE_INFINITY,
    retry: 1,
  });

  if (status === "authenticated") return <Navigate to="/" replace />;
  if (status === "loading" || setup.isPending) return <LoadingState full />;

  const setupRequired = setup.data?.setup_required ?? false;

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="items-center text-center">
          <div className="flex items-center gap-2">
            <Server className="h-6 w-6" aria-hidden />
            <CardTitle className="text-xl">HostyPanel</CardTitle>
          </div>
          <CardDescription>
            {setupRequired
              ? "Welcome! Create the admin account to get started."
              : "Sign in to your hosting panel"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {setupRequired ? <SetupForm onDone={() => setup.refetch()} /> : <LoginForm />}
        </CardContent>
      </Card>
    </div>
  );
}
