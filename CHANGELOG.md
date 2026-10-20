# Changelog

## 2.1.0 — 2026-10-20

Highlights: disk image import, a new macOS-style dashboard with light and dark theme, and quieter, smarter health alerts.

### Added
#### Disk image import
- New **Import** page: drop a `.vhd`, `.vhdx`, `.vmdk`, `.qcow2`, `.raw`, `.img` or `.ova` file on the dashboard
  (chunked, resumable upload) or pick one that is already in a storage's `import/` folder, and get a Proxmox VM.
- The image is inspected (real format, size, UEFI/BIOS and Windows/Linux from the partition table, OVF settings for
  OVA) and a settings dialog suggests name, next VM ID, the **least loaded node**, storage, CPU/RAM, OS type,
  firmware/TPM, disk bus, NIC, bridge/VLAN and the VirtIO ISO.
- The VM is created by Proxmox itself (`import-from`), with live progress, result, *Open in Proxmox* link,
  optional start and optional deletion of the source file.
- **Disk size** field: grow the system disk during the import (never shrink); the VM is started only after the resize.
- **Add to HA** option (on by default, optional HA group): the new VM is registered as an HA resource, state `started`
  only when it was started, so HA never powers on a VM left off.
- Safety: strict file names, uploads confined to the import folder, free-space check, images with backing files,
  external data files or foreign VMDK extents are rejected; audit log entries for uploads, imports and deletes.
- `GET/POST/PUT/DELETE /api/import/*`; `CB_IMPORT_MAX_GB`, `CB_IMPORT_JOBS`; new module `dashboard/importer.py`
  (installed by `install.sh`).

#### New dashboard design
- macOS-style window with a grouped sidebar, large page titles and a **balance / RAM / CPU rings** summary on the Overview page
  ("Cluster is balanced — no migration needed …").
- **Light and dark theme**: follows the system setting, toggle in the toolbar (remembered); charts follow the theme.
- Full-width, denser layout for wide screens; works on phones (collapsible sidebar).
- Info card on the Import page explaining the workflow.
- New logo (a spirit level whose bubble sits in the middle): sidebar, favicon, `docs/logo.svg`, `docs/social-preview.png`.

#### Proxmox UI widget
- Redesigned to match the dashboard: logo button, rings, balance status with the gap and threshold, *Open dashboard* /
  *Import* / *Settings* shortcuts, Turkish/English with proper characters. `install.sh` loads it with a version query so
  browsers pick up updates.
- `/api/widget-summary` also returns the balance score.

#### Smarter backup health
- A new VM no longer triggers a "no backup" alarm right away: for 26 h after it is created (Proxmox `ctime`) or first seen by
  the dashboard it is shown as *waiting for its first backup*.
- VMs that no enabled backup job covers (all / VMID list / pool, node filter, exclude list) are reported as a problem of their own
  (`reason: "no_job"`), also in the n8n e-mail.
- Guests tagged `temp` or `no-backup` are skipped (`CB_BACKUP_SKIP_TAGS`); `CB_FIRST_SEEN` stores first-seen times.

#### Quieter failed-task alerts
- Console sessions (`vncproxy`, `termproxy`, …) are no longer reported as failed tasks (`CB_TASKS_IGNORE`).
- A failure a later success settled is not an alarm: a VM's failed start/stop/shutdown followed by a successful power task,
  or the same task type retried successfully on the same VM. Unresolved failures (e.g. a backup never retried) still alert.

#### HA coverage
- The HA card lists running VMs that are not HA-managed (information only, no alarm); tag `no-ha` to leave a VM out
  on purpose (`CB_HA_SKIP_TAGS`).

### Fixed
- The dashboard could hang completely (all requests, n8n "panel unreachable") when one client opened a TCP connection
  and never finished the TLS handshake: the handshake now runs in the request thread and silent clients are dropped
  after 30 s (`tests/test_tls_server.py` reproduces the hang).

## 2.0.0 — 2026-09-29 (updated 2026-09-30)

Hardened, production-tested release. Highlights:

### Added on 2026-09-30
#### Placement rules
- New **Rules** page: keep VMs apart (anti-affinity), keep them together (affinity) or pin them to a node, without Proxmox tags. Live status per rule; tag-based rules are shown read-only.
- The balancer now **fixes rule violations** (reason `rule`, shown in the migration history) before balancing, inside the migration window and within `max_migrations`. Excluded VMs are never moved for a rule and are reported as "could not be fixed".
- Balancing no longer splits an affinity group.
- `rules` / `enforce_rules` in `config.yaml`; `GET/POST /api/rules`.

#### Dashboard
- **English and Turkish UI** with a TR/EN switch (defaults to the browser language); Turkish texts with proper characters.
- Config file is read/written as UTF-8.

#### Quality
- Test suite (pytest + node:test) on an anonymized 3-node cluster snapshot with a fake `pvesh`; GitHub Actions CI (Python 3.11/3.13, JS checks, shellcheck).
- Balancer paths configurable with `CB_DIR`, `CB_CONFIG`, `CB_STATE_FILE`, `CB_LOG`.

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
