import { FormField } from "@/components/form-field";
import { PlansSection } from "@/components/plans-section";
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
import { useAuth } from "@/lib/auth";
/**
 * Users (Phase 11a, admin-only): create client accounts with a temporary
 * password (shown once), suspend/unsuspend, set quotas, reset passwords and
 * delete accounts (reassign their sites or tear them down).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, KeyRound, Plus, Trash2, UserRound, VenetianMask } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";

type AdminUser = components["schemas"]["UserAdminResponse"];
type CreatedUser = components["schemas"]["CreatedUserResponse"];

function quotaLabel(value: number | null | undefined): string {
  return value === null || value === undefined ? "Unlimited" : String(value);
}

function TempPasswordDialog({
  created,
  onClose,
}: {
  created: CreatedUser | null;
  onClose: () => void;
}) {
  return (
    <Dialog open={created !== null} onClose={onClose}>
      <DialogContent>
        <DialogTitle>Temporary password for {created?.user.username}</DialogTitle>
        <DialogDescription>
          {created?.email_sent
            ? `The temporary password was emailed to ${created.user.email}.`
            : "Share it over a secure channel. It is shown only once and must be changed on first login."}
        </DialogDescription>
        {created?.temp_password && (
          <div className="flex items-center gap-2">
            <code className="flex-1 break-all rounded-md bg-muted px-3 py-2 text-sm">
              {created.temp_password}
            </code>
            <Button
              variant="outline"
              size="icon"
              aria-label="Copy temporary password"
              onClick={async () => {
                await navigator.clipboard.writeText(created.temp_password ?? "");
                toast.success("Copied to clipboard");
              }}
            >
              <Copy className="h-4 w-4" />
            </Button>
          </div>
        )}
        <DialogActions>
          <Button onClick={onClose}>Done</Button>
        </DialogActions>
      </DialogContent>
    </Dialog>
  );
}

function CreateUserDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (created: CreatedUser) => void;
}) {
  const queryClient = useQueryClient();
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [maxSites, setMaxSites] = useState("");
  const [maxDatabases, setMaxDatabases] = useState("");
  const [error, setError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: async () => {
      const {
        data,
        error: apiError,
        response,
      } = await api.POST("/api/users", {
        body: {
          username: username.trim(),
          email: email.trim() === "" ? null : email.trim(),
          phone: phone.trim() === "" ? null : phone.trim(),
          max_sites: maxSites === "" ? null : Number(maxSites),
          max_databases: maxDatabases === "" ? null : Number(maxDatabases),
        },
      });
      if (apiError || !data) {
        throw new Error(apiErrorMessage(apiError, `Create failed (${response.status})`));
      }
      return data;
    },
    onSuccess: async (data) => {
      await queryClient.invalidateQueries({ queryKey: ["users"] });
      setUsername("");
      setEmail("");
      setPhone("");
      setMaxSites("");
      setMaxDatabases("");
      setError(null);
      onClose();
      onCreated(data);
    },
    onError: (err) => setError(err.message),
  });

  return (
    <Dialog open={open} onClose={onClose}>
      <DialogContent>
        <DialogTitle>New client account</DialogTitle>
        <DialogDescription>
          A strong temporary password is generated. If SMTP is configured and an email is provided,
          it is mailed to the client; otherwise it is shown once. Leave a quota blank for unlimited.
        </DialogDescription>
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
          noValidate
        >
          <FormField label="Username" htmlFor="new-username" error={error ?? undefined}>
            <Input
              id="new-username"
              autoComplete="off"
              spellCheck={false}
              placeholder="acme-client"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </FormField>
          <FormField label="Email" htmlFor="new-email">
            <Input
              id="new-email"
              type="email"
              autoComplete="email"
              placeholder="client@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </FormField>
          <FormField label="Phone" htmlFor="new-phone">
            <Input
              id="new-phone"
              type="tel"
              autoComplete="tel"
              placeholder="+1 555 0100"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
            />
          </FormField>
          <div className="grid grid-cols-2 gap-4">
            <FormField label="Max sites" htmlFor="new-max-sites">
              <Input
                id="new-max-sites"
                type="number"
                min={0}
                placeholder="Unlimited"
                value={maxSites}
                onChange={(e) => setMaxSites(e.target.value)}
              />
            </FormField>
            <FormField label="Max databases" htmlFor="new-max-databases">
              <Input
                id="new-max-databases"
                type="number"
                min={0}
                placeholder="Unlimited"
                value={maxDatabases}
                onChange={(e) => setMaxDatabases(e.target.value)}
              />
            </FormField>
          </div>
          <DialogActions>
            <Button variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={username.trim().length < 3} loading={create.isPending}>
              Create account
            </Button>
          </DialogActions>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** UpdateUserRequest types every clear_/reset flag as required (they carry
 *  defaults in the generated client), so partial updates must spell out this
 *  "change nothing extra" baseline. */
