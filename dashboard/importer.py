"""Disk image import: turn a .vhd / .vhdx / .vmdk / .qcow2 / .raw / .img / .ova file into a Proxmox VM.

Files live in the `import/` folder of a Proxmox storage that has the "import" content type
(Datacenter > Storage > Edit > Content: Import). They get there either by drag & drop in the
dashboard (chunked, resumable upload) or by copying them onto the share yourself.

The VM is created with `pvesh create /nodes/<node>/qemu ... --sata0 <storage>:0,import-from=<file>`,
so Proxmox itself converts the image (qemu-img) and shows the job in its task log.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request

bp = Blueprint("importer", __name__)
A = None  # the dashboard module: pvesh, get_res, get_nodes, load_cb_config, _audit (set by attach())

DISK_EXT = (".vhd", ".vhdx", ".vmdk", ".qcow2", ".raw", ".img")
OVA_EXT = (".ova",)
ALL_EXT = DISK_EXT + OVA_EXT
PART_SUFFIX = ".cbpart"
# File names end up inside a Proxmox disk option string ("...,import-from=<path>"), so no commas,
# equals signs, slashes or leading dots.
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ ()+-]{0,150}$")
VM_NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,61}[A-Za-z0-9])?$")
IMPORT_TYPES = ("dir", "nfs", "cifs", "glusterfs", "cephfs")
FORMATS = {"vpc": "VHD", "vhdx": "VHDX", "vmdk": "VMDK", "qcow2": "QCOW2", "raw": "RAW"}
OSTYPES = ("win11", "win10", "win8", "win7", "w2k8", "l26", "other")
BUSES = ("sata", "scsi", "virtio", "ide")
NIC_MODELS = ("e1000", "virtio", "vmxnet3", "rtl8139")

CHUNK_MAX = 64 * 1024 * 1024
MAX_BYTES = int(float(os.environ.get("CB_IMPORT_MAX_GB", "4096")) * 1024 ** 3)
FREE_MARGIN = 2 * 1024 ** 3
JOBS_FILE = os.environ.get("CB_IMPORT_JOBS", "/var/lib/clusterbalance/import-jobs.json")
KEEP_JOBS = 50

_upload_locks = {}
_jobs = {}
_jobs_lock = threading.Lock()


class ImportError_(Exception):
    """A user-facing problem. `code` maps to an `imp.err.<code>` text in the dashboard."""

    def __init__(self, code, status=400, **info):
        super().__init__(code)
        self.code, self.status, self.info = code, status, info


@bp.errorhandler(ImportError_)
def _handle(e):
    return jsonify({"success": False, "error": e.code, **e.info}), e.status


def attach(app, dashboard_module):
    global A
    A = dashboard_module
    app.register_blueprint(bp)
    _load_jobs()


# ------------------------------------------------------------------ helpers

def _run(args, timeout=60):
    """Run a command without a shell. Returns (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return -1, "", str(e)


def _pvesh_json(args, timeout=30):
    """pvesh with an argument list (values may contain spaces), JSON result or None."""
    rc, out, _ = _run(["pvesh"] + list(args) + ["--output-format", "json"], timeout)
    if rc != 0:
        return None
    try:
        return json.loads(out) if out.strip() else None
    except ValueError:
        return None


def _storage_path(s):
    if s.get("path"):
        return s["path"]
    if s.get("type") in ("nfs", "cifs", "glusterfs", "cephfs"):
        return f"/mnt/pve/{s['storage']}"
    return None


def _disk_usage(path):
    try:
        u = shutil.disk_usage(path)
        return u.free, u.total
    except OSError:
        return None, None


def import_storages():
    """Storages with the 'import' content type that are reachable from the dashboard node."""
    host = socket.gethostname()
    out = []
    for s in A.pvesh("get /storage") or []:
        if s.get("disable") or s.get("type") not in IMPORT_TYPES:
            continue
        if "import" not in str(s.get("content") or "").split(","):
            continue
        nodes = [n for n in str(s.get("nodes") or "").split(",") if n] or None
        if nodes and host not in nodes:
            continue
        path = _storage_path(s)
        if not path or not os.path.isdir(path):
            continue
        free, total = _disk_usage(path)
        shared = bool(s.get("shared")) or s["type"] != "dir"
        out.append({"storage": s["storage"], "dir": os.path.join(path, "import"), "shared": shared,
                    "nodes": nodes, "free": free, "total": total,
                    "images": "images" in str(s.get("content") or "").split(",")})
    return out


def _import_storage(name):
    for s in import_storages():
        if s["storage"] == name:
            return s
    raise ImportError_("storage")


def _safe_path(storage, filename, partial=False):
    """Absolute path of a file in the storage's import folder; rejects anything that could escape it."""
    if not isinstance(filename, str) or not FILE_RE.match(filename) or ".." in filename:
        raise ImportError_("filename")
    if not filename.lower().endswith(ALL_EXT):
        raise ImportError_("extension")
    s = _import_storage(storage)
    base = os.path.realpath(s["dir"])
    path = os.path.join(base, filename + (PART_SUFFIX if partial else ""))
    if os.path.dirname(os.path.realpath(path)) != base:
        raise ImportError_("filename")
    return s, path


def _kind(filename):
    return "ova" if filename.lower().endswith(OVA_EXT) else "disk"


# ------------------------------------------------------------------ image inspection

def qemu_info(path):
    rc, out, err = _run(["qemu-img", "info", "-U", "--output=json", path], timeout=120)
    if rc != 0:
        raise ImportError_("unreadable", detail=(err or out).strip()[-300:])
    try:
        return json.loads(out)
    except ValueError:
        raise ImportError_("unreadable")


def check_image(path, info):
    """Refuse images that point at other files on the host (backing files, external data, foreign extents)."""
    fmt = info.get("format")
    if fmt not in FORMATS:
        raise ImportError_("format", format=fmt)
    if info.get("backing-filename") or info.get("full-backing-filename"):
        raise ImportError_("backing")
    spec = info.get("format-specific") or {}
    data = spec.get("data") or {}
    if data.get("data-file"):
        raise ImportError_("data_file")
    if spec.get("type") == "vmdk":
        base = os.path.dirname(os.path.realpath(path))
        for ext in data.get("extents") or []:
            fn = ext.get("filename") or ""
            real = os.path.realpath(fn if os.path.isabs(fn) else os.path.join(base, fn))
            if os.path.dirname(real) != base:
                raise ImportError_("extent")
    return fmt


_MS_GUIDS = {"a2a0d0eb-e5b9-3344-87c0-68b6b72699c7", "16e3c9e3-5c0b-b84d-817d-f92df00215ae"}  # basic data, MSR
_LINUX_GUIDS = {"af3dc60f-8384-7247-8e79-3d69d8477de4", "79d3d6e6-07f5-c244-a23c-238f2a3df928",
                "6dfd5706-aba4-c443-84e5-0933c84b4f4f"}  # linux fs, LVM, swap (on-disk byte order)
_ESP_GUID = "28732ac1-1ff8-d211-ba4b-00a0c93ec93b"


def _guid(b):
    h = b.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _head_bytes(path, fmt, size=34 * 512):
    """First sectors of the virtual disk (MBR + GPT header + partition entries)."""
    if fmt == "raw":
        with open(path, "rb") as f:
            return f.read(size)
    with tempfile.TemporaryDirectory(prefix="cb-import-") as td:
        out = os.path.join(td, "head.raw")
        rc, _, _ = _run(["qemu-img", "dd", "-U", "-f", fmt, "-O", "raw", "bs=512", f"count={size // 512}",
                         f"if={path}", f"of={out}"], timeout=60)
        if rc != 0 or not os.path.exists(out):
            return b""
        with open(out, "rb") as f:
            return f.read(size)


def guess_platform(head):
    """(firmware, os) guessed from the partition table: ('uefi'|'bios'|None, 'windows'|'linux'|None)."""
    if len(head) < 1024:
        return None, None
    if head[512:520] == b"EFI PART":
        guids = set()
        for i in range(128):
            off = 1024 + i * 128
            if off + 16 > len(head):
                break
            g = head[off:off + 16]
            if any(g):
                guids.add(_guid(g))
        osg = "windows" if guids & _MS_GUIDS else "linux" if guids & _LINUX_GUIDS else None
        return ("uefi" if _ESP_GUID in guids or osg == "windows" else "bios"), osg
    if head[510:512] == b"\x55\xaa":
        types = {head[446 + i * 16 + 4] for i in range(4)}
        if 0xEE in types:
            return "uefi", None
        osg = "windows" if types & {0x07, 0x27} else "linux" if types & {0x83, 0x8E, 0x82} else None
        return "bios", osg
    return None, None


def suggest_vm_name(filename):
    base = re.sub(r"\.(ova|vhdx?|vmdk|qcow2|raw|img)$", "", filename, flags=re.I)
    base = re.sub(r"[-_ ]?(disk|flat|s0\d+)[-_ ]?\d*$", "", base, flags=re.I) or base
    name = re.sub(r"[^A-Za-z0-9.-]+", "-", base).strip("-.")[:63].strip("-.")
    return name or "imported-vm"


def inspect_file(storage, filename):
    s, path = _safe_path(storage, filename)
    if not os.path.isfile(path):
        raise ImportError_("missing", 404)
    out = {"storage": storage, "file": filename, "kind": _kind(filename), "size": os.path.getsize(path),
           "shared": s["shared"], "suggested_name": suggest_vm_name(filename), "warnings": []}
    if out["kind"] == "ova":
        host = socket.gethostname()
        meta = _pvesh_json(["get", f"/nodes/{host}/storage/{storage}/import-metadata",
                            "--volume", f"{storage}:import/{filename}"], timeout=120)
        if not meta or not meta.get("disks"):
            raise ImportError_("ova")
        args = meta.get("create-args") or {}
        out.update({"format": "OVA", "disks": [{"key": k, "volid": v} for k, v in sorted(meta["disks"].items())],
                    "firmware": "uefi" if args.get("bios") == "ovmf" else "bios",
                    "os": "windows" if str(args.get("ostype", "")).startswith("w") else
                          "linux" if str(args.get("ostype", "")).startswith("l") else None,
                    "cores": args.get("cores"), "memory_mb": args.get("memory"),
                    "ostype": args.get("ostype"),
                    "warnings": [w.get("type", str(w)) if isinstance(w, dict) else str(w) for w in meta.get("warnings") or []]})
        if args.get("name") and VM_NAME_RE.match(str(args["name"])):
            out["suggested_name"] = args["name"]
        return out
    info = qemu_info(path)
    fmt = check_image(path, info)
    firmware, osg = guess_platform(_head_bytes(path, fmt))
    out.update({"format": FORMATS[fmt], "qemu_format": fmt, "virtual_size": info.get("virtual-size"),
                "firmware": firmware, "os": osg})
    if firmware is None:
        out["warnings"].append("no_partition_table")
    return out


# ------------------------------------------------------------------ uploads

def _lock_for(path):
    return _upload_locks.setdefault(path, threading.Lock())


def _in_use(storage, filename):
    """Is a running import job reading this file?"""
    with _jobs_lock:
        return any(j["status"] == "running" and j["storage"] == storage and j["file"] == filename
                   for j in _jobs.values())


@bp.route("/api/import/storages")
def api_storages():
    return jsonify({"storages": import_storages(), "dashboard_node": socket.gethostname(),
                    "max_bytes": MAX_BYTES, "chunk_bytes": 32 * 1024 * 1024})


@bp.route("/api/import/files")
def api_files():
    files = []
    for s in import_storages():
        try:
            entries = list(os.scandir(s["dir"]))
        except OSError:
            continue
        for e in entries:
            if not e.is_file(follow_symlinks=False):
                continue
            name, partial = e.name, e.name.endswith(PART_SUFFIX)
            real = name[:-len(PART_SUFFIX)] if partial else name
            if not real.lower().endswith(ALL_EXT) or not FILE_RE.match(real):
                continue
            st = e.stat(follow_symlinks=False)
            files.append({"storage": s["storage"], "file": real, "size": st.st_size, "partial": partial,
                          "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                          "kind": _kind(real), "ext": real.rsplit(".", 1)[-1].lower(), "shared": s["shared"]})
    files.sort(key=lambda f: f["mtime"], reverse=True)
    return jsonify({"files": files})


@bp.route("/api/import/upload", methods=["POST"])
def api_upload_init():
    d = request.json or {}
    storage, filename = d.get("storage"), d.get("file")
    try:
        size = int(d.get("size"))
    except (TypeError, ValueError):
        raise ImportError_("size")
    if size <= 0 or size > MAX_BYTES:
        raise ImportError_("size", max_bytes=MAX_BYTES)
    s, path = _safe_path(storage, filename)
    if os.path.exists(path):
        raise ImportError_("exists", 409)
    os.makedirs(s["dir"], exist_ok=True)
    part = path + PART_SUFFIX
    offset = os.path.getsize(part) if os.path.exists(part) else 0
    if offset > size:
        raise ImportError_("size")
    free, _ = _disk_usage(s["dir"])
    if free is not None and free - FREE_MARGIN < size - offset:
        raise ImportError_("space", 507, free=free)
    if offset == 0:
        open(part, "ab").close()
        A._audit(f"import: yukleme basladi {storage}:{filename} ({size} bayt)")
    return jsonify({"success": True, "offset": offset})


@bp.route("/api/import/upload", methods=["PUT"])
def api_upload_chunk():
    storage, filename = request.args.get("storage"), request.args.get("file")
    try:
        offset, size = int(request.args.get("offset")), int(request.args.get("size"))
    except (TypeError, ValueError):
        raise ImportError_("size")
    length = request.content_length
    if length is None or length <= 0 or length > CHUNK_MAX:
        raise ImportError_("chunk", 413)
    _, path = _safe_path(storage, filename)
    part = path + PART_SUFFIX
    if not os.path.exists(part):
        raise ImportError_("no_upload", 404)
    lock = _lock_for(part)
    if not lock.acquire(blocking=False):
        raise ImportError_("busy", 409)
    try:
        current = os.path.getsize(part)
        if offset != current:
            raise ImportError_("offset", 409, offset=current)
        if offset + length > size or size > MAX_BYTES:
            raise ImportError_("size")
        written = 0
        with open(part, "ab") as f:
            while written < length:
                buf = request.stream.read(min(1024 * 1024, length - written))
                if not buf:
                    break
                f.write(buf)
                written += len(buf)
        if written != length:
            with open(part, "ab") as f:
                f.truncate(offset)
            raise ImportError_("incomplete", 400, offset=offset)
        done = offset + written == size
        if done:
            if os.path.exists(path):  # someone copied a file with the same name onto the share meanwhile
                raise ImportError_("exists", 409)
            os.replace(part, path)
            A._audit(f"import: yukleme tamamlandi {storage}:{filename}")
        return jsonify({"success": True, "offset": offset + written, "done": done})
    finally:
        lock.release()


@bp.route("/api/import/file", methods=["DELETE"])
def api_delete_file():
    storage, filename = request.args.get("storage"), request.args.get("file")
    partial = request.args.get("partial") == "1"
    _, path = _safe_path(storage, filename, partial=partial)
    if not os.path.isfile(path):
        raise ImportError_("missing", 404)
    if _in_use(storage, filename):
        raise ImportError_("in_use", 409)
    os.remove(path)
    A._audit(f"import: dosya silindi {storage}:{os.path.basename(path)}")
    return jsonify({"success": True})


@bp.route("/api/import/inspect")
def api_inspect():
    return jsonify(inspect_file(request.args.get("storage"), request.args.get("file")))


# ------------------------------------------------------------------ wizard options

def recommend_node(mem_bytes=0):
    """Least loaded online node (memory weighs double, like the balancer) that can hold the new VM."""
    cfg = A.load_cb_config()
    maint = set(cfg.get("maintenance_nodes") or [])
    nodes = [n for n in A.get_nodes() if n["name"] not in maint]
    fits = [n for n in nodes if n["maxmem"] - n["mem"] >= mem_bytes * 1.1] or nodes
    if not fits:
        return None
    return min(fits, key=lambda n: n["mem_percent"] * 2 + n["cpu"])["name"]


def node_options(node):
    storages = []
    for s in _pvesh_json(["get", f"/nodes/{node}/storage", "--content", "images", "--enabled", "1"]) or []:
        if s.get("active", 1):
            storages.append({"storage": s["storage"], "type": s.get("type"), "avail": s.get("avail"),
                             "shared": bool(s.get("shared"))})
    bridges = sorted(b["iface"] for b in _pvesh_json(["get", f"/nodes/{node}/network", "--type", "any_bridge"]) or []
                     if b.get("iface"))
    return {"storages": sorted(storages, key=lambda s: (not s["shared"], s["storage"])), "bridges": bridges}


def virtio_isos():
    host = socket.gethostname()
    out = []
    for s in A.pvesh("get /storage") or []:
        if s.get("disable") or "iso" not in str(s.get("content") or "").split(","):
            continue
        for c in _pvesh_json(["get", f"/nodes/{host}/storage/{s['storage']}/content", "--content", "iso"]) or []:
            if "virtio" in str(c.get("volid", "")).lower():
                out.append(c["volid"])
    return sorted(set(out))


@bp.route("/api/import/options")
def api_options():
    try:
        mem_mb = int(request.args.get("memory_mb", 0))
    except ValueError:
        mem_mb = 0
    nodes = [{"name": n["name"], "mem_percent": n["mem_percent"], "cpu": n["cpu"],
              "free_mem": n["maxmem"] - n["mem"], "maxcpu": n["maxcpu"]} for n in A.get_nodes()]
    rec = recommend_node(mem_mb * 1024 ** 2)
    node = request.args.get("node") or rec
    if node not in {n["name"] for n in nodes}:
        raise ImportError_("node")
    nextid = _pvesh_json(["get", "/cluster/nextid"])
    return jsonify({"nodes": nodes, "recommended": rec, "node": node, **node_options(node),
                    "nextid": int(nextid) if nextid else None, "virtio_isos": virtio_isos(),
                    "ostypes": OSTYPES, "buses": BUSES, "nic_models": NIC_MODELS, "ha_groups": ha_groups()})


def ha_groups():
    return sorted(g["group"] for g in _pvesh_json(["get", "/cluster/ha/groups"]) or [] if g.get("group"))


# ------------------------------------------------------------------ create the VM

def _int(d, key, lo, hi, default=None):
    v = d.get(key, default)
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ImportError_("field", field=key)
    if not lo <= v <= hi:
        raise ImportError_("field", field=key)
    return v


def validate_request(d):
    """Check every wizard field against the live cluster; returns a normalized spec."""
    storage, filename = d.get("storage"), d.get("file")
    src, path = _safe_path(storage, filename)
    if not os.path.isfile(path):
        raise ImportError_("missing", 404)
    if _in_use(storage, filename):
        raise ImportError_("in_use", 409)
    kind = _kind(filename)
    vmid = _int(d, "vmid", 100, 999999999)
    res = A.get_res()
    if any(r.get("vmid") == vmid for r in res):
        raise ImportError_("vmid_taken", 409, vmid=vmid)
    name = str(d.get("name") or "").strip()
    if not VM_NAME_RE.match(name):
        raise ImportError_("vm_name")
    online = {n["name"]: n for n in A.get_nodes()}
    node = d.get("node")
    if node not in online:
        raise ImportError_("node")
    if not src["shared"] and node != socket.gethostname():
        raise ImportError_("node_local", node=socket.gethostname())
    opts = node_options(node)
    target = d.get("target_storage")
    if target not in {s["storage"] for s in opts["storages"]}:
        raise ImportError_("target_storage")
    cores = _int(d, "cores", 1, max(1, online[node]["maxcpu"]))
    memory = _int(d, "memory_mb", 256, max(256, online[node]["maxmem"] // 1024 ** 2))
    ostype = d.get("ostype")
    if ostype not in OSTYPES:
        raise ImportError_("field", field="ostype")
    bios = d.get("bios")
    if bios not in ("seabios", "ovmf"):
        raise ImportError_("field", field="bios")
    bus = d.get("bus")
    if bus not in BUSES:
        raise ImportError_("field", field="bus")
    nic = d.get("nic_model")
    if nic not in NIC_MODELS:
        raise ImportError_("field", field="nic_model")
    bridge = d.get("bridge")
    if bridge not in opts["bridges"]:
        raise ImportError_("field", field="bridge")
    vlan = d.get("vlan")
    vlan = _int(d, "vlan", 1, 4094) if vlan not in (None, "", 0, "0") else None
    iso = d.get("virtio_iso") or None
    if iso and iso not in virtio_isos():
        raise ImportError_("field", field="virtio_iso")
    ha = bool(d.get("ha"))
    ha_group = d.get("ha_group") or None
    if ha_group and (not ha or ha_group not in ha_groups()):
        raise ImportError_("field", field="ha_group")
    work = None
    info = None
    if kind == "ova":
        meta = inspect_file(storage, filename)
        sources = [x["volid"] for x in meta["disks"]]
        if not src["images"]:
            # Proxmox unpacks OVA disks first; that needs a file-based storage with the 'images' content type
            tinfo = next(s for s in opts["storages"] if s["storage"] == target)
            if tinfo["type"] not in IMPORT_TYPES:
                raise ImportError_("ova_work")
            work = target
    else:
        info = qemu_info(path)
        check_image(path, info)
        sources = [path]
    return {"storage": storage, "file": filename, "path": path, "kind": kind, "sources": sources,
            "vmid": vmid, "name": name, "node": node, "target_storage": target, "cores": cores,
            "memory_mb": memory, "ostype": ostype, "bios": bios, "tpm": bool(d.get("tpm")) and bios == "ovmf",
            "bus": bus, "nic_model": nic, "bridge": bridge, "vlan": vlan, "virtio_iso": iso,
            "start": bool(d.get("start")), "delete_source": bool(d.get("delete_source")), "work_storage": work,
            "ha": ha, "ha_group": ha_group, "resize_gb": _resize_gb(d, info)}


def _resize_gb(d, info):
    """Requested size of the system disk in GiB when it is larger than the image; None = keep the image's size."""
    want = d.get("disk_gb")
    if want in (None, "", 0, "0"):
        return None
    if not info or not info.get("virtual-size"):
        raise ImportError_("field", field="disk_gb")      # OVA / unknown size: not supported
    current = -(-int(info["virtual-size"]) // 1024 ** 3)   # ceil to whole GiB
    gb = _int(d, "disk_gb", current, 64 * 1024)
    return gb if gb > current else None


def build_ha_args(spec):
    """Put the new VM under HA; 'started' only when it was started, so HA never powers on a VM the user left off."""
    args = ["create", "/cluster/ha/resources", "--sid", f"vm:{spec['vmid']}",
            "--state", "started" if spec["start"] else "stopped"]
    if spec.get("ha_group"):
        args += ["--group", spec["ha_group"]]
    return args


def build_create_args(spec):
    """pvesh argument list that creates the VM and imports its disk(s) in one Proxmox task."""
    t, bus = spec["target_storage"], spec["bus"]
    args = ["create", f"/nodes/{spec['node']}/qemu", "--vmid", str(spec["vmid"]), "--name", spec["name"],
            "--cores", str(spec["cores"]), "--sockets", "1", "--memory", str(spec["memory_mb"]),
            "--ostype", spec["ostype"], "--machine", "q35", "--scsihw", "virtio-scsi-single",
            "--bios", spec["bios"], "--tags", "imported",
            "--description", f"Imported by ClusterBalance from {spec['storage']}:import/{spec['file']} "
                             f"on {datetime.now():%Y-%m-%d %H:%M}"]
    for i, src in enumerate(spec["sources"]):
        args += [f"--{bus}{i}", f"{t}:0,import-from={src}"]
    args += ["--boot", f"order={bus}0"]
    if spec["bios"] == "ovmf":
        args += ["--efidisk0", f"{t}:1,efitype=4m,pre-enrolled-keys=0"]
        if spec["tpm"]:
            args += ["--tpmstate0", f"{t}:1,version=v2.0"]
    net = f"{spec['nic_model']},bridge={spec['bridge']}" + (f",tag={spec['vlan']}" if spec["vlan"] else "")
    args += ["--net0", net]
    if spec["virtio_iso"]:
        cd = "sata5" if bus == "sata" and len(spec["sources"]) <= 5 else "ide2" if bus != "ide" else "sata0"
        args += [f"--{cd}", f"{spec['virtio_iso']},media=cdrom"]
    if spec.get("work_storage"):
        args += ["--import-working-storage", spec["work_storage"]]
    if spec["start"] and not spec.get("resize_gb"):
        args += ["--start", "1"]
    return args


# ------------------------------------------------------------------ jobs

def _load_jobs():
    try:
        with open(JOBS_FILE) as f:
            for j in json.load(f):
                if j.get("status") == "running":
                    j["status"], j["error"] = "unknown", "dashboard_restarted"
                _jobs[j["id"]] = j
    except (OSError, ValueError):
        pass


def _save_jobs():
    try:
        os.makedirs(os.path.dirname(JOBS_FILE), exist_ok=True)
        keep = sorted(_jobs.values(), key=lambda j: j["created"], reverse=True)[:KEEP_JOBS]
        tmp = JOBS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(keep, f, indent=1)
        os.replace(tmp, JOBS_FILE)
    except OSError:
        pass


def _update(job, **kw):
    with _jobs_lock:
        job.update(kw)
        _save_jobs()


_PROGRESS_RE = re.compile(r"\((\d{1,3}(?:\.\d+)?)%\)")


def _find_upid(job):
    for t in A.pvesh("get /cluster/tasks") or []:
        if t.get("type") == "qmcreate" and str(t.get("id")) == str(job["vmid"]) and t.get("starttime", 0) >= job["started_ts"] - 5:
            return t.get("upid")
    return None


def _watch_task(job):
    """Follow the Proxmox task log for progress until the creating process ends."""
    seen = 0
    while job["status"] == "running":
        if not job.get("upid"):
            upid = _find_upid(job)
            if upid:
                _update(job, upid=upid)
        if job.get("upid"):
            log = _pvesh_json(["get", f"/nodes/{job['node']}/tasks/{job['upid']}/log", "--start", str(seen),
                               "--limit", "500"]) or []
            for line in log:
                seen = max(seen, int(line.get("n", seen)))
                text = str(line.get("t", ""))
                if "importing disk" in text.lower() or text.lower().startswith("transferred"):
                    job["last_line"] = text[-200:]
                m = _PROGRESS_RE.search(text)
                if m:
                    job["progress"] = float(m.group(1))
                if "importing disk" in text.lower():
                    job["disk_index"] = job.get("disk_index", 0) + 1
        time.sleep(2)


def _audit_bg(job, msg):
    """Audit line from the job thread (no request context there; same format as the dashboard's _audit)."""
    print(f"AUDIT {datetime.now():%Y-%m-%d %H:%M:%S} {job.get('user', '?')}: {msg}", flush=True)


_UPID_RE = re.compile(r"UPID:[^\s\"']+")


def _wait_task(job, upid, poll=3):
    """Proxied pvesh calls return as soon as the task starts; the task status is the real result."""
    while True:
        st = _pvesh_json(["get", f"/nodes/{job['node']}/tasks/{upid}/status"]) or {}
        if st.get("status") == "stopped":
            return st.get("exitstatus") == "OK", st.get("exitstatus") or ""
        time.sleep(poll)


def _run_job(job, spec):
    watcher = threading.Thread(target=_watch_task, args=(job,), daemon=True)
    watcher.start()
    rc, out, err = _run(["pvesh"] + build_create_args(spec), timeout=7 * 24 * 3600)
    text = (out + "\n" + err).strip()
    ok = rc == 0 and "TASK ERROR" not in text
    if ok:
        m = _UPID_RE.search(out)
        upid = job.get("upid") or (m.group(0) if m else None)
        if upid:
            _update(job, upid=upid)
            ok, exitstatus = _wait_task(job, upid)
            if not ok:
                text = exitstatus
    if ok:
        _audit_bg(job, f"import: VM {spec['vmid']} ({spec['name']}) {spec['node']} uzerinde olusturuldu "
                       f"<- {spec['storage']}:{spec['file']}")
        if spec.get("resize_gb"):
            rc_rs, out_rs, err_rs = _run(["pvesh", "set", f"/nodes/{spec['node']}/qemu/{spec['vmid']}/resize",
                                          "--disk", f"{spec['bus']}0", "--size", f"{spec['resize_gb']}G"], timeout=300)
            if rc_rs == 0:
                _update(job, resized_gb=spec["resize_gb"])
                _audit_bg(job, f"import: VM {spec['vmid']} diski {spec['resize_gb']} GB'a buyutuldu")
            else:
                _update(job, resize_error=((err_rs or out_rs).strip().splitlines() or ["?"])[-1][-200:])
        if spec.get("ha"):
            rc_ha, out_ha, err_ha = _run(["pvesh"] + build_ha_args(spec), timeout=60)
            if rc_ha == 0:
                _update(job, ha_added=True)
                _audit_bg(job, f"import: VM {spec['vmid']} HA'ya eklendi" + (f" (grup {spec['ha_group']})" if spec.get("ha_group") else ""))
            else:
                _update(job, ha_added=False, ha_error=((err_ha or out_ha).strip().splitlines() or ["?"])[-1][-200:])
        if spec["start"] and spec.get("resize_gb") and not job.get("ha_added"):
            # HA starts a VM added with state 'started' by itself; otherwise start it now that the disk is ready
            rc_st, out_st, err_st = _run(["pvesh", "create", f"/nodes/{spec['node']}/qemu/{spec['vmid']}/status/start"], timeout=120)
            if rc_st != 0:
                _update(job, start_error=((err_st or out_st).strip().splitlines() or ["?"])[-1][-200:])
        _update(job, status="done", progress=100.0, finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        if spec["delete_source"]:
            try:
                os.remove(spec["path"])
                _update(job, source_deleted=True)
            except OSError as e:
                _update(job, source_deleted=False, delete_error=str(e))
    else:
        lines = [l for l in text.splitlines() if l.strip()]
        _update(job, status="failed", error=(lines[-1] if lines else f"exit {rc}")[-300:],
                finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        _audit_bg(job, f"import: VM {spec['vmid']} olusturulamadi: {job['error']}")


@bp.route("/api/import/start", methods=["POST"])
def api_start():
    spec = validate_request(request.json or {})
    job = {"id": uuid.uuid4().hex[:12], "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "started_ts": time.time(), "status": "running", "progress": 0.0, "disk_count": len(spec["sources"]),
           "user": request.authorization.username if request.authorization else "?",
           **{k: spec[k] for k in ("storage", "file", "vmid", "name", "node", "target_storage", "start", "delete_source",
                                    "ha", "ha_group")}}
    with _jobs_lock:
        _jobs[job["id"]] = job
        _save_jobs()
    A._audit(f"import: VM {spec['vmid']} ({spec['name']}) olusturuluyor {spec['node']}/{spec['target_storage']} "
             f"<- {spec['storage']}:{spec['file']}")
    threading.Thread(target=_run_job, args=(job, spec), daemon=True).start()
    return jsonify({"success": True, "job": job})


@bp.route("/api/import/jobs")
def api_jobs():
    with _jobs_lock:
        jobs = sorted((dict(j) for j in _jobs.values()), key=lambda j: j["created"], reverse=True)
    ips = {x.get("name"): x.get("ip") for x in A.get_cluster_status() if x.get("type") == "node"}
    for j in jobs:
        j["node_ip"] = ips.get(j["node"])
    return jsonify({"jobs": jobs})
