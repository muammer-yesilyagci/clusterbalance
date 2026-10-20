"""Disk image import: uploads, path safety, image checks, platform detection, VM creation and jobs."""
import base64
import json
import os
import time

import pytest
import yaml
from werkzeug.security import generate_password_hash

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()}
_real_sleep = time.sleep


class FakeHost:
    """Answers the importer's own pvesh / qemu-img calls."""

    def __init__(self):
        self.qemu_info = {}          # file name -> qemu-img info JSON
        self.created = []            # pvesh create argument lists
        self.ha_added = []           # pvesh create /cluster/ha/resources argument lists
        self.ha_fail = False
        self.resized = []
        self.started = []
        self.create_rc = 0
        self.task_exit = "OK"
        self.task_log = [{"n": 1, "t": "importing disk 'x' to VM 503 ..."},
                         {"n": 2, "t": "transferred 5.0 GiB of 10.0 GiB (50.00%)"}]
        self.ova_meta = {"create-args": {"name": "app-server", "cores": 4, "memory": 8192, "ostype": "l26",
                                         "bios": "ovmf"},
                         "disks": {"scsi0": "nfs-vms:import/appliance.ova/disk1.vmdk",
                                   "scsi1": "nfs-vms:import/appliance.ova/disk2.vmdk"},
                         "warnings": [{"type": "ova-needs-extracting"}]}

    def run(self, args, timeout=60):
        if args[:2] == ["qemu-img", "info"]:
            name = os.path.basename(args[-1])
            if name not in self.qemu_info:
                return 1, "", "Could not open"
            return 0, json.dumps(self.qemu_info[name]), ""
        if args[:3] == ["pvesh", "create", "/cluster/ha/resources"]:
            self.ha_added.append(args[1:])
            return (1, "", "resource 'vm:503' already defined") if self.ha_fail else (0, "", "")
        if args[:2] == ["pvesh", "set"] and args[2].endswith("/resize"):
            self.resized.append(args[1:])
            return 0, "", ""
        if args[:2] == ["pvesh", "create"] and args[2].endswith("/status/start"):
            self.started.append(args[1:])
            return 0, "", ""
        if args[:2] == ["pvesh", "create"]:
            self.created.append(args[1:])
            return self.create_rc, "UPID:pve2:00001:0:0:qmcreate:503:root@pam:\n", "" if not self.create_rc else "boom"
        raise AssertionError(f"unexpected command {args}")

    def pvesh_json(self, args, timeout=30):
        path = args[1]
        if path == "/cluster/ha/groups":
            return [{"group": "prefer-pve2"}, {"group": "pve3-only"}]
        if path == "/cluster/nextid":
            return "503"
        if path.endswith("/storage") and "--content" in args:
            return [{"storage": "local-lvm", "type": "lvmthin", "avail": 10 ** 12, "active": 1},
                    {"storage": "nfs-vms", "type": "dir", "avail": 5 * 10 ** 12, "active": 1, "shared": 1}]
        if path.endswith("/network"):
            return [{"iface": "vmbr0"}, {"iface": "vmbr1"}]
        if path.endswith("/content"):
            return [{"volid": "local:iso/virtio-win-0.1.262.iso"}, {"volid": "local:iso/debian-12.iso"}]
        if path.endswith("/import-metadata"):
            return self.ova_meta
        if path.endswith("/status"):
            return {"status": "stopped", "exitstatus": self.task_exit}
        if path.endswith("/log"):
            return self.task_log
        raise AssertionError(f"unexpected pvesh {args}")


@pytest.fixture
def imp(app_mod):
    return app_mod.importer


@pytest.fixture
def host(imp, monkeypatch):
    h = FakeHost()
    monkeypatch.setattr(imp, "_run", h.run)
    monkeypatch.setattr(imp, "_pvesh_json", h.pvesh_json)
    monkeypatch.setattr(imp.time, "sleep", lambda s: _real_sleep(0.005))
    return h


@pytest.fixture
def share(tmp_path):
    d = tmp_path / "nfs-vms"
    (d / "import").mkdir(parents=True)
    return d


