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
from dataclasses import MISSING, asdict, dataclass, field
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
# network dependency edge (F1 — runtime telemetry discovery)
# ---------------------------------------------------------------------------
@dataclass
class NetDep:
    """One observed network dependency edge (F1): src host -> dst ip:port.

    Discovered from runtime telemetry (Prometheus custom connection metric,
    Zabbix item, or an agentless collector's ``ss -tn`` snapshot). The dst is
    resolved to a Server (by ip/hostname) in ``normalize.merge_network_deps``;
    unresolved dsts (external endpoints, unmanaged hosts, infra w/o an app)
    are reported, not silently dropped. Not a standard ingest source — it does
    not produce RawAssets; it emits dependency edges that fold into
    ``Workload.depends_on``.
    """
    src_server_id: str = ""
    src_host: str = ""
    dst_ip: str = ""              # ip or hostname
    dst_port: int = 0
    observed_at: str = ""
    source: str = ""               # prometheus / zabbix / collector / fixture

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "NetDep":
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
    # F4 — assessment-confidence signals (how much we actually know about this host)
    sizing_basis: str = ""           # measured / estimated (sizing from live util?)
    assessment_confidence: Optional[float] = None  # 0..1 data-coverage score
    # hardware support status (traditional-IDC reality: CMDB often lacks this).
    # ``warranty_status`` is an explicit bucket (active/expiring/expired/unknown);
    # ``hardware_eol`` is the optional ISO date the bucket can be derived from.
    # Drives the F2 on-prem extended-support premium, the F10 hw_support readiness
    # signal, and the data-gaps "unknown warranty" count. See match.warranty_bucket.
    warranty_status: str = ""       # "" (not assessed) / active / expiring / expired / unknown
    hardware_eol: str = ""           # ISO date YYYY-MM-DD of hardware end-of-support, or ""
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
# cost estimate (F2 — TCO / business case)
# ---------------------------------------------------------------------------
@dataclass
class CostEstimate:
    """Monthly/yearly run-cost estimate for one server's matched target.

    Computed by ``idc.core.cost`` from the matched ``Target`` + Tencent Cloud
    pricing (public API by default; an ``old -> new`` override map supports
    contract pricing). The store persists one row per server (upserted on
    rebuild); ``Match`` carries an optional hydrated copy for the UI / agent
    context so callers don't need a second lookup.
    """
    server_id: str = ""
    target_product: str = ""        # CVM / CDB / Redis / ...
    spec: str = ""                  # instance type / sku
    region: str = ""
    monthly_usd: float = 0.0
    yearly_usd: float = 0.0
    basis: str = "estimated"        # measured / estimated (sizing basis)
    pricing_source: str = "public" # public / contract
    computed_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CostEstimate":
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
    # F2 — hydrated run-cost for this match (side-stored in cost_estimates;
    # None when cost has not been computed, e.g. before rebuild or LLM-only
    # matches). Not persisted on the matches row itself.
    cost: Optional[CostEstimate] = None
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Match":
        d = dict(d)
        tgt = Target.from_dict(d.pop("target", {}) or {})
        cost = d.pop("cost", None)
        cost_obj = CostEstimate.from_dict(cost) if cost else None
        return cls(**d, target=tgt, cost=cost_obj)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# wave plan
# ---------------------------------------------------------------------------
STAGE_LZ = "1_landing_zone"
STAGE_DB = "2_data"
STAGE_APP = "3_application"
STAGE_CUTOVER = "4_cutover"
# non-migrating dispositions (7R retain/retire) — not part of the migration
# sequence; surfaced as trailing waves so every server is still accounted for.
STAGE_RETAIN = "retain"
STAGE_RETIRE = "retire"
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
# migration execution (F6 — per-host migration job lifecycle)
# ---------------------------------------------------------------------------
# A MigrationJob tracks ONE server's actual migration through a cutover-safe
# state machine (planned -> replicating -> tested -> ready_for_cutover ->
# cut_over -> finalized), with revert at every non-terminal stage and an
# explicit rolled_back sink. Wave.status rolls these up (F7). In Phase 2 the
# default mode is "track only": the operator / an external tool performs the
# migration and idc-migrate records state + runs declarative validation gates.
# The Tencent SMS/DTS driver (F6.5) populates executor_ref when wired.
MJS_PLANNED = "planned"
MJS_REPLICATING = "replicating"
MJS_TESTED = "tested"
MJS_READY_FOR_CUTOVER = "ready_for_cutover"
MJS_CUT_OVER = "cut_over"
MJS_FINALIZED = "finalized"
MJS_ROLLED_BACK = "rolled_back"
MIGRATION_JOB_STATUSES = (
    MJS_PLANNED, MJS_REPLICATING, MJS_TESTED, MJS_READY_FOR_CUTOVER,
    MJS_CUT_OVER, MJS_FINALIZED, MJS_ROLLED_BACK,
)
# terminal states — no further transitions out
MIGRATION_JOB_TERMINAL = (MJS_FINALIZED, MJS_ROLLED_BACK)

