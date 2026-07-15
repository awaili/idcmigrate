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
    # derived support buckets, persisted so SQL can facet/filter on them (the
    # raw values above derive these; rebuild recomputes, the PUT-warranty path
    # recomputes warranty_bucket). "" = not yet computed (rebuild will fill).
    warranty_bucket: str = ""        # active / expiring / expired / unknown
    os_eol_bucket: str = ""          # active / expiring / expired / unknown
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
        # filter to known fields so a SELECT * that returns a column added by
        # a later migration (no matching dataclass field) doesn't raise
        # TypeError — matches Disk/Workload/etc. from_dict behaviour.
        keep = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**keep, disks=disks, utilization=util)  # type: ignore[arg-type]


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
    # F3 — operator-declared maintenance/downtime window for this wave's cutover
    # (ISO-8601 UTC start/end). Empty dict = no window declared. Surfaced in
    # rationale and consumed by the cutover-playbook generator; the deterministic
    # planner does not schedule around it (the LLM-policy path can filter on it
    # via WaveSpec.downtime_windows). Optional, back-compat: absent on old rows.
    downtime_window: Dict[str, str] = field(default_factory=dict)

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
#
# The 7R framework (the seven migration strategies the strategist picks among):
#   rehost · rehost-container · replatform · refactor · repurchase · retire ·
#   relocate
# ``retain`` (keep on-prem — regulated/mainframe) is a NON-7R *disposition*: the
# strategist may also return it (so a regulated mainframe isn't forced into a
# migrating R), and it drives wave exclusion, but it's not one of the seven.
PATTERN_REHOST = "rehost"                 # lift-and-shift to CVM, no code change
PATTERN_REHOST_CONTAINER = "rehost-container"  # lift-and-shift, containerize onto TKE
PATTERN_REPLATFORM = "replatform"         # minor changes (swap service discovery / managed DB)
PATTERN_REFACTOR = "refactor"             # meaningful code changes / re-architect
PATTERN_REPURCHASE = "repurchase"         # replace with SaaS/managed product (e.g. Kafka->CKafka)
PATTERN_RELOCATE = "relocate"            # move to a different platform/location, NO schema/code change
PATTERN_RETAIN = "retain"                # keep on-prem (regulated / mainframe / not worth it)
PATTERN_RETIRE = "retire"                 # decommission (unused / redundant / sunset)
PATTERN_REWRITE = "rewrite"              # legacy alias: rebuild (kept for back-compat with
                                          # existing executor-pushed profiles)
# the 7R — the seven migration strategies (relocate replaces the older
# "retain-as-a-7R"; retain is still a valid disposition, just not one of the 7)
ALL_PATTERNS_7R = (
    PATTERN_REHOST, PATTERN_REHOST_CONTAINER, PATTERN_REPLATFORM,
    PATTERN_REFACTOR, PATTERN_REPURCHASE, PATTERN_RETIRE, PATTERN_RELOCATE,
)
# the full set the 7R strategist may return: the seven Rs PLUS retain (the
# keep-on-prem disposition). Used by LLM validation + the UI vocabulary.
STRATEGY_CHOICES = ALL_PATTERNS_7R + (PATTERN_RETAIN,)
# legacy 4-pattern tuple (still used by older executor profiles); the 7R set
# is the canonical vocabulary now.
ALL_PATTERNS = (PATTERN_REHOST, PATTERN_REPLATFORM, PATTERN_REFACTOR, PATTERN_REWRITE)
MIGRATING_PATTERNS = (PATTERN_REHOST, PATTERN_REHOST_CONTAINER,
                      PATTERN_REPLATFORM, PATTERN_REFACTOR, PATTERN_REPURCHASE,
                      PATTERN_RELOCATE)
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
DB_ENGINE_POSTGRESQL = "postgresql"   # F6 — PG target (canonical Ora2Pg landing)

