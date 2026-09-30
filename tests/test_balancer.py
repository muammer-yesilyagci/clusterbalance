"""Balancer behaviour on an anonymized snapshot of a real 3-node cluster (pve1/pve2/pve3).

Cluster facts used below:
  * 27 VMs; 110, 300, 301 (tag "kritik") and 500 are on pve3 / pve2 and excluded from balancing
  * 500 has a USB device, its EFI disk on node-local storage and is in the restricted HA group "pve2-fixed"
  * all other VM disks live on the shared dir storage "nfs-vms"
"""
import json

import pytest


def ids(migrations):
    return [m["vmid"] for m in migrations]


# ---------------------------------------------------------------- planning

def test_balanced_cluster_plans_nothing(make_balancer):
    cb = make_balancer()
    assert cb.collect_data()
    assert cb.plan_migrations() == []


def test_pile_up_is_spread_and_state_restored(make_balancer, pve):
    pve.pile_up_on("pve3")
    cb = make_balancer()
    cb.collect_data()
    before = cb.calculate_node_scores()
    migs = cb.plan_migrations()

    assert 1 <= len(migs) <= 3
    assert all(m["source"] == "pve3" for m in migs)
    assert {m["target"] for m in migs} <= {"pve1", "pve2"}
    # every planned move improves the balance by at least min_improvement
    for m in migs:
        assert m["balanciness_before"] - m["balanciness_after"] >= 5.0
    # planning is a simulation: real node state is untouched afterwards
    assert cb.calculate_node_scores() == before


def test_excluded_and_passthrough_vms_never_planned(make_balancer, pve):
    pve.pile_up_on("pve3")
    cb = make_balancer()
    cb.collect_data()
    planned = ids(cb.plan_migrations())
    for vmid in (110, 300, 301, 500):
        assert vmid not in planned
        assert vmid in cb.ignored_guests


def test_target_without_mounted_storage_is_rejected(make_balancer, pve):
    pve.pile_up_on("pve3")
    cb = make_balancer(unmounted=("pve2",))
    cb.collect_data()
    migs = cb.plan_migrations()
    assert migs and all(m["target"] != "pve2" for m in migs)
    reasons = [r for per_vm in cb.rejections.values() for n, r in per_vm.items() if n == "pve2"]
    assert reasons and all("BAGLI DEGIL" in r for r in reasons)


def test_min_improvement_blocks_pointless_moves(make_balancer, pve):
    pve.pile_up_on("pve3")
    cb = make_balancer(min_improvement=10_000)
    cb.collect_data()
    assert cb.plan_migrations() == []


def test_max_migrations_respected(make_balancer, pve):
    pve.pile_up_on("pve3")
    cb = make_balancer(max_migrations=1)
    cb.collect_data()
    assert len(cb.plan_migrations()) == 1


# ---------------------------------------------------------------- safety checks

def test_storage_and_ha_checks_for_usb_vm(make_balancer):
    cb = make_balancer()
    cb.collect_data()
    usb_vm = {"vmid": 500, "node": "pve2", "type": "qemu"}
    ok, reason = cb.guest_storage_ok(usb_vm, "pve1")
    assert not ok and "local" in reason
    ok, reason = cb.ha_target_ok(500, "pve1")
    assert not ok and "pve2-fixed" in reason
    assert cb.ha_target_ok(111, "pve2") == (True, "ok")


@pytest.mark.parametrize("window,now,allowed", [
    ("20:00-07:00", "23:30", True),
    ("20:00-07:00", "06:59", True),
    ("20:00-07:00", "14:00", False),
    ("", "14:00", True),
    ("garbage", "14:00", False),
])
def test_migration_window(make_balancer, balancer_mod, monkeypatch, window, now, allowed):
    real = balancer_mod.datetime

    class FakeDT(real):
        @classmethod
        def now(cls, tz=None):
            return real.strptime("2026-09-30 " + now, "%Y-%m-%d %H:%M")

    monkeypatch.setattr(balancer_mod, "datetime", FakeDT)
    cb = make_balancer(migration_window=window)
    assert cb.in_migration_window() is allowed


