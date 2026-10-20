"""HA card: running guests outside HA are listed as information, never as an alarm."""
import pytest


@pytest.fixture
def ha(app_mod, pve, monkeypatch):
    monkeypatch.setattr(app_mod, "pvesh", pve)

    def run():
        app_mod._cache.clear()
        return app_mod.health_ha()

    run.pve = pve
    return run


def managed(pve):
    return {x["sid"] for x in pve.ha_status if x.get("type") == "service"}


def add(pve, vmid, **kw):
    pve.resources.append({"type": "qemu", "vmid": vmid, "name": f"vm-{vmid}", "node": "pve2", "status": "running", **kw})


def test_fixture_cluster_lists_only_running_guests_without_ha(ha):
    d = ha()
    expected = sorted(r["vmid"] for r in ha.pve.resources
                      if r.get("type") in ("qemu", "lxc") and r.get("status") == "running" and not r.get("template")
                      and ("vm:" if r["type"] == "qemu" else "ct:") + str(r["vmid"]) not in managed(ha.pve)
                      and "no-ha" not in (r.get("tags") or ""))
    assert [u["vmid"] for u in d["unprotected"]] == expected


def test_new_running_vm_outside_ha_is_information_not_alarm(ha):
    before = ha()
    add(ha.pve, 988)
    d = ha()
    assert 988 in [u["vmid"] for u in d["unprotected"]]
    assert d["ok"] == before["ok"] and d["bad"] == before["bad"]       # no new alarm


@pytest.mark.parametrize("kw", [{"tags": "altyapi;no-ha"}, {"status": "stopped"}, {"template": 1}])
def test_tagged_stopped_or_template_guests_are_left_out(ha, kw):
    add(ha.pve, 989, **kw)
    assert 989 not in [u["vmid"] for u in ha()["unprotected"]]


def test_container_outside_ha_uses_ct_sid(ha):
    ha.pve.resources.append({"type": "lxc", "vmid": 990, "name": "ct-990", "node": "pve1", "status": "running"})
    assert 990 in [u["vmid"] for u in ha()["unprotected"]]
    ha.pve.ha_status.append({"type": "service", "sid": "ct:990", "state": "started", "request_state": "started", "node": "pve1"})
    assert 990 not in [u["vmid"] for u in ha()["unprotected"]]
