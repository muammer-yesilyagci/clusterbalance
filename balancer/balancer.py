#!/usr/bin/env python3
"""
ClusterBalance - Advanced Proxmox Cluster Load Balancer
Comprehensive VM/CT balancing with affinity rules, maintenance mode, and I/O balancing

Features:
- Memory, CPU, Disk, and I/O based balancing
- Affinity and Anti-Affinity rules
- Maintenance mode for node operations
- Node pinning for licensing/hardware requirements
- PSI (Pressure Stall Information) metrics support
- Parallel and live migrations
- Tag-based exclusions
- Overprovisioning protection
- Daemon mode with scheduling

Original author: Cemal Demirci (github.com/cemal-demirci)
Version 2: safety checks, maintenance mode with return, migration window - https://github.com/muammer-yesilyagci/clusterbalance
"""

import subprocess
import json
import yaml
import sys
import os
import time
import argparse
import signal
import logging
import smtplib
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

# Optional: requests for webhooks
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# Configuration
CONFIG_FILE = "/opt/clusterbalance/config.yaml"
STATE_FILE = "/opt/clusterbalance/maintenance_state.json"  # bakim: bosaltilan VM'ler + geri donus istekleri
LOG_FILE = "/var/log/clusterbalance.log"

# Default configuration
DEFAULT_CONFIG = {
    "enabled": True,
    "daemon": False,
    "schedule_interval": 21600,  # 6 hours
    "dry_run": False,
    "log_level": "INFO",

    # Balancing method
    "method": "combined",  # memory, cpu, disk, io, combined
    "mode": "used",  # used, assigned, psi
    "balanciness_threshold": 15,  # Max delta between nodes

    # Resource thresholds
    "cpu_enabled": True,
    "cpu_threshold": 80,
    "cpu_weight": 1.0,

    "memory_enabled": True,
    "memory_threshold": 85,
    "memory_weight": 2.0,

    "disk_enabled": True,
    "disk_threshold": 85,
    "disk_weight": 1.0,

    "io_enabled": True,
    "io_threshold": 70,
    "io_weight": 1.5,

    # PSI thresholds (Pressure Stall Information)
    "psi_memory_threshold": 10.0,
    "psi_cpu_threshold": 25.0,
    "psi_io_threshold": 10.0,

    # Migration settings
    "migration_type": "live",  # live, offline
    "max_migrations": 3,
    "parallel_migrations": 1,
    "migration_timeout": 900,
    "migration_window": "20:00-07:00",  # real balance migrations only in this window ("" = always)
    "min_improvement": 5.0,  # min balanciness gain for a migration to be worth it
    "migration_bandwidth": 0,  # 0 = unlimited
    "with_local_disks": False,

    # Guest selection
    "balance_vms": True,
    "balance_cts": True,
    "balance_larger_first": True,

    # Exclusions
    "exclude_vmids": [],
    "exclude_names": [],
    "exclude_tags": ["kritik", "pinned", "no-migrate"],
    "exclude_nodes": [],
    "maintenance_nodes": [],

    # Affinity rules (tag-based)
    # VMs with same affinity tag stay on same node
    "affinity_tag_prefix": "cb_affinity_",

    # Anti-affinity rules (tag-based)
    # VMs with same anti-affinity tag spread across nodes
    "anti_affinity_tag_prefix": "cb_anti_affinity_",

    # Node pinning (tag-based)
    # cb_pin_nodename pins VM to specific node
    "pin_tag_prefix": "cb_pin_",

    # Ignore tag
    "ignore_tag_prefix": "cb_ignore",

    # Pool-based rules
    "pools": {},
    # Example:
    # pools:
    #   database_cluster:
    #     type: affinity
    #   web_servers:
    #     type: anti-affinity
    #     pin: [node1, node2]

    # Overprovisioning protection
    "overprovisioning_protection": True,
    "max_memory_usage": 95,
    "max_cpu_usage": 95,

    # Notifications
    "webhook_enabled": False,
    "webhook_url": "",
}

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)




class NotificationManager:
    """Multi-channel notification system for ClusterBalance"""

    def __init__(self, config):
        self.config = config.get('notifications', {})
        self.enabled = self.config.get('enabled', False)

    def send(self, event_type, title, message, data=None):
        """Send notification to all enabled channels"""
        if not self.enabled:
            return

        event_map = {
            'migration': 'notify_on_migration',
            'error': 'notify_on_error',
            'maintenance': 'notify_on_maintenance',
            'imbalance': 'notify_on_imbalance'
        }
        if event_type in event_map and not self.config.get(event_map[event_type], True):
            return

        if self.config.get('smtp', {}).get('enabled'):
            self._send_email(title, message, data)
        if self.config.get('discord', {}).get('enabled'):
            self._send_discord(title, message, data)
        if self.config.get('teams', {}).get('enabled'):
            self._send_teams(title, message, data)
        if self.config.get('slack', {}).get('enabled'):
            self._send_slack(title, message, data)

    def _send_email(self, title, message, data=None):
        """Send email notification via SMTP"""
        smtp_cfg = self.config.get('smtp', {})
        try:
            msg = MIMEMultipart()
            prefix = smtp_cfg.get('subject_prefix', '[ClusterBalance]')
            msg['Subject'] = f"{prefix} {title}"
            msg['From'] = smtp_cfg.get('from_address', 'cb@localhost')
            msg['To'] = ', '.join(smtp_cfg.get('to_addresses', []))
            body = f"{message}\n\n"
            if data:
                body += "Details:\n" + json.dumps(data, indent=2)
            msg.attach(MIMEText(body, 'plain'))
            if smtp_cfg.get('use_tls', True):
                server = smtplib.SMTP(smtp_cfg.get('host', 'localhost'), smtp_cfg.get('port', 587))
                server.starttls()
            else:
                server = smtplib.SMTP(smtp_cfg.get('host', 'localhost'), smtp_cfg.get('port', 25))
            if smtp_cfg.get('username'):
                server.login(smtp_cfg['username'], smtp_cfg.get('password', ''))
            server.send_message(msg)
            server.quit()
            logger.debug("Email notification sent")
        except Exception as e:
            logger.error(f"Email notification failed: {e}")

    def _send_discord(self, title, message, data=None):
        if not HAS_REQUESTS:
            return
        cfg = self.config.get('discord', {})
        url = cfg.get('webhook_url', '')
        if not url:
            return
        try:
            embed = {"title": title, "description": message, "color": 16743424, "footer": {"text": "ClusterBalance"}}
            if data:
                fields = [{"name": str(k), "value": str(v)[:100], "inline": True} for k, v in list(data.items())[:5]]
                embed["fields"] = fields
            payload = {"username": cfg.get('username', 'ClusterBalance'), "embeds": [embed]}
            if cfg.get('mention_role_id'):
                payload["content"] = f"<@&{cfg['mention_role_id']}>"
            requests.post(url, json=payload, timeout=10)
            logger.debug("Discord notification sent")
        except Exception as e:
            logger.error(f"Discord notification failed: {e}")

    def _send_teams(self, title, message, data=None):
        if not HAS_REQUESTS:
            return
        cfg = self.config.get('teams', {})
        url = cfg.get('webhook_url', '')
        if not url:
            return
        try:
            facts = [{"name": str(k), "value": str(v)} for k, v in list(data.items())[:6]] if data else []
            payload = {"@type": "MessageCard", "@context": "http://schema.org/extensions",
                      "themeColor": cfg.get('theme_color', 'f97316'), "summary": title,
                      "sections": [{"activityTitle": title, "facts": facts, "text": message}]}
            requests.post(url, json=payload, timeout=10)
            logger.debug("Teams notification sent")
        except Exception as e:
            logger.error(f"Teams notification failed: {e}")

    def _send_slack(self, title, message, data=None):
        if not HAS_REQUESTS:
            return
        cfg = self.config.get('slack', {})
        url = cfg.get('webhook_url', '')
        if not url:
            return
        try:
            blocks = [{"type": "header", "text": {"type": "plain_text", "text": title}},
                     {"type": "section", "text": {"type": "mrkdwn", "text": message}}]
            if data:
                fields = [{"type": "mrkdwn", "text": f"*{k}:* {v}"} for k, v in list(data.items())[:10]]
                if fields:
                    blocks.append({"type": "section", "fields": fields[:10]})
            payload = {"username": cfg.get('username', 'ClusterBalance'), "icon_emoji": cfg.get('icon_emoji', ':scales:'), "blocks": blocks}
            if cfg.get('channel'):
                payload["channel"] = cfg['channel']
            requests.post(url, json=payload, timeout=10)
            logger.debug("Slack notification sent")
        except Exception as e:
            logger.error(f"Slack notification failed: {e}")