# what kind of migration this job drives
MJK_HOST = "host"   # lift-and-shift via Tencent SMS (or external tool, track-only)
MJK_DB = "db"       # DB migration via Tencent DTS (heterogeneous: see F5)
MJK_CODE = "code"   # code refactor via the external executor (ChangeJob)
MIGRATION_JOB_KINDS = (MJK_HOST, MJK_DB, MJK_CODE)

# allowed next states from each status. Forward progress + revert to the
# previous stage + abort to rolled_back; terminal states have none. The
# executor (F6 execute.py) validates every transition against this map.
MIGRATION_JOB_TRANSITIONS: Dict[str, tuple] = {
    MJS_PLANNED: (MJS_REPLICATING, MJS_ROLLED_BACK),
    MJS_REPLICATING: (MJS_TESTED, MJS_PLANNED, MJS_ROLLED_BACK),
    MJS_TESTED: (MJS_READY_FOR_CUTOVER, MJS_REPLICATING, MJS_ROLLED_BACK),
    MJS_READY_FOR_CUTOVER: (MJS_CUT_OVER, MJS_TESTED, MJS_ROLLED_BACK),
    MJS_CUT_OVER: (MJS_FINALIZED, MJS_READY_FOR_CUTOVER, MJS_ROLLED_BACK),
    MJS_FINALIZED: (),
    MJS_ROLLED_BACK: (),
}


@dataclass
class MigrationJob:
    """One server's migration through the cutover-safe state machine (F6)."""
    id: str = field(default_factory=lambda: _new_id("mjob"))
    server_id: str = ""
    app_id: str = ""                 # owning app (for code-kind + grouping)
    wave_id: str = ""                 # wave this job belongs to
    kind: str = MJK_HOST              # one of MIGRATION_JOB_KINDS
    # reference to the runner doing the actual work: a Tencent SMS/DTS job id
    # (F6.5), a ChangeJob.id (code-kind), or a free string for an external
    # tool in track-only mode. "" before the runner accepts the job.
    executor_ref: str = ""
    status: str = MJS_PLANNED         # one of MIGRATION_JOB_STATUSES
    # [{status, at, note, by}] — append-only audit of every state change
    stage_history: List[Dict[str, Any]] = field(default_factory=list)
    # declarative post-launch validation gates (F6): [{name, kind, must_pass,
    # result, at}]. kinds: connect / http_response / process_up /
    # volume_consistency / app_health_promql. A must_pass gate that fails
    # blocks advance to finalized.
    validation_gates: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MigrationJob":
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
# code intelligence — from the external agent executor (scan / comb / modify)
# ---------------------------------------------------------------------------
# The executor is an external service that scans an application's source repo,
# combs it into a migration assessment, and optionally applies code changes.
# It pushes results back to idc-migrate as a CodeProfile (per app) and a
# ChangeJob (audit trail per scan/comb/modify run). See
# docs/agent-executor.md for the full contract.

# migration pattern the executor recommends for an app — the 7R strategy
# (the classic 6R + Rehost-container). The AI assigns one of these per app
# after reconsolidate (see idc.llm.client.LLMClient.seven_r_strategy).
PATTERN_REHOST = "rehost"                 # lift-and-shift to CVM, no code change
PATTERN_REHOST_CONTAINER = "rehost-container"  # lift-and-shift, containerize onto TKE
PATTERN_REPLATFORM = "replatform"         # minor changes (swap service discovery / managed DB)
PATTERN_REFACTOR = "refactor"             # meaningful code changes / re-architect
PATTERN_REPURCHASE = "repurchase"         # replace with SaaS/managed product (e.g. Kafka->CKafka)
PATTERN_RETAIN = "retain"                # keep on-prem (regulated / mainframe / not worth it)
PATTERN_RETIRE = "retire"                 # decommission (unused / redundant / sunset)
PATTERN_REWRITE = "rewrite"              # legacy alias: rebuild (kept for back-compat with
                                          # existing executor-pushed profiles)
