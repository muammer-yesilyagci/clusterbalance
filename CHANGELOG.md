# Changelog

## 2.0.0 — 2026-09-29

Hardened, production-tested release. Highlights:

### Balancer
- Greedy planning with simulation: node load is recalculated after every planned move; a move must reduce the imbalance by `min_improvement` (default 5).
- Storage safety: every disk/ISO of a VM must be on storage that is shared, enabled for the target node and actually mounted there (`findmnt` over SSH for shared `dir` storages).
- HA awareness: restricted HA groups are respected.
- Migration window (`migration_window`, default `20:00-07:00`) for balancing moves.
- Migrations are awaited (task status + VM running on target), with `migration_timeout` (default 900 s).
- Maintenance mode rewritten: preview (`--maintenance-preview NODE`), optional draining of excluded VMs, list of unmovable VMs with reasons, automatic return of drained VMs on exit (`maintenance_state.json`).
- `dry_run` is the default for new installations.

### Dashboard
- Login (HTTP Basic, werkzeug hashes) over HTTPS using the Proxmox node certificate; security headers.
- Removed endpoints that changed apt repositories / patched Proxmox UI files on all nodes; CORS closed.
- Health cards (quorum, storage mounts, HA, PBS backups, failed tasks) with a legend.
- Migration history (reasons, results, durations, errors, ping-pong detection, balance-score chart, filters).
- Maintenance mode UI with preview and progress banner.
- Calm dark theme; colour only for status.
- Settings now drive the ClusterBalance config (key mapping), "run" is dry-run only.
- `/api/health`, `/api/history`, `/api/maintenance*`, public minimal `/api/widget-summary` for the Proxmox UI widget.

### Other
- `install.sh`, systemd units, logrotate, n8n health-alert workflow with HTML e-mail.

## 1.x

Original ClusterBalance by Cemal Demirci: memory/CPU/disk/I/O balancing, affinity / anti-affinity / pinning tags,
maintenance nodes, PSI mode, notifications, dashboard and Proxmox UI widget.
