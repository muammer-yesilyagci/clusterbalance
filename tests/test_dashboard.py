"""Dashboard API: auth, security boundaries, health, history parser, settings and maintenance endpoints."""
import base64
import datetime
import json
import os
import shutil

import pytest
import yaml
from werkzeug.security import generate_password_hash

from conftest import FIX

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()}


@pytest.fixture
def client(app_mod, pve, tmp_path, monkeypatch):
    users = tmp_path / "users"
    users.write_text("admin:" + generate_password_hash("secret") + "\n")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"enabled": True, "dry_run": True, "method": "combined", "balanciness_threshold": 15,
                                   "max_migrations": 3, "migration_type": "live", "exclude_tags": ["kritik"],
                                   "maintenance_nodes": [], "notifications": {"smtp": {"password": "keep-me"}}}))
    log = tmp_path / "cb.log"
    shutil.copy(FIX / "clusterbalance.log", log)
    for attr, val in {"USERS_FILE": users, "CB_CONFIG_FILE": cfg, "CB_LOG": log,
                      "CB_STATE_FILE": tmp_path / "state.json"}.items():
        monkeypatch.setattr(app_mod, attr, str(val))
    monkeypatch.setattr(app_mod, "pvesh", pve)
    monkeypatch.setattr(app_mod.socket, "gethostname", lambda: "pve1")
    monkeypatch.setattr(app_mod, "node_cmd", lambda name, ip, args: (0, "10.0.0.13:/mnt/nfs-vms nfs4"))
    monkeypatch.setattr(app_mod, "_balancer_busy", lambda: False)
    monkeypatch.setattr(app_mod, "_kick_balancer", lambda: None)
    monkeypatch.setattr(app_mod, "_cb_state_lock", lambda: open(os.devnull, "w"))
    app_mod._cache.clear()
    app_mod._hist_cache.clear()
    c = app_mod.app.test_client()
    c.cfg_path = cfg
    return c


# ---------------------------------------------------------------- auth & security

@pytest.mark.parametrize("path", ["/", "/api/health", "/api/config", "/api/history", "/api/maintenance"])
def test_login_required(client, path):
    r = client.get(path)
    assert r.status_code == 401
    assert "Basic" in r.headers["WWW-Authenticate"]