def test_balance_moves_deferred_outside_window(make_balancer, pve, balancer_mod, monkeypatch):
    real = balancer_mod.datetime

    class Noon(real):
        @classmethod
        def now(cls, tz=None):
            return real(2026, 9, 30, 12, 0)

    monkeypatch.setattr(balancer_mod, "datetime", Noon)
    pve.pile_up_on("pve3")
    result = make_balancer(migration_window="20:00-07:00").run()
    assert result["status"] == "deferred"
    assert pve.migrations == []


# ---------------------------------------------------------------- execution

def test_execute_waits_until_vm_runs_on_target(make_balancer, pve):
    cb = make_balancer()
    m = {"vmid": 131, "name": "x", "type": "qemu", "source": pve.node_of(131), "target": "pve2", "reason": "balance"}
    assert cb.execute_migration(m) is True
    assert pve.migrations == [(131, m["source"], "pve2")]


def test_execute_reports_failed_task(make_balancer, pve):
    pve.task_exitstatus = "migration problems"
    cb = make_balancer()
    m = {"vmid": 131, "name": "x", "type": "qemu", "source": pve.node_of(131), "target": "pve2", "reason": "balance"}
    assert cb.execute_migration(m) is False


def test_execute_times_out_when_vm_never_arrives(make_balancer, pve, balancer_mod, monkeypatch):
    pve.land = False
    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(balancer_mod.time, "time", lambda: next(clock))
    cb = make_balancer(migration_timeout=120)
    m = {"vmid": 131, "name": "x", "type": "qemu", "source": pve.node_of(131), "target": "pve2", "reason": "balance"}
    assert cb.execute_migration(m) is False


def test_dry_run_never_calls_migrate(make_balancer, pve):
    pve.pile_up_on("pve3")
    result = make_balancer(dry_run=True).run()
    assert result["status"] == "completed"
    assert pve.migrations == []


# ---------------------------------------------------------------- maintenance mode

@pytest.mark.parametrize("node,moves,stuck", [("pve1", 13, 0), ("pve2", 8, 1), ("pve3", 3, 0)])
def test_maintenance_preview(make_balancer, node, moves, stuck):
    pv = make_balancer().maintenance_preview(node)
    assert len(pv["moves"]) == moves
    assert len(pv["unmovable"]) == stuck
    assert pv["fits"] is True
    assert all(m["target"] != node for m in pv["moves"])
    if stuck:
        assert pv["unmovable"][0]["vmid"] == 500 and "USB" in pv["unmovable"][0]["reason"]


def test_maintenance_moves_excluded_vms_when_allowed(make_balancer):
    moved = ids(make_balancer().maintenance_preview("pve3")["moves"])
    assert sorted(moved) == [110, 300, 301]
    pv = make_balancer(maintenance_include_excluded=False).maintenance_preview("pve3")
    assert pv["moves"] == [] and len(pv["unmovable"]) == 3


def test_maintenance_drain_and_return(make_balancer, pve):
    start = pve.placement()
    r = make_balancer(maintenance_nodes=["pve3"]).run()
    assert r["success"] == r["total"] == 3
    assert "pve3" not in pve.placement()

    state = json.loads(make_balancer.state_file.read_text())
    assert sorted(e["vmid"] for e in state["drained"]["pve3"]) == [110, 300, 301]

    # still in maintenance: nothing more to do
    assert make_balancer(maintenance_nodes=["pve3"]).run()["status"] == "balanced"

    # leave maintenance with a return request -> VMs go back
    state["return_requested"] = ["pve3"]
    make_balancer.state_file.write_text(json.dumps(state))
    r = make_balancer().run()
    assert r["success"] == 3
    assert pve.placement() == start
    state = json.loads(make_balancer.state_file.read_text())
    assert state["drained"] == {} and state["return_requested"] == []
