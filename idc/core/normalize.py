"""Cross-source normalization and entity resolution.

Takes RawAssets from all four sources and produces unified ``Server`` records.
ServiceNow CMDB is the authoritative source for identity & classification
(role, env, criticality, apps); RVTools enriches VM sizing/disks; Zabbix and
Prometheus provide utilization. Per-field provenance is kept in
``Server.source_refs`` so every claim is auditable.

Also loads the app/workload dependency file (``fixtures/apps.csv`` or a custom
path) and resolves hostnames → server ids into ``Workload`` records.
"""
from __future__ import annotations

import csv
import os
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

from ..config import FIXTURES
from .models import (
    Disk,
    RawAsset,
    Server,
    Utilization,
    Workload,
)


def _key(hostname: str, fqdn: str, ip: str = "") -> str:
    # Hostname is the primary identity within an org; fqdn/ip are fallbacks.
    # Keying on fqdn-first would split a host when some sources lack fqdn
    # (e.g. Prometheus/RVTools only carry hostname).
    return (hostname or fqdn or ip).strip().lower()


def _to_int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _to_float(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _infer_role(attrs: dict, tags: Iterable[str], os_full: str) -> str:
    tags = [t.lower() for t in tags]
    if "db" in tags or "mysql" in tags or "oracle" in tags or "postgresql" in tags:
        return "db"
    if "web" in tags or "nginx" in tags:
        return "web"
    if "cache" in tags or "redis" in tags:
        return "cache"
    if "hadoop" in tags:
        return "hadoop"
    if "k8s" in tags:
        return "k8s"
    if "monitoring" in tags or "grafana" in tags:
        return "monitoring"
    # hint from os/path
    s = (os_full or "").lower()
    if "mysql" in s:
        return "db"
    return "app"


def _role_from_servicenow(row: dict) -> str:
    # ServiceNow has no single 'role' col in our fixture; infer from name/tags.
    name = (row.get("name") or row.get("hostname") or "").lower()
    for tok, role in [("db-mysql", "db"), ("db-oracle", "db"), ("mysql", "db"),
                      ("oracle", "db"), ("redis", "cache"), ("cache", "cache"),
                      ("hdop", "hadoop"), ("hadoop", "hadoop"),
                      ("k8s-master", "k8s"), ("k8s-worker", "k8s"), ("k8s", "k8s"),
                      ("web-portal", "web"), ("nginx", "web"),
                      ("grafana", "monitoring"), ("mon-", "monitoring")]:
        if tok in name:
            return role
    return ""


def normalize(assets: List[RawAsset]) -> List[Server]:
    """Merge raw assets into unified Server records (dedup by hostname/fqdn)."""
    by_key: Dict[str, List[RawAsset]] = defaultdict(list)
    for a in assets:
        k = _key(a.hostname, a.fqdn, a.ip)
        if k:
            by_key[k].append(a)

    servers: List[Server] = []
    for k, group in by_key.items():
        srv = Server()
        # authoritative: servicenow first, then rvtools for sizing, zbx/prom for util
        for a in group:
            srv.source_refs.append({"source": a.source, "source_id": a.source_id,
                                    "ip": a.ip, "attrs": a.attrs})
            if a.source == "servicenow":
                srv.hostname = srv.hostname or a.hostname
                srv.fqdn = srv.fqdn or a.fqdn
                if a.ip and a.ip not in srv.ips:
                    srv.ips.append(a.ip)
                srv.os = _clean_os(a.attrs.get("os", srv.os))
                srv.os_version = a.attrs.get("os_version") or srv.os_version
                srv.cpu_cores = _to_int(a.attrs.get("cpus")) or srv.cpu_cores
                srv.mem_gb = _to_int(a.attrs.get("ram_gb")) or srv.mem_gb
                srv.subnet = a.attrs.get("subnet") or srv.subnet
                srv.datacenter = a.attrs.get("location") or srv.datacenter
                srv.env = (a.attrs.get("environment") or srv.env).lower() or srv.env
                srv.business_criticality = (a.attrs.get("business_criticality")
                                            or srv.business_criticality)
                virt = str(a.attrs.get("virtual", "")).lower()
                if virt == "false":
                    srv.source_type = "baremetal"
                srv.role = srv.role or _role_from_servicenow(a.attrs)
            elif a.source == "rvtools":
                srv.hostname = srv.hostname or a.hostname
                if a.ip and a.ip not in srv.ips:
                    srv.ips.append(a.ip)
                os_full = a.attrs.get("os", "")
                if os_full and not srv.os:
                    srv.os = _clean_os(os_full)
                srv.cpu_cores = _to_int(a.attrs.get("cpus")) or srv.cpu_cores
                mem_mb = _to_int(a.attrs.get("memory_mb"))
                if mem_mb:
                    srv.mem_gb = max(srv.mem_gb, mem_mb // 1024 or 1)
                srv.datacenter = a.attrs.get("datacenter") or srv.datacenter
                srv.cluster = a.attrs.get("cluster") or srv.cluster
                # disks
                for d in a.attrs.get("disks", []):
                    srv.disks.append(Disk(name=d.get("name", ""), size_gb=_to_int(d.get("size_gb")),
                                          kind=(d.get("kind") or "").lower(), fs=d.get("fs", "")))
                srv.source_type = "vm"  # rvtools only sees VMs
            elif a.source == "zabbix":
                srv.hostname = srv.hostname or a.hostname
                srv.fqdn = srv.fqdn or a.fqdn
                if a.ip and a.ip not in srv.ips:
                    srv.ips.append(a.ip)
                tags = a.attrs.get("tags") or {}
                srv.env = srv.env or (tags.get("env") or "").lower()
                srv.role = srv.role or (tags.get("role") or "").lower()
                u = a.attrs.get("utilization") or {}
                if u:
                    srv.utilization = Utilization(
                        cpu_p95=_to_float(u.get("cpu_p95")),
                        mem_p95=_to_float(u.get("mem_p95")),
                        disk_used_pct=_to_float(u.get("disk_used_pct")),
                        net_rx_mbps=_to_float(u.get("net_rx_mbps")),
                        net_tx_mbps=_to_float(u.get("net_tx_mbps")),
                        source="zabbix",
                    )
            elif a.source == "prometheus":
                srv.hostname = srv.hostname or a.hostname
                u = a.attrs.get("utilization") or {}
                if u and not srv.utilization.cpu_p95:
                    srv.utilization = Utilization(
                        cpu_p95=_to_float(u.get("cpu_p95")),
                        mem_p95=_to_float(u.get("mem_p95")),
                        disk_used_pct=_to_float(u.get("disk_used_pct")),
                        net_rx_mbps=_to_float(u.get("net_rx_mbps")),
                        net_tx_mbps=_to_float(u.get("net_tx_mbps")),
                        source="prometheus",
                    )
        # fallbacks / inference
        if not srv.role:
            srv.role = _infer_role({}, srv.tags, srv.os)
        if not srv.disks and srv.source_type == "vm":
            pass
        srv.role = srv.role or "app"
        if not srv.business_criticality:
            srv.business_criticality = "medium" if srv.env == "prod" else "low"
        if not srv.env:
            srv.env = "prod"
        srv.updated_at = srv.created_at
        servers.append(srv)
    return servers


def _clean_os(s: str) -> str:
    s = (s or "").strip().lower()
    if "centos" in s:
        return "centos"
    if "ubuntu" in s:
        return "ubuntu"
    if "oracle linux" in s or "oracle" in s:
        return "oracle linux"
    if "red hat" in s or "rhel" in s:
        return "rhel"
    if "windows" in s:
        return "windows"
    if "debian" in s:
        return "debian"
    if "suse" in s or "sles" in s:
        return "suse"
    return s or "unknown"


# ---------------------------------------------------------------------------
# workloads / app dependency graph
# ---------------------------------------------------------------------------
def load_workloads(path: Optional[str] = None) -> List[Workload]:
    p = path or str(FIXTURES / "apps.csv")
    if not os.path.exists(p):
        return []
    out: List[Workload] = []
    with open(p, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.append(Workload(
                app_id=row["app_id"], name=row.get("name", ""),
                tier=row.get("tier", ""), env=(row.get("env") or "").lower(),
                server_ids=[],  # resolved later by resolve_workloads()
                depends_on=[x for x in (row.get("depends_on") or "").split(";") if x],
                notes=row.get("notes", ""),
                # stash hostnames temporarily via attrs-free field
            ))
            out[-1].server_ids = [h for h in (row.get("server_hostnames") or "").split(";") if h]
    return out


def resolve_workloads(workloads: List[Workload], servers: List[Server]) -> List[Workload]:
    """Convert server_hostnames (stored in server_ids slot) → resolved server ids."""
    by_host = {s.hostname.lower(): s.id for s in servers}
    by_fqdn = {s.fqdn.lower(): s.id for s in servers if s.fqdn}
    for w in workloads:
        resolved = []
        for h in w.server_ids:
            hid = by_host.get(h.lower()) or by_fqdn.get(h.lower())
            if hid:
                resolved.append(hid)
        w.server_ids = resolved
    return workloads