@pytest.fixture
def client(app_mod, imp, pve, host, share, tmp_path, monkeypatch):
    users = tmp_path / "users"
    users.write_text("admin:" + generate_password_hash("secret") + "\n")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"enabled": True, "dry_run": True, "maintenance_nodes": ["pve1"]}))
    monkeypatch.setattr(app_mod, "USERS_FILE", str(users))
    monkeypatch.setattr(app_mod, "CB_CONFIG_FILE", str(cfg))
    monkeypatch.setattr(app_mod, "pvesh", pve)
    monkeypatch.setattr(app_mod.socket, "gethostname", lambda: "pve1")
    local = tmp_path / "local-imp"
    (local / "import").mkdir(parents=True)
    pve.storage.append({"storage": "local-imp", "type": "dir", "content": "import", "path": str(local)})
    paths = {"nfs-vms": str(share), "local-imp": str(local)}
    monkeypatch.setattr(imp, "_storage_path", lambda s: paths.get(s["storage"]))
    monkeypatch.setattr(imp, "JOBS_FILE", str(tmp_path / "jobs.json"))
    imp._jobs.clear()
    app_mod._cache.clear()
    c = app_mod.app.test_client()
    c.share = share
    c.local = local
    return c


def upload(client, data, name="srv01.vhdx", storage="nfs-vms", chunk=4):
    r = client.post("/api/import/upload", json={"storage": storage, "file": name, "size": len(data)}, headers=AUTH)
    assert r.status_code == 200, r.json
    off = r.json["offset"]
    while off < len(data):
        part = data[off:off + chunk]
        r = client.put(f"/api/import/upload?storage={storage}&file={name}&offset={off}&size={len(data)}",
                       data=part, headers=AUTH)
        assert r.status_code == 200, r.json
        off = r.json["offset"]
    return r


def gpt_image(windows=True):
    head = bytearray(34 * 512)
    head[446 + 4] = 0xEE
    head[510:512] = b"\x55\xaa"
    head[512:520] = b"EFI PART"
    esp = bytes.fromhex("28732ac11ff8d211ba4b00a0c93ec93b")
    data = bytes.fromhex("a2a0d0ebe5b9334487c068b6b72699c7" if windows else "af3dc60f838472478e793d69d8477de4")
    head[1024:1040] = esp
    head[1152:1168] = data
    return bytes(head)


def mbr_image(ptype):
    head = bytearray(4096)
    head[446 + 4] = ptype
    head[510:512] = b"\x55\xaa"
    return bytes(head)


# ---------------------------------------------------------------- storages & paths

def test_requires_login(client):
    assert client.get("/api/import/files").status_code == 401


def test_lists_only_import_storages_reachable_here(client):
    st = client.get("/api/import/storages", headers=AUTH).json["storages"]
    assert {s["storage"]: s["shared"] for s in st} == {"nfs-vms": True, "local-imp": False}


@pytest.mark.parametrize("name", ["../etc/passwd.vhd", "a/b.vhd", ".hidden.vhd", "disk.exe", "x,import-from=y.vhd",
                                  "disk.vhd;rm", ""])
def test_rejects_unsafe_file_names(client, name):
    r = client.post("/api/import/upload", json={"storage": "nfs-vms", "file": name, "size": 10}, headers=AUTH)
    assert r.status_code == 400
    assert r.json["error"] in ("filename", "extension")


def test_unknown_storage_rejected(client):
    r = client.post("/api/import/upload", json={"storage": "local-lvm", "file": "a.vhd", "size": 10}, headers=AUTH)
    assert r.json["error"] == "storage"


# ---------------------------------------------------------------- uploads

def test_chunked_upload_and_listing(client):
    data = os.urandom(19)
    r = upload(client, data)
    assert r.json["done"] is True
    assert (client.share / "import" / "srv01.vhdx").read_bytes() == data
    files = client.get("/api/import/files", headers=AUTH).json["files"]
    assert [(f["file"], f["size"], f["partial"]) for f in files] == [("srv01.vhdx", 19, False)]


def test_upload_resumes_from_partial_file(client):
    (client.share / "import" / "srv01.vhdx.cbpart").write_bytes(b"0123456789")
    r = client.post("/api/import/upload", json={"storage": "nfs-vms", "file": "srv01.vhdx", "size": 15}, headers=AUTH)
    assert r.json["offset"] == 10
    files = client.get("/api/import/files", headers=AUTH).json["files"]
    assert files[0]["partial"] is True
    r = client.put("/api/import/upload?storage=nfs-vms&file=srv01.vhdx&offset=10&size=15", data=b"abcde", headers=AUTH)
    assert r.json == {"success": True, "offset": 15, "done": True}
    assert (client.share / "import" / "srv01.vhdx").read_bytes() == b"0123456789abcde"