const UPDATE_FLAG_DEFAULTS = {
  clear_max_sites: false,
  clear_max_databases: false,
  clear_max_disk_mb: false,
  clear_cpu_quota_percent: false,
  clear_memory_max_mb: false,
  clear_plan: false,
  reset_totp: false,
} as const;

function EditQuotasDialog({
  user,
  onClose,
}: {
  user: AdminUser | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [maxSites, setMaxSites] = useState(user?.max_sites?.toString() ?? "");
  const [maxDatabases, setMaxDatabases] = useState(user?.max_databases?.toString() ?? "");
  const [maxDisk, setMaxDisk] = useState(user?.max_disk_mb?.toString() ?? "");
  const [cpuQuota, setCpuQuota] = useState(user?.cpu_quota_percent?.toString() ?? "");
  const [memoryMax, setMemoryMax] = useState(user?.memory_max_mb?.toString() ?? "");
  const [planId, setPlanId] = useState(user?.plan_id?.toString() ?? "");

  const plans = useQuery({
    queryKey: ["plans"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/plans");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load plans"));
      return data;
    },
  });

  const save = useMutation({
    mutationFn: async () => {
      if (!user) return;
      const num = (v: string) => (v === "" ? null : Number(v));
      const { error, response } = await api.PATCH("/api/users/{user_id}", {
        params: { path: { user_id: user.id } },
        body: {
          max_sites: num(maxSites),
          max_databases: num(maxDatabases),
          max_disk_mb: num(maxDisk),
          cpu_quota_percent: num(cpuQuota),
          memory_max_mb: num(memoryMax),
          plan_id: planId === "" ? null : Number(planId),
          clear_max_sites: maxSites === "",
          clear_max_databases: maxDatabases === "",
          clear_max_disk_mb: maxDisk === "",
          clear_cpu_quota_percent: cpuQuota === "",
          clear_memory_max_mb: memoryMax === "",
          clear_plan: planId === "",
          reset_totp: false,
        },
      });
      if (error) throw new Error(apiErrorMessage(error, `Update failed (${response.status})`));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["users"] });
      toast.success("Limits updated");
      onClose();
    },
    onError: (err) => toast.error(err.message),
  });

  const resetTotp = useMutation({
    mutationFn: async () => {
      if (!user) return;
      const { error, response } = await api.PATCH("/api/users/{user_id}", {
        params: { path: { user_id: user.id } },
        body: { ...UPDATE_FLAG_DEFAULTS, reset_totp: true },
      });
      if (error) throw new Error(apiErrorMessage(error, `Reset failed (${response.status})`));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["users"] });
      toast.success(`2FA reset for ${user?.username} — they can log in with password only`);
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <Dialog open={user !== null} onClose={onClose}>
      <DialogContent>
        <DialogTitle>Limits for {user?.username}</DialogTitle>
        <DialogDescription>
          A plan supplies defaults; explicit values below override it. Leave blank for unlimited /
          plan default.
        </DialogDescription>
        <FormField label="Plan" htmlFor="edit-plan">
          <select
            id="edit-plan"
            className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
            value={planId}
            onChange={(e) => setPlanId(e.target.value)}
          >
            <option value="">No plan</option>
            {(plans.data ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </FormField>
        <div className="grid grid-cols-2 gap-4">
          <FormField label="Max sites" htmlFor="edit-max-sites">
            <Input
              id="edit-max-sites"
              type="number"
              min={0}
              placeholder="Unlimited"
              value={maxSites}
              onChange={(e) => setMaxSites(e.target.value)}
            />
          </FormField>
          <FormField label="Max databases" htmlFor="edit-max-databases">
            <Input
              id="edit-max-databases"
              type="number"
              min={0}
              placeholder="Unlimited"
              value={maxDatabases}
              onChange={(e) => setMaxDatabases(e.target.value)}
            />
          </FormField>
          <FormField label="Disk (MB)" htmlFor="edit-max-disk">
            <Input
              id="edit-max-disk"
              type="number"
              min={1}
              placeholder="Unlimited"
              value={maxDisk}
              onChange={(e) => setMaxDisk(e.target.value)}
            />
          </FormField>
          <FormField label="CPU quota (%)" htmlFor="edit-cpu-quota">
            <Input
              id="edit-cpu-quota"
              type="number"
              min={1}
              max={1600}
              placeholder="Unlimited"
              value={cpuQuota}
              onChange={(e) => setCpuQuota(e.target.value)}
            />
          </FormField>
          <FormField label="Memory (MB)" htmlFor="edit-memory-max">
            <Input
              id="edit-memory-max"
              type="number"
              min={16}
              placeholder="Unlimited"
              value={memoryMax}
              onChange={(e) => setMemoryMax(e.target.value)}
            />
          </FormField>
        </div>
        {user?.totp_enabled && (
          <div className="rounded-md border border-border p-3 text-sm">
            <p className="font-medium">Two-factor authentication is on</p>
            <p className="mb-2 text-muted-foreground">
              Locked out of their authenticator? Resetting also signs them out everywhere.
            </p>
            <Button
              variant="outline"
              size="sm"
              loading={resetTotp.isPending}
              onClick={() => resetTotp.mutate()}
            >
              Reset 2FA
            </Button>
          </div>
        )}
        <DialogActions>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button loading={save.isPending} onClick={() => save.mutate()}>
            Save limits
          </Button>
        </DialogActions>
      </DialogContent>
    </Dialog>
  );
}

function DeleteUserDialog({
  user,
  onClose,
}: {
  user: AdminUser | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [confirm, setConfirm] = useState("");
  const [mode, setMode] = useState<"reassign" | "delete_sites">("reassign");

  const del = useMutation({
    mutationFn: async () => {
      if (!user) return;
      const { error, response } = await api.DELETE("/api/users/{user_id}", {
        params: { path: { user_id: user.id } },
        body: { mode, confirm_username: confirm },
      });
      if (error) throw new Error(apiErrorMessage(error, `Delete failed (${response.status})`));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["users"] });
      await queryClient.invalidateQueries({ queryKey: ["sites"] });
      toast.success(
        mode === "reassign"
          ? `${user?.username} deleted — their sites now belong to you`
          : `${user?.username} deleted — site teardown started`,
      );
      setConfirm("");
      onClose();
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <Dialog open={user !== null} onClose={onClose}>
      <DialogContent>
        <DialogTitle>Delete {user?.username}?</DialogTitle>
        <DialogDescription>
          This account owns {user?.site_count ?? 0} site(s) and {user?.database_count ?? 0}{" "}
          database(s). Choose what happens to them.
        </DialogDescription>
        <div className="space-y-2 text-sm">
          <label className="flex items-start gap-2" htmlFor="delete-mode-reassign">
            <input
              id="delete-mode-reassign"
              type="radio"
              name="delete-mode"
              className="mt-1"
              checked={mode === "reassign"}
              onChange={() => setMode("reassign")}
            />
            <span>
              <span className="font-medium">Reassign to me</span>
              <span className="block text-muted-foreground">
                Sites keep running; you become their owner.
              </span>
            </span>
          </label>
          <label className="flex items-start gap-2" htmlFor="delete-mode-teardown">
            <input
              id="delete-mode-teardown"
              type="radio"
              name="delete-mode"
              className="mt-1"
              checked={mode === "delete_sites"}
              onChange={() => setMode("delete_sites")}
            />
            <span>
              <span className="font-medium">Delete their sites too</span>
              <span className="block text-muted-foreground">
                Files, databases, vhosts and Linux users are removed permanently.
              </span>
            </span>
          </label>
        </div>
        <FormField label="Type the username to confirm" htmlFor="delete-confirm">
          <Input
            id="delete-confirm"
            placeholder={user?.username}
            autoComplete="off"
            spellCheck={false}
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        </FormField>
        <DialogActions>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={confirm.trim() !== user?.username}
            loading={del.isPending}
            onClick={() => del.mutate()}
          >
            Delete account
          </Button>
        </DialogActions>
      </DialogContent>
    </Dialog>
  );
}

function UserRowActions({
  user,
  onEditQuotas,
  onDelete,
  onTempPassword,
}: {
  user: AdminUser;
  onEditQuotas: () => void;
  onDelete: () => void;
  onTempPassword: (created: CreatedUser) => void;
}) {
  const queryClient = useQueryClient();
  const { impersonate } = useAuth();
  const navigate = useNavigate();

  const loginAs = useMutation({
    mutationFn: async () => {
      const { data, error, response } = await api.POST("/api/users/{user_id}/impersonate", {
        params: { path: { user_id: user.id } },
      });
      if (error || !data) {
        throw new Error(apiErrorMessage(error, `Impersonation failed (${response.status})`));
      }
      return data;
    },
    onSuccess: async (data) => {
      await impersonate(data.access_token);
      toast.success(`Now acting as ${data.username} — loudly audited`);
      navigate("/");
    },
    onError: (err) => toast.error(err.message),
  });

  const suspend = useMutation({
    mutationFn: async () => {
      const { error, response } = await api.PATCH("/api/users/{user_id}", {
        params: { path: { user_id: user.id } },
        body: { ...UPDATE_FLAG_DEFAULTS, suspended: !user.suspended },
      });
      if (error) throw new Error(apiErrorMessage(error, `Update failed (${response.status})`));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["users"] });
      toast.success(user.suspended ? `${user.username} unsuspended` : `${user.username} suspended`);
    },
    onError: (err) => toast.error(err.message),
  });

  const resetPassword = useMutation({
    mutationFn: async () => {
      const { data, error, response } = await api.POST("/api/users/{user_id}/reset-password", {
        params: { path: { user_id: user.id } },
      });
      if (error || !data) {
        throw new Error(apiErrorMessage(error, `Reset failed (${response.status})`));
      }
      return data;
    },
    onSuccess: async (data) => {
      await queryClient.invalidateQueries({ queryKey: ["users"] });
      onTempPassword(data);
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <div className="flex items-center justify-end gap-1">
      <Button
        variant="outline"
        size="sm"
        loading={suspend.isPending}
        onClick={() => suspend.mutate()}
      >
        {user.suspended ? "Unsuspend" : "Suspend"}
      </Button>
      <Button variant="outline" size="sm" onClick={onEditQuotas}>
        Limits
      </Button>
      <Button
        variant="ghost"
        size="icon"
        aria-label={`Log in as ${user.username}`}
        title="Log in as this client (support)"
        loading={loginAs.isPending}
        onClick={() => loginAs.mutate()}
      >
        <VenetianMask className="h-4 w-4" />
      </Button>
      <Button
        variant="ghost"
        size="icon"
        aria-label={`Reset password for ${user.username}`}
        loading={resetPassword.isPending}
        onClick={() => resetPassword.mutate()}
      >
        <KeyRound className="h-4 w-4" />
      </Button>
      <Button variant="ghost" size="icon" aria-label={`Delete ${user.username}`} onClick={onDelete}>
        <Trash2 className="h-4 w-4 text-destructive" />
      </Button>
    </div>
  );
}

export function UsersPage() {
  const { user: me } = useAuth();
  const [createOpen, setCreateOpen] = useState(false);
  const [created, setCreated] = useState<CreatedUser | null>(null);
  const [quotaUser, setQuotaUser] = useState<AdminUser | null>(null);
  const [deleteUser, setDeleteUser] = useState<AdminUser | null>(null);

  const users = useQuery({
    queryKey: ["users"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/users");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load users"));
      return data;
    },
  });

  const clients = (users.data ?? []).filter((u) => u.role !== "admin");
  const admins = (users.data ?? []).filter((u) => u.role === "admin");

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Users</h1>
          <p className="text-sm text-muted-foreground">
            Client accounts see and manage only their own sites, databases and backups.
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" aria-hidden /> New client
        </Button>
      </div>

      {users.isPending ? (
        <LoadingState label="Loading users…" />
      ) : users.isError ? (
        <ErrorState message={users.error.message} onRetry={() => users.refetch()} />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>User</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="hidden sm:table-cell">Sites</TableHead>
              <TableHead className="hidden sm:table-cell">Databases</TableHead>
              <TableHead className="hidden md:table-cell">Quotas (sites / DBs)</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {admins.map((u) => (
              <TableRow key={u.id}>
                <TableCell>
                  <div>
                    <span className="flex items-center gap-2 font-medium">
                      <UserRound className="h-4 w-4 text-muted-foreground" aria-hidden />
                      {u.username}
                      {u.id === me?.id && (
                        <span className="text-xs text-muted-foreground">(you)</span>
                      )}
                    </span>
                    {u.email && <div className="text-xs text-muted-foreground">{u.email}</div>}
                    {u.phone && <div className="text-xs text-muted-foreground">{u.phone}</div>}
                  </div>
                </TableCell>
                <TableCell>
                  <Badge variant="secondary">admin</Badge>
                </TableCell>
                <TableCell className="hidden sm:table-cell">{u.site_count}</TableCell>
                <TableCell className="hidden sm:table-cell">{u.database_count}</TableCell>
                <TableCell className="hidden text-muted-foreground md:table-cell">—</TableCell>
                <TableCell className="text-right text-xs text-muted-foreground">
                  Manage in Settings
                </TableCell>
              </TableRow>
            ))}
            {clients.map((u) => (
              <TableRow key={u.id}>
                <TableCell>
                  <div>
                    <span className="flex items-center gap-2 font-medium">
                      <UserRound className="h-4 w-4 text-muted-foreground" aria-hidden />
                      {u.username}
                    </span>
                    {u.email && <div className="text-xs text-muted-foreground">{u.email}</div>}
                    {u.phone && <div className="text-xs text-muted-foreground">{u.phone}</div>}
                  </div>
                </TableCell>
                <TableCell>
                  <span className="flex flex-wrap gap-1">
                    {u.suspended ? (
                      <Badge variant="destructive">Suspended</Badge>
                    ) : (
                      <Badge variant="success">Active</Badge>
                    )}
                    {u.must_change_password && <Badge variant="outline">Temp password</Badge>}
                    {u.totp_enabled && <Badge variant="outline">2FA</Badge>}
                  </span>
                </TableCell>
                <TableCell className="hidden sm:table-cell">{u.site_count}</TableCell>
                <TableCell className="hidden sm:table-cell">{u.database_count}</TableCell>
                <TableCell className="hidden md:table-cell">
                  {quotaLabel(u.max_sites)} / {quotaLabel(u.max_databases)}
                  {u.plan_name && (
                    <Badge variant="outline" className="ml-2">
                      {u.plan_name}
                    </Badge>
                  )}
                </TableCell>
                <TableCell>
                  <UserRowActions
                    user={u}
                    onEditQuotas={() => setQuotaUser(u)}
                    onDelete={() => setDeleteUser(u)}
                    onTempPassword={setCreated}
                  />
                </TableCell>
              </TableRow>
            ))}
            {clients.length === 0 && (
              <TableRow>
                <TableCell colSpan={6}>
                  <EmptyState
                    title="No client accounts yet"
                    description="Create one to give a customer their own scoped panel access."
                  />
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      )}

      <PlansSection />

      <CreateUserDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={setCreated}
      />
      <TempPasswordDialog created={created} onClose={() => setCreated(null)} />
      {quotaUser && <EditQuotasDialog user={quotaUser} onClose={() => setQuotaUser(null)} />}
      {deleteUser && <DeleteUserDialog user={deleteUser} onClose={() => setDeleteUser(null)} />}
    </div>
  );
}
