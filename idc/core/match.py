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


def _largest_data_disk_gb(s: Server) -> int:
    data = [d for d in s.disks if d.fs and d.fs not in ("/", "/boot") and d.size_gb > 0]
    return max((d.size_gb for d in data), default=0) or (max((d.size_gb for d in s.disks), default=0))


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
    if eol_d < today_d:
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


def match_servers(servers: List[Server], sizing_strategy: str = "as_is") -> List[Match]:
    return [match_server(s, sizing_strategy) for s in servers]


# ---------------------------------------------------------------------------
# F3 — conversational what-if pure functions (no ingest re-run)
# ---------------------------------------------------------------------------
# These wrap match_server so the LLM copilot / ``cost.what_if`` can answer
# "what if I right-size this host?" / "what if I move it to ap-guangzhou?"
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