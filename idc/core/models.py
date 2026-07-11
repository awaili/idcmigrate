"""Domain model for idc-migrate.

The unit of migration is a ``Server`` (a VM, bare-metal host, or k8s node).
Four inventory sources (ServiceNow CMDB, RVTools, Zabbix, Prometheus) each
emit ``RawAsset`` records that are normalized and merged into ``Server``
records, with per-field provenance kept in ``source_refs``.

Everything is plain dataclasses + stdlib JSON so the core has zero hard deps
and is shared verbatim by the CLI and the web backend.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# source provenance
# ---------------------------------------------------------------------------
SOURCE_SERVICENOW = "servicenow"
SOURCE_RVTOOLS = "rvtools"
SOURCE_ZABBIX = "zabbix"
SOURCE_PROMETHEUS = "prometheus"
SOURCE_FIXTURE = "fixture"
ALL_SOURCES = (SOURCE_SERVICENOW, SOURCE_RVTOOLS, SOURCE_ZABBIX, SOURCE_PROMETHEUS)


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# building blocks
# ---------------------------------------------------------------------------
@dataclass
class Disk:
    name: str = ""
    size_gb: int = 0
    kind: str = ""  # hdd / ssd / nvme / san / vmdk
    fs: str = ""    # mount / drive letter

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Disk":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


@dataclass
class Utilization:
    cpu_p95: Optional[float] = None       # percent 0-100
    mem_p95: Optional[float] = None       # percent 0-100
    disk_used_pct: Optional[float] = None # percent 0-100
    net_rx_mbps: Optional[float] = None
    net_tx_mbps: Optional[float] = None
    source: str = ""                      # which source provided utilization

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Utilization":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# raw asset (per-source, pre-normalization)
# ---------------------------------------------------------------------------
@dataclass
class RawAsset:
    """One row from one source, before cross-source merge."""
    source: str                      # one of SOURCE_*
    source_id: str                   # id within that source (cmdb sys_id, vm uuid, zabbix hostid, ...)
    hostname: str = ""
    fqdn: str = ""
    ip: str = ""
    attrs: Dict[str, Any] = field(default_factory=dict)  # everything else, source-specific
    ingested_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RawAsset":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# unified server (the unit of migration)
# ---------------------------------------------------------------------------
@dataclass
class Server:
    id: str = field(default_factory=lambda: _new_id("srv"))
    hostname: str = ""
    fqdn: str = ""
    ips: List[str] = field(default_factory=list)
    source_type: str = "vm"          # vm / baremetal / k8s-node / k8s-master
    role: str = ""                   # app / web / db / cache / hadoop / k8s / general
    os: str = ""                     # e.g. centos, windows, rhel
    os_version: str = ""
    cpu_cores: int = 0
    cpu_model: str = ""
    mem_gb: int = 0
    disks: List[Disk] = field(default_factory=list)
    subnet: str = ""
    vlan: str = ""
    datacenter: str = ""
    cluster: str = ""                # vcenter cluster / k8s cluster
    env: str = ""                    # prod / staging / dev
    app_ids: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    business_criticality: str = ""  # high / medium / low
    migration_tier: str = ""         # 1..4 (migration priority)
    utilization: Utilization = field(default_factory=Utilization)
    # which sources contributed + their raw payload (provenance / audit trail)
    source_refs: List[Dict[str, Any]] = field(default_factory=list)
    status: str = "discovered"       # discovered / matched / planned / migrated / verified
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    # convenience
    @property
    def total_disk_gb(self) -> int:
        return sum(d.size_gb for d in self.disks)

    @property
    def identity_key(self) -> str:
        """Best-effort stable key for cross-source entity resolution."""
        return (self.fqdn or self.hostname or "").lower().strip()

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Server":
        d = dict(d)
        disks = [Disk.from_dict(x) for x in d.pop("disks", []) or []]
        util = Utilization.from_dict(d.pop("utilization", {}) or {})
        return cls(**d, disks=disks, utilization=util)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# workload / app dependency
# ---------------------------------------------------------------------------
@dataclass
class Workload:
    app_id: str
    name: str = ""
    tier: str = ""                  # frontend / backend / data / infra
    env: str = ""
    server_ids: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)  # other app_ids
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Workload":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# target + match
# ---------------------------------------------------------------------------
@dataclass
class Target:
    product: str = ""               # CVM / TKE / CDB / BM / COS / CFS / EMR / VPC / SG ...
    spec: str = ""                  # instance type / sku
    region: str = ""
    az: str = ""
    extras: Dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Target":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


@dataclass
class Match:
    server_id: str
    target: Target = field(default_factory=Target)
    confidence: float = 0.0         # 0..1
    method: str = "rule"            # rule / llm / hybrid
    rationale: str = ""
    alternatives: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Match":
        d = dict(d)
        tgt = Target.from_dict(d.pop("target", {}) or {})
        return cls(**d, target=tgt)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# wave plan
# ---------------------------------------------------------------------------
STAGE_LZ = "1_landing_zone"
STAGE_DB = "2_data"
STAGE_APP = "3_application"
STAGE_CUTOVER = "4_cutover"
WAVE_STAGES = (STAGE_LZ, STAGE_DB, STAGE_APP, STAGE_CUTOVER)


@dataclass
class Wave:
    id: str = field(default_factory=lambda: _new_id("wave"))
    name: str = ""
    stage: str = STAGE_APP
    server_ids: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)   # other wave ids
    rationale: str = ""
    status: str = "planned"         # planned / running / done / rolled-back

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Wave":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ingest run + agent task (audit records)
# ---------------------------------------------------------------------------
@dataclass
class IngestRun:
    id: str = field(default_factory=lambda: _new_id("ingest"))
    source: str = ""
    mode: str = ""                  # online / fixture
    raw_count: int = 0
    started_at: str = field(default_factory=_now)
    finished_at: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AgentTask:
    id: str = field(default_factory=lambda: _new_id("task"))
    prompt: str = ""
    mode: str = "plan"              # plan / execute
    status: str = "pending"         # pending / running / done / error / timeout
    created_at: str = field(default_factory=_now)
    finished_at: str = ""
    output: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# JSON helpers for sqlite storage
# ---------------------------------------------------------------------------
def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(s: str) -> Any:
    return json.loads(s) if s else None