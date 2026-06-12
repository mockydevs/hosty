# Runbook: restore a site from backup

Applies to local and S3 backups. Time required: minutes for files-only,
longer for large databases.

## A. Restore onto the same server (normal case)

1. Backups page → locate the backup (badges show local/S3 and scope).
2. Restore → choose scope (full / files-only / database-only) → type the
   confirmation → watch the step progress.
3. Verify: open the site; for WordPress also check wp-admin loads and
   `wp core verify-checksums` (site → WordPress tab → after core update).

If the backup exists only in S3, the restore fetches and checksum-verifies
it first; corrupted downloads abort before touching the site.

## B. Disaster: rebuild a site on a FRESH server

1. Install Hosty: `curl -fsSL .../installer/install.sh | sudo bash`.
2. Recreate the site (same domain) in the panel; wait until `active`.
3. Configure the S3 target in Backups → S3 settings (same bucket/prefix).
4. The site's old backups appear under Backups (S3 badge) → Restore → full.
5. DNS: recreate the zone or re-point records at the new server IP; wait for
   the certificate state on the site Overview to turn active.

## C. Disaster: the panel itself

Panel DB snapshots: `/var/lib/hosty/backups/_panel/hosty-YYYYMMDD.db` (7 kept).

```bash
systemctl stop hosty
cp /var/lib/hosty/backups/_panel/hosty-<date>.db /var/lib/hosty/hosty.db
systemctl start hosty
```

Sites keep serving throughout — Caddy, PHP-FPM and MariaDB do not depend on
the panel process. After restore, the panel re-syncs Caddy on the next site
mutation (or restart with `HOSTY_PANEL_DOMAIN` set).

## Drill log

| Date | Scenario | Result | Notes |
|---|---|---|---|
| _pending_ | B on fresh VM | — | scheduled for the VM verification round |
