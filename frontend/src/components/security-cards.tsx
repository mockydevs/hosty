import { FormField } from "@/components/form-field";
import { ErrorState, LoadingState } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogActions,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { api, apiErrorMessage } from "@/lib/api/client";
import { useAuth } from "@/lib/auth";
/**
 * Account security (Phase 11d): TOTP 2FA enrollment/disable and the active
 * refresh-token sessions list with revoke / revoke-others.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, ShieldCheck, ShieldOff } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

// --- 2FA -------------------------------------------------------------------------

function EnrollDialog({
  open,
  onClose,
  onEnabled,
}: {
  open: boolean;
  onClose: () => void;
  onEnabled: () => void;
}) {
  const [secret, setSecret] = useState<string | null>(null);
  const [uri, setUri] = useState<string>("");
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);

  const setup = useMutation({
    mutationFn: async () => {
      const { data, error: apiError } = await api.POST("/api/auth/2fa/setup");
      if (apiError || !data) {
        throw new Error(apiErrorMessage(apiError, "Could not start 2FA setup"));
      }
      return data;
    },
    onSuccess: (data) => {
      setSecret(data.secret);
      setUri(data.otpauth_uri);
    },
    onError: (err) => setError(err.message),
  });

  const enable = useMutation({
    mutationFn: async () => {
      const { error: apiError, response } = await api.POST("/api/auth/2fa/enable", {
        body: { code: code.replaceAll(" ", "") },
      });
      if (apiError) {
        throw new Error(apiErrorMessage(apiError, `Verification failed (${response.status})`));
      }
    },
    onSuccess: () => {
      toast.success("Two-factor authentication enabled");
      onEnabled();
      onClose();
    },
    onError: (err) => setError(err.message),
  });

  // Generate the secret the moment the dialog opens.
  const setupMutate = setup.mutate;
  const setupIdle = setup.isIdle;
  useEffect(() => {
    if (open && !secret && setupIdle) setupMutate();
  }, [open, secret, setupIdle, setupMutate]);

  return (
    <Dialog open={open} onClose={onClose}>
      <DialogContent>
        <DialogTitle>Set up two-factor authentication</DialogTitle>
        <DialogDescription>
          Add this secret to your authenticator app (Google Authenticator, Aegis, 1Password…), then
          confirm with the 6-digit code it shows.
        </DialogDescription>
        {setup.isPending || !secret ? (
          <LoadingState label="Generating secret…" />
        ) : (
          <div className="space-y-4">
            <div className="flex items-center gap-2">
              <code
                className="flex-1 break-all rounded-md bg-muted px-3 py-2 text-sm"
                data-testid="totp-secret"
              >
                {secret}
              </code>
              <Button
                variant="outline"
                size="icon"
                aria-label="Copy secret"
                onClick={async () => {
                  await navigator.clipboard.writeText(secret);
                  toast.success("Secret copied");
                }}
              >
                <Copy className="h-4 w-4" />
              </Button>
            </div>
            <p className="break-all text-xs text-muted-foreground">{uri}</p>
            <form
              className="space-y-4"
              onSubmit={(e) => {
                e.preventDefault();
                enable.mutate();
              }}
              noValidate
            >
              <FormField
                label="Code from your app"
                htmlFor="enroll-code"
                error={error ?? undefined}
              >
                <Input
                  id="enroll-code"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  placeholder="123 456"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                />
              </FormField>
              <DialogActions>
                <Button variant="outline" onClick={onClose}>
                  Cancel
                </Button>
                <Button
                  type="submit"
                  disabled={code.replaceAll(" ", "").length < 6}
                  loading={enable.isPending}
                >
                  Enable 2FA
                </Button>
              </DialogActions>
            </form>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function DisableDialog({
  open,
  onClose,
  onDisabled,
}: {
  open: boolean;
  onClose: () => void;
  onDisabled: () => void;
}) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  const disable = useMutation({
    mutationFn: async () => {
      const { error: apiError, response } = await api.POST("/api/auth/2fa/disable", {
        body: { password },
      });
      if (apiError) {
        throw new Error(apiErrorMessage(apiError, `Request failed (${response.status})`));
      }
    },
    onSuccess: () => {
      toast.success("Two-factor authentication disabled");
      setPassword("");
      onDisabled();
      onClose();
    },
    onError: (err) => setError(err.message),
  });

  return (
    <Dialog open={open} onClose={onClose}>
      <DialogContent>
        <DialogTitle>Disable two-factor authentication?</DialogTitle>
        <DialogDescription>
          Your account falls back to password-only login. Confirm with your password.
        </DialogDescription>
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            disable.mutate();
          }}
          noValidate
        >
          <FormField label="Password" htmlFor="disable-password" error={error ?? undefined}>
            <Input
              id="disable-password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </FormField>
          <DialogActions>
            <Button variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              variant="destructive"
              disabled={password.length === 0}
              loading={disable.isPending}
            >
              Disable 2FA
            </Button>
          </DialogActions>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function TwoFactorCard() {
  const { user, reload } = useAuth();
  const [enrollOpen, setEnrollOpen] = useState(false);
  const [disableOpen, setDisableOpen] = useState(false);
  const enabled = user?.totp_enabled ?? false;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          {enabled ? (
            <ShieldCheck className="h-4 w-4 text-green-600" aria-hidden />
          ) : (
            <ShieldOff className="h-4 w-4 text-muted-foreground" aria-hidden />
          )}
          Two-factor authentication
          {enabled ? <Badge variant="success">On</Badge> : <Badge variant="outline">Off</Badge>}
        </CardTitle>
        <CardDescription>
          {enabled
            ? "Logging in requires a code from your authenticator app."
            : "Protect your account with one-time codes from an authenticator app."}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {enabled ? (
          <Button variant="outline" onClick={() => setDisableOpen(true)}>
            Disable 2FA
          </Button>
        ) : (
          <Button onClick={() => setEnrollOpen(true)}>Set up 2FA</Button>
        )}
        <EnrollDialog
          open={enrollOpen}
          onClose={() => setEnrollOpen(false)}
          onEnabled={() => void reload()}
        />
        <DisableDialog
          open={disableOpen}
          onClose={() => setDisableOpen(false)}
          onDisabled={() => void reload()}
        />
      </CardContent>
    </Card>
  );
}

// --- active sessions ---------------------------------------------------------------

export function SessionsCard() {
  const queryClient = useQueryClient();
  const sessions = useQuery({
    queryKey: ["auth", "sessions"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/auth/sessions");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load sessions"));
      return data;
    },
  });

  const revoke = useMutation({
    mutationFn: async (sessionId: number) => {
      const { error, response } = await api.DELETE("/api/auth/sessions/{session_id}", {
        params: { path: { session_id: sessionId } },
      });
      if (error) throw new Error(apiErrorMessage(error, `Revoke failed (${response.status})`));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["auth", "sessions"] });
      toast.success("Session revoked");
    },
    onError: (err) => toast.error(err.message),
  });

  const revokeOthers = useMutation({
    mutationFn: async () => {
      const { error } = await api.POST("/api/auth/sessions/revoke-others");
      if (error) throw new Error(apiErrorMessage(error, "Could not revoke other sessions"));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["auth", "sessions"] });
      toast.success("Other sessions revoked");
    },
    onError: (err) => toast.error(err.message),
  });

  const rows = sessions.data ?? [];

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Active sessions</CardTitle>
        <CardDescription>
          Browsers holding a valid refresh token for your account. Revoke anything you don't
          recognize.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {sessions.isPending ? (
          <LoadingState label="Loading sessions…" />
        ) : sessions.isError ? (
          <ErrorState message={sessions.error.message} onRetry={() => sessions.refetch()} />
        ) : (
          <>
            <ul className="space-y-2">
              {rows.map((s) => (
                <li
                  key={s.id}
                  className="flex items-center justify-between rounded-md border border-border px-3 py-2 text-sm"
                >
                  <span>
                    <span className="font-medium">
                      Signed in {new Date(s.created_at).toLocaleString()}
                    </span>
                    <span className="block text-xs text-muted-foreground">
                      Expires {new Date(s.expires_at).toLocaleString()}
                    </span>
                  </span>
                  {s.current ? (
                    <Badge variant="secondary">This session</Badge>
                  ) : (
                    <Button
                      variant="outline"
                      size="sm"
                      loading={revoke.isPending}
                      onClick={() => revoke.mutate(s.id)}
                    >
                      Revoke
                    </Button>
                  )}
                </li>
              ))}
            </ul>
            {rows.length > 1 && (
              <Button
                variant="outline"
                size="sm"
                loading={revokeOthers.isPending}
                onClick={() => revokeOthers.mutate()}
              >
                Revoke all other sessions
              </Button>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
