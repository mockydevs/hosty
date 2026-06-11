# Hosty Admin Guide

Everything an operator needs day-to-day. For architecture and security
internals see `ARCHITECTURE.md` and `SECURITY.md`.

## First boot

Open the panel URL printed by the installer. The first visit shows the
**Create admin account** form (minimum 12-character password). Setup locks
itself permanently once the account exists. Lost the password? There is no
reset flow in v1 — restore a panel DB snapshot from
`/var/lib/hosty/backups/_panel/` or recreate `/var/lib/hosty/hosty.db`
(which erases panel state but not your sites).

## Sites

**Create:** Sites → New site. Enter a domain (unicode is fine — it is stored
as punycode) and pick a PHP version. Provisioning runs as a tracked pipeline:
Linux user → web root → PHP-FPM pool → Caddy vhost → file-manager access.
Any failing step rolls everything back; the site shows `error` with the
reason, and it is safe to delete and retry.

**HTTPS:** automatic. Point an A/AAAA record at the server and Caddy obtains
a certificate; the site's Overview shows certificate status, including a
clear "DNS not pointing here yet" state.

**PHP:** switch versions on the site Overview (the new pool starts before the
old one stops — no downtime) and tune `memory_limit` / upload size per site.

**Delete:** type the domain to confirm. Removes vhost, pools, databases,
files and the Linux user.

## WordPress

Site → WordPress tab → fill the install form. The install provisions a
dedicated MariaDB database/user, downloads core (shared cache), writes
wp-config and runs the installer — all as the site's Linux user, never root.
Afterwards the tab shows version/update status and plugin/theme counts, with
actions for core updates, maintenance mode, salt rotation and a one-time
admin login link.

## Databases

Databases page (or site → Databases tab). Credentials are displayed exactly
once at creation/reset — store them then; the panel keeps only a hash.
"Open Adminer" gives a browser SQL client through the panel's authenticated
proxy. Rows flagged `orphan` exist on the MariaDB server but are not managed
by Hosty; `missing on server` means the panel record's database vanished.

## Files

Site → Files tab (or full screen). Each site's file manager is scoped to its
own directory — sessions are signed per site and cannot cross over.

## DNS

DNS page → create a zone (optionally auto-created with sites). The record
editor validates per record type; templates cover "point at this server",
Google Workspace MX, and SPF/DMARC. Records are served by PowerDNS on this
server — delegate NS records at your registrar accordingly.

## Backups

Backups page: run-now per site, schedules (daily/weekly with hour), retention
pruning, and S3 mirroring (any S3-compatible endpoint; credentials are
encrypted at rest). Restores offer full / files-only / database-only modes
with explicit confirmation. The panel also snapshots its own database daily
to `/var/lib/hosty/backups/_panel/` (keeps 7). For the full disaster
procedure see `RUNBOOK-RESTORE.md`.

## Audit log

Settings → Audit log: every mutating action with user, timestamp, result and
client IP. Request contents are never recorded.

## Service operations

```bash
systemctl status hosty            # the panel itself (auto-restarts)
journalctl -u hosty -f            # panel logs (JSON in prod)
systemctl status caddy mariadb php8.3-fpm pdns hosty-filebrowser
sudo bash /opt/hosty/installer/update.sh [tag]   # update the panel
```

Panel state lives in `/var/lib/hosty` (DB, env, venv, backups). Site content
lives in `/var/www/<domain>`.
