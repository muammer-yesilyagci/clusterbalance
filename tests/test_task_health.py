"""Failed-task card: console sessions ignored, failures a later success settled are not alarms."""
import time

import pytest

T0 = int(time.time()) - 3600


def task(sec, ttype, status="OK", vmid="188", node="pve2"):
    return {"starttime": T0 + sec, "type": ttype, "status": status, "id": vmid, "node": node,
            "upid": f"UPID:{node}:0:0:{T0 + sec}:{ttype}:{vmid}:root@pam:"}


@pytest.fixture
def ht(app_mod, pve, monkeypatch):
    monkeypatch.setattr(app_mod, "pvesh", pve)

    def run(tasks):
        pve.tasks = tasks
        return app_mod.health_tasks()
    return run


def test_new_vm_with_console_timeouts_and_retried_shutdown_is_ok(ht):
    """What really happened to VM 188: created, console timed out twice, shutdown timed out, started again."""
    d = ht([task(0, "qmcreate"), task(24, "qmstart"), task(25, "vncproxy", "connection timed out", node="pve3"),
            task(48, "qmshutdown", "VM quit/powerdown failed - got timeout"), task(108, "qmconfig"),
            task(184, "qmstart"), task(186, "vncproxy", "connection timed out", node="pve3")])
    assert d["ok"] is True and d["count"] == 0
    assert (d["resolved"], d["ignored"]) == (1, 2)


def test_unresolved_failures_still_alarm(ht):
    d = ht([task(0, "vzdump", "job errors", vmid="131"), task(10, "qmigrate", "migration aborted", vmid="141"),
            task(20, "qmshutdown", "got timeout", vmid="150")])
    assert d["ok"] is False and d["count"] == 3
    assert [f["id"] for f in d["failed"]] == ["150", "141", "131"]          # newest first


def test_retried_backup_or_migration_settles_the_failure(ht):
    d = ht([task(0, "vzdump", "job errors", vmid="131"), task(600, "vzdump", vmid="131"),
            task(10, "qmigrate", "aborted", vmid="141"), task(900, "qmigrate", vmid="141")])
    assert d["ok"] is True and d["resolved"] == 2


def test_success_must_come_after_the_failure_and_match_the_vm(ht):
    d = ht([task(0, "qmstart", vmid="160"), task(10, "qmshutdown", "got timeout", vmid="160"),
            task(20, "qmstart", vmid="161"), task(30, "vzdump", vmid="160")])
    assert d["ok"] is False and [f["id"] for f in d["failed"]] == ["160"]


def test_other_task_types_need_their_own_success(ht):
    d = ht([task(0, "qmigrate", "aborted", vmid="170"), task(50, "qmstart", vmid="170")])
    assert d["ok"] is False and d["failed"][0]["type"] == "qmigrate"


def test_old_failures_are_out_of_the_window(ht):
    d = ht([{**task(0, "vzdump", "job errors"), "starttime": int(time.time()) - 30 * 3600}])
    assert d["ok"] is True and d["count"] == 0
