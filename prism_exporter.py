#!/usr/bin/env python3
"""Prometheus exporter for Nutanix Prism Central (API v4.0, clustermgmt).

Multi-target mode: /metrics?target=<prism-central>
One target = one Prism Central. The exporter discovers the AOS clusters it
manages by itself: the "cluster" label comes from the API, not from targets.
A cluster moved to another Prism Central is therefore followed automatically.
"""
import base64
import collections
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

USER = os.environ["PRISM_USER"]
PASSWORD = os.environ["PRISM_PASSWORD"]
PORT = int(os.environ.get("PRISM_EXPORTER_PORT", "9421"))
TIMEOUT = int(os.environ.get("PRISM_TIMEOUT", "20"))
CA_FILE = os.environ.get("PRISM_CA_FILE")

AUTH = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()

if CA_FILE:
    SSL_CTX = ssl.create_default_context(cafile=CA_FILE)
else:
    # Self-signed certificates: no verification
    SSL_CTX = ssl.create_default_context()
    SSL_CTX.check_hostname = False
    SSL_CTX.verify_mode = ssl.CERT_NONE

API = "/api/clustermgmt/v4.0"
MON_API = "/api/monitoring/v4.0"
SEVERITIES = ("CRITICAL", "WARNING", "INFO")
STATS_WINDOW_MIN = 10  # requested window; only the latest point is kept

