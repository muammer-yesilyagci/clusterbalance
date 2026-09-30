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


# ---------------------------------------------------------------- placement rules (config.yaml `rules`)

def final_nodes(pve, migrations):
    where = {r["vmid"]: r["node"] for r in pve.resources if r.get("type") == "qemu"}
    for m in migrations:
        where[m["vmid"]] = m["target"]
    return where


def test_normalize_rules_skips_invalid(balancer_mod):
    rules = balancer_mod.ClusterBalance.normalize_rules([
        {"name": "ok-anti", "type": "anti-affinity", "vms": [141, "151"]},
        {"name": "ok-pin", "type": "pin", "vms": [121], "node": "pve3", "enabled": False},
        {"name": "one-vm", "type": "anti-affinity", "vms": [141]},
        {"name": "no-node", "type": "pin", "vms": [121]},
        {"name": "bad-type", "type": "spread", "vms": [1, 2]},
        {"name": "ok-anti", "type": "affinity", "vms": [1, 2]},   # duplicate name
        {"name": "bad-vm", "type": "affinity", "vms": ["x", 2]},
        "garbage",
    ])
    assert [(r["name"], r["vms"], r["enabled"]) for r in rules] == [("ok-anti", [141, 151], True), ("ok-pin", [121], False)]


def test_anti_affinity_violation_is_fixed(make_balancer, pve):
    cb = make_balancer(rules=[{"name": "renders-apart", "type": "anti-affinity", "vms": [141, 151]}])
    cb.collect_data()
    assert cb.rule_violations() == [{"rule": "renders-apart", "type": "anti-affinity", "vms": [151]}]
    migs = cb.plan_migrations()
    assert [(m["vmid"], m["reason"], m["rule"]) for m in migs] == [(151, "rule", "renders-apart")]
    where = final_nodes(pve, migs)
    assert where[141] != where[151]


def test_pin_moves_vm_to_its_node(make_balancer):
    cb = make_balancer(rules=[{"name": "files-on-pve3", "type": "pin", "vms": [121], "node": "pve3"}])
    cb.collect_data()
    migs = cb.plan_migrations()
    assert [(m["vmid"], m["source"], m["target"], m["reason"]) for m in migs] == [(121, "pve1", "pve3", "rule")]


def test_affinity_group_is_gathered(make_balancer, pve):
    cb = make_balancer(rules=[{"name": "app-db", "type": "affinity", "vms": [141, 161]}])
    cb.collect_data()
    migs = cb.plan_migrations()
    assert len(migs) == 1 and migs[0]["reason"] == "rule"
    where = final_nodes(pve, migs)
    assert where[141] == where[161]


def test_rules_never_move_excluded_vms(make_balancer):
    cb = make_balancer(rules=[{"name": "erp-pve1", "type": "pin", "vms": [300], "node": "pve1"},
                              {"name": "dc-erp-apart", "type": "anti-affinity", "vms": [110, 301]}])
    cb.collect_data()
    assert cb.plan_migrations() == []
    assert sorted(u["vmid"] for u in cb.rule_unfixable) == [300, 301]
    assert all("haric" in u["reason"] for u in cb.rule_unfixable)


def test_anti_affinity_moves_the_movable_member(make_balancer, pve):
    # 110 is excluded (kritik) and stays; 121 must leave pve3 once it is there
    pve.pile_up_on("pve3")
    cb = make_balancer(rules=[{"name": "dc-files-apart", "type": "anti-affinity", "vms": [110, 121]}])
    cb.collect_data()
    migs = cb.plan_migrations()
    assert migs[0]["vmid"] == 121 and migs[0]["reason"] == "rule" and migs[0]["target"] != "pve3"


def test_balancing_respects_rules(make_balancer, pve):
    pve.pile_up_on("pve3")
    cb = make_balancer(max_migrations=10, rules=[
        {"name": "pin-141", "type": "pin", "vms": [141], "node": "pve3"},
        {"name": "together", "type": "affinity", "vms": [151, 161]},
        {"name": "apart", "type": "anti-affinity", "vms": [181, 190]},
    ])
    cb.collect_data()
    migs = cb.plan_migrations()
    moved = {m["vmid"] for m in migs}
    assert any(m["reason"] == "balance" for m in migs)
    assert 141 not in moved
    assert not ({151, 161} & moved)
    where = final_nodes(pve, migs)
    assert where[181] != where[190]


def test_rule_fixes_come_first_and_respect_max_migrations(make_balancer, pve):
    pve.pile_up_on("pve3")
    cb = make_balancer(max_migrations=1, rules=[{"name": "pin-121", "type": "pin", "vms": [121], "node": "pve1"}])
    cb.collect_data()
    migs = cb.plan_migrations()
    assert [(m["vmid"], m["reason"]) for m in migs] == [(121, "rule")]


def test_disabled_rule_is_ignored(make_balancer):
    cb = make_balancer(rules=[{"name": "off", "type": "pin", "vms": [121], "node": "pve3", "enabled": False}])
    cb.collect_data()
    assert cb.rules == [] and cb.plan_migrations() == []


def test_rule_moves_wait_for_migration_window(make_balancer, pve, balancer_mod, monkeypatch):
    real = balancer_mod.datetime

    class Noon(real):
        @classmethod
        def now(cls, tz=None):
            return real(2026, 9, 30, 12, 0)

    monkeypatch.setattr(balancer_mod, "datetime", Noon)
    cb = make_balancer(migration_window="20:00-07:00", rules=[{"name": "p", "type": "pin", "vms": [121], "node": "pve3"}])
    assert cb.run()["status"] == "deferred"
    assert pve.migrations == []
