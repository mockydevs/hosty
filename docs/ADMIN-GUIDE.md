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

## Settings

**Panel domain (HTTPS):** Settings → Panel domain. Enter a domain and the
panel publishes its own Caddy vhost, obtains a certificate and switches
session cookies to HTTPS-only. The domain must resolve to this server first;
if it does not, the error offers two paths: **Create the A record on the DNS
page** (one click — publishes the record into the matching zone hosted on
this panel's DNS page and retries) or **Enable anyway** while DNS propagates.
The one-click record is only live on the internet if the zone is actually
delegated to this server — check the zone's delegation status on the DNS page.

**Email delivery:** Settings → Email delivery. Configure the outgoing SMTP
account used for temporary passwords and panel alerts. Saving connects and
authenticates immediately and reports the exact failure inline; use the test
field to send yourself a message. The password is encrypted at rest and never
re-displayed. **Network** defaults to IPv4 only; "IPv4 + IPv6" also tries
IPv6, but IPv4 is always attempted first so broken IPv6 routing cannot block
mail. Alert recipients receive notification emails (disk full, service down,
backup failures).

**Two-factor authentication:** Settings → Set up 2FA for one-time codes from
an authenticator app. Active sessions are listed alongside — revoke anything
you do not recognize.

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