def test_wrong_password_rejected(client):
    bad = {"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()}
    assert client.get("/api/nodes", headers=bad).status_code == 401
    assert client.get("/api/nodes", headers=AUTH).status_code == 200


def test_missing_users_file_denies_everyone(client, app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "USERS_FILE", "/nonexistent/users")
    assert client.get("/api/nodes", headers=AUTH).status_code == 401


@pytest.mark.parametrize("path", ["/api/proxmox/switch-to-free", "/api/proxmox/disable-subscription-nag"])
def test_dangerous_endpoints_removed(client, path):
    assert client.post(path, headers=AUTH).status_code == 404


def test_no_cors_for_foreign_origins(client):
    r = client.get("/api/nodes", headers={**AUTH, "Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in r.headers
    assert r.headers["X-Frame-Options"] == "DENY"


def test_widget_summary_is_public_but_cors_limited(client):
    r = client.get("/api/widget-summary", headers={"Origin": "https://10.0.0.12:8006"})
    assert r.status_code == 200
    assert r.headers["Access-Control-Allow-Origin"] == "https://10.0.0.12:8006"
    assert set(r.json) == {"node_count", "total", "running_vms", "avg_cpu", "avg_mem", "balanced", "max_diff"}
    r = client.get("/api/widget-summary", headers={"Origin": "https://evil.example:8006"})
    assert "Access-Control-Allow-Origin" not in r.headers


# ---------------------------------------------------------------- health

def test_health_all_green(client):
    d = client.get("/api/health", headers=AUTH).json
    for card in ("cluster", "storage", "ha", "tasks"):
        assert d[card]["ok"] is True, card
    assert d["cluster"]["quorate"] is True and len(d["cluster"]["nodes"]) == 3
    assert d["backups"]["storage"]  # auto-detected PBS storage


def test_health_detects_unmounted_storage(client, app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "node_cmd", lambda name, ip, args: (1, "") if name == "pve2" else (0, "x nfs4"))
    d = client.get("/api/health", headers=AUTH).json
    assert d["storage"]["ok"] is False
    assert [i["node"] for i in d["storage"]["items"] if not i["mounted"]] == ["pve2"]


def test_health_detects_ha_error(client, pve):
    for s in pve.ha_status:
        if s.get("sid") == "vm:500":
            s["state"] = "error"
    d = client.get("/api/health", headers=AUTH).json
    assert d["ha"]["ok"] is False and d["ha"]["bad"][0]["sid"] == "vm:500"


# ---------------------------------------------------------------- history

def test_history_parser(app_mod, client):
    d = app_mod.parse_cb_history(24 * 3650)
    assert d["summary"]["runs"] > 100
    reasons = {m["reason"] for m in d["migrations"]}
    assert {"legacy", "balance"} <= reasons
    dry = [m for m in d["migrations"] if m["mode"] == "test"]
    assert dry and dry[0]["bal_before"] > dry[0]["bal_after"]
    assert d["last_run"]["bal"] is not None


def _run(ts, vmid, src, dst, ok=True):
    lines = [f"{ts},000 [INFO] ClusterBalance - Starting balance run",
             f"{ts},001 [INFO] Current balanciness: 20.0 (threshold: 15)",
             f"{ts},002 [INFO] Planned: {vmid} (vm-{vmid}) {src} -> {dst}, balanciness 20.0 -> 10.0",
             f"{ts},003 [INFO] Migrating {vmid} (vm-{vmid}) from {src} to {dst}"]
    lines.append(f"{ts},900 [INFO] Migration completed: {vmid} is running on {dst}" if ok
                 else f"{ts},900 [ERROR] Migration task for {vmid} ended: migration problems")
    return "\n".join(lines) + "\n"


def test_history_detects_ping_pong(app_mod, client):
    now = datetime.datetime.now()
    t = lambda h: (now - datetime.timedelta(hours=h)).strftime("%Y-%m-%d %H:%M:%S")
    log = (_run(t(10), 141, "pve1", "pve2") + _run(t(5), 141, "pve2", "pve1")        # back within 24h
           + _run(t(9), 151, "pve1", "pve2") + _run(t(4), 151, "pve2", "pve1", ok=False)  # return failed
           + _run(t(8), 161, "pve1", "pve2"))
    with open(app_mod.CB_LOG, "w") as f:
        f.write(log)
    d = app_mod.parse_cb_history(24)
    flagged = {m["vmid"] for m in d["migrations"] if m["pingpong"]}
    assert flagged == {141}
    assert d["summary"]["pingpong_vms"] == 1
    assert d["summary"]["success"] == 4 and d["summary"]["failed"] == 1


def test_history_endpoint(client):
    d = client.get("/api/history?hours=87600", headers=AUTH).json
    assert set(d) >= {"summary", "migrations", "last_run", "chart", "running"}


# ---------------------------------------------------------------- settings

def test_config_roundtrip_keeps_unknown_keys(client):
    g = client.get("/api/config", headers=AUTH).json
    assert g["mode"] == "combined" and g["migration_type"] == "online"
    body = {**g, "threshold": 20, "mode": "memory", "migration_type": "offline",
            "exclude_tags": "kritik, critical", "evil_key": "x"}
    assert client.post("/api/config", headers=AUTH, json=body).json["success"]
    saved = yaml.safe_load(client.cfg_path.read_text())
    assert saved["balanciness_threshold"] == 20 and saved["method"] == "memory"
    assert saved["migration_type"] == "offline" and saved["exclude_tags"] == ["kritik", "critical"]
    assert saved["notifications"]["smtp"]["password"] == "keep-me"
    assert "evil_key" not in saved


# ---------------------------------------------------------------- maintenance

def test_maintenance_enter_exit(client, app_mod):
    r = client.post("/api/maintenance/enter", headers=AUTH, json={"node": "pve3"})
    assert r.json["success"]
    assert yaml.safe_load(client.cfg_path.read_text())["maintenance_nodes"] == ["pve3"]
    # only one node at a time
    r = client.post("/api/maintenance/enter", headers=AUTH, json={"node": "pve1"})
    assert r.status_code == 409
    # unknown node
    assert client.post("/api/maintenance/enter", headers=AUTH, json={"node": "nope"}).status_code == 400

    with open(app_mod.CB_STATE_FILE, "w") as f:
        json.dump({"drained": {"pve3": [{"vmid": 110, "name": "dc-01", "to": "pve1", "ts": "x"}]}, "return_requested": []}, f)
    m = client.get("/api/maintenance", headers=AUTH).json
    n3 = next(n for n in m["nodes"] if n["name"] == "pve3")
    assert n3["maintenance"] and n3["drained"][0]["vmid"] == 110
    assert m["dashboard_node"] == "pve1"

    assert client.post("/api/maintenance/exit", headers=AUTH, json={"node": "pve3"}).json["success"]
    assert yaml.safe_load(client.cfg_path.read_text())["maintenance_nodes"] == []
    with open(app_mod.CB_STATE_FILE) as f:
        assert json.load(f)["return_requested"] == ["pve3"]


def test_maintenance_preview_rejects_bad_node_name(client):
    assert client.get("/api/maintenance/preview?node=../etc", headers=AUTH).status_code == 400