ALL_PATTERNS_7R = (
    PATTERN_REHOST, PATTERN_REHOST_CONTAINER, PATTERN_REPLATFORM,
    PATTERN_REFACTOR, PATTERN_REPURCHASE, PATTERN_RETAIN, PATTERN_RETIRE,
)
# legacy 4-pattern tuple (still used by older executor profiles); the 7R set
# is the canonical vocabulary now.
ALL_PATTERNS = (PATTERN_REHOST, PATTERN_REPLATFORM, PATTERN_REFACTOR, PATTERN_REWRITE)
MIGRATING_PATTERNS = (PATTERN_REHOST, PATTERN_REHOST_CONTAINER,
                      PATTERN_REPLATFORM, PATTERN_REFACTOR, PATTERN_REPURCHASE)
NON_MIGRATING_PATTERNS = (PATTERN_RETAIN, PATTERN_RETIRE)

# finding categories — the canonical scan taxonomy the executor MUST emit
FINDING_CATEGORIES = (
    "hardcoded_ip",          # on-prem IPs / hostnames baked into code/config
    "service_discovery",     # bespoke DNS/Zookeeper/Consul instead of cloud sd
    "db_connection",         # JDBC/ORM connection strings, driver specifics
    "stateful_local",        # local files / sqlite / session on disk
    "scheduled_job",         # cron / systemd timer / quartz assumed on host
    "secrets_in_repo",       # credentials committed to source
    "baremetal_assumption",  # host UUID / MAC / serial / NIC count coupling
    "network_dependency",    # calls to on-prem endpoints not in app graph
    "os_dependency",         # shell/systemd/kernel-coupled logic
    "legacy_runtime",        # EOL language/framework blocking cloud target
    "config_coupling",       # env-specific config without parameterization
    # DB schema/SQL conversion (F5 — heterogeneous DB migration assessment)
    "plsql_compat",          # Oracle PL/SQL constructs with no MySQL equivalent
    "db_feature_gap",        # engine-specific feature gap (sequences, partitioning, ...)
    "db_size_complexity",    # schema size / object count driving conversion cost
)


@dataclass
class ScanFinding:
    """One issue the executor found while scanning an app's code."""
    category: str = ""            # one of FINDING_CATEGORIES
    severity: str = "medium"      # low / medium / high / blocker
    file: str = ""                # repo-relative path
    line: int = 0
    message: str = ""
    evidence: str = ""            # snippet / matched token (truncated)
    remediation: str = ""         # suggested fix

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ScanFinding":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


@dataclass
class CodeProfile:
    """Per-app code assessment pushed by the external agent executor.

    Keyed by ``app_id`` (matches Workload.app_id). The pipeline consumes:
      * ``code_deps``     — app_ids discovered from code (call/conn analysis),
                            merged into Workload.depends_on during reconsolidate
      * ``cloud_readiness`` — 0..1, lowers match confidence when low
      * ``migration_pattern`` — rehost/replatform/refactor/rewrite, drives
                            wave ordering (refactor/rewrite later) + strategy
      * ``refactor_effort`` — low/medium/high, secondary wave ordering key
      * ``blockers``      — hard blockers surfaced into match rationale
      * ``findings``      — full evidence trail (audit)
    """
    app_id: str = ""
    repo_url: str = ""
    branch: str = ""
    scan_id: str = ""               # executor job id that produced this
    scanner: str = ""               # executor name + version
    scanned_at: str = field(default_factory=_now)
    language: str = ""
    runtime: str = ""               # e.g. jdk8 / python3.9 / node18 / go1.21
    framework: str = ""
    cloud_readiness: float = 0.0    # 0..1
    migration_pattern: str = ""     # one of ALL_PATTERNS
    refactor_effort: str = "medium"  # low / medium / high
    findings: List[ScanFinding] = field(default_factory=list)
    code_deps: List[str] = field(default_factory=list)      # app_ids
    network_endpoints: List[str] = field(default_factory=list)  # host:port the code calls
    required_changes: List[Dict[str, Any]] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    summary: str = ""
    # F9 — provenance of this profile: "repo" (executor scanned source) or
    # "runtime-derived" (no repo — inferred from Zabbix/Prometheus process+port+
    # software inventory; confidence is capped lower + a Question gates modify).
    source: str = "repo"
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CodeProfile":
        d = dict(d)
        # idempotent: a row may already carry ScanFinding objects (the store's
        # _row_to_profile pre-deserializes findings), so keep those as-is and
        # only from_dict the dict-shaped ones.
        findings = [x if isinstance(x, ScanFinding) else ScanFinding.from_dict(x)
                    for x in d.pop("findings", []) or []]
        return cls(**d, findings=findings)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# DB heterogeneous conversion assessment (F5 — Oracle/SQLServer → TDSQL/CDB)
