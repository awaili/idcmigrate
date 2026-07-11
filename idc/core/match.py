"""Rule-based matching: source server → Tencent Cloud target.

Maps each ``Server`` to a ``Target`` (product + spec + region + az) with a
confidence score and a human-readable rationale. This is deterministic and
testable; LLM explanation is layered on top by the caller (see ``idc.llm``).

Target products covered: CVM, TKE, TencentDB for MySQL (CDB), TencentDB for
Redis, TDSQL (Oracle-compat), EMR, CPM, CBS (block storage), COS, CFS.

Spec sizing for CVM uses Tencent SA5 (AMD) standard types and picks the
smallest type that fits CPU+RAM, so the recommendation is right-sized rather
than over-provisioned. Adjust ``CVM_TYPES`` / ``REGION_MAP`` to fit the real
env.
"""
from __future__ import annotations

from typing import List, Tuple

from .models import Match, Server, Target

# datacenter → Tencent Cloud region (edit to match the real DC mapping)
REGION_MAP = {
    "dc1": "ap-shanghai",
    "dc2": "ap-beijing",
    "dc3": "ap-guangzhou",
    "default": "ap-shanghai",
}

# (vcpu, mem_gb, sku) — smallest fit wins; SA5 = AMD standard generation 5
CVM_TYPES = [
    (2, 4, "SA5.SMALL2"),
    (2, 8, "SA5.SMALL4"),
    (4, 8, "SA5.MEDIUM4"),
    (4, 16, "SA5.LARGE8"),
    (8, 16, "SA5.2XLARGE8"),
    (8, 32, "SA5.2XLARGE16"),
    (16, 32, "SA5.4XLARGE16"),
    (16, 64, "SA5.4XLARGE32"),
    (32, 128, "SA5.8XLARGE64"),
]

# TencentDB for MySQL spec by memory (HA edition)
CDB_MYSQL_SPECS = [
    (2, "MYSQL.HighMem - 2GB"), (4, "MYSQL.HighMem - 4GB"), (8, "MYSQL.HighMem - 8GB"),
    (16, "MYSQL.HighMem - 16GB"), (32, "MYSQL.HighMem - 32GB"), (64, "MYSQL.HighMem - 64GB"),
]

REDIS_SPECS = [
    (1, "Redis 标准版 1GB"), (2, "Redis 标准版 2GB"), (4, "Redis 标准版 4GB"),
    (8, "Redis 标准版 8GB"), (16, "Redis 标准版 16GB"), (32, "Redis 标准版 32GB"),
]


def _region(s: Server) -> str:
    return REGION_MAP.get((s.datacenter or "").lower(), REGION_MAP["default"])


def _az(region: str) -> str:
    # default to zone 3; real selection needs stock/HA constraints
    return f"{region}-3"


def _size_cvm(cpu: int, mem: int) -> Tuple[str, str]:
    for c, m, sku in CVM_TYPES:
        if c >= cpu and m >= mem:
            return sku, f"{c} vCPU / {m} GB"
    biggest = CVM_TYPES[-1]
    return biggest[2], f"{biggest[0]} vCPU / {biggest[1]} GB (upscale — review)"


def _size_by_mem(mem: int, table) -> Tuple[str, int]:
    for cap, spec in table:
        if cap >= mem:
            return spec, cap
    return table[-1][1], table[-1][0]


def _largest_data_disk_gb(s: Server) -> int:
    data = [d for d in s.disks if d.fs and d.fs not in ("/", "/boot") and d.size_gb > 0]
    return max((d.size_gb for d in data), default=0) or (max((d.size_gb for d in s.disks), default=0))