# F6 — per-object conversion status. The executor's db-scan ``mode=convert``
# emits one row per converted schema object so the compatibility report is
# actionable (not just a single grade). Mirrors AWS DMS SC / Ora2Pg reporting.
DBOBJ_TABLE = "table"
DBOBJ_VIEW = "view"
DBOBJ_PACKAGE = "package"      # Oracle PL/SQL package / SQLServer proc
DBOBJ_TRIGGER = "trigger"
DBOBJ_SEQUENCE = "sequence"
DBOBJ_FUNCTION = "function"
DBOBJ_TYPES = (DBOBJ_TABLE, DBOBJ_VIEW, DBOBJ_PACKAGE, DBOBJ_TRIGGER,
               DBOBJ_SEQUENCE, DBOBJ_FUNCTION)

DBOBJ_AUTO = "auto_converted"     # rule engine converted it, no review needed
DBOBJ_REVIEW = "manual_review"   # converted but needs a human eyeball
DBOBJ_BLOCKED = "blocked"         # no equivalent — rewrite or retain
DBOBJ_STATUSES = (DBOBJ_AUTO, DBOBJ_REVIEW, DBOBJ_BLOCKED)


@dataclass
class DBConversionObject:
    """One schema object's conversion outcome (F6 compatibility report row)."""
    name: str = ""                 # e.g. PKG_ORDERS / TRG_AUDIT_LOG / orders
    kind: str = ""                 # one of DBOBJ_TYPES
    status: str = ""               # one of DBOBJ_STATUSES
    converted: str = ""            # converted DDL/SQL ("" when blocked)
    issue: str = ""                # why it needs review / is blocked
    effort_days: float = 0.0       # manual effort for this object (0 when auto)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DBConversionObject":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


@dataclass
class DBConversion:
    """The converted-schema artifact + compatibility report (F6).

    Omitted from a ``DBConversionProfile`` when the executor ran in
    ``mode=assess`` (grade-only). Present when ``mode=convert`` was requested.
    ``auto_convert_pct`` here mirrors the profile-level field, derived from the
    share of objects with ``status=auto_converted``.
    """
    target_engine: str = ""            # the engine converted TO
    ddl: List[str] = field(default_factory=list)   # converted DDL statements
    objects: List[DBConversionObject] = field(default_factory=list)
    auto_convert_pct: float = 0.0      # = auto_converted / total
    report_md: str = ""                # markdown compatibility report

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DBConversion":
        d = dict(d)
        objs = [x if isinstance(x, DBConversionObject)
                else DBConversionObject.from_dict(x)
                for x in d.pop("objects", []) or []]
        return cls(**d, objects=objs)  # type: ignore[arg-type]


@dataclass
class DBConversionProfile:
    """Heterogeneous DB conversion assessment for one DB host (F5).

    Keyed by ``db_server_id`` (a ``Server.id`` whose role is ``db`` and source
    engine is Oracle/SQLServer). Produced by the executor's DB-scan step
    (``POST /v1/db-scan`` → ``PUT /api/db-profiles/{db_server_id}``) and
    consumed by ``codeintel.enrich_match_db`` to bias the match + wave plan:
    a hard (C) conversion lowers confidence, forces ``replatform``, and
    defers the host into a later wave.

    F6 — ``conversion`` carries the converted-schema artifact + per-object
    compatibility report when the executor ran in ``mode=convert``. It is
    ``None`` for ``mode=assess`` (grade-only) profiles, so old executor pushes
    keep working unchanged.
    """
    db_server_id: str = ""
    source_engine: str = ""        # oracle / sqlserver / mysql
    target_engine: str = ""         # tdsql / cdb_mysql / postgresql
    difficulty: str = ""            # A / B / C (DB_DIFFICULTY_*)
    est_man_days: float = 0.0       # manual conversion effort estimate
    review_objects: List[str] = field(default_factory=list)   # objects needing manual review
    blockers: List[str] = field(default_factory=list)
    reverse_replication: bool = False   # keep write-back channel during cutover (rollback net)
    auto_convert_pct: float = 0.0   # 0..1 share the rule engine can auto-convert
    scan_id: str = ""               # executor job id that produced this
    scanned_at: str = field(default_factory=_now)
    summary: str = ""
    conversion: Optional[DBConversion] = None   # F6 — None when mode=assess
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DBConversionProfile":
        d = dict(d)
        conv = d.pop("conversion", None)
        if isinstance(conv, dict):
            conv = DBConversion.from_dict(conv)
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__
                       if k != "conversion"}, conversion=conv)  # type: ignore[arg-type]