# ---------------------------------------------------------------------------
# Sits alongside CodeProfile for DB-host apps: the executor scans the DB
# schema/SQL (not the app repo) and pushes a DBConversionProfile back. The
# difficulty grade (A/B/C) drives match enrichment + wave deferral, mirroring
# AWS DMS Schema Conversion's quality scoring / Azure Ora2Pg A–C grading.
DB_DIFFICULTY_A = "A"   # low — mostly auto-convertible (simple DDL, few PL/SQL)
DB_DIFFICULTY_B = "B"   # medium — manual review of some objects/packages
DB_DIFFICULTY_C = "C"   # hard — heavy PL/SQL / engine-specific features; replatform
DB_DIFFICULTY_GRADES = (DB_DIFFICULTY_A, DB_DIFFICULTY_B, DB_DIFFICULTY_C)
DB_ENGINE_ORACLE = "oracle"
DB_ENGINE_SQLSERVER = "sqlserver"
DB_ENGINE_MYSQL = "mysql"
DB_ENGINE_TDSQL = "tdsql"
DB_ENGINE_CDB_MYSQL = "cdb_mysql"


@dataclass
class DBConversionProfile:
    """Heterogeneous DB conversion assessment for one DB host (F5).

    Keyed by ``db_server_id`` (a ``Server.id`` whose role is ``db`` and source
    engine is Oracle/SQLServer). Produced by the executor's DB-scan step
    (``POST /v1/db-scan`` → ``PUT /api/db-profiles/{db_server_id}``) and
    consumed by ``codeintel.enrich_match_db`` to bias the match + wave plan:
    a hard (C) conversion lowers confidence, forces ``replatform``, and
    defers the host into a later wave.
    """
    db_server_id: str = ""
    source_engine: str = ""        # oracle / sqlserver / mysql
    target_engine: str = ""         # tdsql / cdb_mysql
    difficulty: str = ""            # A / B / C (DB_DIFFICULTY_*)
    est_man_days: float = 0.0       # manual conversion effort estimate
    review_objects: List[str] = field(default_factory=list)   # objects needing manual review
    blockers: List[str] = field(default_factory=list)
    reverse_replication: bool = False   # keep write-back channel during cutover (rollback net)
    auto_convert_pct: float = 0.0   # 0..1 share the rule engine can auto-convert
    scan_id: str = ""               # executor job id that produced this
    scanned_at: str = field(default_factory=_now)
    summary: str = ""
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DBConversionProfile":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


# who assigned a strategy
STRATEGY_SOURCE_AI = "ai"
STRATEGY_SOURCE_OPERATOR = "operator"


@dataclass
class AppStrategy:
    """The AI-assigned 7R migration strategy for an app (separate from the
    executor's ``CodeProfile.migration_pattern`` so the two never overwrite
    each other).

    ``strategy`` is one of ``ALL_PATTERNS_7R``. ``source`` records who assigned
    it (``ai`` from the 7R LLM, ``operator`` manual). The wave planners accept
    a strategies overlay (``Dict[app_id, AppStrategy]``) so retain/retire can
    drive wave exclusion WITHOUT first persisting to the DB — pass the 7R
    result straight into ``plan_waves`` / ``build_from_policy`` / ``propose``.
    """
    app_id: str = ""
    strategy: str = ""              # one of ALL_PATTERNS_7R
    rationale: str = ""
    target: str = ""                # Tencent product / "on-prem" / "decommission"
    confidence: float = 0.5
    effort: str = "medium"           # low / medium / high
    key_changes: List[str] = field(default_factory=list)
    source: str = STRATEGY_SOURCE_AI     # ai | operator
    assigned_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AppStrategy":
        # use field defaults for missing keys (so a partial dict still yields a
        # well-formed object — key_changes -> [] not None, confidence -> 0.5, etc.)
        out: Dict[str, Any] = {}
        for k, f in cls.__dataclass_fields__.items():
            if k in d:
                out[k] = d[k]
            elif f.default is not MISSING:
                out[k] = f.default
            elif f.default_factory is not MISSING:  # type: ignore[misc]
                out[k] = f.default_factory()
        return cls(**out)