# (API v4 field, metric, help, extra labels, conversion factor)
STATS = [
    ("hypervisorCpuUsagePpm", "nutanix_cluster_cpu_usage_ratio",
     "Hypervisor CPU usage (0-1)", {}, 1e-6),
    ("aggregateHypervisorMemoryUsagePpm", "nutanix_cluster_memory_usage_ratio",
     "Hypervisor memory usage (0-1)", {}, 1e-6),
    ("overallMemoryUsageBytes", "nutanix_cluster_memory_usage_bytes",
     "Memory used", {}, 1),
    ("storageUsageBytes", "nutanix_cluster_storage_usage_bytes",
     "Physical storage used", {}, 1),
    ("storageCapacityBytes", "nutanix_cluster_storage_capacity_bytes",
     "Physical storage capacity", {}, 1),
    ("freePhysicalStorageBytes", "nutanix_cluster_storage_free_bytes",
     "Physical storage free", {}, 1),
    ("logicalStorageUsageBytes", "nutanix_cluster_storage_logical_usage_bytes",
     "Logical storage used (before data reduction)", {}, 1),
    ("controllerNumIops", "nutanix_cluster_controller_iops",
     "Controller IOPS", {"op": "total"}, 1),
    ("controllerNumReadIops", "nutanix_cluster_controller_iops",
     "Controller IOPS", {"op": "read"}, 1),
    ("controllerNumWriteIops", "nutanix_cluster_controller_iops",
     "Controller IOPS", {"op": "write"}, 1),
    ("controllerAvgIoLatencyUsecs", "nutanix_cluster_controller_latency_seconds",
     "Controller average I/O latency", {"op": "total"}, 1e-6),
    ("controllerAvgReadIoLatencyUsecs", "nutanix_cluster_controller_latency_seconds",
     "Controller average I/O latency", {"op": "read"}, 1e-6),
    ("controllerAvgWriteIoLatencyUsecs", "nutanix_cluster_controller_latency_seconds",
     "Controller average I/O latency", {"op": "write"}, 1e-6),
    ("healthCheckScore", "nutanix_cluster_health_check_score",
     "Prism health check score (raw value)", {}, 1),
]


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def get(target, path, params=None, api=API):
    host = target if ":" in target else f"{target}:9440"
    url = f"https://{host}{api}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, safe="$:", quote_via=urllib.parse.quote)
    req = urllib.request.Request(
        url, headers={"Authorization": AUTH, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as resp:
        return json.load(resp)


def list_clusters(target):
    """AOS clusters managed by this Prism Central (the PC itself is excluded)."""
    data = get(target, "/config/clusters", {"$limit": 100}).get("data") or []
    return [
        c for c in data
        if "AOS" in ((c.get("config") or {}).get("clusterFunction") or [])
    ]


def latest_stats(target, ext_id):
    """Latest value of each cluster statistic."""
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    params = {
        "$startTime": (now - timedelta(minutes=STATS_WINDOW_MIN)).strftime(fmt),
        "$endTime": now.strftime(fmt),
        "$samplingInterval": 60,
        "$statType": "AVG",
    }
    data = get(target, f"/stats/clusters/{ext_id}", params).get("data") or {}
    out = {}
    for key, points in data.items():
        if isinstance(points, list) and points:
            last = max(points, key=lambda p: p.get("timestamp", ""))
            if last.get("value") is not None:
                out[key] = last["value"]
    return out


def list_hosts(target, ext_id):
    """Hosts (nodes) of a cluster."""
    return get(target, f"/config/clusters/{ext_id}/hosts", {"$limit": 100}).get("data") or []


def open_alerts(target):
    """Unresolved alerts known to this Prism Central (with pagination)."""
    out, page = [], 0
    while page < 10:
        d = get(target, "/serviceability/alerts",
                {"$filter": "isResolved eq false", "$limit": 100, "$page": page}, api=MON_API)
        batch = d.get("data") or []
        out += batch
        total = (d.get("metadata") or {}).get("totalAvailableResults") or 0
        if not batch or len(out) >= total:
            break
        page += 1
    return out


def software_version(cfg, kind):
    """Short version (7.3.1.5, 5.2.1) from clusterSoftwareMap."""
    for s in cfg.get("clusterSoftwareMap") or []:
        if s.get("softwareType") == kind:
            v = s.get("version") or ""
            m = re.search(r"-(\d+(?:\.\d+)+)-", v)
            if m:
                return m.group(1)
            return v[4:] if v.startswith("ncc-") else v
    return ""


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Out:
    """Groups samples by metric (required by the Prometheus format)."""

    def __init__(self):
        self.families = {}

    def add(self, name, help_, labels, value):
        fam = self.families.setdefault(name, (help_, []))
        lab = ",".join(f'{k}="{esc(v)}"' for k, v in labels.items())
        fam[1].append(f"{name}{{{lab}}} {value}" if lab else f"{name} {value}")

    def add_num(self, name, help_, labels, value):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self.add(name, help_, labels, value)

    def text(self):
        lines = []
        for name, (help_, samples) in self.families.items():
            lines += [f"# HELP {name} {help_}", f"# TYPE {name} gauge"]
            lines += samples
        return "\n".join(lines) + "\n"


def render_cluster(out, c, stats):
    cfg = c.get("config") or {}
    lab = {"cluster": c.get("name") or c.get("extId")}
    ft = cfg.get("faultToleranceState") or {}

    out.add("nutanix_cluster_info", "Cluster versions (always 1)",
            {**lab, "aos_version": software_version(cfg, "NOS"),
             "ncc_version": software_version(cfg, "NCC")}, 1)
    out.add("nutanix_cluster_available",
            "1 if Prism Central sees the cluster as available, 0 otherwise",
            lab, 1 if cfg.get("isAvailable") else 0)
    out.add_num("nutanix_cluster_nodes", "Number of nodes",
                lab, (c.get("nodes") or {}).get("numberOfNodes"))
    out.add_num("nutanix_cluster_vms", "Number of VMs", lab, c.get("vmCount"))
    out.add_num("nutanix_cluster_redundancy_factor", "Redundancy factor (RF)",
                lab, cfg.get("redundancyFactor"))
    out.add_num("nutanix_cluster_fault_tolerance",
                "Number of node failures tolerated (current/desired)",
                {**lab, "state": "current"}, ft.get("currentMaxFaultTolerance"))
    out.add_num("nutanix_cluster_fault_tolerance",
                "Number of node failures tolerated (current/desired)",
                {**lab, "state": "desired"}, ft.get("desiredMaxFaultTolerance"))
    if cfg.get("operationMode"):
        out.add("nutanix_cluster_operation_mode",
                "Operation mode (always 1)",
                {**lab, "mode": cfg["operationMode"]}, 1)
    if c.get("upgradeStatus"):
        out.add("nutanix_cluster_upgrade_status",
                "Last upgrade status (always 1)",
                {**lab, "status": c["upgradeStatus"]}, 1)

    out.add("nutanix_cluster_stats_up",
            "1 if cluster statistics were retrieved, 0 otherwise",
            lab, 1 if stats else 0)
    for key, metric, help_, extra, factor in STATS:
        v = (stats or {}).get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            out.add(metric, help_, {**lab, **extra}, round(v * factor, 9))


def hypervisor_version(hv):
    """'AHV 10.3.1.4' -> '10.3.1.4'."""
    m = re.search(r"(\d+(?:\.\d+)+)", hv.get("fullName") or "")
    return m.group(1) if m else ""


def render_hosts(out, cluster, hosts):
    versions = set()
    for h in hosts:
        hv = h.get("hypervisor") or {}
        cvm = h.get("controllerVm") or {}
        lab = {"cluster": cluster, "host": h.get("hostName") or h.get("extId")}
        state = hv.get("state") or ""
        mstate = h.get("maintenanceState") or ""
        version = hypervisor_version(hv)
        ip = ((hv.get("externalAddress") or {}).get("ipv4") or {}).get("value") or ""
        if version:
            versions.add(version)
        connected = hv.get("acropolisConnectionState") == "CONNECTED"
        maint = (bool(cvm.get("isInMaintenanceMode"))
                 or "MAINTENANCE" in state.upper()
                 or mstate.lower() not in ("", "normal"))
        out.add("nutanix_host_info", "Host states and version (always 1)",
                {**lab, "hypervisor_state": state, "maintenance_state": mstate,
                 "hypervisor_version": version, "ip": ip}, 1)
        out.add_num("nutanix_host_vms", "Number of VMs on the host", lab, hv.get("numberOfVms"))
        out.add("nutanix_host_maintenance", "1 if the host or its CVM is in maintenance, 0 otherwise",
                lab, 1 if maint else 0)
        out.add("nutanix_host_up",
                "1 if the host is connected and not in maintenance (in service), 0 otherwise",
                lab, 1 if connected and not maint else 0)
    if versions:
        # Several versions possible during a rolling node upgrade
        key = lambda v: [int(x) for x in v.split(".")]
        out.add("nutanix_cluster_hypervisor_version",
                "Hypervisor version(s) on the cluster (always 1)",
                {"cluster": cluster, "version": " / ".join(sorted(versions, key=key))}, 1)


def render_alerts(out, clusters, alerts):
    names = {c.get("extId"): c.get("name") or c.get("extId") for c in clusters}
    per_cluster = collections.Counter()
    per_pc = collections.Counter()
    for a in alerts:
        uuid = a.get("originatingClusterUUID") or a.get("clusterUUID")
        sev = (a.get("severity") or "").upper()
        if uuid in names:
            per_cluster[(names[uuid], sev)] += 1
        else:
            per_pc[sev] += 1
    for name in names.values():
        for sev in SEVERITIES:
            out.add("nutanix_cluster_alerts", "Open (unresolved) alerts by severity",
                    {"cluster": name, "severity": sev.lower()}, per_cluster[(name, sev)])
    for sev in SEVERITIES:
        out.add("nutanix_pc_alerts",
                "Open alerts of Prism Central itself (not tied to a cluster)",
                {"severity": sev.lower()}, per_pc[sev])


def render(target):
    out = Out()
    try:
        clusters = list_clusters(target)
        up = 1 if clusters else 0
        if not clusters:
            log(f"{target}: no AOS cluster in the response")
    except Exception as exc:
        log(f"{target}: failed to list clusters ({exc})")
        clusters, up = [], 0
    out.add("nutanix_up",
            "1 if Prism Central responded with at least one cluster, 0 otherwise",
            {}, up)

    def one(c):
        name = c.get("name")
        try:
            stats = latest_stats(target, c["extId"])
            if not stats:
                log(f"{target}: {name}: no statistics in the window")
        except Exception as exc:
            log(f"{target}: {name}: failed to get statistics ({exc})")
            stats = None
        try:
            hosts = list_hosts(target, c["extId"])
        except Exception as exc:
            log(f"{target}: {name}: failed to list hosts ({exc})")
            hosts = []
        return c, stats, hosts

    with ThreadPoolExecutor(max_workers=8) as pool:
        for c, stats, hosts in pool.map(one, clusters):
            render_cluster(out, c, stats)
            render_hosts(out, c.get("name") or c.get("extId"), hosts)

    if clusters:
        try:
            alerts = open_alerts(target)
            alerts_up = 1
        except Exception as exc:
            log(f"{target}: failed to get alerts ({exc})")
            alerts, alerts_up = [], 0
        out.add("nutanix_alerts_up", "1 if alerts were retrieved, 0 otherwise",
                {}, alerts_up)
        if alerts_up:
            render_alerts(out, clusters, alerts)

    return out.text()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/health":
            # Liveness only: no call to Prism Central
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok\n")
            return
        target = parse_qs(url.query).get("target", [None])[0]
        if url.path != "/metrics" or not target:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"usage: /metrics?target=<prism-central>\n")
            return
        body = render(target).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # no log on every scrape


if __name__ == "__main__":
    log(f"nutanix-prism-central-exporter listening on port {PORT}")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