# who assigned a strategy
STRATEGY_SOURCE_AI = "ai"
STRATEGY_SOURCE_OPERATOR = "operator"


# ---------------------------------------------------------------------------
# F7 — legacy / unsupported OS disposition (executor pushes; idc-migrate gates)
# ---------------------------------------------------------------------------
# EOL *detection* is table-driven (idc.core.eol). The *recommendation* —
# containerize vs re-platform vs rewrite vs retain — is the executor's call
# (it sees the workload: runtime inventory, code profile, role, criticality,
# whether a source repo exists). idc-migrate owns the model + storage + the
# fold-in: `eol.apply_os_eol` reads the stored disposition (when present)
# instead of the old fixed "replatform to a supported base image" string.
LD_CONTAINERIZE = "containerize"   # stateless / source-less → Dockerfile scaffold
LD_REPLATFORM = "replatform"        # swap base image / managed runtime, keep shape
LD_REWRITE = "rewrite"             # tightly-coupled legacy → re-architect
LD_RETAIN = "retain"              # regulated/mainframe → keep on-prem
LD_RETIRE = "retire"               # decommission (operator: sunset / redundant)
LEGACY_DISPOSITIONS = (LD_CONTAINERIZE, LD_REPLATFORM, LD_REWRITE, LD_RETAIN,
                      LD_RETIRE)
# legacy-disposition is non-migrating only when retain/retire; the other three
# migrate (containerize/replatform = migrating; rewrite = migrating, later
# wave). retain keeps the host on-prem; retire decommissions it. Both are also
# selectable by the operator from the inventory host drawer
# (PUT /api/servers/{sid}/disposition), not just the executor's EOL analysis.
LD_NON_MIGRATING = (LD_RETAIN, LD_RETIRE)


@dataclass
class LegacyDisposition:
    """The executor's recommendation for one EOL/unsupported-OS host (F7).

    Keyed by ``server_id`` (a stable identity — the hostname lowercased, like
    db-profiles — NOT the rebuild-changing ``Server.id``). Produced by the
    executor's legacy-disposition step (``POST /v1/legacy-disposition`` →
    ``PUT /api/legacy-dispositions/{server_id}``) and consumed by
    ``eol.apply_os_eol`` to replace the fixed "replatform" string with a real
    containerize/replatform/rewrite call grounded in the workload.
    """
    server_id: str = ""               # stable identity (hostname lowercased)
    disposition: str = ""             # one of LEGACY_DISPOSITIONS
    rationale: str = ""
    confidence: float = 0.0            # 0..1 — executor's confidence in the call
    target_base_image: str = ""       # e.g. tencentos-server-3.1:jdk17
    effort_days: float = 0.0
    prereqs: List[str] = field(default_factory=list)   # before-cutover checks
    scan_id: str = ""                 # executor job id that produced this
    scanned_at: str = field(default_factory=_now)
    summary: str = ""
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "LegacyDisposition":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# F9 — generated docs: cutover playbook + as-built (+ persisted runbook)
# ---------------------------------------------------------------------------
# The executor generates grounded markdown from data idc-migrate sends (keeps
# idc-migrate LLM-free for these). One row per (doc_type, scope_id): the
# per-wave LLM runbook (assess_wave) is also persisted here so every wave has a
# downloadable doc artifact, closing the "runbook produces no document" gap.
DOC_RUNBOOK = "runbook"           # per-wave pre-checks/cutover/rollback (assess_wave)
DOC_CUTOVER = "cutover"           # ordered per-server cutover playbook
DOC_AS_BUILT = "as_built"         # post-finalization as-built
DOC_TYPES = (DOC_RUNBOOK, DOC_CUTOVER, DOC_AS_BUILT)


