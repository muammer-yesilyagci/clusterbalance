#!/usr/bin/env python3
"""ClusterBalance Dashboard — Proxmox VE load balancer web UI"""
from flask import Flask, render_template, jsonify, request, Response
from werkzeug.security import check_password_hash
import subprocess, json, time, yaml, os, re

# ============ PATHS (override with environment variables) ============
CB_DIR = os.environ.get("CB_DIR", "/opt/clusterbalance")
CB_CONFIG_FILE = os.environ.get("CB_CONFIG", f"{CB_DIR}/config.yaml")
CB_BALANCER = os.environ.get("CB_BALANCER", f"{CB_DIR}/balancer.py")
CB_STATE_FILE = os.environ.get("CB_STATE_FILE", f"{CB_DIR}/maintenance_state.json")
CB_LOG = os.environ.get("CB_LOG", "/var/log/clusterbalance.log")
USERS_FILE = os.environ.get("CB_USERS_FILE", "/etc/clusterbalance/dashboard-users")
DASHBOARD_PORT = int(os.environ.get("CB_DASHBOARD_PORT", "5000"))
from datetime import datetime

app = Flask(__name__)

# ============ AUTH ============
# Kullanicilar: USERS_FILE (her satir "kullanici:werkzeug-hash")
# Dosya yoksa veya bossa tum istekler reddedilir.

