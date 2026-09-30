#!/usr/bin/env bash
# ClusterBalance installer — run as root on ONE Proxmox VE node of the cluster.
#   ./install.sh                 install / upgrade balancer + dashboard
#   ./install.sh --widget        also add the ClusterBalance box to the Proxmox web UI on all nodes
# Re-running is safe: existing config.yaml and dashboard users are kept.
set -euo pipefail

PREFIX=/opt/clusterbalance
USERS_FILE=/etc/clusterbalance/dashboard-users
PORT="${CB_DASHBOARD_PORT:-5000}"
HERE="$(cd "$(dirname "$0")" && pwd)"
WIDGET=0
[[ "${1:-}" == "--widget" ]] && WIDGET=1

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root"
command -v pvesh >/dev/null || die "pvesh not found - run this on a Proxmox VE node"

say "Checking Python dependencies"
if ! python3 -c 'import flask, yaml' 2>/dev/null; then
    apt-get update -qq && apt-get install -y -qq python3-flask python3-yaml
fi

say "Installing balancer to $PREFIX"
install -d -m 755 "$PREFIX" "$PREFIX/dashboard/templates"
install -m 755 "$HERE/balancer/balancer.py" "$PREFIX/balancer.py"
if [[ ! -f "$PREFIX/config.yaml" ]]; then
    install -m 644 "$HERE/balancer/config.example.yaml" "$PREFIX/config.yaml"
    say "Created $PREFIX/config.yaml (dry_run: true - nothing will be migrated yet)"
else
    say "Keeping existing $PREFIX/config.yaml"
fi

say "Installing dashboard"
install -m 755 "$HERE/dashboard/app.py" "$PREFIX/dashboard/app.py"
install -m 644 "$HERE/dashboard/templates/index.html" "$PREFIX/dashboard/templates/index.html"

if [[ ! -s "$USERS_FILE" ]]; then
    say "Create the dashboard admin user"
    install -d -m 700 "$(dirname "$USERS_FILE")"
    read -rp "Dashboard username [admin]: " U; U="${U:-admin}"
    python3 - "$U" "$USERS_FILE" <<'PY'
import getpass, sys
from werkzeug.security import generate_password_hash
user, path = sys.argv[1], sys.argv[2]
while True:
    p1 = getpass.getpass("Password: "); p2 = getpass.getpass("Repeat: ")
    if p1 and p1 == p2: break
    print("Passwords empty or do not match, try again.")
open(path, "a").write(f"{user}:{generate_password_hash(p1)}\n")
PY
    chmod 600 "$USERS_FILE"
else
    say "Keeping existing users in $USERS_FILE"
fi

say "Installing logrotate + systemd units"
install -m 644 "$HERE/deploy/logrotate/clusterbalance" /etc/logrotate.d/clusterbalance
install -m 644 "$HERE/deploy/systemd/clusterbalance.service" /etc/systemd/system/
install -m 644 "$HERE/deploy/systemd/clusterbalance.timer" /etc/systemd/system/
install -m 644 "$HERE/deploy/systemd/clusterbalance-dashboard.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now clusterbalance.timer >/dev/null
systemctl enable clusterbalance-dashboard.service >/dev/null
systemctl restart clusterbalance-dashboard.service

NODE_IP="$(hostname -I | awk '{print $1}')"
URL="https://${NODE_IP}:${PORT}"

if [[ $WIDGET -eq 1 ]]; then
    say "Installing the Proxmox UI widget on all online nodes (dashboard: $URL)"
    TMP="$(mktemp)"; sed "s#__DASHBOARD_URL__#${URL}#" "$HERE/widget/clusterbalance.js" > "$TMP"
    for ip in $(pvesh get /cluster/status --output-format json | python3 -c 'import json,sys;[print(n["ip"]) for n in json.load(sys.stdin) if n.get("type")=="node" and n.get("online")]'); do
        if [[ "$ip" == "$NODE_IP" ]]; then RUN=(bash -c); CP=(install -m 644 "$TMP" /usr/share/pve-manager/js/clusterbalance.js)
        else RUN=(ssh -o BatchMode=yes "root@$ip"); CP=(scp -q "$TMP" "root@$ip:/usr/share/pve-manager/js/clusterbalance.js"); fi
        "${CP[@]}"
        "${RUN[@]}" 'grep -q clusterbalance.js /usr/share/pve-manager/index.html.tpl || sed -i "s#</body>#  <script src=\"/pve2/js/clusterbalance.js\"></script></body>#" /usr/share/pve-manager/index.html.tpl'
        echo "   widget installed on $ip"
    done
    rm -f "$TMP"
    echo "   Note: a pve-manager package upgrade overwrites index.html.tpl; re-run ./install.sh --widget afterwards."
fi

sleep 2
systemctl is-active --quiet clusterbalance-dashboard || die "dashboard did not start: journalctl -u clusterbalance-dashboard"
cat <<EOF

ClusterBalance installed.
  Dashboard : $URL   (uses this node's Proxmox certificate)
  Config    : $PREFIX/config.yaml   (dry_run: true until you switch it off)
  Log       : /var/log/clusterbalance.log   (history is also shown in the dashboard)
  Timer     : systemctl list-timers clusterbalance.timer

Test a run now (only logs, never migrates):  python3 $PREFIX/balancer.py --dry-run
EOF
