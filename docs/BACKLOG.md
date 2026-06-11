# Post-1.0 backlog

Deliberately out of scope for v1.0 (see TASKS.md "Definition of Done").

- Multi-user accounts and roles (schema already supports a role column).
- Two-factor authentication (TOTP).
- Drop panel root privilege: dedicated `hosty` user + per-command sudoers
  whitelist (ADR-002 revisit).
- MariaDB driver + bound parameters if databases ever accept arbitrary
  external input (ADR-007 revisit).
- Filebrowser upload ownership normalization via event hooks (Phase 6 note).
- Adminer credential auto-fill via login plugin (ADR-007 tradeoff).
- Monitoring graphs (CPU/RAM/disk history, per-site traffic).
- Staging clones of sites; one-click PHP version canary.
- Redis object-cache toggle for WordPress sites.
- Server firewall (ufw) management.
- Email notifications for backup failures (currently dashboard-surfaced).
- API reference published as static HTML (generated from /api/openapi.json).
