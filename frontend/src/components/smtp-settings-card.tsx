import { FormField } from "@/components/form-field";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api, apiErrorMessage } from "@/lib/api/client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { toast } from "sonner";

function recipientsToText(values: string[] | undefined): string {
  return (values ?? []).join("\n");
}

function textToRecipients(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export function SMTPSettingsCard() {
  const queryClient = useQueryClient();
  const [host, setHost] = useState("");
  const [port, setPort] = useState("587");
  const [security, setSecurity] = useState<"starttls" | "ssl" | "none">("starttls");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [fromEmail, setFromEmail] = useState("");
  const [fromName, setFromName] = useState("Hosty");
  const [recipients, setRecipients] = useState("");
  const [testTo, setTestTo] = useState("");
  const [error, setError] = useState<string | null>(null);

  const config = useQuery({
    queryKey: ["notifications", "smtp"],
    queryFn: async () => {
      const { data, error: apiError } = await api.GET("/api/notifications/smtp");
      if (apiError || !data) throw new Error(apiErrorMessage(apiError, "Failed to load SMTP"));
      return data;
    },
  });

  useEffect(() => {
    if (!config.data?.configured) return;
    setHost(config.data.host);
    setPort(String(config.data.port));
    setSecurity(config.data.security);
    setUsername(config.data.username);
    setFromEmail(config.data.from_email);
    setFromName(config.data.from_name);
    setRecipients(recipientsToText(config.data.notification_recipients));
    if (!testTo) setTestTo(config.data.from_email);
  }, [config.data, testTo]);

  const save = useMutation({
    mutationFn: async () => {
      const { error: apiError, response } = await api.PUT("/api/notifications/smtp", {
        body: {
          host: host.trim(),
          port: Number(port),
          security,
          username: username.trim(),
          password: password === "" ? null : password,
          from_email: fromEmail.trim(),
          from_name: fromName.trim(),
          notification_recipients: textToRecipients(recipients),
        },
      });
      if (apiError) throw new Error(apiErrorMessage(apiError, `Save failed (${response.status})`));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["notifications", "smtp"] });
      setPassword("");
      setError(null);
      toast.success("SMTP settings saved");
    },
    onError: (err) => setError(err.message),
  });

  const sendTest = useMutation({
    mutationFn: async () => {
      const { error: apiError, response } = await api.POST("/api/notifications/smtp/test", {
        body: { to: testTo.trim() },
      });
      if (apiError) throw new Error(apiErrorMessage(apiError, `Test failed (${response.status})`));
    },
    onSuccess: () => toast.success("Test email sent"),
    onError: (err) => toast.error(err.message),
  });

  const remove = useMutation({
    mutationFn: async () => {
      const { error: apiError } = await api.DELETE("/api/notifications/smtp");
      if (apiError) throw new Error(apiErrorMessage(apiError, "Failed to remove SMTP settings"));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["notifications", "smtp"] });
      setHost("");
      setPort("587");
      setSecurity("starttls");
      setUsername("");
      setPassword("");
      setFromEmail("");
      setFromName("Hosty");
      setRecipients("");
      setTestTo("");
      setError(null);
      toast.success("SMTP settings removed");
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">SMTP email</CardTitle>
        <CardDescription>
          Sends temporary passwords to users with email addresses and forwards panel notifications
          to the recipients below.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
          noValidate
        >
          <div className="grid grid-cols-[1fr_96px] gap-3">
            <FormField label="SMTP host" htmlFor="smtp-host" error={error ?? undefined}>
              <Input
                id="smtp-host"
                placeholder="smtp.example.com"
                value={host}
                onChange={(e) => setHost(e.target.value)}
              />
            </FormField>
            <FormField label="Port" htmlFor="smtp-port">
              <Input
                id="smtp-port"
                type="number"
                min={1}
                max={65535}
                value={port}
                onChange={(e) => setPort(e.target.value)}
              />
            </FormField>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <FormField label="From email" htmlFor="smtp-from-email">
              <Input
                id="smtp-from-email"
                type="email"
                placeholder="panel@example.com"
                value={fromEmail}
                onChange={(e) => setFromEmail(e.target.value)}
              />
            </FormField>
            <FormField label="From name" htmlFor="smtp-from-name">
              <Input
                id="smtp-from-name"
                value={fromName}
                onChange={(e) => setFromName(e.target.value)}
              />
            </FormField>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <FormField label="Username" htmlFor="smtp-username">
              <Input
                id="smtp-username"
                autoComplete="off"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </FormField>
            <FormField
              label={config.data?.has_password ? "Password (stored)" : "Password"}
              htmlFor="smtp-password"
            >
              <Input
                id="smtp-password"
                type="password"
                autoComplete="new-password"
                placeholder={config.data?.has_password ? "Leave blank to keep current" : ""}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </FormField>
          </div>
          <FormField label="Security" htmlFor="smtp-security">
            <select
              id="smtp-security"
              className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
              value={security}
              onChange={(e) => setSecurity(e.target.value as typeof security)}
            >
              <option value="starttls">STARTTLS</option>
              <option value="ssl">SSL/TLS</option>
              <option value="none">None</option>
            </select>
          </FormField>
          <FormField label="Notification recipients" htmlFor="smtp-recipients">
            <textarea
              id="smtp-recipients"
              className="min-h-20 w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm"
              placeholder="admin@example.com"
              value={recipients}
              onChange={(e) => setRecipients(e.target.value)}
            />
          </FormField>
          <div className="flex flex-wrap gap-2">
            <Button
              type="submit"
              disabled={host.trim() === "" || fromEmail.trim() === ""}
              loading={save.isPending}
            >
              Save SMTP
            </Button>
            {config.data?.configured && (
              <Button
                type="button"
                variant="outline"
                loading={remove.isPending}
                onClick={() => remove.mutate()}
              >
                Remove
              </Button>
            )}
          </div>
        </form>
        {config.data?.configured && (
          <div className="mt-4 flex gap-2">
            <Input
              type="email"
              placeholder="test@example.com"
              value={testTo}
              onChange={(e) => setTestTo(e.target.value)}
            />
            <Button
              type="button"
              variant="outline"
              disabled={testTo.trim() === ""}
              loading={sendTest.isPending}
              onClick={() => sendTest.mutate()}
            >
              Send test
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