@dataclass
class DocArtifact:
    """A generated doc artifact (F9). Keyed by (doc_type, scope_id) — scope_id
    is a wave id for runbook/cutover/as_built. Pushed by the executor
    (``PUT /api/docs/{doc_type}/{scope_id}``) and rendered/exported by the web.
    The existing ``assess_wave`` runbook JSON is also persisted as a
    ``DOC_RUNBOOK`` artifact so every wave has a downloadable document.
    """
    doc_type: str = ""              # one of DOC_TYPES
    scope_id: str = ""             # wave id (or other scope key)
    doc_md: str = ""               # the markdown document
    scan_id: str = ""             # executor job id that produced this
    scanned_at: str = field(default_factory=_now)
    summary: str = ""
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DocArtifact":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# F5 — IaC generation: landing-zone + workload Terraform + guardrail checks
# ---------------------------------------------------------------------------
# The executor emits structured IaC modules AND runs the Well-Architected
# guardrail checks (the ``LZ_BLUEPRINTS.policy_as_code`` strings become the
# rule ids the executor evaluates against the emitted resources). idc-migrate
# stores the artifact + checks; ``check_lz_gate`` blocks a workload wave from
# launching until the target archetype's latest LZ artifact passes guardrails
# — replacing the operator-flag-only gate with a real guardrail evaluation.
IAC_SCOPE_LZ = "landing_zone"      # scope_id = "lz:<archetype>"
IAC_SCOPE_WORKLOAD = "workload"    # scope_id = "wl:<server_id>"
IAC_SCOPES = (IAC_SCOPE_LZ, IAC_SCOPE_WORKLOAD)

# Well-Architected pillars the guardrail checks map to.
PILLAR_SECURITY = "security"
PILLAR_COST = "cost"
PILLAR_RELIABILITY = "reliability"
PILLAR_OPS = "operations"
PILLAR_PERF = "performance"
IAC_PILLARS = (PILLAR_SECURITY, PILLAR_COST, PILLAR_RELIABILITY, PILLAR_OPS, PILLAR_PERF)

GR_PASS = "pass"
GR_FAIL = "fail"
GR_WARN = "warn"
GR_STATUSES = (GR_PASS, GR_FAIL, GR_WARN)

# severity that blocks the launch gate (a fail at >= this severity blocks)
GR_SEV_HIGH = "high"
GR_SEV_MEDIUM = "medium"
GR_SEV_LOW = "low"


@dataclass
class GuardrailCheck:
    """One Well-Architected guardrail result against the emitted IaC (F5)."""
    pillar: str = ""            # one of IAC_PILLARS
    rule: str = ""             # the rule id (mirrors LZ_BLUEPRINTS.policy_as_code)
    status: str = ""           # pass / fail / warn
    finding: str = ""          # what's wrong ("" on pass)
    severity: str = GR_SEV_MEDIUM   # high / medium / low

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GuardrailCheck":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


@dataclass
class IaCArtifact:
    """Generated IaC modules + Well-Architected guardrail results (F5).

    Keyed by ``scope_id`` — ``lz:<archetype>`` for landing-zone IaC,
    ``wl:<server_id>`` for per-workload IaC. Produced by the executor's
    iac-emit step (``POST /v1/iac-emit`` → ``PUT /api/iac-artifacts/{scope_id}``)
    and consumed by ``check_lz_gate`` to block launch until guardrails pass.
    """
    scope_id: str = ""                # lz:<arch> | wl:<server_id>
    scope: str = ""                   # landing_zone | workload
    modules: List[Dict[str, str]] = field(default_factory=list)  # {path, content}
    guardrails: List[GuardrailCheck] = field(default_factory=list)
    guardrail_pass: bool = False      # no fail at severity >= medium
    plan_summary: str = ""           # terraform plan summary (if run)
    target: Dict[str, str] = field(default_factory=dict)  # {cloud, region}
    scan_id: str = ""
    scanned_at: str = field(default_factory=_now)
    summary: str = ""
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "IaCArtifact":
        d = dict(d)
        gr = [x if isinstance(x, GuardrailCheck) else GuardrailCheck.from_dict(x)
              for x in d.pop("guardrails", []) or []]
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__
                      if k != "guardrails"}, guardrails=gr)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# F8 — operator-authored Landing Zone design (LLM-driven LZ design, Phase A)