def test_wrong_offset_reports_current_offset(client):
    (client.share / "import" / "a.vhd.cbpart").write_bytes(b"12345")
    r = client.put("/api/import/upload?storage=nfs-vms&file=a.vhd&offset=0&size=10", data=b"xx", headers=AUTH)
    assert r.status_code == 409 and r.json["offset"] == 5


def test_chunk_cannot_exceed_declared_size(client):
    (client.share / "import" / "a.vhd.cbpart").write_bytes(b"")
    r = client.put("/api/import/upload?storage=nfs-vms&file=a.vhd&offset=0&size=3", data=b"toolong", headers=AUTH)
    assert r.status_code == 400 and r.json["error"] == "size"


def test_existing_file_not_overwritten(client):
    (client.share / "import" / "a.vhd").write_bytes(b"keep")
    r = client.post("/api/import/upload", json={"storage": "nfs-vms", "file": "a.vhd", "size": 4}, headers=AUTH)
    assert r.status_code == 409
    assert (client.share / "import" / "a.vhd").read_bytes() == b"keep"


def test_not_enough_space(client, imp, monkeypatch):
    monkeypatch.setattr(imp, "_disk_usage", lambda p: (imp.FREE_MARGIN + 100, 10 ** 12))
    r = client.post("/api/import/upload", json={"storage": "nfs-vms", "file": "a.vhd", "size": 101}, headers=AUTH)
    assert r.status_code == 507 and r.json["error"] == "space"


def test_size_limit(client, imp, monkeypatch):
    monkeypatch.setattr(imp, "MAX_BYTES", 100)
    r = client.post("/api/import/upload", json={"storage": "nfs-vms", "file": "a.vhd", "size": 101}, headers=AUTH)
    assert r.json["error"] == "size"


def test_delete_file(client):
    (client.share / "import" / "a.vhd").write_bytes(b"x")
    assert client.delete("/api/import/file?storage=nfs-vms&file=a.vhd", headers=AUTH).json["success"]
    assert not (client.share / "import" / "a.vhd").exists()
    (client.share / "import" / "b.vhd.cbpart").write_bytes(b"x")
    assert client.delete("/api/import/file?storage=nfs-vms&file=b.vhd&partial=1", headers=AUTH).json["success"]


# ---------------------------------------------------------------- inspection & safety

def test_inspect_windows_uefi_raw(client, host):
    (client.share / "import" / "SRV-DC01_disk.raw").write_bytes(gpt_image(windows=True))
    host.qemu_info["SRV-DC01_disk.raw"] = {"format": "raw", "virtual-size": 64 * 1024 ** 3}
    r = client.get("/api/import/inspect?storage=nfs-vms&file=SRV-DC01_disk.raw", headers=AUTH).json
    assert (r["format"], r["firmware"], r["os"], r["suggested_name"]) == ("RAW", "uefi", "windows", "SRV-DC01")


@pytest.mark.parametrize("ptype,expected", [(0x07, ("bios", "windows")), (0x83, ("bios", "linux")),
                                            (0xEE, ("uefi", None)), (0x00, ("bios", None))])
def test_guess_platform_mbr(imp, ptype, expected):
    assert imp.guess_platform(mbr_image(ptype)) == expected


def test_guess_platform_gpt_linux(imp):
    assert imp.guess_platform(gpt_image(windows=False)) == ("uefi", "linux")
    assert imp.guess_platform(b"\0" * 2048) == (None, None)


@pytest.mark.parametrize("info,code", [
    ({"format": "qcow2", "backing-filename": "/etc/shadow"}, "backing"),
    ({"format": "qcow2", "format-specific": {"type": "qcow2", "data": {"data-file": "/dev/sda"}}}, "data_file"),
    ({"format": "vmdk", "format-specific": {"type": "vmdk", "data": {"extents": [{"filename": "/root/secret"}]}}}, "extent"),
    ({"format": "luks"}, "format"),
])
def test_unsafe_images_rejected(client, host, info, code):
    (client.share / "import" / "evil.qcow2").write_bytes(b"x")
    host.qemu_info["evil.qcow2"] = info
    r = client.get("/api/import/inspect?storage=nfs-vms&file=evil.qcow2", headers=AUTH)
    assert r.status_code == 400 and r.json["error"] == code


def test_vmdk_extent_next_to_descriptor_is_fine(client, host, imp):
    path = client.share / "import" / "web.vmdk"
    path.write_bytes(b"x")
    info = {"format": "vmdk", "format-specific": {"type": "vmdk", "data": {"extents": [{"filename": str(path)}]}}}
    assert imp.check_image(str(path), info) == "vmdk"


