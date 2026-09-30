"""Shared test setup: a fake Proxmox API built from anonymized fixtures of a real 3-node cluster.

Nothing here talks to a real Proxmox host. All file paths (log, config, state, users)
point to a per-session temporary directory via the CB_* environment variables.
"""
import copy
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"

# Paths must be set before the modules are imported (they read them at import time).
_TMP = Path(tempfile.mkdtemp(prefix="cb-tests-"))
os.environ.setdefault("CB_LOG", str(_TMP / "clusterbalance.log"))
os.environ.setdefault("CB_CONFIG", str(_TMP / "config.yaml"))
os.environ.setdefault("CB_STATE_FILE", str(_TMP / "maintenance_state.json"))
os.environ.setdefault("CB_USERS_FILE", str(_TMP / "users"))


def load_fixture(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def _import(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def balancer_mod():
    return _import("cb_balancer", ROOT / "balancer" / "balancer.py")


@pytest.fixture(scope="session")
def app_mod():
    return _import("cb_app", ROOT / "dashboard" / "app.py")


class FakePve:
    """Minimal stand-in for `pvesh` answering the calls the balancer and dashboard make."""

    def __init__(self):
        self.resources = load_fixture("resources")
        self.storage = load_fixture("storage")
        self.cluster_status = load_fixture("cluster_status")
        self.ha_groups = load_fixture("ha_groups")
        self.ha_resources = load_fixture("ha_resources")
        self.ha_status = load_fixture("ha")
        self.tasks = load_fixture("tasks")
        self.pbs = load_fixture("pbs")
        self.vm_configs = load_fixture("vm_configs")
        self.migrations = []          # (vmid, source, target) actually "executed"
        self.task_exitstatus = "OK"   # what a finished migration task reports
        self.land = True              # does a migrated VM show up on the target?

    def node_of(self, vmid):
        return next(r["node"] for r in self.resources if r.get("vmid") == vmid)

    def placement(self):
        out = {}
        for r in self.resources:
            if r.get("type") == "qemu" and r.get("status") == "running":
                out[r["node"]] = out.get(r["node"], 0) + 1
        return dict(sorted(out.items()))

    def __call__(self, cmd):
        if cmd.startswith("get /cluster/resources"):
            return copy.deepcopy(self.resources)
        if cmd.startswith("get /storage"):
            return copy.deepcopy(self.storage)
        if cmd.startswith("get /cluster/status"):
            return copy.deepcopy(self.cluster_status)
        if cmd.startswith("get /cluster/ha/groups"):
            return copy.deepcopy(self.ha_groups)
        if cmd.startswith("get /cluster/ha/resources"):
            return copy.deepcopy(self.ha_resources)
        if cmd.startswith("get /cluster/ha/status/current"):
            return copy.deepcopy(self.ha_status)
        if cmd.startswith("get /cluster/tasks"):
            return copy.deepcopy(self.tasks)
        if "/content" in cmd:
            return copy.deepcopy(self.pbs)
        if cmd.startswith("get /nodes/") and cmd.endswith("/config"):
            return copy.deepcopy(self.vm_configs.get(cmd.split("/")[4], {}))
        if cmd.startswith("create /nodes/") and "/migrate" in cmd:
            parts = cmd.split()
            vmid, target = int(parts[1].split("/")[4]), parts[3]
            source = self.node_of(vmid)
            self.migrations.append((vmid, source, target))
            if self.land:
                for r in self.resources:
                    if r.get("vmid") == vmid:
                        r["node"] = target
            return f"UPID:{source}:0:0:0:qmigrate:{vmid}:root@pam:"
        if "/tasks/" in cmd:
            return {"status": "stopped", "exitstatus": self.task_exitstatus}
        raise AssertionError(f"unexpected pvesh call: {cmd}")

    def pile_up_on(self, node, keep=(110, 208, 300, 301, 500)):
        """Move every other running VM (and its load) onto `node`, like after a failed storage mount."""
        nodes = {r["node"]: r for r in self.resources if r.get("type") == "node"}
        for r in self.resources:
            if r.get("type") == "qemu" and r.get("status") == "running" and r["vmid"] not in keep and r["node"] != node:
                src = nodes[r["node"]]
                src["mem"] -= r.get("mem", 0)
                nodes[node]["mem"] += r.get("mem", 0)
                cores = r.get("cpu", 0) * r.get("maxcpu", 1)
                src["cpu"] -= cores / src["maxcpu"]
                nodes[node]["cpu"] += cores / nodes[node]["maxcpu"]
                r["node"] = node


BASE_CONFIG = {
    "enabled": True, "dry_run": False, "method": "combined", "mode": "used",
    "balanciness_threshold": 15, "memory_weight": 2.0, "cpu_weight": 1.0, "disk_weight": 1.0,
    "io_enabled": False, "max_migrations": 3, "min_improvement": 5.0,
    "exclude_tags": ["kritik", "critical", "pinned", "no-migrate"],
    "overprovisioning_protection": True, "max_memory_usage": 95,
    "migration_type": "live", "migration_window": "", "migration_timeout": 60,
    "maintenance_nodes": [], "notifications": {"enabled": False},
}


@pytest.fixture
def pve():
    return FakePve()


@pytest.fixture
def make_balancer(balancer_mod, pve, tmp_path, monkeypatch):
    """Factory for a ClusterBalance instance wired to the fake API; mounts are all OK unless told otherwise."""
    state = tmp_path / "state.json"
    monkeypatch.setattr(balancer_mod, "STATE_FILE", str(state))
    monkeypatch.setattr(balancer_mod, "_state_lock", lambda: open(os.devnull, "w"))
    monkeypatch.setattr(balancer_mod.time, "sleep", lambda s: None)

    def factory(unmounted=(), **cfg):
        cb = balancer_mod.ClusterBalance(str(tmp_path / "missing.yaml"))
        cb.config.update(copy.deepcopy(BASE_CONFIG))
        cb.config.update(cfg)
        cb.pvesh = pve
        cb._is_mounted = lambda node, path: node not in unmounted
        return cb

    factory.state_file = state
    return factory
