/** Placeholder pages for features arriving in later phases. */
import { EmptyState } from "@/components/states";

function Stub({ title, phase, blurb }: { title: string; phase: string; blurb: string }) {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
      <EmptyState title={`${title} coming in ${phase}`} description={blurb} />
    </div>
  );
}

export function SitesPage() {
  return (
    <Stub
      title="Sites"
      phase="Phase 3"
      blurb="Create domains with automatic HTTPS, per-site PHP versions, and isolated Linux users."
    />
  );
}

export function DatabasesPage() {
  return (
    <Stub
      title="Databases"
      phase="Phase 5"
      blurb="Provision MariaDB databases per site and manage them with Adminer."
    />
  );
}

export function DnsPage() {
  return (
    <Stub
      title="DNS"
      phase="Phase 7"
      blurb="Manage zones and records with PowerDNS, with one-click templates."
    />
  );
}

export function BackupsPage() {
  return (
    <Stub
      title="Backups"
      phase="Phase 8"
      blurb="Scheduled per-site backups to local storage and S3, with one-click restores."
    />
  );
}