def test_inspect_ova_uses_proxmox_metadata(client):
    (client.share / "import" / "appliance.ova").write_bytes(b"x")
    r = client.get("/api/import/inspect?storage=nfs-vms&file=appliance.ova", headers=AUTH).json
    assert r["format"] == "OVA" and r["firmware"] == "uefi" and r["os"] == "linux"
    assert r["suggested_name"] == "app-server" and len(r["disks"]) == 2


@pytest.mark.parametrize("fname,name", [("SRV-DC01.vhdx", "SRV-DC01"), ("my server_disk1.vmdk", "my-server"),
                                        ("web-flat.vmdk", "web"), ("Ubuntu 22.04 (copy).qcow2", "Ubuntu-22.04-copy"),
                                        ("___.img", "imported-vm")])
def test_suggest_vm_name(imp, fname, name):
    assert imp.suggest_vm_name(fname) == name


# ---------------------------------------------------------------- wizard options

def test_options_recommend_least_loaded_node_outside_maintenance(client):
    r = client.get("/api/import/options?memory_mb=4096", headers=AUTH).json
    assert r["recommended"] == "pve3"           # pve1 is in maintenance, pve3 has the most free memory
    assert r["nextid"] == 503 and r["bridges"] == ["vmbr0", "vmbr1"]
    assert r["storages"][0]["storage"] == "nfs-vms"   # shared storages first
    assert r["virtio_isos"] == ["local:iso/virtio-win-0.1.262.iso"]


# ---------------------------------------------------------------- creating the VM

def wizard(**over):
    d = {"storage": "nfs-vms", "file": "SRV-DC01.vhdx", "vmid": 503, "name": "SRV-DC01", "node": "pve2",
         "target_storage": "nfs-vms", "cores": 4, "memory_mb": 8192, "ostype": "win10", "bios": "ovmf", "tpm": False,
         "bus": "sata", "nic_model": "e1000", "bridge": "vmbr0", "vlan": "", "virtio_iso": "", "start": False,
         "delete_source": False}
    d.update(over)
    return d


@pytest.fixture
def vhdx(client, host):
    (client.share / "import" / "SRV-DC01.vhdx").write_bytes(b"x")
    host.qemu_info["SRV-DC01.vhdx"] = {"format": "vhdx", "virtual-size": 10 ** 10}
    return client.share / "import" / "SRV-DC01.vhdx"


@pytest.mark.parametrize("over,code", [
    ({"vmid": 102}, "vmid_taken"), ({"vmid": 5}, "field"), ({"name": "bad name!"}, "vm_name"),
    ({"node": "pve9"}, "node"), ({"target_storage": "pbs"}, "target_storage"), ({"bridge": "eth0"}, "field"),
    ({"bus": "nvme"}, "field"), ({"ostype": "dos"}, "field"), ({"cores": 0}, "field"), ({"vlan": 5000}, "field"),
    ({"virtio_iso": "local:iso/debian-12.iso"}, "field"), ({"file": "nope.vhdx"}, "missing"),
])
def test_start_validation(client, vhdx, over, code):
    r = client.post("/api/import/start", json=wizard(**over), headers=AUTH)
    assert r.status_code in (400, 404, 409) and r.json["error"] == code


def test_local_import_storage_only_on_its_node(client, host):
    (client.local / "import" / "a.vhd").write_bytes(b"x")
    host.qemu_info["a.vhd"] = {"format": "vpc"}
    r = client.post("/api/import/start", json=wizard(storage="local-imp", file="a.vhd"), headers=AUTH)
    assert r.json["error"] == "node_local" and r.json["node"] == "pve1"


def test_build_args_windows_uefi(imp):
    spec = {"node": "pve2", "vmid": 503, "name": "SRV-DC01", "cores": 4, "memory_mb": 8192, "ostype": "win11",
            "bios": "ovmf", "tpm": True, "bus": "sata", "target_storage": "nfs-vms", "storage": "nfs-vms",
            "file": "SRV-DC01.vhdx", "sources": ["/mnt/nfs-vms/import/SRV-DC01.vhdx"], "nic_model": "e1000",
            "bridge": "vmbr0", "vlan": 20, "virtio_iso": "local:iso/virtio-win.iso", "start": True}
    a = imp.build_create_args(spec)
    s = " ".join(a)
    assert a[:2] == ["create", "/nodes/pve2/qemu"]
    assert "--sata0 nfs-vms:0,import-from=/mnt/nfs-vms/import/SRV-DC01.vhdx" in s
    assert "--efidisk0 nfs-vms:1,efitype=4m,pre-enrolled-keys=0" in s and "--tpmstate0 nfs-vms:1,version=v2.0" in s
    assert "--net0 e1000,bridge=vmbr0,tag=20" in s and "--sata5 local:iso/virtio-win.iso,media=cdrom" in s
    assert "--boot order=sata0" in s and "--start 1" in s and "--bios ovmf" in s