def match_server(s: Server) -> Match:
    region = _region(s)
    az = _az(region)
    cpu, mem = s.cpu_cores or 2, s.mem_gb or 4
    role = (s.role or "app").lower()
    stype = s.source_type or "vm"
    disk_gb = _largest_data_disk_gb(s)
    high = s.business_criticality.lower() == "high"

    # --- container platform ---------------------------------------------
    if stype in ("k8s-master", "k8s-node") or role == "k8s":
        tags_lc = [t.lower() for t in s.tags]
        is_master = (stype == "k8s-master" or "master" in s.hostname.lower()
                     or "control-plane" in tags_lc or "controlplane" in tags_lc)
        if is_master:
            tgt = Target(product="TKE", spec="托管集群(Managed Control Plane)",
                         region=region, az=az,
                         extras={"api": "managed control plane (no master VM to migrate)"},
                         notes="Use Tencent TKE managed cluster; masters are SaaS.")
            return Match(s.id, tgt, confidence=0.9, method="rule",
                         rationale="Kubernetes control plane → Tencent TKE managed control plane "
                                   "(no need to migrate master VMs).",
                         alternatives=["自建 TKE (CVM masters, more ops)"])
        # k8s worker
        sku, sz = _size_cvm(cpu, mem)
        tgt = Target(product="CVM", spec=sku, region=region, az=az,
                     extras={"tke_node_pool": True, "size": sz,
                             "system_disk": 50, "data_disk": disk_gb},
                     notes="TKE worker node — join managed TKE cluster node pool.")
        return Match(s.id, tgt, confidence=0.88, method="rule",
                     rationale=f"K8s worker ({cpu}vCPU/{mem}GB) → CVM {sku} as TKE node pool member.",
                     alternatives=["原生 Pod 迁移到 TKE 并弃用节点"])

    # --- big data / baremetal -------------------------------------------
    if stype == "baremetal" or role == "hadoop":
        tgt = Target(product="EMR", spec="EMR Hadoop cluster", region=region, az=az,
                     extras={"nn_size": f"{cpu}vCPU/{mem}GB", "data_disks": disk_gb},
                     notes="Tencent EMR managed Hadoop; re-onboard data via COS/HDFS distcp.")
        return Match(s.id, tgt, confidence=0.75, method="rule",
                     rationale="Bare-metal Hadoop node → Tencent EMR (managed). "
                               "Data migrated via COS distcp/HDFS sync.",
                     alternatives=["CPM 云物理机 (lift-and-shift, keep Hadoop stack)"])

    # --- databases ------------------------------------------------------
    if role == "db":
        # Detect the Oracle *DB engine* from tags/hostname only — NOT from OS,
        # because Oracle Linux normalizes to "oracle linux" and would misroute
        # a MySQL server running on it to the CVM lift-and-shift path.
        tags_lc = [t.lower() for t in s.tags]
        hints = " ".join(tags_lc) + " " + (s.hostname or "").lower()
        if "oracle" in hints:
            sku, sz = _size_cvm(cpu, mem)
            tgt = Target(product="CVM", spec=sku, region=region, az=az,
                         extras={"size": sz, "data_disk": disk_gb, "license": "Oracle BYOL"},
                         notes="Oracle licensed app — lift-and-shift to CVM (BYOL). "
                               "Evaluate TDSQL Oracle-compat for re-platforming later.")
            return Match(s.id, tgt, confidence=0.7, method="rule",
                         rationale="Oracle DB → CVM lift-and-shift (license mobility). "
                                   "Re-platforming to TDSQL is a separate project.",
                         alternatives=["TDSQL (Oracle 兼容)", "腾讯云数据库 Oracle 版"])
        # default mysql/mariadb/postgres
        spec, cap = _size_by_mem(mem, CDB_MYSQL_SPECS)
        edition = "高可用版(HA)" if high else "基础版"
        is_replica = "replica" in " ".join(s.tags).lower()
        if is_replica:
            edition = "只读实例(RO)"
        tgt = Target(product="CDB", spec=f"MySQL 8.0 {spec} {edition}",
                     region=region, az=az,
                     extras={"mem_gb": cap, "disk_gb": disk_gb or 500, "edition": edition},
                     notes=f"TencentDB for MySQL, disk ≈ {disk_gb or 500} GB.")
        return Match(s.id, tgt, confidence=0.9, method="rule",
                     rationale=f"DB role (mysql-class) → TencentDB for MySQL {edition}, "
                               f"sized by {mem}GB RAM / {disk_gb}GB data.",
                     alternatives=["自建 MySQL on CVM (more ops, license)"])

    # --- cache ----------------------------------------------------------
    if role == "cache":
        if high:
            spec, cap = _size_by_mem(mem, REDIS_SPECS)
            tgt = Target(product="TencentDB for Redis", spec=spec, region=region, az=az,
                         extras={"mem_gb": cap}, notes="Managed Redis (标准版集群).")
            return Match(s.id, tgt, confidence=0.85, method="rule",
                         rationale="Prod cache → TencentDB for Redis (managed, HA).",
                         alternatives=["自建 Redis on CVM"])
        sku, sz = _size_cvm(cpu, mem)
        tgt = Target(product="CVM", spec=sku, region=region, az=az,
                     extras={"size": sz, "data_disk": disk_gb},
                     notes="Non-critical cache → CVM self-hosted Redis.")
        return Match(s.id, tgt, confidence=0.8, method="rule",
                     rationale="Low-criticality cache → CVM with self-managed Redis.",
                     alternatives=["TencentDB for Redis"])

    # --- web / app / monitoring / general -------------------------------
    sku, sz = _size_cvm(cpu, mem)
    tgt = Target(product="CVM", spec=sku, region=region, az=az,
                 extras={"size": sz, "system_disk": 50, "data_disk": disk_gb,
                         "sg": "derived-from-vlan", "vpc": "derived-from-subnet"},
                 notes=f"Rehost to CVM {sku}; CBS data disk ≈ {disk_gb} GB.")
    rationale = f"{role or 'app'} server ({cpu}vCPU/{mem}GB) → CVM {sku} (right-sized)."
    if role == "web":
        rationale += " Front-end; consider CDN + CLB in front of the CVM group."
    if role == "monitoring":
        rationale += " Observability; consider migrating to Tencent Cloud Monitor."
    return Match(s.id, tgt, confidence=0.85, method="rule", rationale=rationale,
                 alternatives=["容器化到 TKE (refactor)"])


def match_servers(servers: List[Server]) -> List[Match]:
    return [match_server(s) for s in servers]