def _state_lock():
    import fcntl
    fh = open(STATE_FILE + ".lock", "w")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


def load_maintenance_state() -> Dict:
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault("drained", {})
    data.setdefault("return_requested", [])
    return data


def save_maintenance_state(data: Dict):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, STATE_FILE)


class ClusterBalance:
    """Main ClusterBalance class for Proxmox load balancing"""

    def __init__(self, config_file: str = CONFIG_FILE):
        self.config = self.load_config(config_file)
        self.nodes: Dict = {}
        self.guests: List[Dict] = []
        self.migrations: List[Dict] = []
        self.affinity_groups: Dict[str, List[int]] = {}
        self.anti_affinity_groups: Dict[str, List[int]] = {}
        self.pinned_guests: Dict[int, str] = {}
        self.ignored_guests: List[int] = []
        self.io_sensitive_guests: List[int] = []  # I/O sensitive VMs
        self.rejections: Dict[int, Dict[str, str]] = {}

        # Initialize notification manager
        self.notifier = NotificationManager(self.config)

        # Set log level
        log_level = getattr(logging, self.config.get("log_level", "INFO").upper())
        logger.setLevel(log_level)

    def load_config(self, config_file: str) -> Dict:
        """Load configuration from YAML file"""
        config = DEFAULT_CONFIG.copy()

        if os.path.exists(config_file):
            try:
                with open(config_file, 'r') as f:
                    file_config = yaml.safe_load(f) or {}
                    config.update(file_config)
            except Exception as e:
                logger.error(f"Error loading config: {e}")

        return config

    def pvesh(self, cmd: str) -> Optional[Any]:
        """Execute pvesh command and return JSON result"""
        try:
            result = subprocess.run(
                f"pvesh {cmd} --output-format json",
                shell=True,
                capture_output=True,
                text=True,
                timeout=60
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
            else:
                logger.error(f"pvesh error: {result.stderr}")
                return None
        except Exception as e:
            logger.error(f"pvesh exception: {e}")
            return None

    def get_cluster_resources(self) -> List[Dict]:
        """Get all cluster resources"""
        return self.pvesh("get /cluster/resources") or []

    def get_node_status(self, node: str) -> Optional[Dict]:
        """Get detailed node status including I/O metrics"""
        return self.pvesh(f"get /nodes/{node}/status")

    def get_node_rrddata(self, node: str, timeframe: str = "hour") -> Optional[List]:
        """Get RRD data for I/O metrics"""
        return self.pvesh(f"get /nodes/{node}/rrddata?timeframe={timeframe}")

    def get_guest_config(self, node: str, vmid: int, guest_type: str) -> Optional[Dict]:
        """Get guest configuration"""
        endpoint = "qemu" if guest_type == "qemu" else "lxc"
        return self.pvesh(f"get /nodes/{node}/{endpoint}/{vmid}/config")

    def get_psi_metrics(self, node: str) -> Dict[str, float]:
        """Get PSI (Pressure Stall Information) metrics for a node"""
        psi = {"memory": 0.0, "cpu": 0.0, "io": 0.0}

        try:
            # PSI metrics are available in /proc/pressure on newer kernels
            result = subprocess.run(
                f"ssh -o StrictHostKeyChecking=no root@{node} 'cat /proc/pressure/memory /proc/pressure/cpu /proc/pressure/io 2>/dev/null'",
                shell=True,
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                for i, metric in enumerate(['memory', 'cpu', 'io']):
                    if i < len(lines):
                        # Parse "some avg10=X.XX avg60=X.XX avg300=X.XX total=X"
                        parts = lines[i].split()
                        for part in parts:
                            if part.startswith('avg10='):
                                psi[metric] = float(part.split('=')[1])
                                break
        except Exception as e:
            logger.debug(f"Could not get PSI metrics for {node}: {e}")

        return psi

    def get_io_metrics(self, node: str) -> Dict[str, float]:
        """Get disk I/O metrics for a node"""
        io_metrics = {"read_iops": 0, "write_iops": 0, "read_bps": 0, "write_bps": 0, "io_wait": 0}

        try:
            # Get I/O stats from /proc/diskstats
            result = subprocess.run(
                f"ssh -o StrictHostKeyChecking=no root@{node} 'cat /proc/diskstats; cat /proc/stat | grep cpu'",
                shell=True,
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                total_reads = 0
                total_writes = 0

                for line in lines:
                    parts = line.split()
                    if len(parts) >= 14 and (parts[2].startswith('sd') or parts[2].startswith('nvme')):
                        # Fields: reads_completed, reads_merged, sectors_read, ms_reading,
                        #         writes_completed, writes_merged, sectors_written, ms_writing, ...
                        total_reads += int(parts[3])
                        total_writes += int(parts[7])
                    elif parts[0] == 'cpu' and len(parts) >= 5:
                        # CPU line includes iowait
                        total = sum(int(x) for x in parts[1:])
                        if total > 0:
                            io_metrics["io_wait"] = (int(parts[5]) / total) * 100

                io_metrics["read_iops"] = total_reads
                io_metrics["write_iops"] = total_writes
        except Exception as e:
            logger.debug(f"Could not get I/O metrics for {node}: {e}")

        return io_metrics

    def get_vm_passthrough(self, node: str, vmid: int, guest_type: str) -> Dict:
        """Check if VM has USB or PCI passthrough configured"""
        passthrough = {"usb": [], "pci": [], "has_passthrough": False}

        try:
            endpoint = "qemu" if guest_type == "qemu" else "lxc"
            result = self.pvesh(f"get /nodes/{node}/{endpoint}/{vmid}/config")
            if result:
                config = json.loads(result) if isinstance(result, str) else result

                # Check for USB passthrough (usb0, usb1, usb2, etc.)
                for key, value in config.items():
                    if key.startswith("usb") and key[3:].isdigit():
                        if value and "host=" in str(value):
                            passthrough["usb"].append({"key": key, "value": str(value)[:50]})
                            passthrough["has_passthrough"] = True

                    # Check for PCI passthrough (hostpci0, hostpci1, etc.)
                    if key.startswith("hostpci"):
                        passthrough["pci"].append({"key": key, "value": str(value)[:50]})
                        passthrough["has_passthrough"] = True
        except Exception as e:
            logger.debug(f"Could not get passthrough info for {vmid}: {e}")

        return passthrough

    def collect_data(self):
        """Collect all cluster data"""
        logger.info("Collecting cluster data...")

        resources = self.get_cluster_resources()
        if not resources:
            logger.error("Could not get cluster resources")
            return False

        # Collect nodes
        self.nodes = {}
        for r in resources:
            if r.get("type") == "node" and r.get("status") == "online":
                node_name = r["node"]

                # Skip excluded and maintenance nodes
                if node_name in self.config.get("exclude_nodes", []):
                    logger.info(f"Skipping excluded node: {node_name}")
                    continue
                if node_name in self.config.get("maintenance_nodes", []):
                    logger.info(f"Node in maintenance mode: {node_name}")
                    # We still collect it but mark it

                self.nodes[node_name] = {
                    "name": node_name,
                    "cpu": r.get("cpu", 0) * 100,
                    "maxcpu": r.get("maxcpu", 1),
                    "mem": r.get("mem", 0),
                    "maxmem": r.get("maxmem", 1),
                    "mem_percent": (r.get("mem", 0) / r.get("maxmem", 1)) * 100,
                    "disk": r.get("disk", 0),
                    "maxdisk": r.get("maxdisk", 1),
                    "disk_percent": (r.get("disk", 0) / r.get("maxdisk", 1)) * 100,
                    "guests": [],
                    "vm_count": 0,
                    "ct_count": 0,
                    "assigned_mem": 0,
                    "assigned_cpu": 0,
                    "maintenance": node_name in self.config.get("maintenance_nodes", []),
                }

                # Get I/O metrics
                if self.config.get("io_enabled"):
                    io_metrics = self.get_io_metrics(node_name)
                    self.nodes[node_name].update(io_metrics)

                # Get PSI metrics if mode is PSI
                if self.config.get("mode") == "psi":
                    psi_metrics = self.get_psi_metrics(node_name)
                    self.nodes[node_name]["psi"] = psi_metrics

        if not self.nodes:
            logger.error("No online nodes found")
            return False

        # Collect guests
        self.guests = []
        self.affinity_groups = {}
        self.anti_affinity_groups = {}
        self.pinned_guests = {}
        self.ignored_guests = []
        self.passthrough_guests = []  # VMs with USB/PCI passthrough

        for r in resources:
            if r.get("type") in ["qemu", "lxc"]:
                vmid = r["vmid"]
                node = r["node"]
                guest_type = r["type"]
                tags = r.get("tags", "") or ""
                name = r.get("name", "")
                status = r.get("status", "stopped")

                # Check exclusions (VM listede kalir; normal dengelemede ignored_guests ile atlanir,
                # bakim modunda maintenance_include_excluded=True ise yine tasinabilir)
                tag_list = [t.strip() for t in tags.split(";") if t.strip()]
                excluded_by = None
                if vmid in self.config.get("exclude_vmids", []):
                    excluded_by = "exclude_vmids"
                elif any(ex in name for ex in self.config.get("exclude_names", [])):
                    excluded_by = "exclude_names"
                else:
                    ex_tag = next((t for t in self.config.get("exclude_tags", []) if t in tag_list), None)
                    ignore_prefix = self.config.get("ignore_tag_prefix", "cb_ignore")
                    if ex_tag:
                        excluded_by = f"etiket:{ex_tag}"
                    elif any(t.startswith(ignore_prefix) for t in tag_list):
                        excluded_by = f"etiket:{ignore_prefix}"
                if excluded_by:
                    self.ignored_guests.append(vmid)

                # Process affinity tags
                affinity_prefix = self.config.get("affinity_tag_prefix", "cb_affinity_")
                for tag in tag_list:
                    if tag.startswith(affinity_prefix):
                        group = tag[len(affinity_prefix):]
                        if group not in self.affinity_groups:
                            self.affinity_groups[group] = []
                        self.affinity_groups[group].append(vmid)

                # Process anti-affinity tags
                anti_affinity_prefix = self.config.get("anti_affinity_tag_prefix", "cb_anti_affinity_")
                for tag in tag_list:
                    if tag.startswith(anti_affinity_prefix):
                        group = tag[len(anti_affinity_prefix):]
                        if group not in self.anti_affinity_groups:
                            self.anti_affinity_groups[group] = []
                        self.anti_affinity_groups[group].append(vmid)

                # Process pin tags
                pin_prefix = self.config.get("pin_tag_prefix", "cb_pin_")
                for tag in tag_list:
                    if tag.startswith(pin_prefix):
                        pinned_node = tag[len(pin_prefix):]
                        if pinned_node in self.nodes:
                            self.pinned_guests[vmid] = pinned_node

                # Calculate guest metrics
                mem = r.get("mem", 0)
                maxmem = r.get("maxmem", 0)
                cpu = r.get("cpu", 0) * 100
                maxcpu = r.get("maxcpu", 1)

                # Check for passthrough (USB/PCI)
                passthrough = {"usb": [], "pci": [], "has_passthrough": False}
                if self.config.get("detect_passthrough", True) and guest_type == "qemu":
                    passthrough = self.get_vm_passthrough(node, vmid, guest_type)
                    if passthrough["has_passthrough"]:
                        self.passthrough_guests.append(vmid)
                        # Auto-exclude passthrough VMs if configured
                        if self.config.get("exclude_passthrough", True) and vmid not in self.ignored_guests:
                            self.ignored_guests.append(vmid)

                guest = {
                    "vmid": vmid,
                    "name": name,
                    "node": node,
                    "type": guest_type,
                    "status": status,
                    "tags": tag_list,
                    "mem": mem,
                    "maxmem": maxmem,
                    "mem_percent": (mem / maxmem * 100) if maxmem > 0 else 0,
                    "cpu": cpu,
                    "maxcpu": maxcpu,
                    "disk": r.get("maxdisk", 0),
                    "score": 0,  # Will be calculated
                    "passthrough": passthrough,
                    "excluded_by": excluded_by,
                }

                self.guests.append(guest)

                # Update node stats
                if node in self.nodes:
                    self.nodes[node]["guests"].append(vmid)
                    if guest_type == "qemu":
                        self.nodes[node]["vm_count"] += 1
                    else:
                        self.nodes[node]["ct_count"] += 1

                    if status == "running":
                        self.nodes[node]["assigned_mem"] += maxmem
                        self.nodes[node]["assigned_cpu"] += maxcpu

        # Process pool-based rules
        self._process_pool_rules()

        # Storage / HA placement rules
        self._load_placement_rules()

        logger.info(f"Collected {len(self.nodes)} nodes, {len(self.guests)} guests")
        logger.info(f"Affinity groups: {list(self.affinity_groups.keys())}")
        logger.info(f"Anti-affinity groups: {list(self.anti_affinity_groups.keys())}")
        logger.info(f"Pinned guests: {len(self.pinned_guests)}")
        logger.info(f"Ignored guests: {len(self.ignored_guests)}")
        logger.info(f"I/O sensitive guests: {len(self.io_sensitive_guests)}")
        logger.info(f"Passthrough guests (USB/PCI): {len(self.passthrough_guests)}")

        return True

    def _process_pool_rules(self):
        """Process pool-based affinity/anti-affinity rules"""
        pools = self.config.get("pools", {})

        for pool_name, pool_config in pools.items():
            pool_type = pool_config.get("type", "")
            pin_nodes = pool_config.get("pin", [])

            # Get VMs in pool
            pool_vms = []
            for guest in self.guests:
                # Check if VM has pool tag
                if f"pool_{pool_name}" in guest["tags"] or pool_name in guest.get("pool", ""):
                    pool_vms.append(guest["vmid"])

            if not pool_vms:
                continue

            if pool_type == "affinity":
                self.affinity_groups[f"pool_{pool_name}"] = pool_vms
            elif pool_type == "anti-affinity":
                self.anti_affinity_groups[f"pool_{pool_name}"] = pool_vms

            # Handle pinning
            if pin_nodes:
                for vmid in pool_vms:
                    if vmid not in self.pinned_guests:
                        self.pinned_guests[vmid] = pin_nodes[0]  # Pin to first node

    def is_io_sensitive(self, guest: Dict) -> bool:
        """Check if guest is I/O sensitive (database, etc.)"""
        io_config = self.config.get('io_sensitive_workloads', {})
        if not io_config.get('enabled', True):
            return False

        vmid = guest.get('vmid')
        name = guest.get('name', '').lower()
        tags = guest.get('tags', [])

        # Check if manually tagged as I/O sensitive
        io_tag = io_config.get('io_sensitive_tag', 'cb_io_sensitive')
        if io_tag in tags:
            return True

        # Auto-detect by name patterns
        patterns = io_config.get('auto_detect_patterns', [
            'sql', 'mssql', 'mysql', 'postgres', 'mariadb', 
            'oracle', 'mongodb', 'redis', 'elasticsearch', 
            'kafka', 'database', 'db'
        ])
        for pattern in patterns:
            if pattern.lower() in name:
                return True

        return False
    def calculate_node_scores(self) -> Dict[str, float]:
        """Calculate balance score for each node"""
        scores = {}
        method = self.config.get("method", "combined")
        mode = self.config.get("mode", "used")

        for node_name, node in self.nodes.items():
            score = 0.0

            if method in ["memory", "combined"]:
                if mode == "used":
                    mem_score = node["mem_percent"]
                elif mode == "assigned":
                    mem_score = (node["assigned_mem"] / node["maxmem"]) * 100 if node["maxmem"] > 0 else 0
                elif mode == "psi":
                    mem_score = node.get("psi", {}).get("memory", 0)
                else:
                    mem_score = node["mem_percent"]

                score += mem_score * self.config.get("memory_weight", 2.0)

            if method in ["cpu", "combined"]:
                if mode == "used":
                    cpu_score = node["cpu"]
                elif mode == "assigned":
                    cpu_score = (node["assigned_cpu"] / node["maxcpu"]) * 100 if node["maxcpu"] > 0 else 0
                elif mode == "psi":
                    cpu_score = node.get("psi", {}).get("cpu", 0)
                else:
                    cpu_score = node["cpu"]

                score += cpu_score * self.config.get("cpu_weight", 1.0)

            if method in ["disk", "combined"]:
                score += node["disk_percent"] * self.config.get("disk_weight", 1.0)

            if method in ["io", "combined"] and self.config.get("io_enabled"):
                io_score = node.get("io_wait", 0)
                if mode == "psi":
                    io_score = node.get("psi", {}).get("io", 0)
                score += io_score * self.config.get("io_weight", 1.5)

            scores[node_name] = score

        return scores

    def calculate_balanciness(self, scores: Dict[str, float]) -> float:
        """Calculate balanciness (difference between max and min scores)"""
        if not scores:
            return 0.0

        values = list(scores.values())
        return max(values) - min(values)

    def calculate_guest_score(self, guest: Dict) -> float:
        """Calculate migration priority score for a guest"""
        score = 0.0
        method = self.config.get("method", "combined")

        if method in ["memory", "combined"]:
            score += guest["maxmem"] / (1024**3) * self.config.get("memory_weight", 2.0)

        if method in ["cpu", "combined"]:
            score += guest["maxcpu"] * self.config.get("cpu_weight", 1.0)

        if method in ["disk", "combined"]:
            score += guest["disk"] / (1024**3) * self.config.get("disk_weight", 1.0)

        return score

    # ------------------------------------------------------------------
    # Placement rules: storage availability + HA groups (ZD patch 2026-09)
    # ------------------------------------------------------------------
    DISK_KEY_RE = re.compile(r"^(ide|sata|scsi|virtio|efidisk|tpmstate)\d+$")

    def _load_placement_rules(self):
        """Load storage definitions and HA group restrictions once per run."""
        self.storage_cfg = {s["storage"]: s for s in (self.pvesh("get /storage") or [])}
        self.ha_groups = {}
        for g in self.pvesh("get /cluster/ha/groups") or []:
            nodes = {n.split(":")[0] for n in str(g.get("nodes", "")).split(",") if n}
            self.ha_groups[g["group"]] = {"nodes": nodes, "restricted": bool(g.get("restricted"))}
        self.ha_resources = {}
        for r in self.pvesh("get /cluster/ha/resources") or []:
            sid = str(r.get("sid", ""))
            if ":" in sid and sid.split(":")[1].isdigit():
                self.ha_resources[int(sid.split(":")[1])] = r.get("group")
        self._guest_cfg_cache = {}
        self._mount_cache = {}
        self._node_ips = {}
        for x in self.pvesh("get /cluster/status") or []:
            if x.get("type") == "node":
                self._node_ips[x["name"]] = x.get("ip")

    def _guest_config(self, guest: Dict) -> Dict:
        key = guest["vmid"]
        if key not in self._guest_cfg_cache:
            self._guest_cfg_cache[key] = self.get_guest_config(guest["node"], guest["vmid"], guest["type"]) or {}
        return self._guest_cfg_cache[key]

    def _is_mounted(self, node: str, path: str) -> bool:
        key = (node, path)
        if key not in self._mount_cache:
            import socket
            cmd = ["findmnt", "-n", path]
            if node != socket.gethostname():
                ip = self._node_ips.get(node, node)
                cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", f"root@{ip}"] + cmd
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                self._mount_cache[key] = r.returncode == 0 and bool(r.stdout.strip())
            except Exception:
                self._mount_cache[key] = False
        return self._mount_cache[key]

    def guest_storage_ok(self, guest: Dict, target: str) -> Tuple[bool, str]:
        """All disks/ISOs of the guest must be on storage usable from the target node."""
        cfg = self._guest_config(guest)
        if not cfg:
            return False, "config okunamadi"
        for key, value in cfg.items():
            if not self.DISK_KEY_RE.match(key) and not (guest["type"] == "lxc" and (key == "rootfs" or key.startswith("mp"))):
                continue
            vol = str(value).split(",")[0]
            if vol == "none":
                continue
            if ":" not in vol:
                return False, f"{key}: fiziksel aygit ({vol})"
            sid = vol.split(":")[0]
            st = self.storage_cfg.get(sid)
            if not st:
                return False, f"{key}: depolama '{sid}' tanimli degil"
            if st.get("disable"):
                return False, f"{key}: depolama '{sid}' devre disi"
            if st.get("nodes") and target not in str(st["nodes"]).split(","):
                return False, f"{key}: '{sid}' {target} uzerinde tanimli degil"
            if not st.get("shared") and not self.config.get("with_local_disks"):
                return False, f"{key}: yerel depolama '{sid}'"
            if st.get("type") == "dir" and st.get("shared") and st.get("path"):
                if not self._is_mounted(target, st["path"]):
                    return False, f"{key}: '{sid}' {target} uzerinde BAGLI DEGIL ({st['path']})"
        return True, "ok"

    def ha_target_ok(self, vmid: int, target: str) -> Tuple[bool, str]:
        group = self.ha_resources.get(vmid)
        g = self.ha_groups.get(group) if group else None
        if g and g["restricted"] and target not in g["nodes"]:
            return False, f"HA grubu '{group}' sadece {sorted(g['nodes'])}"
        return True, "ok"

    def _apply_move(self, guest: Dict, target: str, sign: int = 1):
        """Simulate moving a guest (sign=1) or undo it (sign=-1) on the in-memory node stats."""
        src, dst = (guest["node"], target) if sign == 1 else (target, guest["node"])
        for node_name, direction in ((src, -1), (dst, 1)):
            n = self.nodes.get(node_name)
            if not n:
                continue
            n["mem"] += direction * guest["mem"]
            n["mem_percent"] = n["mem"] / n["maxmem"] * 100 if n["maxmem"] else 0
            n["cpu"] += direction * guest["cpu"] * guest["maxcpu"] / max(n["maxcpu"], 1)
            if guest["status"] == "running":
                n["assigned_mem"] += direction * guest["maxmem"]
                n["assigned_cpu"] += direction * guest["maxcpu"]

    def in_migration_window(self) -> bool:
        """migration_window: 'HH:MM-HH:MM' (may wrap midnight). Empty = always allowed."""
        window = str(self.config.get("migration_window", "") or "").strip()
        if not window:
            return True
        try:
            start_s, end_s = [x.strip() for x in window.split("-")]
            now = datetime.now().time()
            start = datetime.strptime(start_s, "%H:%M").time()
            end = datetime.strptime(end_s, "%H:%M").time()
            return start <= now < end if start < end else (now >= start or now < end)
        except Exception:
            logger.warning(f"Gecersiz migration_window '{window}', tasima yapilmayacak")
            return False

    def find_best_target_node(self, guest: Dict, exclude_nodes: List[str] = None) -> Optional[str]:
        """Find the best target node for a guest (respects storage, HA groups, capacity)"""
        exclude_nodes = exclude_nodes or []
        current_node = guest["node"]
        vmid = guest["vmid"]

        def placement_ok(node_name: str) -> bool:
            ok, reason = self.ha_target_ok(vmid, node_name)
            if ok:
                ok, reason = self.guest_storage_ok(guest, node_name)
            if not ok:
                logger.debug(f"VM {vmid} -> {node_name} uygun degil: {reason}")
                self.rejections.setdefault(vmid, {})[node_name] = reason
            return ok

        # Check if guest is pinned
        if vmid in self.pinned_guests:
            pinned_node = self.pinned_guests[vmid]
            if pinned_node != current_node and pinned_node not in exclude_nodes and placement_ok(pinned_node):
                return pinned_node
            return None

        # Get available nodes
        available_nodes = []
        for node_name, node in self.nodes.items():
            if node_name == current_node or node_name in exclude_nodes or node["maintenance"]:
                continue

            # Check overprovisioning protection
            if self.config.get("overprovisioning_protection"):
                future_mem = (node["mem"] + guest["maxmem"]) / node["maxmem"] * 100
                if future_mem > self.config.get("max_memory_usage", 95):
                    continue

            if not placement_ok(node_name):
                continue

            available_nodes.append(node_name)

        if not available_nodes:
            return None

        # Check anti-affinity rules
        for group, vmids in self.anti_affinity_groups.items():
            if vmid in vmids:
                for other_vmid in vmids:
                    if other_vmid == vmid:
                        continue
                    other_guest = next((g for g in self.guests if g["vmid"] == other_vmid), None)
                    if other_guest and other_guest["node"] in available_nodes:
                        available_nodes.remove(other_guest["node"])

        # Check affinity rules
        for group, vmids in self.affinity_groups.items():
            if vmid in vmids:
                affinity_nodes = []
                for other_vmid in vmids:
                    if other_vmid == vmid:
                        continue
                    other_guest = next((g for g in self.guests if g["vmid"] == other_vmid), None)
                    if other_guest and other_guest["node"] in available_nodes:
                        affinity_nodes.append(other_guest["node"])
                if affinity_nodes:
                    available_nodes = affinity_nodes

        if not available_nodes:
            return None

        # Select node with lowest score
        scores = self.calculate_node_scores()
        return min(available_nodes, key=lambda n: scores.get(n, float('inf')))

    def should_migrate(self, guest: Dict, source_score: float, target_score: float) -> bool:
        """Determine if migration would improve balance"""
        guest_score = self.calculate_guest_score(guest)

        # Check if migration would improve balance
        new_source_score = source_score - guest_score * 0.1  # Approximate
        new_target_score = target_score + guest_score * 0.1

        current_diff = abs(source_score - target_score)
        new_diff = abs(new_source_score - new_target_score)

        return new_diff < current_diff

    def plan_migrations(self) -> List[Dict]:
        """Plan migrations greedily, re-simulating node load after every planned move."""
        import copy
        logger.info("Planning migrations...")

        self.migrations = []
        self.rejections = {}
        orig_nodes = copy.deepcopy(self.nodes)
        orig_guest_nodes = {g["vmid"]: g["node"] for g in self.guests}

        try:
            scores = self.calculate_node_scores()
            balanciness = self.calculate_balanciness(scores)
            threshold = self.config.get("balanciness_threshold", 15)
            logger.info(f"Current balanciness: {balanciness:.2f} (threshold: {threshold})")

            # Bakim sonrasi geri donus (bosaltilan VM'ler eski node'una)
            self.maintenance_unmovable = []
            state = load_maintenance_state()
            for node_name in list(state.get("return_requested", [])):
                if node_name not in self.nodes or self.nodes[node_name]["maintenance"]:
                    continue
                logger.info(f"Processing maintenance return for {node_name}")
                for entry in state.get("drained", {}).get(node_name, []):
                    guest = next((g for g in self.guests if g["vmid"] == entry["vmid"]), None)
                    if not guest or guest["status"] != "running" or guest["node"] == node_name:
                        continue
                    ok, reason = self.ha_target_ok(guest["vmid"], node_name)
                    if ok:
                        ok, reason = self.guest_storage_ok(guest, node_name)
                    if not ok:
                        logger.warning(f"Bakim donusu: VM {guest['vmid']} ({guest['name']}) {node_name} node'una donemez: {reason}")
                        continue
                    source = guest["node"]
                    self._apply_move(guest, node_name)
                    self.migrations.append({"vmid": guest["vmid"], "name": guest["name"], "type": guest["type"],
                                            "source": source, "target": node_name, "reason": "maintenance_return"})
                    guest["node"] = node_name

            # Bakim modu: node'u bosalt (denge durumundan bagimsiz, tasima penceresini beklemez)
            maintenance_target = self.config.get("maintenance_target", "")
            include_excluded = self.config.get("maintenance_include_excluded", True)
            for node_name, node in self.nodes.items():
                if not node["maintenance"]:
                    continue
                logger.info(f"Processing maintenance mode for {node_name}")
                for vmid in list(node["guests"]):
                    guest = next((g for g in self.guests if g["vmid"] == vmid), None)
                    if not guest or guest["status"] != "running":
                        continue
                    reason = None
                    if guest["passthrough"]["has_passthrough"]:
                        reason = "USB/PCI aygiti bagli (canli tasinamaz)"
                    elif guest.get("excluded_by") and not include_excluded:
                        reason = f"haric tutuluyor ({guest['excluded_by']})"
                    target = None
                    if not reason:
                        if maintenance_target and maintenance_target in self.nodes and not self.nodes[maintenance_target]["maintenance"]:
                            ok, why = self.ha_target_ok(vmid, maintenance_target)
                            if ok:
                                ok, why = self.guest_storage_ok(guest, maintenance_target)
                            target = maintenance_target if ok else None
                            reason = None if ok else why
                        else:
                            target = self.find_best_target_node(guest)
                            if not target:
                                why = self.rejections.get(vmid, {})
                                reason = "; ".join(f"{n}: {r}" for n, r in why.items()) or "uygun hedef node yok (kapasite/kural)"
                    if not target:
                        self.maintenance_unmovable.append({"vmid": vmid, "name": guest["name"], "node": node_name, "reason": reason})
                        logger.warning(f"Bakim: VM {vmid} ({guest['name']}) {node_name} uzerinde kaliyor: {reason}")
                        continue
                    self._apply_move(guest, target)
                    self.migrations.append({"vmid": vmid, "name": guest["name"], "type": guest["type"],
                                            "source": node_name, "target": target, "reason": "maintenance_mode",
                                            "excluded_by": guest.get("excluded_by")})
                    guest["node"] = target

            max_migrations = self.config.get("max_migrations", 3)
            min_improvement = float(self.config.get("min_improvement", 5.0))

            candidates = [g for g in self.guests if g["status"] == "running" and g["vmid"] not in self.ignored_guests]
            if not self.config.get("balance_vms", True):
                candidates = [g for g in candidates if g["type"] != "qemu"]
            if not self.config.get("balance_cts", True):
                candidates = [g for g in candidates if g["type"] != "lxc"]

            while len(self.migrations) < max_migrations:
                scores = self.calculate_node_scores()
                balanciness = self.calculate_balanciness(scores)
                if balanciness <= threshold:
                    break

                best = None
                for guest in candidates:
                    if any(m["vmid"] == guest["vmid"] for m in self.migrations):
                        continue
                    source = guest["node"]
                    target = self.find_best_target_node(guest)
                    if not target or scores.get(source, 0) <= scores.get(target, 0):
                        continue
                    self._apply_move(guest, target)
                    new_bal = self.calculate_balanciness(self.calculate_node_scores())
                    self._apply_move(guest, target, sign=-1)
                    gain = balanciness - new_bal
                    if gain >= min_improvement and (best is None or gain > best[2]):
                        best = (guest, target, gain, new_bal)

                if not best:
                    logger.info("Dengeyi iyilestiren uygun tasima bulunamadi")
                    break

                guest, target, gain, new_bal = best
                source = guest["node"]
                self._apply_move(guest, target)
                guest["node"] = target
                self.migrations.append({"vmid": guest["vmid"], "name": guest["name"], "type": guest["type"],
                                        "source": source, "target": target, "reason": "balance",
                                        "balanciness_before": round(balanciness, 2),
                                        "balanciness_after": round(new_bal, 2)})
                logger.info(f"Planned: {guest['vmid']} ({guest['name']}) {source} -> {target}, "
                            f"balanciness {balanciness:.2f} -> {new_bal:.2f}")

            for vmid, reasons in self.rejections.items():
                logger.info(f"VM {vmid} bazi node'lara tasinamaz: " + "; ".join(f"{n}: {r}" for n, r in reasons.items()))
        finally:
            self.planned_nodes = copy.deepcopy(self.nodes)
            # Restore real state; only the migration list is kept
            self.nodes = orig_nodes
            for g in self.guests:
                g["node"] = orig_guest_nodes[g["vmid"]]

        logger.info(f"Planned {len(self.migrations)} migrations")
        return self.migrations

    def execute_migration(self, migration: Dict) -> bool:
        """Execute a single migration and wait until the guest is running on the target."""
        vmid = migration["vmid"]
        source = migration["source"]
        target = migration["target"]
        endpoint = "qemu" if migration["type"] == "qemu" else "lxc"

        cmd = f"create /nodes/{source}/{endpoint}/{vmid}/migrate --target {target}"
        if self.config.get("migration_type") == "live":
            cmd += " --online 1" if endpoint == "qemu" else " --restart 1"
        if self.config.get("with_local_disks") and endpoint == "qemu":
            cmd += " --with-local-disks 1"
        if self.config.get("migration_bandwidth", 0) > 0:
            cmd += f" --bwlimit {self.config['migration_bandwidth']}"

        logger.info(f"Migrating {vmid} ({migration['name']}) from {source} to {target}")

        if self.config.get("dry_run"):
            logger.info(f"[DRY-RUN] Would execute: pvesh {cmd}")
            return True

        upid = self.pvesh(cmd)
        if upid is None:
            logger.error(f"Migration failed to start for {vmid}")
            return False

        timeout = int(self.config.get("migration_timeout", 900))
        deadline = time.time() + timeout
        # 1) wait for the task (for HA guests this is only the HA request)
        if isinstance(upid, str) and upid.startswith("UPID:"):
            task_node = upid.split(":")[1]
            while time.time() < deadline:
                st = self.pvesh(f"get /nodes/{task_node}/tasks/{upid}/status") or {}
                if st.get("status") == "stopped":
                    if st.get("exitstatus") != "OK":
                        logger.error(f"Migration task for {vmid} ended: {st.get('exitstatus')}")
                        return False
                    break
                time.sleep(10)
        # 2) wait until the guest is actually running on the target (HA migrations run async)
        while time.time() < deadline:
            for r in self.get_cluster_resources():
                if r.get("vmid") == vmid and r.get("node") == target and r.get("status") == "running":
                    logger.info(f"Migration completed: {vmid} is running on {target}")
                    return True
            time.sleep(10)
        logger.error(f"Migration timeout for {vmid} after {timeout}s")
        return False

    def execute_migrations(self) -> Dict:
        """Execute all planned migrations"""
        logger.info(f"Executing {len(self.migrations)} migrations...")

        results = {
            "total": len(self.migrations),
            "success": 0,
            "failed": 0,
            "migrations": []
        }

        parallel = self.config.get("parallel_migrations", 1)

        for i, migration in enumerate(self.migrations):
            # Simple sequential execution for now
            # TODO: Implement parallel migrations

            success = self.execute_migration(migration)

            migration["success"] = success
            results["migrations"].append(migration)

            if success:
                results["success"] += 1
            else:
                results["failed"] += 1

            # Wait between migrations
            if i < len(self.migrations) - 1:
                time.sleep(5)

        return results

    def send_webhook(self, message: str, data: Dict = None):
        """Send webhook notification"""
        if not self.config.get("webhook_enabled"):
            return

        url = self.config.get("webhook_url", "")
        if not url:
            return

        try:
            import requests
            payload = {
                "text": message,
                "data": data or {}
            }
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            logger.error(f"Webhook error: {e}")

    def get_status(self) -> Dict:
        """Get current cluster status"""
        if not self.collect_data():
            return {"error": "Could not collect cluster data"}

        scores = self.calculate_node_scores()
        balanciness = self.calculate_balanciness(scores)

        return {
            "timestamp": datetime.now().isoformat(),
            "nodes": len(self.nodes),
            "guests": len(self.guests),
            "balanciness": balanciness,
            "threshold": self.config.get("balanciness_threshold", 15),
            "balanced": balanciness <= self.config.get("balanciness_threshold", 15),
            "node_scores": scores,
            "affinity_groups": len(self.affinity_groups),
            "anti_affinity_groups": len(self.anti_affinity_groups),
            "pinned_guests": len(self.pinned_guests),
            "maintenance_nodes": self.config.get("maintenance_nodes", [])
        }

    def update_maintenance_state(self, results: Dict):
        """Bosaltilan VM'leri kaydet; geri donusu tamamlanan node'lari temizle."""
        lock = _state_lock()
        try:
            state = load_maintenance_state()
            changed = False
            for m in results.get("migrations", []):
                if not m.get("success"):
                    continue
                if m.get("reason") == "maintenance_mode":
                    lst = state["drained"].setdefault(m["source"], [])
                    if not any(e["vmid"] == m["vmid"] for e in lst):
                        lst.append({"vmid": m["vmid"], "name": m["name"], "to": m["target"],
                                    "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                        changed = True
            running = {g["vmid"]: g for g in self.guests if g["status"] == "running"}
            for m in results.get("migrations", []):
                if m.get("success") and m.get("reason") == "maintenance_return":
                    running.setdefault(m["vmid"], {})["node"] = m["target"]
            for node_name in list(state["return_requested"]):
                left = [e for e in state["drained"].get(node_name, [])
                        if e["vmid"] in running and running[e["vmid"]].get("node") != node_name]
                attempts = state.setdefault("return_attempts", {}).get(node_name, 0) + 1
                state["return_attempts"][node_name] = attempts
                if not left or attempts >= 5:
                    if left:
                        logger.warning(f"Bakim donusu {node_name}: {len(left)} VM 5 denemede geri donemedi, birakiliyor: "
                                       + ", ".join(str(e["vmid"]) for e in left))
                    state["drained"].pop(node_name, None)
                    state["return_requested"].remove(node_name)
                    state["return_attempts"].pop(node_name, None)
                    logger.info(f"Bakim donusu tamamlandi: {node_name}")
                else:
                    state["drained"][node_name] = left
                changed = True
            if changed:
                save_maintenance_state(state)
        finally:
            lock.close()

    def maintenance_preview(self, node: str) -> Dict:
        """Node bakima alinirsa ne olur? (hicbir sey tasimaz, log'a yazmaz)"""
        if not self.collect_data():
            return {"error": "Cluster verisi alinamadi"}
        if node not in self.nodes:
            return {"error": f"{node} cevrimici node degil"}
        for n in self.nodes.values():
            n["maintenance"] = (n["name"] == node)
        before = {n: {"mem_percent": round(v["mem_percent"], 1), "cpu": round(v["cpu"], 1)} for n, v in self.nodes.items()}
        moves = [m for m in self.plan_migrations() if m["reason"] == "maintenance_mode"]
        after = {n: {"mem_percent": round(v["mem_percent"], 1), "cpu": round(v["cpu"], 1),
                     "fits": v["mem_percent"] <= self.config.get("max_memory_usage", 95)}
                 for n, v in getattr(self, "planned_nodes", {}).items() if n != node}
        return {"node": node, "moves": moves, "unmovable": self.maintenance_unmovable,
                "before": before, "after": after, "fits": all(a["fits"] for a in after.values())}

    def run(self) -> Dict:
        """Run the balancer"""
        logger.info("=" * 60)
        logger.info("ClusterBalance - Starting balance run")
        logger.info("=" * 60)

        if not self.config.get("enabled", True):
            logger.info("Balancer is disabled")
            return {"status": "disabled"}

        # Collect data
        if not self.collect_data():
            return {"status": "error", "message": "Could not collect cluster data"}

        # Plan migrations
        migrations = self.plan_migrations()

        if not migrations:
            logger.info("No migrations needed")
            if not self.config.get("dry_run"):
                self.update_maintenance_state({"migrations": []})
            return {
                "status": "balanced",
                "migrations": 0
            }

        # Balance migrations only inside the migration window (maintenance moves always allowed)
        if not self.config.get("dry_run") and not self.in_migration_window():
            deferred = [m for m in self.migrations if m.get("reason") == "balance"]
            if deferred:
                logger.info(f"Tasima penceresi disinda ({self.config.get('migration_window')}): "
                            f"{len(deferred)} dengeleme tasimasi ertelendi")
            self.migrations = [m for m in self.migrations if m.get("reason") != "balance"]
            if not self.migrations:
                return {"status": "deferred", "migrations": 0}

        # Execute migrations
        results = self.execute_migrations()
        if not self.config.get("dry_run"):
            self.update_maintenance_state(results)

        # Send notifications
        if results["success"] > 0:
            # New multi-channel notification
            self.notifier.send(
                'migration',
                f"Migrations Completed: {results['success']}/{results['total']}",
                f"ClusterBalance completed {results['success']} migrations.",
                {"total": results["total"], "success": results["success"], "failed": results["failed"]}
            )
            # Legacy webhook
            self.send_webhook(
                f"ClusterBalance: Completed {results['success']} migrations",
                results
            )

        # Error notification
        if results["failed"] > 0:
            self.notifier.send('error', f"Migration Errors: {results['failed']}",
                f"ClusterBalance had {results['failed']} failed migrations.", results)

        logger.info("=" * 60)
        logger.info(f"Balance run complete: {results['success']}/{results['total']} successful")
        logger.info("=" * 60)

        return {
            "status": "completed",
            **results
        }

    def run_daemon(self):
        """Run in daemon mode"""
        logger.info("Starting daemon mode...")

        interval = self.config.get("schedule_interval", 21600)

        def signal_handler(sig, frame):
            logger.info("Received shutdown signal, exiting...")
            sys.exit(0)

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        while True:
            try:
                self.run()
            except Exception as e:
                logger.error(f"Error in daemon run: {e}")

            logger.info(f"Sleeping for {interval} seconds...")
            time.sleep(interval)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="ClusterBalance - Proxmox Cluster Load Balancer")
    parser.add_argument("-c", "--config", default=CONFIG_FILE, help="Configuration file path")
    parser.add_argument("-d", "--dry-run", action="store_true", help="Dry run mode")
    parser.add_argument("-s", "--status", action="store_true", help="Show cluster status")
    parser.add_argument("--daemon", action="store_true", help="Run in daemon mode")
    parser.add_argument("-j", "--json", action="store_true", help="Output as JSON")
    parser.add_argument("-v", "--version", action="store_true", help="Show version")
    parser.add_argument("--maintenance-preview", metavar="NODE", help="Node bakima alinirsa ne olur (JSON, log'a yazmaz)")

    args = parser.parse_args()

    if args.version:
        print("ClusterBalance v2.0.0")
        return

    if args.maintenance_preview:
        # Onizleme gecmis log'unu kirletmesin: dosya log'unu kapat
        for h in list(logging.getLogger().handlers):
            if isinstance(h, logging.FileHandler):
                logging.getLogger().removeHandler(h)
        logging.getLogger().setLevel(logging.ERROR)
        pv = ClusterBalance(args.config)
        pv.config["dry_run"] = True
        print(json.dumps(pv.maintenance_preview(args.maintenance_preview), ensure_ascii=False))
        return

    balancer = ClusterBalance(args.config)

    if args.dry_run:
        balancer.config["dry_run"] = True

    if args.status:
        status = balancer.get_status()
        if args.json:
            print(json.dumps(status, indent=2))
        else:
            print(f"\n{'='*60}")
            print("ClusterBalance - Cluster Status")
            print(f"{'='*60}")
            print(f"Nodes: {status.get('nodes', 0)}")
            print(f"Guests: {status.get('guests', 0)}")
            print(f"Balanciness: {status.get('balanciness', 0):.2f}")
            print(f"Threshold: {status.get('threshold', 15)}")
            print(f"Status: {'Balanced' if status.get('balanced') else 'Unbalanced'}")
            print(f"\nNode Scores:")
            for node, score in status.get('node_scores', {}).items():
                print(f"  {node}: {score:.2f}")
            print(f"{'='*60}\n")
        return

    if args.daemon:
        balancer.run_daemon()
    else:
        result = balancer.run()
        if args.json:
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
