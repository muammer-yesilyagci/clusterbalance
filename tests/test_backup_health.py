"""Backup health card: grace period for new VMs, guests no backup job covers, skip tags."""
import json
import time

import pytest

NOW = time.time()


@pytest.fixture
def hb(app_mod, pve, tmp_path, monkeypatch):
    """health_backups() wired to the fake cluster; every guest has a fresh backup unless a test changes it."""
    monkeypatch.setattr(app_mod, "pvesh", pve)
    monkeypatch.setattr(app_mod.socket, "gethostname", lambda: "pve1")
    monkeypatch.setattr(app_mod, "FIRST_SEEN_FILE", str(tmp_path / "first_seen.json"))
    guests = [r for r in pve.resources if r.get("type") in ("qemu", "lxc") and not r.get("template")]
    pve.pbs = [{"vmid": g["vmid"], "ctime": int(NOW - 3600), "content": "backup"} for g in guests]

    def run():
        app_mod._cache.clear()
        return app_mod.health_backups()

    run.pve = pve
    run.first_seen = tmp_path / "first_seen.json"
    run.add_vm = lambda vmid, **kw: pve.resources.append({"type": "qemu", "vmid": vmid, "name": f"new-{vmid}",
                                                          "node": "pve2", "status": "running", **kw})
    return run


def test_all_backed_up(hb):
    d = hb()
    assert d["ok"] and not d["problems"] and not d["pending"] and d["skipped"] == 0
    assert d["newest_ok"] == d["total"]


def test_new_vm_waits_for_first_backup_without_alarm(hb):
    hb()                                   # baseline: the dashboard has seen today's guests
    hb.add_vm(900)
    d = hb()
    assert d["ok"] is True                 # no alarm, so no n8n e-mail
    assert [p["vmid"] for p in d["pending"]] == [900]
    assert d["pending"][0]["reason"] == "first_backup_pending"
    assert json.loads(hb.first_seen.read_text())["900"] > 0


def test_new_vm_alarms_after_grace_period(hb):
    hb()
    hb.add_vm(900)
    hb()
    seen = json.loads(hb.first_seen.read_text())
    seen["900"] = int(NOW - 30 * 3600)     # first seen 30 hours ago, still no backup
    hb.first_seen.write_text(json.dumps(seen))
    d = hb()
    assert d["ok"] is False and [p["vmid"] for p in d["problems"]] == [900]
    assert d["problems"][0]["level"] == "bad" and not d["pending"]


def test_guest_without_backup_at_first_run_is_not_treated_as_new(hb):
    hb.pve.pbs = [b for b in hb.pve.pbs if b["vmid"] != 102]
    d = hb()                               # no first_seen file yet: everything counts as old
    assert d["ok"] is False and [p["vmid"] for p in d["problems"]] == [102]


def test_vm_created_recently_by_proxmox_ctime(hb):
    hb.pve.pbs = [b for b in hb.pve.pbs if b["vmid"] != 102]
    hb.pve.vm_configs["102"] = {"meta": f"creation-qemu=9.2.0,ctime={int(NOW - 2 * 3600)}"}
    d = hb()                               # first run, but Proxmox says the VM is 2 hours old
    assert d["ok"] is True and [p["vmid"] for p in d["pending"]] == [102]


def test_temp_tag_is_skipped(hb):
    hb()
    hb.add_vm(9500, tags="temp")
    hb.pve.pbs.append({"vmid": 9500, "ctime": int(NOW - 90 * 24 * 3600)})
    d = hb()
    assert d["ok"] is True and d["skipped"] == 1
    assert 9500 not in [p["vmid"] for p in d["problems"] + d["pending"]]


def test_guest_outside_every_backup_job(hb):
    hb.pve.backup_jobs = [{"id": "sel", "enabled": 1, "vmid": "102,110"}]
    d = hb()
    assert d["ok"] is False
    assert d["uncovered"] == d["total"] - 2
    assert all(p["reason"] == "no_job" and p["level"] == "bad" for p in d["problems"])
    assert {102, 110}.isdisjoint(p["vmid"] for p in d["problems"])


def test_new_vm_not_in_job_list_alarms_immediately(hb):
    ids = ",".join(str(b["vmid"]) for b in hb.pve.pbs)
    hb.pve.backup_jobs = [{"id": "sel", "enabled": 1, "vmid": ids}]
    hb()
    hb.add_vm(901)
    d = hb()                               # new, but no job will ever back it up
    assert [(p["vmid"], p["reason"]) for p in d["problems"]] == [(901, "no_job")]
    assert not d["pending"]


@pytest.mark.parametrize("job,uncovered", [
    ({"enabled": 1, "all": 1, "exclude": "102, 110"}, {102, 110}),
    ({"enabled": 0, "all": 1}, "all"),
    ({"enabled": 1, "all": 1, "node": "pve1"}, "not-pve1"),
    ({"enabled": 1, "pool": "prod"}, "not-pool"),
])
def test_job_selection_rules(hb, job, uncovered):
    hb.pve.backup_jobs = [job]
    hb.pve.pools = {"prod": {"members": [{"vmid": 102}, {"vmid": 110}, {"id": "storage/pve1/local"}]}}
    guests = [r for r in hb.pve.resources if r.get("type") in ("qemu", "lxc") and not r.get("template")]
    if uncovered == "all":
        uncovered = {g["vmid"] for g in guests}
    elif uncovered == "not-pve1":
        uncovered = {g["vmid"] for g in guests if g["node"] != "pve1"}
    elif uncovered == "not-pool":
        uncovered = {g["vmid"] for g in guests} - {102, 110}
    d = hb()
    assert {p["vmid"] for p in d["problems"] if p["reason"] == "no_job"} == uncovered


def test_deleted_vmid_is_forgotten_so_a_reused_id_gets_a_new_grace_period(hb):
    hb()
    hb.add_vm(902)
    hb()
    hb.pve.resources = [r for r in hb.pve.resources if r.get("vmid") != 902]
    hb()
    assert "902" not in json.loads(hb.first_seen.read_text())
