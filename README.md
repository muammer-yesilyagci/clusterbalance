# ClusterBalance v2

**A safety-first load balancer, maintenance mode and health dashboard for Proxmox VE clusters.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Proxmox VE 8.x](https://img.shields.io/badge/Proxmox%20VE-8.x-E57000)
![Python 3](https://img.shields.io/badge/Python-3.11-3776AB)

🇹🇷 [Türkçe README](README.tr.md)

![Dashboard](docs/screenshots/dashboard.jpg)

ClusterBalance runs on one node of your Proxmox VE cluster. Every 15 minutes it measures how
evenly the nodes are loaded and, if the difference is too big, **live-migrates a few VMs** to even
it out — but only when the move is safe and actually helps. On top of that you get:

- a **maintenance mode** that drains a node before a reboot/upgrade and moves every VM back afterwards,
- a **web dashboard** with cluster health, migration history and settings,
- a small **widget inside the Proxmox web UI**,
- a **health API** you can poll for e-mail/Teams/Slack alerts (an n8n workflow is included).

> The dashboard UI is currently in Turkish. Translations are welcome — see [Contributing](#contributing).

### What's new in v2

Version 2 is a hardened, production-tested evolution of the original ClusterBalance by Cemal Demirci:
storage/HA-aware safety checks, simulated planning that stops ping-pong migrations, a migration window,
awaited migrations, maintenance mode with automatic return, a secured HTTPS dashboard with health cards and
migration history, and ready-made alerts. See the [CHANGELOG](CHANGELOG.md).

---

## Contents

- [Why this project exists](#why-this-project-exists)
- [Features](#features)
- [How balancing works](#how-balancing-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Dashboard](#dashboard)
- [Maintenance mode](#maintenance-mode)
- [Migration history](#migration-history)
- [Health API and alerts](#health-api-and-alerts)
- [API reference](#api-reference)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Upgrading and uninstalling](#upgrading-and-uninstalling)
- [Limitations and roadmap](#limitations-and-roadmap)
- [Credits](#credits) · [License](#license)

---

## Why this project exists

ClusterBalance started as an internal tool for a 3-node Proxmox VE cluster whose VM disks live on a
shared NFS storage. Running it in production for months taught us — sometimes the hard way — what an
automatic balancer must never do. Every safeguard in this repository comes from a real incident:

| What happened | What ClusterBalance does now |
|---|---|
| After a cluster-wide reboot two nodes silently failed to mount the shared NFS storage. Proxmox kept showing the storage as *active*, so for **three weeks HA could not fail over anything**, and the balancer kept trying to move VMs onto nodes that could not see their disks. | Before every migration it checks that **each disk of the VM is on storage that is enabled, shared and actually mounted on the target node**. The dashboard shows a mount-status card and the alert workflow e-mails you within 5 minutes. |
| The first version picked the three "largest" VMs and moved them all to the same node without re-calculating. It made **~7,000 migration attempts in a few months**, the same VMs bouncing back and forth between nodes. | Planning is **greedy and simulated**: after every planned move the node loads are recalculated, a move is only accepted if it lowers the imbalance by at least `min_improvement`, and the history view flags **ping-pong** VMs. |
| Migrations were started 5 seconds apart without waiting — effectively in parallel — and a "started" task was counted as a success. | Each migration is **awaited**: first the Proxmox task, then until the VM is really *running* on the target (works for HA-managed VMs too). Failures and timeouts are logged with the reason. |
| VMs were moved in the middle of the working day. | Balancing migrations only happen inside a **migration window** (default `20:00–07:00`). Maintenance moves are the only exception. |
| A VM with a USB licence dongle, a VM whose EFI disk was on node-local storage and a VM in a restricted HA group were candidates for migration. | **USB/PCI passthrough, local disks and restricted HA groups** are detected and never migrated. |
| The first dashboard had no login and could run root commands on all nodes from any browser tab. | Dashboard requires **login over HTTPS**, dangerous endpoints were removed, CORS is closed. |

We are publishing it because these lessons apply to any Proxmox cluster.

## Features

**Balancer** (`balancer/balancer.py`, systemd timer every 15 min)
- Node score from used memory, CPU, root disk and optional I/O (weights configurable), or assigned resources, or PSI.
- Greedy planning with simulation, `min_improvement`, `max_migrations` per run, migration window.
- Safety checks per VM and target node: shared + enabled + mounted storage, HA group restrictions, USB/PCI passthrough, overprovisioning protection.
- Tag based rules: exclude (`kritik`, `critical`, `pinned`, `no-migrate`, `cb_ignore*`), pin to a node (`cb_pin_<node>`), affinity (`cb_affinity_<group>`) and anti-affinity (`cb_anti_affinity_<group>`).
- `dry_run` mode that only logs decisions — the default after installation.

**Maintenance mode**
- One click in the dashboard: preview → drain the node → *"node is empty, safe to reboot"* → exit → every VM moves back to where it was.
- Also drains VMs excluded from normal balancing (optional); VMs that cannot move are listed with the reason and **never shut down**.

**Dashboard** (`dashboard/app.py`, HTTPS, login)
- Health cards: quorum/nodes, shared-storage mounts per node, HA services, last PBS backup per VM, failed tasks.
- Migration history with reasons, results, duration, errors, balance-score chart and ping-pong detection.
- Settings (threshold, mode, max migrations, excluded tags, dry run), manual dry-run, raw log.
- Maintenance mode UI with preview.

**Proxmox UI widget** — a small box in the Proxmox web interface with the cluster balance status and a link to the dashboard.

**Alerts** — `GET /api/health` returns one JSON document for the whole cluster; the included n8n workflow sends a clear HTML e-mail only when something changes (problem / warning / recovered).

| Maintenance preview | Migration history |
|---|---|
| ![Maintenance preview](docs/screenshots/maintenance-preview.jpg) | ![Migration history](docs/screenshots/migration-history.jpg) |

## How balancing works

Every run (default: every 15 minutes):

1. **Collect** nodes and guests with `pvesh get /cluster/resources`, storage definitions, HA groups/resources and the config of each VM.
2. **Score** each node. With the default `method: combined` and `mode: used`:

   ```
   node_score = memory_used% × memory_weight + cpu% × cpu_weight + rootdisk% × disk_weight (+ io_wait × io_weight)
   balanciness = max(node_score) − min(node_score)
   ```
3. If `balanciness <= balanciness_threshold` → nothing to do.
4. Otherwise **plan greedily**: for every candidate VM, pick the best allowed target node, simulate the move,
   and keep the move that reduces balanciness the most — if it reduces it by at least `min_improvement`.
   Apply it to the simulated state and repeat until balanced or `max_migrations` is reached.
5. **Execute** (only inside `migration_window`, unless `dry_run`): `pvesh create /nodes/<src>/qemu/<id>/migrate --target <dst> --online 1`,
   then wait for the task and for the VM to be *running* on the target (up to `migration_timeout`).

A VM is **never** a candidate if it is stopped, excluded by tag/ID/name, has USB/PCI passthrough, is pinned elsewhere,
or has any disk/ISO on storage that is local, disabled, not defined for the target, or **not mounted** on the target.
Targets in a restricted HA group, in maintenance, or that would exceed `max_memory_usage` are skipped.
The reasons are written to the log and shown in the dashboard ("why can't VM X move to node Y").

## Requirements

- Proxmox VE 8.x cluster (developed and used on 8.4), 2+ nodes.
- Shared storage for live migration (NFS, Ceph, iSCSI/LVM, …). Node-local disks are never migrated.
- On the node that runs ClusterBalance: `python3`, `python3-flask`, `python3-yaml` (installed by `install.sh` from the Debian repos),
  and root SSH between cluster nodes (standard in a Proxmox cluster — used for mount checks and the widget).

## Installation

On **one** Proxmox node, as root:

```bash
git clone https://github.com/muammer-yesilyagci/clusterbalance.git
cd clusterbalance
./install.sh            # balancer + dashboard
./install.sh --widget   # optional: also add the widget to the Proxmox web UI on all nodes
```

The installer:
- copies the balancer and the dashboard to `/opt/clusterbalance/`,
- creates `/opt/clusterbalance/config.yaml` from the example **with `dry_run: true`** (existing config is kept),
- asks for a dashboard username/password (stored as a werkzeug hash in `/etc/clusterbalance/dashboard-users`),
- installs `clusterbalance.service` + `clusterbalance.timer`, `clusterbalance-dashboard.service` and logrotate.

Then:

1. Open `https://<node-ip>:5000` (it uses the node's Proxmox certificate, so your browser shows the same warning as on `:8006`).
2. Watch **Settings → Migration history** for a day or two: every decision is visible, nothing is migrated in dry-run mode.
3. When you are happy with the decisions, switch **Dry Run** off in the dashboard.

Try a run by hand at any time — it never migrates in dry-run:

```bash
python3 /opt/clusterbalance/balancer.py --dry-run
```

## Configuration

`/opt/clusterbalance/config.yaml` — see [`balancer/config.example.yaml`](balancer/config.example.yaml) for all options.
The most important ones:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch |
| `dry_run` | `true` | Only log what would be migrated |
| `method` | `combined` | `memory`, `cpu`, `disk`, `io` or `combined` |
| `mode` | `used` | `used` (actual usage), `assigned` (configured RAM/cores), `psi` (pressure stall info) |
| `memory_weight` / `cpu_weight` / `disk_weight` / `io_weight` | 2 / 1 / 1 / 1.5 | Weights in the node score |
| `balanciness_threshold` | `15` | Allowed score difference between nodes |
| `min_improvement` | `5.0` | A migration must reduce the difference by at least this much |
| `max_migrations` | `3` | Balancing migrations per run |
| `migration_window` | `20:00-07:00` | Real balancing migrations only in this window (`""` = always) |
| `migration_type` | `live` | `live` (online) or `offline` |
| `migration_timeout` | `900` | Seconds to wait for one migration |
| `exclude_tags` | `kritik, critical, pinned, no-migrate` | Proxmox tags excluded from balancing |
| `exclude_vmids` / `exclude_names` | `[]` | More exclusions |
| `maintenance_nodes` | `[]` | Nodes being drained (set from the dashboard) |
| `maintenance_include_excluded` | `true` | Also drain excluded VMs during maintenance |
| `overprovisioning_protection` / `max_memory_usage` | `true` / `95` | Never push a node above this memory % |
| `with_local_disks` | `false` | Allow migrating VMs with local disks (not recommended) |

Tag prefixes (Proxmox VM tags): `cb_ignore…` (exclude), `cb_pin_<node>` (keep on node),
`cb_affinity_<group>` (keep together), `cb_anti_affinity_<group>` (keep apart).

Dashboard environment variables (set them in `clusterbalance-dashboard.service`):

| Variable | Default |
|---|---|
| `CB_DASHBOARD_PORT` | `5000` |
| `CB_USERS_FILE` | `/etc/clusterbalance/dashboard-users` |
| `CB_TLS_CERT` / `CB_TLS_KEY` | `/etc/pve/local/pve-ssl.pem` / `.key` |
| `CB_BACKUP_STORAGE` | first enabled PBS storage |
| `CB_DIR`, `CB_CONFIG`, `CB_LOG`, `CB_STATE_FILE` | `/opt/clusterbalance`, `…/config.yaml`, `/var/log/clusterbalance.log`, `…/maintenance_state.json` |

Add another dashboard user:

```bash
python3 -c "import getpass;from werkzeug.security import generate_password_hash as g;print('alice:'+g(getpass.getpass()))" >> /etc/clusterbalance/dashboard-users
```

## Dashboard

| Page | What you get |
|---|---|
| **Dashboard** | Health cards (with an ⓘ legend explaining the colours), cluster totals, VM distribution and resource charts |
| **Nodes** | Per-node CPU/RAM/disk and the **maintenance mode** buttons |
| **Virtual machines / Containers** | Guests with status, CPU, memory |
| **Monitoring** | Cluster CPU/RAM/disk gauges |
| **Settings** | Balancer settings, manual dry-run, **migration history**, raw log |

Colour is only used when it means something: green/yellow/red = status, blue = actions, one colour per data series.

## Maintenance mode

**Nodes → Enter maintenance ("Bakıma al")**

1. **Preview** (≈40 s, read-only): which VM goes to which node, which VMs cannot move and why
   (e.g. *"USB/PCI device attached"*), and the estimated RAM of the remaining nodes.
2. **Confirm**: the node is added to `maintenance_nodes` and the balancer starts immediately. Maintenance moves
   ignore the migration window. A banner shows progress; when no VM is left it says the node is safe to reboot.
3. **Exit maintenance ("Bakımdan çıkar")**: every VM that was moved away is live-migrated **back to the original node**
   (tracked in `/opt/clusterbalance/maintenance_state.json`, retried up to 5 runs).

Rules: only one node at a time; VMs that cannot move keep running on the node and are **never shut down**;
every enter/exit is written to the journal as `AUDIT … <user>: …`.

Preview from the command line (does not write to the log):

```bash
python3 /opt/clusterbalance/balancer.py --maintenance-preview pve2
```

## Migration history

Settings → **Migration history** is built from `/var/log/clusterbalance.log` (and rotated copies). For each migration:
time, VM, source → target, **reason** (balance score before → after, maintenance, return after maintenance),
mode (real/test) and **result**:

| Result | Meaning |
|---|---|
| Success + duration | VM is running on the target |
| Failed / Timeout | With the error (e.g. *"VM was locked by a backup"*) |
| Deferred | Planned outside the migration window |
| Test | Dry-run: planned, not migrated |

Also: "last run" decision in plain words, summary counters, balance-score chart with the threshold line,
filters (real / test / failed / ping-pong) and a **ping-pong** badge for VMs moved back within 24 h or 3+ times.

## Health API and alerts

`GET /api/health` (login required) returns the state of the whole cluster in one call:

```json
{
  "cluster":  { "ok": true, "quorate": true, "nodes": [{ "name": "pve1", "online": true, "uptime": 2462400 }] },
  "storage":  { "ok": false, "items": [{ "storage": "nfs-vms", "node": "pve2", "mounted": false, "path": "/mnt/nfs-vms" }] },
  "ha":       { "ok": true, "total": 25, "bad": [] },
  "backups":  { "ok": true, "storage": "pbs", "total": 27, "newest_ok": 27, "problems": [] },
  "tasks":    { "ok": true, "count": 0, "failed": [] },
  "time": "17:40:46"
}
```

Rules: a card is **bad** if quorum is lost or a node is offline; if a shared *dir* storage is not mounted on a node;
if an HA service is in `error/fence/recovery/freeze` or not running although requested; if a VM's last PBS backup
is older than 50 h (warning after 26 h) or missing; failed tasks are listed for the last 24 h.

**n8n workflow** — [`alerts/n8n-health-alert.json`](alerts/n8n-health-alert.json):
polls `/api/health` every 5 minutes and sends an HTML e-mail **only when a state changes**
(`[SORUN]` / `[UYARI]` / `[DÜZELDİ]`), with what changed, what to do, and a status table.

1. n8n → create a *Basic Auth* credential with a dashboard user (create a dedicated one, e.g. `n8n`).
2. Import the workflow, set the URL (`https://YOUR-PVE-NODE:5000/api/health`), enable *Ignore SSL Issues*, choose your SMTP credential and e-mail addresses (also the panel URL at the top of the Code node).
3. Activate / publish the workflow.

Any other poller works too (Uptime Kuma keyword monitor, a cron + curl script, Zabbix HTTP agent …).

## API reference

All endpoints need HTTP Basic auth except `/api/widget-summary`.

| Endpoint | Description |
|---|---|
| `GET /api/health` | Cluster health (see above) |
| `GET /api/history?hours=168` | Migration history, runs, balance-score chart data |
| `GET /api/maintenance` | Maintenance state per node |
| `GET /api/maintenance/preview?node=X` | Drain preview (read-only) |
| `POST /api/maintenance/enter` `{"node":"X"}` | Put node into maintenance |
| `POST /api/maintenance/exit` `{"node":"X"}` | Leave maintenance and move VMs back |
| `GET/POST /api/config` | Read / change balancer settings |
| `POST /api/proxmox/run-balance` | Run the balancer in dry-run now |
| `GET /api/proxmox/balance-status` | Last 50 log lines |
| `GET /api/summary`, `/api/nodes`, `/api/guests`, `/api/distribution`, `/api/top`, `/api/recommendations` | Data for the dashboard pages |
| `GET /api/widget-summary` | **No auth**, minimal counters for the Proxmox UI widget; CORS allowed only for `https://<cluster node>:8006` |

## Security

- The balancer and the dashboard run as **root** (they use `pvesh` and SSH to the other nodes). Treat the dashboard like the Proxmox UI: keep it on the management network, never expose it to the internet.
- The dashboard only works over HTTPS and every endpoint except the widget summary requires a login. There is no CORS for other origins.
- Passwords are stored as werkzeug hashes. Use a dedicated user for automation (n8n).
- The dashboard can change the balancer configuration and put nodes into maintenance — i.e. it can cause live migrations. It cannot run arbitrary commands.
- Built-in Flask/werkzeug server: fine for a handful of admins; put it behind a reverse proxy if you need more.

## Troubleshooting

| Symptom | Check |
|---|---|
| Nothing is ever migrated | `dry_run` still on? Are you inside `migration_window`? Look at "last run" in Migration history — it states why (balanced / no move improves enough / VMs rejected with reasons). |
| "Storage not mounted" card is red | On that node: `findmnt <path>`; mount it and fix `/etc/fstab` (`_netdev,x-systemd.automount`). For `dir` storages also set `is_mountpoint` in Proxmox. |
| Dashboard does not start | `journalctl -u clusterbalance-dashboard -e` — usually a missing users file or certificate path. |
| Widget shows "Connection error" | Open the dashboard URL once in that browser and accept the certificate. |
| Widget disappeared after an upgrade | A `pve-manager` update replaces `index.html.tpl`; run `./install.sh --widget` again. |
| Migration history is empty | Is `/var/log/clusterbalance.log` written? `systemctl list-timers clusterbalance.timer` |

## Upgrading and uninstalling

Upgrade: `git pull && ./install.sh` (config and users are kept).

Uninstall:

```bash
systemctl disable --now clusterbalance.timer clusterbalance-dashboard.service
rm -f /etc/systemd/system/clusterbalance*.{service,timer} /etc/logrotate.d/clusterbalance
rm -rf /opt/clusterbalance /etc/clusterbalance
# widget, on every node:
sed -i '/clusterbalance.js/d' /usr/share/pve-manager/index.html.tpl; rm -f /usr/share/pve-manager/js/clusterbalance.js
```

## Limitations and roadmap

- Dashboard UI is Turkish only (i18n welcome).
- One cluster per installation; the balancer runs on one node (if that node is down, balancing pauses — HA keeps working).
- Planned: English UI, per-node history graphs, non-root API token, N-1 capacity card, "movability" view per VM, rule editor for affinity tags.

## Contributing

Issues and pull requests are welcome. Please describe your cluster (Proxmox version, number of nodes, storage type)
when reporting balancing behaviour, and attach the relevant part of `/var/log/clusterbalance.log`.

## Credits

- **Muammer Yeşilyağcı** ([@muammer-yesilyagci](https://github.com/muammer-yesilyagci)) — maintainer; ClusterBalance v2: safety checks, maintenance mode, health cards, migration history, alerts and documentation.
- **Cemal Demirci** ([@cemal-demirci](https://github.com/cemal-demirci)) — original author of ClusterBalance v1, the dashboard and the Proxmox widget; co-developed v2.

## License

[MIT](LICENSE)
