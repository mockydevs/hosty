import { AuditLogCard } from "@/components/audit-log-card";

/** Audit log as a dedicated page: every change made through the panel. */
export function AuditPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Audit log</h1>
      <AuditLogCard />
    </div>
  );
}