# ---------------------------------------------------------------------------
# Replaces the role of the hardcoded LZ_BLUEPRINTS (idc/core/lz.py) as the
# source of truth for LZ Terraform emit + the placement gate. The ACTIVE
# design (is_active=True; at most one) drives /api/lz/archetypes, lz_context,
# iac-emit, lz_readiness and check_lz_gate. When no active custom design
# exists, the built-in LZ_BLUEPRINTS is the default design (strict superset —
# nothing existing breaks).
#
# ``archetypes`` is {archetype_name: blueprint_dict} — same shape as one
# LZ_BLUEPRINTS[arch] entry (vpc / peering / internet_egress / security_groups
# / cam_roles / tag_policy / policy_as_code). ``requirements`` captures the
# operator's stated needs (the LLM interview output, Phase B).
# ``conversation`` is the full multi-turn chat history (Phase B).
# ``onprem_cidrs`` lists on-prem CIDR ranges the cloud VPCs must not overlap.
@dataclass
class LZDesign:
    design_id: str = ""
    name: str = ""
    # one-line AI description of the design (MigraQ authors it during the
    # interview; surfaced in the Saved designs table so an operator can tell
    # saved designs apart without opening them). Empty for hand-authored /
    # pre-AI designs.
    summary: str = ""
    archetypes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    requirements: Dict[str, Any] = field(default_factory=dict)
    conversation: List[Dict[str, str]] = field(default_factory=list)
    onprem_cidrs: List[str] = field(default_factory=list)
    is_active: bool = False
    # the scale tier this design was authored for (small/medium/large) — drives
    # the default strategy (LZ_SCALE_TIERS). Empty for pre-scale designs. The
    # governance block (identity/audit/network golden rules) lives at
    # requirements.governance (JSON), not a column.
    scale: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    updated_by: str = "operator"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "LZDesign":
        d = dict(d)
        # JSON columns come back as strings; coerce the structured fields.
        for k in ("archetypes", "requirements", "conversation", "onprem_cidrs"):
            v = d.get(k)
            if isinstance(v, str):
                d[k] = loads(v) or ({} if k != "conversation" and k != "onprem_cidrs"
                                    else [])
        d["is_active"] = bool(d.get("is_active"))
        d["scale"] = str(d.get("scale") or "")   # '' for pre-scale rows
        d["summary"] = str(d.get("summary") or "")  # '' for pre-AI rows
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# F8 (Phase B+) — network designer: VPC + subnets per account + server placement
# ---------------------------------------------------------------------------
# Sits on top of the LZ design (Phase A/B): the LZ design gives each archetype a
# VPC cidr; the network designer CARVES that VPC into subnets (one per tier:
# web/app/data/infra) and PLACES every server into a VPC + subnet + IP. Each
# archetype maps to an ACCOUNT (the user's "VPC and subnet for each account"),
# so the topology is account-scoped. The LLM may propose the carving policy
# (subnet size, tier grouping, account map); the deterministic engine
# instantiates + validates (subnets within VPC, non-overlapping, IP in subnet,
# capacity). Placements are keyed by STABLE hostname (not Server.id) so they
# survive a rebuild. Per-server subnet overrides (the operator moving a server
# to a different subnet) ride the design and survive regenerate.
@dataclass
class NetworkDesign:
    design_id: str = ""
    name: str = ""
    # one-line AI description of the carving policy (MigraQ authors it as the
    # policy ``notes`` when it proposes the carving; surfaced in the Saved
    # network designs table). Empty for the deterministic default policy.
    summary: str = ""
    # archetype -> account name (each archetype's VPC lives in this account)
    accounts: Dict[str, str] = field(default_factory=dict)
    # per-archetype VPC + carved subnets: [{archetype, account, name, cidr,
    # subnets:[{name, cidr, tier, capacity, used, gateway}]}]
    vpcs: List[Dict[str, Any]] = field(default_factory=list)
    # hostname(lowercased) -> {archetype, account, vpc, subnet, ip, tier, role,
    # overridden} — the server's placement (stable key, survives rebuild)
    placements: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # the carving policy used: {subnet_prefix, tier_order, tiers{role:tier},
    # account_map{arch:acct}}
    policy: Dict[str, Any] = field(default_factory=dict)
    # hostname -> subnet name (operator pin, survives regenerate)
    overrides: Dict[str, str] = field(default_factory=dict)
    # hostname -> archetype (operator pin moving a server to a different VPC;
    # survives regenerate). VPC is 1:1 with archetype, so a VPC change IS an
    # archetype reclassification, stored here so it composes with the subnet
    # overrides above (and doesn't mutate inventory tags / the LZ classifier).
    archetype_overrides: Dict[str, str] = field(default_factory=dict)
    # hostname -> account name (operator pin reattributing a server to a
    # different account WITHOUT moving its VPC; survives regenerate). Account is
    # otherwise derived 1:1 from the archetype (accounts[arch] / policy.account_map),
    # so this is the only way to decouple billing/ownership from topology. The
    # value must be one of the accounts already in the design (the union of
    # accounts.values() / vpcs[*].account) — the placement endpoint validates it.
    account_overrides: Dict[str, str] = field(default_factory=dict)
    onprem_cidrs: List[str] = field(default_factory=list)
    validation: Dict[str, Any] = field(default_factory=dict)
    is_active: bool = False
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    updated_by: str = "operator"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "NetworkDesign":
        d = dict(d)
        # JSON columns may arrive as strings; coerce to the right type per key.
        # vpcs + onprem_cidrs are LISTS — ``loads(v) or {}`` would turn an empty
        # ``[]`` into ``{}`` (wrong type), so use list defaults for them.
        list_keys = ("vpcs", "onprem_cidrs")
        for k in ("accounts", "vpcs", "placements", "policy", "overrides",
                  "archetype_overrides", "account_overrides", "onprem_cidrs",
                  "validation"):
            v = d.get(k)
            if isinstance(v, str):
                d[k] = loads(v) or ([] if k in list_keys else {})
        d["is_active"] = bool(d.get("is_active"))
        d["summary"] = str(d.get("summary") or "")  # '' for the default policy
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# F10 — post-migration optimization (right-size / reserved / anomaly / perf)
# ---------------------------------------------------------------------------
# The executor pulls post-migration cloud metrics (its own Cloud Monitor /
# Prometheus access) + returns recommendations. idc-migrate stores them keyed
# by (server_id, kind), triggers on finalized hosts, and rolls the savings into
# the cost module. Pre-migration ``right_size`` (match.right_size) sizes a
# target FROM on-prem utilization; this is the post-mig counterpart: resize
# the now-RUNNING cloud resource based on its actual cloud metrics.
PM_RIGHT_SIZE = "right_size"     # resize the running CVM/CDB down (or up)
PM_RESERVED = "reserved"         # steady-state → 1yr reserved instance
PM_ANOMALY = "anomaly"           # metric spike / outlier to investigate
PM_PERF = "perf"                # perf tuning (pool size, index, JVM flag)
PM_KINDS = (PM_RIGHT_SIZE, PM_RESERVED, PM_ANOMALY, PM_PERF)


