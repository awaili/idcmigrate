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

import dataclasses
import datetime
import math
from typing import List, Optional, Tuple

from .models import ALL_SOURCES, Match, Server, Target
from .eol import os_long_term_eol

# datacenter → Tencent Cloud region. Target landing zone is Thailand, so the
# whole estate maps to Tencent Cloud's Bangkok region (ap-bangkok) — Tencent
# Cloud has a single Thailand region, so every on-prem datacenter lands there.
REGION_MAP = {
    "dc1": "ap-bangkok",
    "dc2": "ap-bangkok",
    "dc3": "ap-bangkok",
    "default": "ap-bangkok",
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
# PostgreSQL managed DB uses the same mem tiers as MySQL but its own spec label,
# so the PG Target spec string never carries a "MYSQL" label (and cost.py maps
# the PG mem tier back onto the CDB price book at the same tier).
CDB_PG_SPECS = [
    (2, "PG.HighMem - 2GB"), (4, "PG.HighMem - 4GB"), (8, "PG.HighMem - 8GB"),
    (16, "PG.HighMem - 16GB"), (32, "PG.HighMem - 32GB"), (64, "PG.HighMem - 64GB"),
]

REDIS_SPECS = [
    (1, "Redis 标准版 1GB"), (2, "Redis 标准版 2GB"), (4, "Redis 标准版 4GB"),
    (8, "Redis 标准版 8GB"), (16, "Redis 标准版 16GB"), (32, "Redis 标准版 32GB"),
]


def _region(s: Server) -> str:
    return REGION_MAP.get((s.datacenter or "").lower(), REGION_MAP["default"])


# Default AZ per region. Tencent Cloud's Bangkok region (ap-bangkok) has 2 AZs
# (zone 1/2); zone 3 does NOT exist there, so the old hardcoded "{region}-3"
# emitted an invalid AZ into every match/Terraform plan. Default to zone 1
# (every region has at least one AZ); override per region when a different
# default is wanted. Real HA-aware AZ selection still needs stock info.
AZ_MAP = {
    "ap-bangkok": "ap-bangkok-1",
    "default": 1,  # integer => append as "{region}-{n}"
}


def _az(region: str) -> str:
    if region in AZ_MAP:
        az = AZ_MAP[region]
        return az if isinstance(az, str) else f"{region}-{az}"
    n = AZ_MAP.get("default", 1)
    return f"{region}-{n}"


# ---------------------------------------------------------------------------
# F3 — sizing strategy (pure; reused by match_server, right_size, cost.what_if)
# ---------------------------------------------------------------------------
# ``as_is``    : size from CMDB specs (cpu_cores/mem_gb) — lift-and-shift, keep
#                current capacity (the historical match_server behavior).
# ``measured`` / ``right_size`` : size from live utilization p95 — right-size to
#                actual use, with a headroom factor so we don't under-provision
#                spikes. Falls back to specs when no live utilization is present.
RIGHT_SIZE_HEADROOM = 1.3   # 30% headroom over observed p95 utilization


def _sizing_cpu_mem(s: Server, strategy: str = "as_is") -> Tuple[int, int]:
    """(cpu, mem_gb) used for target sizing, by strategy.

    See ``right_size`` for the public wrapper. ``measured`` interprets the
    utilization p95 percentages as a fraction of the on-prem capacity
    (cpu_cores/mem_gb), applies headroom, and rounds up to whole units.
    """
    strat = (strategy or "as_is").lower()
    spec_cpu = s.cpu_cores or 2
    spec_mem = s.mem_gb or 4
    if strat in ("measured", "right_size", "right-size"):
        u = s.utilization
        cpu_p = u.cpu_p95 if u and u.cpu_p95 is not None else None
        mem_p = u.mem_p95 if u and u.mem_p95 is not None else None
        if cpu_p is None or mem_p is None:
            return spec_cpu, spec_mem   # no live util -> keep specs
        cpu = max(2, math.ceil(spec_cpu * (cpu_p / 100.0) * RIGHT_SIZE_HEADROOM))
        mem = max(4, math.ceil(spec_mem * (mem_p / 100.0) * RIGHT_SIZE_HEADROOM))
        # never exceed the on-prem capacity (right-size down, not up)
        cpu = min(cpu, spec_cpu)
        mem = min(mem, spec_mem)
        return cpu, mem
    return spec_cpu, spec_mem


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


def _rehost_container(s: Server, region: str, az: str, cpu: int, mem: int,
                      disk_gb: int, reason: str) -> Match:
    """Rehost-container: containerize the workload onto a Tencent TKE managed
    cluster (running on a right-sized CVM worker node) to escape a self-managed
    middleware runtime or a long-term EOL/EOS OS — keep the app, discard the
    dead image / runtime.

    The target is a CVM worker (priced, right-sized), NOT the unpriced ``TKE``
    control-plane product, so the business case keeps an accurate run-cost for
    the ~3.9k hosts this can apply to. Mirrors the k8s-worker branch
    (CVM + ``tke_node_pool``).
    """
    sku, sz = _size_cvm(cpu, mem)
    tgt = Target(product="CVM", spec=sku, region=region, az=az,
                 extras={"size": sz, "system_disk": 50, "data_disk": disk_gb,
                         "tke_node_pool": True, "rehost_container": True,
                         "sg": "derived-from-vlan", "vpc": "derived-from-subnet"},
                 notes=f"Rehost-container onto TKE worker (CVM {sku}); CBS data "
                       f"disk ≈ {disk_gb} GB. Containerize to escape {reason}.")
    return Match(s.id, tgt, confidence=0.78, method="rule",
                 rationale=f"Rehost-container ({reason}) → containerize onto "
                           f"Tencent TKE worker (CVM {sku}, {cpu}vCPU/{mem}GB). "
                           f"Keeps the app; discards the {reason}.",
                 alternatives=[f"lift-and-shift to CVM {sku} as-is (keeps the "
                               f"{reason} — not recommended)",
                               "refactor to a managed service (separate project)"])


def _db_engine(tags_lc: List[str], hostname: str) -> str:
    """Best-effort DB engine detection from tags + hostname (NOT from OS —
    Oracle Linux would otherwise masquerade as the Oracle engine).

    Returns one of ``postgres`` / ``sqlserver`` / ``mysql`` / ``""``
    (unknown -> defaults to MySQL-class below). Oracle is handled separately
    by :func:`_is_oracle_db` because the ``oracle`` token is ambiguous
    (oracle-backup / oracle-client are not Oracle DBs).
    """
    tags = set(tags_lc)
    name = (hostname or "").lower()
    if "postgres" in tags or "postgresql" in tags or "pg" in tags \
            or "postgres" in name or name.startswith("pg-"):
        return "postgres"
    if "sqlserver" in tags or "mssql" in tags or "sqlserver" in name or "mssql" in name:
        return "sqlserver"
    if "mysql" in tags or "mariadb" in tags or "mysql" in name or "mariadb" in name:
        return "mysql"
    return ""


# oracle-adjacent tokens that do NOT indicate an Oracle DB server
_ORACLE_NON_DB = ("backup", "client", "gateway", "agent", "connect", "proxy")


def _is_oracle_db(tags_lc: List[str], hostname: str) -> bool:
    """True when the host is an Oracle *DB* (not an oracle-client/backup box).

    Within the ``role == "db"`` branch this still needs care: ServiceNow's
    ``_role_from_servicenow`` assigns role=db to ANY hostname containing
    "oracle", so ``oracle-backup-01`` reaches the DB branch. We accept oracle
    as a DB unless the token immediately after "oracle" names a non-DB role,
    or an explicit ``oracle-client``/``oracle-backup`` tag is present.
    """
    tags = set(tags_lc)
    for neg in ("oracle-client", "oracle-backup", "oracle-gateway", "oracle-agent"):
        if neg in tags:
            return False
    if "oracle-db" in tags or "oracle" in tags:
        return True
    name = (hostname or "").lower()
    if "oracle" not in name:
        return False
    tail = name.split("oracle", 1)[1].lstrip("-_. ")
    nxt = tail.split("-", 1)[0].split("_", 1)[0].split(".", 1)[0]
    return nxt not in _ORACLE_NON_DB


def _middleware_kind(tags) -> str:
    """The middleware kind set by ``normalize.detect_middleware`` (the
    ``mw:<kind>`` tag), or "" when none/unspecified."""
    for t in tags or []:
        low = (t or "").strip().lower()
        if low.startswith("mw:"):
            return low[3:]
    return ""


# DB engines with no first-class managed Tencent equivalent: the honest call is
# a CVM rehost (keep the engine, move the VM) with the managed-evaluation note.
# Mongo DOES have a managed equivalent (TencentDB for MongoDB) and is routed
# separately below.
_DB_NO_MANAGED = ("db2", "sybase", "cassandra", "clickhouse", "tidb",
                  "influx", "tdengine", "opengauss", "dameng", "kingbase")
_DB_MANAGED_HINT = {
    "db2": "TDSQL for DB2 (evaluate)",
    "sybase": "TencentDB (evaluate)",
    "cassandra": "TcaplusDB / self-managed on TKE",
    "clickhouse": "TDSQL-C ClickHouse",
    "tidb": "TDSQL-C TiDB",
    "influx": "TencentDB for InfluxDB / self-managed",
    "tdengine": "TDSQL-C TDengine",
    "opengauss": "TDSQL (openGauss)",
    "dameng": "TencentDB (DM, evaluate)",
    "kingbase": "TencentDB (Kingbase, evaluate)",
}


def _db_kind(tags) -> str:
    """The DB engine kind set by ``normalize.detect_db_engine`` (the
    ``db:<engine>`` tag), or "" when none/legacy."""
    for t in tags or []:
        low = (t or "").strip().lower()
        if low.startswith("db:"):
            return low[3:]
    return ""


# Object-store vs file-store kind from the ``object-store:<kind>`` /
# ``file-store:<kind>`` tag set by ``normalize.detect_object_store``. Returns
# ("", "") when the host is not a self-hosted storage node.
def _store_kind(tags) -> str:
    """The storage kind (``"minio"`` / ``"gluster"`` / ...) or ``""``."""
    for t in tags or []:
        low = (t or "").strip().lower()
        if low.startswith("object-store:") or low.startswith("file-store:"):
            return low.split(":", 1)[1]
    return ""


def _store_category(tags) -> str:
    """``"object"`` / ``"file"`` / ``""``."""
    for t in tags or []:
        low = (t or "").strip().lower()
        if low == "object-store" or low.startswith("object-store:"):
            return "object"
        if low == "file-store" or low.startswith("file-store:"):
            return "file"
    return ""


def _cache_kind(tags) -> str:
    """The cache kind (``"redis"`` / ``"memcache"``) or ``""``."""
    for t in tags or []:
        low = (t or "").strip().lower()
        if low in ("redis", "memcache", "memcached"):
            return "redis" if low == "redis" else "memcache"
    return ""


# managed-service alternative per middleware kind (the "replatform" option the
# operator can pick instead of self-managing on TKE). Grounded in Tencent's
# managed equivalents; "" kinds fall through with no alt.
_MIDDLEWARE_MANAGED_ALT = {
    "kafka": "Tencent CKafka (managed Kafka) — replatform instead of self-managed",
    "rabbitmq": "Tencent TDMQ for RabbitMQ (managed)",
    "activemq": "Tencent TDMQ (managed message queue)",
    "rocketmq": "Tencent TDMQ for RocketMQ (managed)",
    "pulsar": "Tencent TDMQ for Pulsar (managed)",
    "zookeeper": "Tencent TSE ZooKeeper (managed coordination)",
    "tomcat": "containerize on TKE (managed runtime)",
    "weblogic": "containerize on TKE / migrate to Oracle WebLogic on cloud",
    "jboss": "containerize on TKE (JBoss EAP)",
    "websphere": "containerize on TKE (WebSphere Liberty)",
    "jetty": "containerize on TKE (managed runtime)",
    "elasticsearch": "Tencent Cloud Search / ES Service (managed)",
    "flink": "Tencent Oceanus (managed streaming)",
    "spark": "Tencent EMR Spark (managed)",
    "zmq": "containerize on TKE",
}

# Middleware kinds that have a DISTINCT managed Tencent service → replatform to
# it as the PRIMARY recommendation (rehost-container becomes the fallback). The
# managed target carries a CVM-equivalent SKU so cost.py prices it under the CVM
# book at the same footprint the self-managed node occupied — so flipping the
# recommendation from rehost-container to managed does NOT change TCO (managed
# pricing is usage-based and not in the bundled book; the CVM figure is a sizing
# placeholder, noted in the rationale). spark routes to EMR (priced in the book).
# App-server runtimes (tomcat/weblogic/jboss/websphere/jetty/zmq) have NO distinct
# managed product — "containerize on TKE" IS the managed-runtime replatform, so
# they keep rehost-container as the primary (handled below, not in this map).
_MIDDLEWARE_MANAGED_TARGET = {
    "kafka":         ("Tencent CKafka",       "Tencent CKafka (managed Kafka)"),
    "zookeeper":     ("Tencent TSE ZooKeeper", "Tencent TSE ZooKeeper (managed coordination)"),
    "rabbitmq":      ("Tencent TDMQ",         "Tencent TDMQ for RabbitMQ (managed)"),
    "activemq":      ("Tencent TDMQ",         "Tencent TDMQ (managed message queue)"),
    "rocketmq":      ("Tencent TDMQ",         "Tencent TDMQ for RocketMQ (managed)"),
    "pulsar":        ("Tencent TDMQ",         "Tencent TDMQ for Pulsar (managed)"),
    "elasticsearch": ("Tencent Cloud Search", "Tencent Cloud Search / ES (managed)"),
    "flink":         ("Tencent Oceanus",      "Tencent Oceanus (managed streaming)"),
    "spark":         ("EMR",                  "Tencent EMR Spark (managed)"),
}


def _data_disk_gb(s: Server) -> int:
    """Aggregate data-disk capacity for cloud block-storage sizing.

    A traditional-IDC DB server typically has several data disks (RAID10/5
    across N spindles). The cloud replaces the RAID group with a single CBS
    volume sized to the *total* on-prem data capacity, so we sum the data
    disks — not the largest single disk (the old behaviour under-provisioned
    CBS by a factor of N, the most common DB shape). Falls back to the largest
    single disk when no dedicated data filesystem is present.
    """
    data = [d for d in s.disks if d.fs and d.fs not in ("/", "/boot") and d.size_gb > 0]
    if data:
        return sum(d.size_gb for d in data)
    return max((d.size_gb for d in s.disks), default=0)


# ---------------------------------------------------------------------------
# F4 — data-coverage confidence: how much do we actually know about this host?
# ---------------------------------------------------------------------------
# The base rule confidence (match_server's hardcoded 0.7-0.9) says "how well the
# rule fits the role/sizing". The assessment-confidence below says "how much
# real data backs that sizing". The final persisted confidence is base x
# coverage x code-intel dips (see codeintel.enrich_match), so a thinly-known
# host is never reported as high-confidence even when its role matches a rule.

# fields whose presence indicates the host is actually characterized
_KEY_FIELDS = ("role", "os", "cpu_cores", "mem_gb", "env", "business_criticality")


def _utilization_is_measured(s: Server) -> bool:
    """True when live utilization telemetry drives sizing (not just CMDB specs)."""
    u = s.utilization
    return bool(u and u.source in ("zabbix", "prometheus")
                and u.cpu_p95 is not None and u.mem_p95 is not None)


def sizing_basis_of(s: Server) -> str:
    """'measured' when live utilization drives sizing, else 'estimated'."""
    return "measured" if _utilization_is_measured(s) else "estimated"


def _provenance_coverage(s: Server) -> float:
    """Fraction of key characterization fields populated (0..1)."""
    vals = (s.role, s.os, s.cpu_cores, s.mem_gb, s.env, s.business_criticality)
    present = sum(1 for v in vals if v)
    return present / len(_KEY_FIELDS)


def assessment_confidence_of(s: Server, has_code_profile: bool = False) -> float:
    """Data-coverage confidence score in [0,1] (F4).

    Weighted: 0.25 source diversity + 0.35 live-utilization history + 0.20
    key-field provenance coverage + 0.20 executor code profile. A host with
    all four sources, live utilization, full key fields and a code profile
    scores ~1.0; a CMDB-only host with no telemetry scores ~0.45.
    """
    src_count = len({r.get("source") for r in s.source_refs
                     if r.get("source") in ALL_SOURCES})
    diversity = min(src_count / 4.0, 1.0)
    if _utilization_is_measured(s):
        util = 1.0
    elif s.utilization and s.utilization.cpu_p95 is not None:
        util = 0.4   # some utilization present but not from a live source
    else:
        util = 0.0
    cov = _provenance_coverage(s)
    profile = 1.0 if has_code_profile else 0.0
    score = 0.25 * diversity + 0.35 * util + 0.20 * cov + 0.20 * profile
    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# hardware support bucket (traditional-IDC reality: CMDB often lacks this)
# ---------------------------------------------------------------------------
# A host's hardware-warranty / end-of-support status is frequently missing,
# stale, or wrong in a traditional IDC (脱保 boxes, shadow IT, manual racking
# with no CMDB update). The bucket below is the single derived signal the
# cost / readiness / data-gaps layers read. Explicit ``Server.warranty_status``
# (operator/source truth) wins; otherwise the bucket is derived from
# ``hardware_eol`` vs today. ``unknown`` is the neutral default — it lowers no
# confidence score (we don't penalize what we haven't assessed) but it IS
# counted in the data-gaps "unknown warranty" report so the operator sees the
# blind spot. See docs/fusion-implementation-plan.md (data-gap gap).
WARRANTY_ACTIVE = "active"
WARRANTY_EXPIRING = "expiring"
WARRANTY_EXPIRED = "expired"
WARRANTY_UNKNOWN = "unknown"
WARRANTY_EXPIRING_DAYS = 90  # expiring window: < this many days to hardware_eol


def _today_iso() -> str:
    return datetime.date.today().isoformat()


def warranty_bucket(s: Server, today_iso: Optional[str] = None) -> str:
    """Hardware support bucket: active / expiring / expired / unknown.

    Explicit ``Server.warranty_status`` (one of the WARRANTY_* constants, or
    ``""`` = not assessed) wins. When unset, the bucket is derived from
    ``hardware_eol`` (an ISO ``YYYY-MM-DD`` date) compared to ``today_iso``
    (default: today). Returns ``unknown`` when neither is available — the
    traditional-IDC reality where CMDB has no support data. Pure w.r.t. the
    server + the supplied today; default-today is the only non-determinism
    and tests pass an explicit ``today_iso`` for reproducibility.
    """
    ws = (s.warranty_status or "").strip().lower()
    if ws in (WARRANTY_ACTIVE, WARRANTY_EXPIRING, WARRANTY_EXPIRED, WARRANTY_UNKNOWN):
        return ws
    eol = (s.hardware_eol or "").strip()
    if not eol:
        return WARRANTY_UNKNOWN
    today = today_iso or _today_iso()
    try:
        eol_d = datetime.date.fromisoformat(eol)
        today_d = datetime.date.fromisoformat(today)
    except ValueError:
        return WARRANTY_UNKNOWN
    if eol_d <= today_d:
        return WARRANTY_EXPIRED
    if (eol_d - today_d).days <= WARRANTY_EXPIRING_DAYS:
        return WARRANTY_EXPIRING
    return WARRANTY_ACTIVE


def match_server(s: Server, sizing_strategy: str = "as_is") -> Match:
    """Rule-match one server to a Tencent Cloud target.

    ``sizing_strategy`` is forwarded to ``_sizing_cpu_mem`` so the same rule
    engine drives both the default ``as_is`` match and the F3 ``measured``
    right-size path (see ``right_size``). Defaults to ``as_is`` (lift-and-shift
    keeps on-prem capacity) — the historical behavior, so existing callers and
    ``test_pipeline`` are unaffected.
    """
    region = _region(s)
    az = _az(region)
    cpu, mem = _sizing_cpu_mem(s, sizing_strategy)
    role = (s.role or "app").lower()
    stype = (s.source_type or "vm").lower()
    disk_gb = _data_disk_gb(s)
    high = (s.business_criticality or "").lower() == "high"

    # --- self-hosted object / file storage ------------------------------
    # Intercepted BEFORE the role branches so a storage node (role stays "app"
    # from normalize, tagged object-store/file-store) is never lift-and-shifted
    # as a plain CVM. minio/oss/s3 → COS (object); gluster/ceph/nfs → CFS (file).
    # Priced via a CVM-equivalent SKU (the bundled book has no COS/CFS line) —
    # the rationale notes actual cost is storage/usage-driven.
    cat = _store_category(s.tags or [])
    if cat:
        sku, sz = _size_cvm(cpu, mem)
        if cat == "object":
            kind = _store_kind(s.tags or "")
            tgt = Target(product="COS", spec=f"对象存储 Standard ({sku})",
                         region=region, az=az,
                         extras={"size": sz, "data_disk": disk_gb,
                                 "replatform": True, "source": kind or "minio"},
                         notes=f"Self-hosted object store ({kind or 'minio'}) → Replatform to "
                               f"Tencent COS (managed object storage). Disk ≈ {disk_gb} GB. "
                               f"Usage-based; the CVM-equivalent SKU is a sizing placeholder.")
            return Match(s.id, tgt, confidence=0.78, method="rule",
                         rationale=f"Object storage ({kind or 'minio'}) → Tencent COS "
                                   "(managed). Migrate buckets/objects, retire the node.",
                         alternatives=["自建 MinIO on CVM",
                                       "CFS 文件存储 (if semantically a file share)"])
        kind = _store_kind(s.tags or "")
        tgt = Target(product="CFS", spec=f"文件存储 Standard ({sku})",
                     region=region, az=az,
                     extras={"size": sz, "data_disk": disk_gb,
                             "replatform": True, "source": kind or "gluster"},
                     notes=f"Self-hosted file store ({kind or 'gluster'}) → Replatform to "
                           f"Tencent CFS (managed file storage). Disk ≈ {disk_gb} GB. "
                           "Usage-based; the CVM-equivalent SKU is a sizing placeholder.")
        return Match(s.id, tgt, confidence=0.74, method="rule",
                     rationale=f"File storage ({kind or 'gluster'}) → Tencent CFS (managed). "
                               "Migrate shares, retire the node.",
                     alternatives=["自建 Gluster/Ceph on CVM",
                                   "COS 对象存储 (if semantically objects)"])

    # --- container platform ---------------------------------------------
    if stype in ("k8s-master", "k8s-node") or role == "k8s":
        tags_lc = [t.lower() for t in (s.tags or [])]
        is_master = (stype == "k8s-master" or "master" in (s.hostname or "").lower()
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

    # --- PaaS platform -------------------------------------------------
    # Inventory analysis step 1 is per-item PaaS detection (normalize.detect_paas
    # → role "paas"). A self-hosted PaaS / middleware platform (the on-prem
    # ``zpaas`` stack) is NOT a lift-and-shift candidate: rehosting the platform
    # control-plane VMs keeps you on a proprietary self-managed PaaS. The right
    # call is to replatform the hosted workloads onto a managed / cloud-native
    # platform (Tencent TKE), which is the immediate Replatform recommendation.
    if role == "paas":
        tgt = Target(product="TKE", spec="TKE 托管集群 (managed, replatform)",
                     region=region, az=az,
                     extras={"workload_shape": f"{cpu}vCPU/{mem}GB",
                             "data_disk": disk_gb, "replatform": True},
                     notes="Self-hosted PaaS platform → replatform workloads onto "
                           "Tencent TKE managed cluster; do NOT lift-and-shift the "
                           "platform control-plane VMs.")
        return Match(s.id, tgt, confidence=0.8, method="rule",
                     rationale="PaaS platform host (zpaas) → Replatform to managed "
                               "TKE (cloud-native). Keep the platform's hosted apps, "
                               "discard the self-managed control plane.",
                     alternatives=["CVM lift-and-shift (keeps the self-hosted PaaS — not recommended)",
                                  "容器化各应用到 TKE 并退役平台节点"])

    # --- middleware / app-server / MQ ----------------------------------
    # Inventory analysis step 1 judges middleware as Replatform-FIRST: a
    # self-managed runtime (Kafka / ZooKeeper / RabbitMQ / Elasticsearch /
    # Flink / Spark) that has a distinct Tencent managed service replatforms to
    # it as the primary call (rehost-container is the fallback). App-server
    # runtimes (Tomcat / WebLogic / JBoss / WebSphere / Jetty / ZeroMQ) have no
    # distinct managed product — "containerize on TKE" IS the managed-runtime
    # replatform, so rehost-container stays their primary.
    if role == "middleware":
        mw = _middleware_kind(s.tags)
        managed = _MIDDLEWARE_MANAGED_TARGET.get(mw)
        if managed:
            product, label = managed
            sku, sz = _size_cvm(cpu, mem)
            if product == "EMR":
                tgt = Target(product="EMR", spec=f"EMR Spark ({sku})",
                             region=region, az=az,
                             extras={"size": sz, "data_disk": disk_gb,
                                     "replatform": True, "source": "spark"},
                             notes=f"Self-managed Spark → Replatform to {label}; "
                                   "re-onboard data via COS/distcp.")
            else:
                tgt = Target(product=product, spec=f"{label} ({sku})",
                             region=region, az=az,
                             extras={"size": sz, "data_disk": disk_gb,
                                     "replatform": True, "source": mw},
                             notes=f"Self-managed {mw} middleware → Replatform to {label} "
                                   "(managed). Discards the self-managed runtime VM. "
                                   "Usage-based; the CVM-equivalent SKU is a sizing placeholder "
                                   "≈ the node footprint it replaces.")
            return Match(s.id, tgt, confidence=0.82, method="rule",
                         rationale=f"Middleware ({mw}) → Replatform to {label} (managed) "
                                   "as the first call; containerize onto TKE only if a managed "
                                   "service doesn't fit the workload.",
                         alternatives=[
                             f"rehost-container onto TKE worker (CVM {sku}) — containerize the self-managed {mw} runtime",
                             "lift-and-shift to CVM as-is (keeps the self-managed middleware — not recommended)",
                         ])
        # app-server runtime: no distinct managed product → containerize on TKE
        # (the managed-runtime replatform) is the primary; managed-service note
        # stays as an alternative where one exists (e.g. Oracle WebLogic on cloud).
        m = _rehost_container(s, region, az, cpu, mem, disk_gb,
                              f"self-managed {mw} middleware" if mw
                              else "self-managed middleware runtime")
        alt = _MIDDLEWARE_MANAGED_ALT.get(mw)
        if alt and alt not in (m.alternatives or []):
            m.alternatives = list(m.alternatives or []) + [alt]
        return m

    # --- big data ------------------------------------------------------
    # EMR is for Hadoop workloads regardless of metal/virtual. NB: do NOT gate
    # on ``stype == "baremetal"`` here — that sent EVERY bare-metal host (Oracle
    # DB, web, postgres ...) to EMR before the db/cache branches could run.
    # A bare-metal non-Hadoop host falls through to its role branch below.
    if role == "hadoop":
        tgt = Target(product="EMR", spec="EMR Hadoop cluster", region=region, az=az,
                     extras={"nn_size": f"{cpu}vCPU/{mem}GB", "data_disks": disk_gb},
                     notes="Tencent EMR managed Hadoop; re-onboard data via COS/HDFS distcp.")
        return Match(s.id, tgt, confidence=0.75, method="rule",
                     rationale="Hadoop node → Tencent EMR (managed). "
                               "Data migrated via COS distcp/HDFS sync.",
                     alternatives=["CPM 云物理机 (lift-and-shift, keep Hadoop stack)"])

    # --- databases ------------------------------------------------------
    if role == "db":
        # Detect the DB *engine* from tags/hostname only — NOT from OS, because
        # Oracle Linux normalizes to "oracle linux" and would misroute a MySQL
        # server running on it. Require the db role AND a specific engine token
        # so a host merely named "oracle-backup-01" / tagged "oracle-client"
        # (an app that *connects to* Oracle) is not lifted-and-shifted as an
        # Oracle DB.
        tags_lc = [t.lower() for t in (s.tags or [])]
        # The engine comes from the ``db:<engine>`` tag set by
        # ``normalize.detect_db_engine`` (mount-based, the live-estate signal),
        # falling back to the legacy ``_db_engine`` (tags + hostname) path so
        # hand-built/tagged servers still route correctly.
        engine = _db_kind(tags_lc) or _db_engine(tags_lc, s.hostname or "")
        if _is_oracle_db(tags_lc, s.hostname or ""):
            # Oracle → Replatform to TData (TencentDB for Oracle, managed
            # Oracle-compatible). Lift-and-shift to CVM BYOL keeps the license
            # but locks the workload onto a self-managed instance; replatforming
            # to TData is the recommended first call (managed, no PL/SQL rewrite
            # for the compatible subset). CVM BYOL stays as the fallback.
            _, cap = _size_by_mem(mem, CDB_MYSQL_SPECS)
            edition = "高可用版(HA)" if high else "基础版"
            tgt = Target(product="TData", spec=f"Oracle-compat {cap}GB {edition}",
                         region=region, az=az,
                         extras={"mem_gb": cap, "data_disk": disk_gb or 500,
                                 "edition": edition, "replatform": True,
                                 "source_engine": "oracle"},
                         notes=f"TData (TencentDB for Oracle, managed Oracle-compatible), "
                               f"disk ≈ {disk_gb or 500} GB. Replatform, not lift-and-shift.")
            return Match(s.id, tgt, confidence=0.78, method="rule",
                         rationale="Oracle DB → Replatform to TData (managed "
                                   "Oracle-compatible). Avoids self-managed CVM + "
                                   "BYOL lock-in; review PL/SQL for the non-compatible tail.",
                         alternatives=["CVM lift-and-shift (Oracle BYOL, license mobility)",
                                       "TDSQL (Oracle 兼容)"])
        if engine == "postgres":
            spec, cap = _size_by_mem(mem, CDB_PG_SPECS)  # PG-labeled tiers
            edition = "高可用版(HA)" if high else "基础版"
            tgt = Target(product="TencentDB for PostgreSQL",
                         spec=f"PostgreSQL 16 {spec} {edition}",
                         region=region, az=az,
                         extras={"mem_gb": cap, "disk_gb": disk_gb or 500, "edition": edition},
                         notes=f"TencentDB for PostgreSQL, disk ≈ {disk_gb or 500} GB.")
            return Match(s.id, tgt, confidence=0.9, method="rule",
                         rationale=f"DB role (postgresql) → TencentDB for PostgreSQL {edition}, "
                                   f"sized by {mem}GB RAM / {disk_gb}GB data.",
                         alternatives=["自建 PostgreSQL on CVM"])
        if engine == "sqlserver":
            sku, sz = _size_cvm(cpu, mem)
            tgt = Target(product="TencentDB for SQL Server",
                         spec=f"SQL Server {sku} {('HA' if high else 'Basic')}",
                         region=region, az=az,
                         extras={"size": sz, "data_disk": disk_gb or 500,
                                 "edition": "HA" if high else "Basic"},
                         notes="TencentDB for SQL Server (managed). License included/BYOL.")
            return Match(s.id, tgt, confidence=0.85, method="rule",
                         rationale="DB role (sqlserver) → TencentDB for SQL Server (managed).",
                         alternatives=["自建 SQL Server on CVM (BYOL)"])
        if engine == "mongo":
            # MongoDB → Replatform to TencentDB for MongoDB (managed replica
            # set / sharded cluster). Sized by the CDB (MySQL) mem tiers so the
            # business case prices it (cost.py aliases the product onto CDB).
            spec, cap = _size_by_mem(mem, CDB_MYSQL_SPECS)
            edition = "高可用版(HA)" if high else "基础版"
            tgt = Target(product="TencentDB for MongoDB",
                         spec=f"MongoDB {spec} {edition}",
                         region=region, az=az,
                         extras={"mem_gb": cap, "disk_gb": disk_gb or 500,
                                 "edition": edition, "replatform": True,
                                 "source_engine": "mongo"},
                         notes=f"TencentDB for MongoDB (managed), disk ≈ {disk_gb or 500} GB. "
                               "Replatform from self-managed Mongo; review drivers/indices.")
            return Match(s.id, tgt, confidence=0.82, method="rule",
                         rationale="MongoDB → Replatform to TencentDB for MongoDB (managed). "
                                   "Avoids self-managed replica-set/sharding ops.",
                         alternatives=["自建 MongoDB on CVM (replica set)",
                                       "容器化 Mongo on TKE (rehost-container)"])
        if engine in _DB_NO_MANAGED:
            # No first-class managed Tencent equivalent → honest CVM rehost (keep
            # the engine, move the VM); the managed option is a separate
            # evaluation noted in the alternatives, not a forced replatform.
            sku, sz = _size_cvm(cpu, mem)
            hint = _DB_MANAGED_HINT.get(engine, "evaluate managed DB")
            tgt = Target(product="CVM", spec=sku, region=region, az=az,
                         extras={"size": sz, "data_disk": disk_gb or 500},
                         notes=f"Self-managed {engine} DB → CVM rehost (no first-class managed "
                               f"Tencent equivalent for {engine}). Disk ≈ {disk_gb or 500} GB.")
            return Match(s.id, tgt, confidence=0.7, method="rule",
                         rationale=f"DB role ({engine}) → CVM rehost; managed equivalent "
                                   f"({hint}) is a separate evaluation.",
                         alternatives=[hint, "容器化到 TKE (rehost-container)"])
        # default mysql/mariadb
        spec, cap = _size_by_mem(mem, CDB_MYSQL_SPECS)
        edition = "高可用版(HA)" if high else "基础版"
        is_replica = "replica" in {(t or "").lower() for t in (s.tags or [])}
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
        # A Redis host is pre-judged as managed PaaS directly (TencentDB for
        # Redis), NOT gated on the `high` criticality flag — the live estate
        # carries no criticality data, so the old gate sent every Redis host to
        # CVM self-hosted. Non-Redis cache (e.g. memcache) keeps the criticality
        # gate: prod → managed Redis, low-crit → CVM.
        if _cache_kind(s.tags or []) == "redis" or high:
            spec, cap = _size_by_mem(mem, REDIS_SPECS)
            tgt = Target(product="TencentDB for Redis", spec=spec, region=region, az=az,
                         extras={"mem_gb": cap}, notes="Managed Redis (标准版集群).")
            return Match(s.id, tgt, confidence=0.85, method="rule",
                         rationale="Redis cache → TencentDB for Redis (managed, HA). "
                                   "Replatform from self-managed Redis.",
                         alternatives=["自建 Redis on CVM"])
        sku, sz = _size_cvm(cpu, mem)
        tgt = Target(product="CVM", spec=sku, region=region, az=az,
                     extras={"size": sz, "data_disk": disk_gb},
                     notes="Non-critical cache → CVM self-hosted Redis.")
        return Match(s.id, tgt, confidence=0.8, method="rule",
                     rationale="Low-criticality cache → CVM with self-managed Redis.",
                     alternatives=["TencentDB for Redis"])

    # --- long-term EOL/EOS OS → rehost-container -------------------------
    # A long-dead OS image must not be lift-and-shifted. Only CVM-bound roles
    # (app/web/monitoring/general) reach here — db/cache/k8s/paas/hadoop/
    # middleware already escaped the OS via a managed / container target above,
    # so this never overrides a managed-service recommendation. Containerize
    # onto a supported TKE base image to escape the EOL OS.
    if os_long_term_eol(s):
        return _rehost_container(s, region, az, cpu, mem, disk_gb,
                                 "long-term EOL/EOS OS")

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


def match_servers(servers: List[Server], sizing_strategy: str = "as_is") -> List[Match]:
    return [match_server(s, sizing_strategy) for s in servers]


# ---------------------------------------------------------------------------
# F3 — conversational what-if pure functions (no ingest re-run)
# ---------------------------------------------------------------------------
# These wrap match_server so the LLM copilot / ``cost.what_if`` can answer
# "what if I right-size this host?" / "what if I re-price it in ap-bangkok?"
# without re-running ingest. Each returns a NEW Match (the original is
# untouched) so the caller can diff targets or feed both into cost.what_if
# for a delta. They are pure: same inputs -> same outputs, no DB, no LLM.


def right_size(server: Server, strategy: str = "measured") -> Match:
    """Re-match a server under an alternate sizing strategy (F3).

    ``strategy``:
      * ``"as_is"``     — keep on-prem capacity (cpu_cores/mem_gb); the
                          lift-and-shift default. Identical to ``match_server``.
      * ``"measured"`` / ``"right_size"`` — right-size from live utilization
                          p95 with headroom (see ``_sizing_cpu_mem``). Smaller
                          spec when the host is under-used; falls back to specs
                          when no live utilization is available.

    Returns a fresh ``Match`` whose ``target`` reflects the new sizing. The
    rationale is annotated with the strategy + the cpu/mem used so the LLM can
    narrate the change. Pure function.
    """
    m = match_server(server, sizing_strategy=strategy)
    cpu, mem = _sizing_cpu_mem(server, strategy)
    note = f" [what-if sizing={strategy} -> {cpu}vCPU/{mem}GB]"
    return dataclasses.replace(m, rationale=(m.rationale or "") + note)


def re_region(server: Server, region: str) -> Match:
    """Re-match a server as if its target region were ``region`` (F3).

    Keeps the product/spec sizing (``as_is``) and only swaps the region + AZ,
    so the caller can compare run-cost across regions without re-ingest. Pure.
    """
    m = match_server(server, sizing_strategy="as_is")
    new_target = dataclasses.replace(m.target, region=region, az=_az(region))
    note = f" [what-if region={region}]"
    return dataclasses.replace(m, target=new_target,
                               rationale=(m.rationale or "") + note)