def load_users():
    users = {}
    try:
        with open(USERS_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and ":" in line:
                    u, h = line.split(":", 1)
                    users[u] = h
    except OSError:
        pass
    return users

PUBLIC_PATHS = {"/api/widget-summary"}  # Proxmox arayuzundeki ClusterBalance kutusu icin

@app.before_request
def require_auth():
    if request.path in PUBLIC_PATHS:
        return None
    auth = request.authorization
    users = load_users()
    if auth and auth.username in users and check_password_hash(users[auth.username], auth.password or ""):
        return None
    return Response("Giris gerekli", 401, {"WWW-Authenticate": 'Basic realm="Proxmox LB Dashboard"'})
_cache = {}

PVE_REPO_FILE = "/etc/apt/sources.list.d/pve-enterprise.list"
CEPH_REPO_FILE = "/etc/apt/sources.list.d/ceph.list"
NO_SUB_JS = "/usr/share/javascript/proxmox-widget-toolkit/proxmoxlib.js"

def cached(key, ttl=15):
    def dec(f):
        def w(*a,**k):
            now=time.time()
            if key in _cache and now-_cache[key]["t"]<ttl: return _cache[key]["v"]
            r=f(*a,**k); _cache[key]={"v":r,"t":now}; return r
        return w
    return dec

def pvesh(cmd):
    try:
        r=subprocess.run(f"pvesh {cmd} --output-format json",shell=True,capture_output=True,text=True,timeout=30)
        return json.loads(r.stdout) if r.returncode==0 else None
    except: return None

def run_cmd(cmd):
    try:
        r=subprocess.run(cmd,shell=True,capture_output=True,text=True,timeout=60)
        return {"success":r.returncode==0,"output":r.stdout,"error":r.stderr}
    except Exception as e:
        return {"success":False,"error":str(e)}

def fmt(b):
    for u in["B","KB","MB","GB","TB"]:
        if b<1024: return f"{b:.1f} {u}"
        b/=1024
    return f"{b:.1f} PB"

@cached("res")
def get_res(): return pvesh("get /cluster/resources") or []

def get_nodes():
    nodes=[]
    for r in get_res():
        if r.get("type")=="node" and r.get("status")=="online":
            nodes.append({"name":r["node"],"status":r["status"],"cpu":round(r.get("cpu",0)*100,1),"maxcpu":r.get("maxcpu",1),
                "mem":r.get("mem",0),"maxmem":r.get("maxmem",1),"mem_percent":round(r.get("mem",0)/r.get("maxmem",1)*100,1),
                "mem_gb":f"{r.get('mem',0)/1024**3:.1f}/{r.get('maxmem',0)/1024**3:.0f} GB",
                "disk_percent":round(r.get("disk",0)/r.get("maxdisk",1)*100,1),"uptime":r.get("uptime",0),
                "uptime_str":f"{r.get('uptime',0)//86400}d {(r.get('uptime',0)%86400)//3600}h","vm_count":0,"ct_count":0})
    for r in get_res():
        for n in nodes:
            if n["name"]==r["node"]:
                if r.get("type")=="qemu": n["vm_count"]+=1
                elif r.get("type")=="lxc": n["ct_count"]+=1
    return nodes

def get_guests():
    guests=[]
    for r in get_res():
        if r.get("type") in["qemu","lxc"]:
            m,mm=r.get("mem",0),r.get("maxmem",1)
            guests.append({"vmid":r["vmid"],"name":r.get("name",""),"node":r["node"],
                "type":"VM" if r["type"]=="qemu" else "CT","status":r.get("status",""),
                "cpu":round(r.get("cpu",0)*100,1),"cores":r.get("maxcpu",1),
                "mem_percent":round(m/max(mm,1)*100,1),"mem_str":f"{fmt(m)}/{fmt(mm)}",
                "disk_str":fmt(r.get("maxdisk",0)),"tags":r.get("tags","")})
    return sorted(guests,key=lambda x:(-1 if x["status"]=="running" else 1,x["vmid"]))

def get_summary():
    nodes,guests=get_nodes(),get_guests()
    if not nodes: return None
    running=[g for g in guests if g["status"]=="running"]
    mp=[n["mem_percent"] for n in nodes]; avg=sum(mp)/len(mp)
    var=sum((x-avg)**2 for x in mp)/len(mp) if len(nodes)>1 else 0
    return {"node_count":len(nodes),"vm_count":sum(1 for g in guests if g["type"]=="VM"),
        "ct_count":sum(1 for g in guests if g["type"]=="CT"),"total":len(guests),
        "running":len(running),"stopped":len(guests)-len(running),
        "avg_cpu":round(sum(n["cpu"] for n in nodes)/len(nodes),1),
        "avg_mem":round(sum(n["mem_percent"] for n in nodes)/len(nodes),1),
        "balance":round(max(0,100-var),1),"max_diff":round(max(mp)-min(mp),1) if mp else 0,
        "balanced":(max(mp)-min(mp))<15 if mp else True,"time":datetime.now().strftime("%H:%M:%S"),
        "running_vms":len(running),
        "top_cpu":sorted(running,key=lambda x:x["cpu"],reverse=True)[:5],
        "top_mem":sorted(running,key=lambda x:x["mem_percent"],reverse=True)[:5]}

def get_recs():
    nodes=get_nodes()
    if len(nodes)<2: return [{"type":"success","title":"OK","msg":"Cluster dengeli"}]
    recs=[]; avg=sum(n["mem_percent"] for n in nodes)/len(nodes)
    for n in nodes:
        d=n["mem_percent"]-avg
        if d>20: recs.append({"type":"danger","title":f"{n['name']} KRITIK","msg":f"RAM {n['mem_percent']:.0f}%"})
        elif d>15: recs.append({"type":"warning","title":f"{n['name']} Yuksek","msg":f"RAM {n['mem_percent']:.0f}%"})
        elif d<-15: recs.append({"type":"info","title":f"{n['name']} Bos","msg":f"RAM {n['mem_percent']:.0f}%"})
    return recs or [{"type":"success","title":"Saglikli","msg":"Sistemler normal"}]

# ============ HEALTH ============
import socket
from concurrent.futures import ThreadPoolExecutor

BACKUP_STORAGE = os.environ.get("CB_BACKUP_STORAGE", "")  # bos = ilk etkin PBS depolamasi otomatik bulunur
BACKUP_WARN_H, BACKUP_BAD_H = 26, 50
HA_BAD_STATES = {"error", "fence", "recovery", "freeze"}

def node_cmd(node_name, node_ip, args):
    """Run a fixed read-only command on a cluster node (locally or via SSH)."""
    local = node_name == socket.gethostname()
    cmd = args if local else ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", f"root@{node_ip}"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        return r.returncode, r.stdout.strip()
    except Exception as e:
        return -1, str(e)

@cached("cluster_status", ttl=30)
def get_cluster_status():
    return pvesh("get /cluster/status") or []

def health_cluster():
    st = get_cluster_status()
    cl = next((x for x in st if x.get("type") == "cluster"), {})
    nodes = [{"name": x["name"], "ip": x.get("ip"), "online": bool(x.get("online"))}
             for x in st if x.get("type") == "node"]
    up = {r["node"]: r.get("uptime", 0) for r in get_res() if r.get("type") == "node"}
    for n in nodes:
        n["uptime"] = up.get(n["name"], 0)
    ok = bool(cl.get("quorate")) and bool(nodes) and all(n["online"] for n in nodes)
    return {"ok": ok, "name": cl.get("name"), "quorate": bool(cl.get("quorate")), "nodes": nodes}

@cached("health_storage", ttl=60)
def health_storage():
    nodes = [n for n in health_cluster()["nodes"] if n["online"]]
    checks = []
    for s in pvesh("get /storage") or []:
        if s.get("type") != "dir" or not s.get("shared") or s.get("disable") or not s.get("path"):
            continue
        allowed = set(s["nodes"].split(",")) if s.get("nodes") else None
        for n in nodes:
            if allowed is None or n["name"] in allowed:
                checks.append((s["storage"], s["path"], n))
    def run(c):
        storage, path, n = c
        rc, out = node_cmd(n["name"], n["ip"], ["findmnt", "-n", "-o", "SOURCE,FSTYPE", path])
        mounted = rc == 0 and bool(out)
        return {"storage": storage, "path": path, "node": n["name"], "mounted": mounted,
                "source": out if mounted else ""}
    with ThreadPoolExecutor(max_workers=8) as ex:
        items = list(ex.map(run, checks))
    return {"ok": all(i["mounted"] for i in items), "items": items}

def health_ha():
    items = []
    for x in pvesh("get /cluster/ha/status/current") or []:
        if x.get("type") != "service":
            continue
        state, req = x.get("state", ""), x.get("request_state", "")
        bad = state in HA_BAD_STATES or (req == "started" and state not in ("started", "starting", "migrate", "relocate"))
        items.append({"sid": x.get("sid"), "node": x.get("node"), "state": state,
                      "request_state": req, "group": x.get("group"), "bad": bad})
    bad = [i for i in items if i["bad"]]
    return {"ok": not bad, "total": len(items), "bad": bad}

@cached("health_backups", ttl=300)
def health_backups():
    host = socket.gethostname()
    storage = BACKUP_STORAGE or next((s["storage"] for s in pvesh("get /storage") or []
                                      if s.get("type") == "pbs" and not s.get("disable")), None)
    if not storage:
        return {"ok": True, "storage": None, "total": 0, "problems": [], "newest_ok": 0,
                "note": "PBS depolamasi bulunamadi"}
    content = pvesh(f"get /nodes/{host}/storage/{storage}/content --content backup") or []
    last = {}
    for b in content:
        v = b.get("vmid")
        if v is not None and b.get("ctime", 0) > last.get(v, 0):
            last[v] = b["ctime"]
    now = time.time()
    items = []
    for r in get_res():
        if r.get("type") not in ("qemu", "lxc") or r.get("template"):
            continue
        ts = last.get(r["vmid"])
        age_h = round((now - ts) / 3600, 1) if ts else None
        level = "bad" if age_h is None or age_h > BACKUP_BAD_H else "warn" if age_h > BACKUP_WARN_H else "ok"
        items.append({"vmid": r["vmid"], "name": r.get("name", ""), "age_h": age_h, "level": level,
                      "last": datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else None})
    items.sort(key=lambda i: (i["level"] == "ok", -(i["age_h"] if i["age_h"] is not None else 1e9)))
    return {"ok": all(i["level"] == "ok" for i in items), "storage": storage,
            "total": len(items), "problems": [i for i in items if i["level"] != "ok"],
            "newest_ok": sum(1 for i in items if i["level"] == "ok")}

def health_tasks():
    since = time.time() - 24 * 3600
    failed = [{"type": t.get("type"), "id": t.get("id"), "node": t.get("node"), "status": t.get("status"),
               "time": datetime.fromtimestamp(t.get("starttime", 0)).strftime("%d.%m %H:%M")}
              for t in pvesh("get /cluster/tasks") or []
              if t.get("starttime", 0) >= since and t.get("status") not in (None, "OK")]
    return {"ok": not failed, "failed": failed[:15], "count": len(failed)}

@app.route("/api/health")
def api_health():
    out = {}
    for key, fn in (("cluster", health_cluster), ("storage", health_storage), ("ha", health_ha),
                    ("backups", health_backups), ("tasks", health_tasks)):
        try:
            out[key] = fn()
        except Exception as e:
            out[key] = {"ok": False, "error": str(e)}
    out["time"] = datetime.now().strftime("%H:%M:%S")
    return jsonify(out)

# ============ MIGRATION HISTORY (clusterbalance log parser) ============
import glob, gzip
from collections import Counter, defaultdict

_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[(\w+)\] (.*)$")
_RX = {
    "start": re.compile(r"ClusterBalance - Starting balance run"),
    "bal": re.compile(r"Current balanciness: ([\d.]+) \(threshold: ([\d.]+)\)"),
    "planned": re.compile(r"Planned: (\d+) \((.*?)\) (\S+) -> (\S+), balanciness ([\d.]+) -> ([\d.]+)"),
    "migrating": re.compile(r"Migrating (\d+) \((.*?)\) from (\S+) to (\S+)$"),
    "dry": re.compile(r"\[DRY-RUN\] Would execute: pvesh create /nodes/\S+/\w+/(\d+)/migrate"),
    "started": re.compile(r"Migration started for (\d+)"),
    "completed": re.compile(r"Migration completed: (\d+) is running on (\S+)"),
    "task_end": re.compile(r"Migration task for (\d+) ended: (.*)"),
    "timeout": re.compile(r"Migration timeout for (\d+)"),
    "failed": re.compile(r"Migration failed(?: to start)? for (\d+)"),
    "pvesh_err": re.compile(r"pvesh (?:error|exception): (.*)"),
    "deferred": re.compile(r"ertelendi"),
    "reject": re.compile(r"VM (\d+) bazi node'lara tasinamaz: (.*)"),
    "maint": re.compile(r"Processing maintenance mode for (\S+)"),
    "maint_ret": re.compile(r"Processing maintenance return for (\S+)"),
    "maint_stay": re.compile(r"Bakim: VM (\d+) \((.*?)\) (\S+) uzerinde kaliyor: (.*)"),
}

def _log_text_since(since_str):
    """Current log + rotated copies (newest first) until the window start is covered."""
    files = [CB_LOG] + sorted(glob.glob(CB_LOG + ".*"),
                              key=lambda p: int(re.sub(r"\D", "", p.rsplit(".log.", 1)[-1]) or 0))
    parts = []
    for path in files:
        try:
            if path.endswith(".gz"):
                with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            else:
                with open(path, "rb") as f:
                    f.seek(0, 2); size = f.tell(); pos = size; data = b""
                    while pos > 0:
                        step = min(4 * 1024 * 1024, pos); pos -= step
                        f.seek(pos); data = f.read(step) + data
                        nl = data.find(b"\n")
                        head = data[nl + 1:nl + 17] if pos > 0 else data[:16]
                        if head.decode("utf-8", "replace") < since_str:
                            break
                    text = data.decode("utf-8", "replace")
                    if pos > 0:
                        text = text[text.find("\n") + 1:]
        except OSError:
            continue
        parts.insert(0, text)
        if text[:16] and text[:16] < since_str:
            break
    return "\n".join(parts)

def parse_cb_history(hours):
    since_dt = datetime.fromtimestamp(time.time() - hours * 3600)
    since_str = since_dt.strftime("%Y-%m-%d %H:%M")
    text = _log_text_since(since_str)
    runs, migrations = [], []
    run = None
    last_err = None

    def finish(r):
        if not r:
            return
        for vmid, p in r["planned"].items():
            if vmid not in r["migs"]:
                r["migs"][vmid] = {**p, "result": "ertelendi" if r["deferred"] else "planlandi", "mode": "gercek"}
        for m in r["migs"].values():
            m.setdefault("result", "devam ediyor")
            migrations.append(m)
        runs.append({"ts": r["ts"], "bal": r["bal"], "thr": r["thr"], "planned": len(r["planned"]) or len(r["migs"]),
                     "executed": sum(1 for m in r["migs"].values() if m["result"] not in ("ertelendi", "planlandi")),
                     "failed": sum(1 for m in r["migs"].values() if m["result"] in ("basarisiz", "zaman asimi")),
                     "dry": r["dry"], "rejections": r["rejections"], "maint": r["maint"],
                     "maint_unmovable": r["maint_unmovable"]})

    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        ts, level, msg = m.group(1), m.group(2), m.group(3)
        if ts[:16] < since_str:
            continue
        if _RX["start"].search(msg):
            finish(run)
            run = {"ts": ts, "bal": None, "thr": None, "planned": {}, "migs": {}, "dry": False,
                   "deferred": False, "rejections": {}, "maint": [], "maint_ret": [], "maint_unmovable": {}}
            last_err = None
            continue
        if run is None:
            continue
        if (x := _RX["bal"].search(msg)):
            run["bal"], run["thr"] = float(x.group(1)), float(x.group(2))
        elif (x := _RX["planned"].search(msg)):
            vmid = int(x.group(1))
            run["planned"][vmid] = {"ts": ts, "vmid": vmid, "name": x.group(2), "source": x.group(3), "target": x.group(4),
                                    "reason": "balance", "bal_before": float(x.group(5)), "bal_after": float(x.group(6)),
                                    "threshold": run["thr"]}
        elif (x := _RX["maint"].search(msg)):
            run["maint"].append(x.group(1))
        elif (x := _RX["maint_ret"].search(msg)):
            run["maint_ret"].append(x.group(1))
        elif (x := _RX["maint_stay"].search(msg)):
            run["maint_unmovable"][x.group(1)] = f"{x.group(2)}: {x.group(4)}"
        elif (x := _RX["migrating"].search(msg)):
            vmid = int(x.group(1))
            base = run["planned"].get(vmid) or {"vmid": vmid, "name": x.group(2), "source": x.group(3), "target": x.group(4),
                                                "reason": "maintenance" if x.group(3) in run["maint"]
                                                          else "maintenance_return" if x.group(4) in run["maint_ret"] else "legacy",
                                                "threshold": run["thr"], "bal_before": run["bal"]}
            run["migs"][vmid] = {**base, "ts": ts, "mode": "gercek"}
        elif (x := _RX["dry"].search(msg)):
            mig = run["migs"].get(int(x.group(1)))
            run["dry"] = True
            if mig:
                mig.update(mode="test", result="test")
        elif (x := _RX["completed"].search(msg)):
            mig = run["migs"].get(int(x.group(1)))
            if mig:
                t0 = datetime.strptime(mig["ts"], "%Y-%m-%d %H:%M:%S")
                mig.update(result="basarili", duration_s=int((datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") - t0).total_seconds()))
        elif (x := _RX["task_end"].search(msg)):
            mig = run["migs"].get(int(x.group(1)))
            if mig:
                mig.update(result="basarisiz", error=x.group(2))
        elif (x := _RX["timeout"].search(msg)):
            mig = run["migs"].get(int(x.group(1)))
            if mig:
                mig.update(result="zaman asimi", error="Hedefte calisir hale gelmedi (zaman asimi)")
        elif (x := _RX["failed"].search(msg)):
            mig = run["migs"].get(int(x.group(1)))
            if mig:
                mig.update(result="basarisiz", error=last_err or "Tasima baslatilamadi")
        elif (x := _RX["started"].search(msg)):
            mig = run["migs"].get(int(x.group(1)))
            if mig:
                mig.update(result="baslatildi")
        elif (x := _RX["pvesh_err"].search(msg)):
            last_err = x.group(1).strip()
        elif (x := _RX["reject"].search(msg)):
            run["rejections"][x.group(1)] = x.group(2)
        elif _RX["deferred"].search(msg):
            run["deferred"] = True
    finish(run)

    # ileri-geri: gercek tasimalarda 24 saat icinde geri donen ya da 3+ kez tasinan VM
    real = sorted((m for m in migrations if m["mode"] == "gercek" and m["result"] in ("basarili", "baslatildi")), key=lambda m: m["ts"])
    pingpong = set()
    by_vm = defaultdict(list)
    for m in real:
        by_vm[m["vmid"]].append(m)
    for vmid, ms in by_vm.items():
        for a, b in zip(ms, ms[1:]):
            dt = (datetime.strptime(b["ts"], "%Y-%m-%d %H:%M:%S") - datetime.strptime(a["ts"], "%Y-%m-%d %H:%M:%S")).total_seconds()
            if dt <= 86400 and a["source"] == b["target"]:
                pingpong.add(vmid)
        if len(ms) >= 3:
            pingpong.add(vmid)
    for m in migrations:
        m["pingpong"] = m["vmid"] in pingpong

    res = Counter(m["result"] for m in migrations)
    summary = {"runs": len(runs), "total": len(migrations),
               "real": sum(1 for m in migrations if m["mode"] == "gercek" and m["result"] not in ("ertelendi", "planlandi")),
               "test": res.get("test", 0), "success": res.get("basarili", 0),
               "failed": res.get("basarisiz", 0) + res.get("zaman asimi", 0), "deferred": res.get("ertelendi", 0),
               "legacy_started": res.get("baslatildi", 0), "pingpong_vms": len(pingpong),
               "max_bal": max((r["bal"] for r in runs if r["bal"] is not None), default=None)}
    step = max(1, len(runs) // 400)
    chart = [{"ts": r["ts"], "bal": r["bal"], "thr": r["thr"]} for r in runs[::step] if r["bal"] is not None]
    try:
        size_mb = round(sum(os.path.getsize(p) for p in [CB_LOG] + glob.glob(CB_LOG + ".*")) / 1048576, 1)
    except OSError:
        size_mb = None
    return {"hours": hours, "since": since_str, "summary": summary,
            "migrations": sorted(migrations, key=lambda m: m["ts"], reverse=True)[:400],
            "last_run": next((r for r in reversed(runs) if r["bal"] is not None), None),
            "running": bool(runs and runs[-1]["bal"] is None), "chart": chart, "log_size_mb": size_mb,
            "log_rotated": bool(glob.glob(CB_LOG + ".*"))}

_hist_cache = {}

@app.route("/api/history")
def api_history():
    try:
        hours = min(max(int(request.args.get("hours", 168)), 1), 24 * 120)
    except ValueError:
        hours = 168
    c = _hist_cache.get(hours)
    if c and time.time() - c[0] < 60:
        return jsonify(c[1])
    data = parse_cb_history(hours)
    _hist_cache[hours] = (time.time(), data)
    return jsonify(data)

# ============ MAINTENANCE MODE ============

def _cb_state_lock():
    import fcntl
    fh = open(CB_STATE_FILE + ".lock", "w")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh

def _read_cb_state():
    try:
        with open(CB_STATE_FILE) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("drained", {}); d.setdefault("return_requested", [])
    return d

def _write_cb_state(d):
    tmp = CB_STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, CB_STATE_FILE)

def _save_cb_config(c):
    with open(CB_CONFIG_FILE, "w") as f:
        yaml.safe_dump(c, f, default_flow_style=False, allow_unicode=True)

def _balancer_busy():
    r = subprocess.run(["systemctl", "is-active", "clusterbalance.service"], capture_output=True, text=True)
    return r.stdout.strip() in ("activating", "active", "reloading")

def _kick_balancer():
    subprocess.run(["systemctl", "start", "--no-block", "clusterbalance.service"], capture_output=True)

def _audit(msg):
    user = request.authorization.username if request.authorization else "?"
    print(f"AUDIT {datetime.now():%Y-%m-%d %H:%M:%S} {user}: {msg}", flush=True)

@app.route("/api/maintenance")
def api_maintenance():
    cfg = load_cb_config()
    maint = list(cfg.get("maintenance_nodes") or [])
    state = _read_cb_state()
    _cache.pop("res", None)  # canli durum
    res = get_res()
    last = (parse_cb_history(6) or {}).get("last_run") or {}
    nodes = []
    for n in sorted({r["node"] for r in res if r.get("type") == "node"}):
        running = [{"vmid": r["vmid"], "name": r.get("name", "")} for r in res
                   if r.get("type") in ("qemu", "lxc") and r.get("node") == n and r.get("status") == "running"]
        nodes.append({"name": n, "maintenance": n in maint, "running": running,
                      "drained": state["drained"].get(n, []), "returning": n in state["return_requested"]})
    return jsonify({"nodes": nodes, "busy": _balancer_busy(), "dry_run": bool(cfg.get("dry_run")),
                    "dashboard_node": socket.gethostname(),
                    "unmovable": last.get("maint_unmovable", {}), "last_run": last.get("ts")})

@app.route("/api/maintenance/preview")
def api_maintenance_preview():
    node = request.args.get("node", "")
    if not re.match(r"^[A-Za-z0-9_-]+$", node):
        return jsonify({"error": "gecersiz node"}), 400
    try:
        r = subprocess.run(["/usr/bin/python3", CB_BALANCER, "--maintenance-preview", node],
                           capture_output=True, text=True, timeout=240)
        return jsonify(json.loads(r.stdout.strip().splitlines()[-1]))
    except Exception as e:
        return jsonify({"error": f"Onizleme alinamadi: {e}"}), 500

@app.route("/api/maintenance/enter", methods=["POST"])
def api_maintenance_enter():
    node = (request.json or {}).get("node", "")
    online = {x["name"] for x in get_nodes()}
    if node not in online:
        return jsonify({"success": False, "message": f"{node} cevrimici bir node degil"}), 400
    cfg = load_cb_config()
    maint = list(cfg.get("maintenance_nodes") or [])
    if maint and node not in maint:
        return jsonify({"success": False, "message": f"Ayni anda tek node bakimda olabilir ({', '.join(maint)} zaten bakimda)"}), 409
    if node in _read_cb_state()["return_requested"]:
        return jsonify({"success": False, "message": f"{node} icin geri donus henuz bitmedi, biraz sonra tekrar deneyin"}), 409
    if node not in maint:
        cfg["maintenance_nodes"] = maint + [node]
        _save_cb_config(cfg)
        _audit(f"{node} bakima alindi")
    _kick_balancer()
    return jsonify({"success": True, "message": f"{node} bakima alindi; VM'ler tasiniyor"})

@app.route("/api/maintenance/exit", methods=["POST"])
def api_maintenance_exit():
    node = (request.json or {}).get("node", "")
    cfg = load_cb_config()
    maint = list(cfg.get("maintenance_nodes") or [])
    if node not in maint:
        return jsonify({"success": False, "message": f"{node} bakimda degil"}), 400
    cfg["maintenance_nodes"] = [n for n in maint if n != node]
    _save_cb_config(cfg)
    lock = _cb_state_lock()
    try:
        st = _read_cb_state()
        if st["drained"].get(node) and node not in st["return_requested"]:
            st["return_requested"].append(node)
            st.setdefault("return_attempts", {})[node] = 0
            _write_cb_state(st)
    finally:
        lock.close()
    _audit(f"{node} bakimdan cikarildi (geri donus: {len(st['drained'].get(node, []))} VM)")
    _kick_balancer()
    return jsonify({"success": True, "message": f"{node} bakimdan cikarildi; VM'ler geri tasiniyor"})

# ============ ROUTES ============

@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/summary")
def api_sum(): return jsonify(get_summary())

@app.route("/api/nodes")
def api_n(): return jsonify(get_nodes())

@app.route("/api/guests")
def api_g(): return jsonify(get_guests())

@app.route("/api/recommendations")
def api_r(): return jsonify(get_recs())

@app.route("/api/widget-summary")
def api_widget_summary():
    """Minimal, sifresiz ozet: sadece Proxmox arayuzundeki kutu icin."""
    s = get_summary() or {}
    return jsonify({k: s.get(k) for k in ("node_count", "total", "running_vms", "avg_cpu", "avg_mem", "balanced", "max_diff")})

def widget_origin_allowed(origin):
    m = re.match(r"^https://([A-Za-z0-9.-]+):8006$", origin or "")
    if not m:
        return False
    host = m.group(1).lower()
    for x in get_cluster_status():
        if x.get("type") != "node":
            continue
        name = str(x.get("name", "")).lower()
        if host == str(x.get("ip", "")).lower() or host == name or host.startswith(name + "."):
            return True
    return False

@app.route("/api/distribution")
def api_d():
    n=get_nodes()
    return jsonify({"labels":[x["name"] for x in n],"vm_counts":[x["vm_count"] for x in n],
        "ct_counts":[x["ct_count"] for x in n],"mem_usage":[x["mem_percent"] for x in n],
        "cpu_usage":[x["cpu"] for x in n],"disk_usage":[x["disk_percent"] for x in n]})

@app.route("/api/top")
def api_t():
    g=[x for x in get_guests() if x["status"]=="running"]
    return jsonify({"cpu":sorted(g,key=lambda x:x["cpu"],reverse=True)[:10],
        "mem":sorted(g,key=lambda x:x["mem_percent"],reverse=True)[:10]})

# ============ CONFIG API ============

# Panel ayarlari ClusterBalance motorunun config.yaml dosyasini yonetir
CB_MODES = {"memory", "cpu", "disk", "io", "combined"}

def load_cb_config():
    try:
        with open(CB_CONFIG_FILE) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

@app.route("/api/config", methods=["GET"])
def get_config():
    c = load_cb_config()
    tags = c.get("exclude_tags") or []
    return jsonify({
        "enabled": c.get("enabled", True),
        "mode": c.get("method", "combined"),
        "threshold": c.get("balanciness_threshold", 15),
        "max_migrations": c.get("max_migrations", 3),
        "migration_type": "online" if c.get("migration_type", "live") == "live" else "offline",
        "exclude_tags": ",".join(tags) if isinstance(tags, list) else str(tags),
        "dry_run": c.get("dry_run", True),
    })

@app.route("/api/config", methods=["POST"])
def update_config():
    try:
        d = request.json or {}
        c = load_cb_config()
        if not c:
            return jsonify({"success": False, "message": "ClusterBalance config okunamadi"})
        if "enabled" in d: c["enabled"] = bool(d["enabled"])
        if "dry_run" in d: c["dry_run"] = bool(d["dry_run"])
        if d.get("mode") in CB_MODES: c["method"] = d["mode"]
        if "threshold" in d: c["balanciness_threshold"] = min(max(int(d["threshold"]), 5), 50)
        if "max_migrations" in d: c["max_migrations"] = min(max(int(d["max_migrations"]), 1), 10)
        if d.get("migration_type") in ("online", "offline"):
            c["migration_type"] = "live" if d["migration_type"] == "online" else "offline"
        if "exclude_tags" in d:
            c["exclude_tags"] = [t.strip() for t in str(d["exclude_tags"]).split(",") if t.strip()]
        with open(CB_CONFIG_FILE, "w") as f:
            yaml.safe_dump(c, f, default_flow_style=False, allow_unicode=True)
        return jsonify({"success": True, "message": "Config saved"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

# ============ PROXMOX SETTINGS API ============

@app.route("/api/proxmox/repo-status", methods=["GET"])
def get_repo_status():
    """Check current repository status"""
    result = {
        "enterprise_enabled": False,
        "no_subscription_enabled": False,
        "subscription_nag_disabled": False
    }

    # Check enterprise repo
    if os.path.exists(PVE_REPO_FILE):
        with open(PVE_REPO_FILE, 'r') as f:
            content = f.read()
            result["enterprise_enabled"] = not content.strip().startswith("#")

    # Check no-subscription repo
    if os.path.exists("/etc/apt/sources.list"):
        with open("/etc/apt/sources.list", 'r') as f:
            result["no_subscription_enabled"] = "pve-no-subscription" in f.read()

    # Check subscription nag
    if os.path.exists(NO_SUB_JS):
        with open(NO_SUB_JS, 'r') as f:
            content = f.read()
            result["subscription_nag_disabled"] = "void({ //Ext.Msg.show" in content or "// Subscription check disabled" in content

    return jsonify(result)

@app.route("/api/proxmox/run-balance", methods=["POST"])
def run_balance():
    """Run the load balancer manually"""
    # Panelden sadece test (dry-run) calistirilabilir
    try:
        r = subprocess.run(["/usr/bin/python3", CB_BALANCER, "--dry-run"],
                           capture_output=True, text=True, timeout=180)
        return jsonify({"success": r.returncode == 0, "output": r.stdout + r.stderr})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/api/proxmox/balance-status", methods=["GET"])
def get_balance_status():
    """Get last balance run status"""
    result = run_cmd(f"tail -50 {CB_LOG} 2>/dev/null || echo 'No logs yet'")
    return jsonify({"logs": result.get("output", "No logs available")})

@app.route("/api/cluster/nodes", methods=["GET"])
def get_cluster_nodes_ips():
    """Get all cluster node IPs"""
    nodes = get_nodes()
    node_info = []
    for node in nodes:
        ip_result = run_cmd(f"getent hosts {node['name']} | awk '{{print $1}}'")
        ip = ip_result.get("output", "").strip() if ip_result.get("success") else ""
        node_info.append({"name": node["name"], "ip": ip})
    return jsonify(node_info)

# ============ TLS ============
# Proxmox'un node sertifikasini kullanir (SAN: node IP + hostname, pveproxy ile ayni CA).
TLS_CERT = os.environ.get("CB_TLS_CERT", "/etc/pve/local/pve-ssl.pem")
TLS_KEY = os.environ.get("CB_TLS_KEY", "/etc/pve/local/pve-ssl.key")

@app.after_request
def security_headers(resp):
    if request.path in PUBLIC_PATHS and widget_origin_allowed(request.headers.get("Origin")):
        resp.headers["Access-Control-Allow-Origin"] = request.headers["Origin"]
        resp.headers["Vary"] = "Origin"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    return resp

if __name__ == "__main__":
    if not (os.path.exists(TLS_CERT) and os.path.exists(TLS_KEY)):
        raise SystemExit(f"TLS sertifikasi bulunamadi: {TLS_CERT} / {TLS_KEY}")
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False, ssl_context=(TLS_CERT, TLS_KEY))