@dataclass
class PostMigRecommendation:
    """One post-migration optimization recommendation for a migrated host (F10).

    Keyed by (server_id, kind) — one rec per kind per host (a host has at most
    one right-size rec, one reserved rec, etc.). Produced by the executor's
    postmig-optimize step (``POST /v1/postmig-optimize`` →
    ``PUT /api/postmig-recs/{server_id}/{kind}``) and rolled into the cost
    module's savings estimate.
    """
    server_id: str = ""               # stable identity (hostname lowercased)
    kind: str = ""                    # one of PM_KINDS
    from_spec: str = ""              # current spec (e.g. SA2.LARGE8)
    to_spec: str = ""               # recommended spec (right_size) / term (reserved)
    reason: str = ""               # grounded in metrics (e.g. "p95 cpu 8% over 30d")
    monthly_saving_usd: float = 0.0   # 0 for anomaly/perf (no direct saving)
    confidence: float = 0.0          # 0..1
    severity: str = ""              # low / medium / high (anomaly urgency)
    detail: str = ""                # extra context (anomaly metric + time, perf note)
    scan_id: str = ""
    scanned_at: str = field(default_factory=_now)
    summary: str = ""
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PostMigRecommendation":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# F11 — automated testing (generate / run / compare pre-vs-post)
# ---------------------------------------------------------------------------
# The executor generates test cases from the app's CodeProfile + workload,
# runs them against a target (pre-mig on-prem, post-mig cloud), and diffs a
# pre-run vs a post-run. A regression in the diff blocks the wave's cutover
# (the ``test_regression`` validation gate) — the real version of the manual
# ``process_up`` stub.
TEST_FUNCTIONAL = "functional"
TEST_INTEGRATION = "integration"
TEST_REGRESSION = "regression"
TEST_KINDS = (TEST_FUNCTIONAL, TEST_INTEGRATION, TEST_REGRESSION)