# executor job kinds
JOB_SCAN = "scan"
JOB_COMB = "comb"
JOB_MODIFY = "modify"
ALL_JOB_KINDS = (JOB_SCAN, JOB_COMB, JOB_MODIFY)


# kinds of concrete change an item represents — drives how the executor applies it
CHANGE_KINDS = (
    "ip",         # hardcoded on-prem IP → env var / new endpoint
    "host",       # hostname → env var / cloud hostname
    "endpoint",   # host:port connection string → parameterized endpoint
    "password",   # inline credential → secret ref (old value redacted in transit)
    "secret",     # any committed secret → secret ref
    "config",     # env-specific config value → externalized/parameterized
    "runtime",    # runtime/scheduler assumption (cron/quartz/jdk) → cloud equivalent
    "other",      # anything not classifiable above
)


@dataclass
class ChangeSpec:
    """Concrete, executor-ready change instructions for one app (modify request body).

    Built by idc-migrate from the app's ``CodeProfile`` (``required_changes`` +
    ``findings``) plus operator-provided value overrides. Sent in the modify
    request so the executor does NOT have to re-scan or guess: each item tells
    it *which file/line*, *which literal to replace* (``old``), and *what to
    replace it with* (``new``).

    When idc-migrate cannot derive ``old``/``new`` confidently, the field is left
    empty and a ``note`` explains why — the executor then knows it must resolve
    that item (or skip it), rather than silently inventing a change. This keeps
    the contract honest: nothing is changed on a guess.

    Each item dict shape::

        {
          "category": "db_connection",      # one of FINDING_CATEGORIES
          "kind": "endpoint",               # one of CHANGE_KINDS
          "file": "src/.../application.yml", # repo-relative, "" if unknown
          "line": 12,                        # 0 if unknown
          "title": "Parameterize DB host",
          "evidence": "url: jdbc:mysql://10.0.4.20:3306/orders",
          "old": "10.0.4.20:3306",           # literal token to find; "" if unknown
          "new": "${DB_HOST}:3306",          # replacement; "" if unknown (resolve later)
          "effort": "low",                   # low / medium / high
          "description": "Replace hardcoded 10.0.4.20 with ${DB_HOST}",
          "note": ""                        # optional: why old/new is unresolved
        }
    """
    app_id: str = ""
    changes: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ChangeSpec":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


@dataclass
class ChangeJob:
    """Audit record for one executor run (scan / comb / modify)."""
    id: str = field(default_factory=lambda: _new_id("cjob"))
    app_id: str = ""
    kind: str = ""                 # one of ALL_JOB_KINDS
    repo_url: str = ""
    branch: str = ""
    status: str = "pending"        # pending / running / done / error / timeout
    patch_ref: str = ""            # commit sha / PR url / artifact ref
    summary: str = ""
    error: str = ""
    created_at: str = field(default_factory=_now)
    finished_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ChangeJob":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


# executor → operator question kinds
QK_VALUE = "value"     # operator supplies a free-form value (e.g. new endpoint)
QK_CHOICE = "choice"   # operator picks one of `options`
QK_CONFIRM = "confirm"  # operator says yes/no
ALL_QUESTION_KINDS = (QK_VALUE, QK_CHOICE, QK_CONFIRM)


@dataclass
class Question:
    """A question the executor raises to the operator mid-job.

    When the executor hits an ambiguous change it cannot resolve from the
    profile / overrides (e.g. ``resolve`` returned ``unknown``, or a finding's
    ``old``/``new`` was empty), it raises a Question instead of guessing or
    stalling silently. The operator answers via the UI; the executor polls
    ``GET /api/questions/{id}`` until ``status=answered`` and continues.

    This closes the "经常交互" loop: idc-migrate never invents values, and the
    executor never blocks silently — unresolved items become questions.
    """
    id: str = field(default_factory=lambda: _new_id("qst"))
    app_id: str = ""
    job_id: str = ""               # ChangeJob id that raised it (optional)
    kind: str = QK_VALUE           # one of ALL_QUESTION_KINDS
    prompt: str = ""               # human-facing question
    options: List[str] = field(default_factory=list)   # for choice
    context: Dict[str, Any] = field(default_factory=dict)  # file/line/evidence/...
    status: str = "pending"        # pending / answered / skipped
    answer: str = ""
    answered_by: str = ""
    created_at: str = field(default_factory=_now)
    answered_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Question":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# JSON helpers for storage (JSON columns are TEXT-serialized in Python)
# ---------------------------------------------------------------------------
def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(s: str) -> Any:
    return json.loads(s) if s else None