def test_build_args_ova_multi_disk_bios(imp):
    spec = {"node": "pve3", "vmid": 600, "name": "app", "cores": 2, "memory_mb": 2048, "ostype": "l26",
            "bios": "seabios", "tpm": False, "bus": "scsi", "target_storage": "local-lvm", "storage": "nfs-vms",
            "file": "a.ova", "sources": ["nfs-vms:import/a.ova/d1.vmdk", "nfs-vms:import/a.ova/d2.vmdk"],
            "nic_model": "virtio", "bridge": "vmbr1", "vlan": None, "virtio_iso": None, "start": False}
    s = " ".join(imp.build_create_args(spec))
    assert "--scsi0 local-lvm:0,import-from=nfs-vms:import/a.ova/d1.vmdk" in s
    assert "--scsi1 local-lvm:0,import-from=nfs-vms:import/a.ova/d2.vmdk" in s
    assert "efidisk0" not in s and "--start" not in s and "--net0 virtio,bridge=vmbr1" in s


def _wait_job(client, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        jobs = client.get("/api/import/jobs", headers=AUTH).json["jobs"]
        if jobs and jobs[0]["status"] != "running":
            return jobs[0]
        _real_sleep(0.02)
    raise AssertionError("job did not finish")


def test_import_job_success_and_delete_source(client, host, vhdx):
    r = client.post("/api/import/start", json=wizard(delete_source=True, start=True), headers=AUTH)
    assert r.status_code == 200
    job = _wait_job(client)
    assert job["status"] == "done" and job["progress"] == 100.0 and job["source_deleted"] is True
    assert job["node_ip"] and job["vmid"] == 503
    assert not vhdx.exists()
    args = host.created[0]
    assert "--sata0" in args and "nfs-vms:0,import-from=" + str(vhdx.resolve()) in args


def test_import_job_task_failure_keeps_source(client, host, vhdx):
    host.task_exit = "unable to create VM 503 - storage full"
    client.post("/api/import/start", json=wizard(delete_source=True), headers=AUTH)
    job = _wait_job(client)
    assert job["status"] == "failed" and "storage full" in job["error"]
    assert vhdx.exists()


def test_running_job_blocks_second_import_and_delete(client, host, vhdx, imp):
    imp._jobs["x"] = {"id": "x", "status": "running", "storage": "nfs-vms", "file": "SRV-DC01.vhdx",
                      "created": "2026-01-01 00:00:00", "node": "pve2"}
    assert client.post("/api/import/start", json=wizard(vmid=504), headers=AUTH).json["error"] == "in_use"
    assert client.delete("/api/import/file?storage=nfs-vms&file=SRV-DC01.vhdx", headers=AUTH).status_code == 409


def ova_wizard(**over):
    return wizard(file="appliance.ova", ostype="l26", bus="scsi", nic_model="virtio", **over)


def test_ova_import_uses_proxmox_volumes(client, host):
    (client.share / "import" / "appliance.ova").write_bytes(b"x")
    r = client.post("/api/import/start", json=ova_wizard(), headers=AUTH)
    assert r.status_code == 200
    _wait_job(client)
    s = " ".join(host.created[0])
    assert "--scsi0 nfs-vms:0,import-from=nfs-vms:import/appliance.ova/disk1.vmdk" in s
    assert "--scsi1 nfs-vms:0,import-from=nfs-vms:import/appliance.ova/disk2.vmdk" in s
    assert "--import-working-storage" not in s       # nfs-vms has the 'images' content type itself


def test_ova_needs_file_based_working_storage(client, host, pve):
    for st in pve.storage:
        if st["storage"] == "nfs-vms":
            st["content"] = "import,iso"
    (client.share / "import" / "appliance.ova").write_bytes(b"x")
    r = client.post("/api/import/start", json=ova_wizard(target_storage="local-lvm"), headers=AUTH)
    assert r.json["error"] == "ova_work"
    r = client.post("/api/import/start", json=ova_wizard(), headers=AUTH)
    assert r.status_code == 200
    _wait_job(client)
    assert "--import-working-storage nfs-vms" in " ".join(host.created[0])


def test_upload_never_overwrites_a_file_copied_meanwhile(client):
    imp_dir = client.share / "import"
    r = client.post("/api/import/upload", json={"storage": "nfs-vms", "file": "a.vhd", "size": 3}, headers=AUTH)
    assert r.json["offset"] == 0
    (imp_dir / "a.vhd").write_bytes(b"copied by hand")
    r = client.put("/api/import/upload?storage=nfs-vms&file=a.vhd&offset=0&size=3", data=b"abc", headers=AUTH)
    assert r.status_code == 409 and r.json["error"] == "exists"
    assert (imp_dir / "a.vhd").read_bytes() == b"copied by hand"


def test_options_list_ha_groups(client):
    assert client.get("/api/import/options", headers=AUTH).json["ha_groups"] == ["prefer-pve2", "pve3-only"]


@pytest.mark.parametrize("start,state", [(True, "started"), (False, "stopped")])
def test_import_adds_vm_to_ha(client, host, vhdx, start, state):
    client.post("/api/import/start", json=wizard(ha=True, ha_group="prefer-pve2", start=start), headers=AUTH)
    job = _wait_job(client)
    assert job["status"] == "done" and job["ha_added"] is True
    assert host.ha_added == [["create", "/cluster/ha/resources", "--sid", "vm:503", "--state", state, "--group", "prefer-pve2"]]


def test_no_ha_unless_asked(client, host, vhdx):
    client.post("/api/import/start", json=wizard(), headers=AUTH)
    job = _wait_job(client)
    assert job["status"] == "done" and "ha_added" not in job and host.ha_added == []


def test_ha_failure_keeps_the_vm_and_reports(client, host, vhdx):
    host.ha_fail = True
    client.post("/api/import/start", json=wizard(ha=True), headers=AUTH)
    job = _wait_job(client)
    assert job["status"] == "done" and job["ha_added"] is False and "already defined" in job["ha_error"]


@pytest.mark.parametrize("over", [{"ha": True, "ha_group": "nope"}, {"ha": False, "ha_group": "prefer-pve2"}])
def test_invalid_ha_group_rejected(client, vhdx, over):
    r = client.post("/api/import/start", json=wizard(**over), headers=AUTH)
    assert r.status_code == 400 and r.json == {"success": False, "error": "field", "field": "ha_group"}


def test_disk_resize_then_start_without_ha(client, host, vhdx):
    host.qemu_info["SRV-DC01.vhdx"]["virtual-size"] = 80 * 1024 ** 3
    client.post("/api/import/start", json=wizard(disk_gb=120, start=True), headers=AUTH)
    job = _wait_job(client)
    assert job["status"] == "done" and job["resized_gb"] == 120
    assert "--start" not in host.created[0]                       # started only after the resize
    assert host.resized == [["set", "/nodes/pve2/qemu/503/resize", "--disk", "sata0", "--size", "120G"]]
    assert host.started == [["create", "/nodes/pve2/qemu/503/status/start"]]


def test_disk_resize_with_ha_lets_ha_start_it(client, host, vhdx):
    host.qemu_info["SRV-DC01.vhdx"]["virtual-size"] = 80 * 1024 ** 3
    client.post("/api/import/start", json=wizard(disk_gb=100, start=True, ha=True), headers=AUTH)
    job = _wait_job(client)
    assert job["resized_gb"] == 100 and job["ha_added"] is True
    assert host.ha_added[0][-1] == "started" and host.started == []


@pytest.mark.parametrize("gb", [80, "", None])
def test_same_size_or_empty_means_no_resize(client, host, vhdx, gb):
    host.qemu_info["SRV-DC01.vhdx"]["virtual-size"] = 80 * 1024 ** 3
    client.post("/api/import/start", json=wizard(disk_gb=gb, start=True), headers=AUTH)
    _wait_job(client)
    assert host.resized == [] and "--start" in host.created[0]


def test_disk_cannot_shrink(client, host, vhdx):
    host.qemu_info["SRV-DC01.vhdx"]["virtual-size"] = 80 * 1024 ** 3
    r = client.post("/api/import/start", json=wizard(disk_gb=40), headers=AUTH)
    assert r.status_code == 400 and r.json["field"] == "disk_gb"