# TestRun phase: pre = baseline before cutover, post = after cutover
TEST_PHASE_PRE = "pre"
TEST_PHASE_POST = "post"
TEST_PHASES = (TEST_PHASE_PRE, TEST_PHASE_POST)

# case status in a run
TS_PASS = "pass"
TS_FAIL = "fail"
TS_ERROR = "error"
TS_SKIP = "skip"

# diff verdict
TV_PASS = "pass"             # pre + post both pass (or both fail identically)
TV_REGRESSION = "regression"   # pre pass → post fail
TV_NEW_FAILURE = "new_failure"  # pre skip/error → post fail (new case failing)
TV_FLAKY = "flaky"           # inconsistent / pre-fail-but-post-pass-needs-review
TV_VERDICTS = (TV_PASS, TV_REGRESSION, TV_NEW_FAILURE, TV_FLAKY)


@dataclass
class TestCase:
    """One auto-generated test case for an app (F11). Keyed by (app_id, name)."""
    __test__ = False   # not a pytest test class (dataclass)
    app_id: str = ""
    name: str = ""
    kind: str = ""              # one of TEST_KINDS
    endpoint: str = ""          # URL or RPC target
    method: str = "GET"         # HTTP method / RPC verb
    request: Dict[str, Any] = field(default_factory=dict)  # body/params/headers
    expected: Dict[str, Any] = field(default_factory=dict)  # {status, body_contains, ...}
    setup: str = ""             # setup/teardown note (fixtures, seed data)
    scan_id: str = ""
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TestCase":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


@dataclass
class TestResult:
    """One case's outcome in a TestRun (F11)."""
    __test__ = False
    case: str = ""             # the TestCase.name
    status: str = ""           # pass / fail / error / skip
    duration_ms: int = 0
    response_summary: str = ""  # e.g. "200 OK" / first line of response
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TestResult":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


@dataclass
class TestRun:
    """One execution of an app's test set against a target (F11). Keyed by id."""
    __test__ = False
    id: str = field(default_factory=lambda: _new_id("trun"))
    app_id: str = ""
    phase: str = ""             # pre (baseline) / post (after cutover)
    target: str = ""            # the endpoint base the run hit (on-prem / cloud)
    results: List[TestResult] = field(default_factory=list)
    passed: int = 0
    failed: int = 0
    errors: int = 0
    run_at: str = field(default_factory=_now)
    scan_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TestRun":
        d = dict(d)
        res = [x if isinstance(x, TestResult) else TestResult.from_dict(x)
               for x in d.pop("results", []) or []]
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__
                      if k != "results"}, results=res)  # type: ignore[arg-type]


@dataclass
class TestDiffItem:
    """One case's pre-vs-post comparison (F11)."""
    __test__ = False
    case: str = ""
    pre_status: str = ""
    post_status: str = ""
    verdict: str = ""           # one of TV_VERDICTS
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TestDiffItem":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


@dataclass
class TestDiff:
    """Pre-vs-post test comparison for one app (F11). Keyed by app_id (latest)."""
    __test__ = False
    app_id: str = ""
    pre_run_id: str = ""
    post_run_id: str = ""
    diff: List[TestDiffItem] = field(default_factory=list)
    regressions: int = 0       # count of regression + new_failure verdicts
    scan_id: str = ""
    scanned_at: str = field(default_factory=_now)
    summary: str = ""
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TestDiff":
        d = dict(d)
        items = [x if isinstance(x, TestDiffItem) else TestDiffItem.from_dict(x)
                 for x in d.pop("diff", []) or []]
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__
                      if k != "diff"}, diff=items)  # type: ignore[arg-type]


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