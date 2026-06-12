import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useAuth } from "@/lib/auth";
import { ChangePasswordForm } from "@/pages/settings";
/**
 * Users: your account today; client-account management lands here in Phase 11
 * (create clients, suspend, quotas — see TASKS.md).
 */
import { UserRound } from "lucide-react";
import { useNavigate } from "react-router";

export function UsersPage() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Users</h1>

      <div className="grid items-start gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <UserRound className="h-4 w-4 text-muted-foreground" aria-hidden /> Your account
            </CardTitle>
            <CardDescription>The account you are currently signed in with.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            <div className="flex items-center justify-between gap-4">
              <span className="text-muted-foreground">Username</span>
              <span className="font-medium">{user?.username}</span>
            </div>
            <div className="flex items-center justify-between gap-4">
              <span className="text-muted-foreground">Role</span>
              <Badge variant="secondary">{user?.role}</Badge>
            </div>
            <p className="pt-2 text-xs text-muted-foreground">
              Managing client accounts (create, suspend, quotas) arrives with multi-tenancy.
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">Change password</CardTitle>
            <CardDescription>
              Changing your password signs you out everywhere, including this session.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ChangePasswordForm
              onChanged={async () => {
                await logout();
                navigate("/login");
              }}
            />
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
