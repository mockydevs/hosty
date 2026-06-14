import { NotificationsCard } from "@/components/notifications-card";

export function NotificationsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Notifications</h1>
        <p className="text-sm text-muted-foreground">All system alerts and notifications.</p>
      </div>
      <NotificationsCard />
    </div>
  );
}
