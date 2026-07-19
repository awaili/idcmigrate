"""Storage for idc-migrate — MariaDB backend (pymysql).

The DB lives on the DB box at ``10.0.0.3:3306`` (MariaDB 10.5); the dialect is
fixed via ``IDC_DB_URL=mysql://user:pass@host:port/db`` (``mariadb://`` is also
accepted). ``Store`` keeps a small connection pool (pymysql, DictCursor,
autocommit) and shares one API across all callers. Complex fields (disks, util,
source_refs, target, ...) are stored as JSON columns; reads use
``JSON_EXTRACT`` / ``JSON_UNQUOTE`` / ``JSON_LENGTH`` (MariaDB 10.2+).
"""
from __future__ import annotations

import threading
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from queue import Empty, Queue
from typing import Any, Dict, Iterable, List, Optional

from .models import (
    AgentTask,
    AppStrategy,
    ChangeJob,
    CodeProfile,
    CostEstimate,
    DBConversionProfile,
    DocArtifact,
    IaCArtifact,
    IngestRun,
    LegacyDisposition,
    LZDesign,
    Match,
    MigrationJob,
    NetworkDesign,
    PostMigRecommendation,
    Repo,
    RepoScan,
    TestCase, TestDiff, TestDiffItem, TestRun, TestResult,
    ScanFinding,
    Server,
    Target,
    Wave,
    Workload,
    _now,
    dumps,
    loads,
)


@dataclass
class ServerFilter:
    """Filter spec for scalable server queries."""
    role: Optional[str] = None
    env: Optional[str] = None
    os: Optional[str] = None
    source_type: Optional[str] = None
    criticality: Optional[str] = None
    cluster: Optional[str] = None
    datacenter: Optional[str] = None
    status: Optional[str] = None
    q: Optional[str] = None
    target_product: Optional[str] = None
    wave_id: Optional[str] = None
    util_cpu_min: Optional[float] = None
    util_mem_min: Optional[float] = None
    util_disk_min: Optional[float] = None
    conf_min: Optional[float] = None
    conf_max: Optional[float] = None
    warranty_bucket: Optional[str] = None
    os_eol_bucket: Optional[str] = None
    app_id: Optional[str] = None       # match servers whose app_ids JSON array contains this app
    hostname_after: Optional[str] = None   # resume cursor: servers.hostname > this (asc order)


# ---------------------------------------------------------------------------
# schema — ids are short text (srv-/wave-/…), JSON fields use the JSON alias,
# sizes are VARCHAR to act as PRIMARY KEY / indexed cols (TEXT can't be PK in
# MySQL without a prefix length). Indexes are created without IF NOT EXISTS
# (MariaDB 10.5 lacks it) — _exec_schema tolerates "Duplicate key name".
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_assets (
    source      VARCHAR(32)  NOT NULL,
    source_id   VARCHAR(255) NOT NULL,
    hostname    VARCHAR(255),
    fqdn        VARCHAR(255),
    ip          VARCHAR(64),
    attrs       JSON,
    ingested_at VARCHAR(40),
    PRIMARY KEY (source, source_id),
    KEY idx_raw_hostname (hostname),
    KEY idx_raw_ip (ip)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS servers (
    id            VARCHAR(64)  PRIMARY KEY,
    hostname      VARCHAR(255),
    fqdn          VARCHAR(255),
    ips           JSON,
    source_type   VARCHAR(32),
    role          VARCHAR(32),
    os            VARCHAR(64),
    os_version    VARCHAR(64),
    cpu_cores     INT,
    cpu_model     VARCHAR(128),
    mem_gb        INT,
    disks         JSON,
    subnet        VARCHAR(128),
    vlan          VARCHAR(64),
    datacenter    VARCHAR(64),
    cluster       VARCHAR(128),
    env           VARCHAR(32),
    app_ids       JSON,
    tags          JSON,
    business_criticality VARCHAR(16),
    migration_tier VARCHAR(8),
    utilization   JSON,
    source_refs   JSON,
    status        VARCHAR(32),
    sizing_basis         VARCHAR(16),   -- F4: measured / estimated
    assessment_confidence DOUBLE,         -- F4: data-coverage score 0..1
    warranty_status VARCHAR(16),          -- hardware support bucket (active/expiring/expired/unknown)
    hardware_eol    VARCHAR(16),          -- ISO date of hardware end-of-support
    warranty_bucket VARCHAR(16),          -- derived: active/expiring/expired/unknown (persisted for faceting)
    os_eol_bucket   VARCHAR(16),          -- derived: active/expiring/expired/unknown (persisted for faceting)
    created_at    VARCHAR(40),
    updated_at    VARCHAR(40),
    KEY idx_srv_role (role),
    KEY idx_srv_env (env),
    KEY idx_srv_status (status),
    KEY idx_srv_os (os),
    KEY idx_srv_crit (business_criticality),
    KEY idx_srv_stype (source_type),
    KEY idx_srv_cluster (cluster)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS workloads (
    app_id     VARCHAR(128) PRIMARY KEY,
    name       VARCHAR(255),
    tier       VARCHAR(32),
    env        VARCHAR(32),
    server_ids JSON,
    depends_on JSON,
    notes      TEXT
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS matches (
    server_id   VARCHAR(64) PRIMARY KEY,
    target      JSON,
    confidence  DOUBLE,
    method      VARCHAR(16),
    rationale   TEXT,
    alternatives JSON,
    created_at  VARCHAR(40),
    KEY idx_match_conf (confidence)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS waves (
    id          VARCHAR(64) PRIMARY KEY,
    name        VARCHAR(255),
    stage       VARCHAR(32),
    server_ids  JSON,
    depends_on  JSON,
    rationale   TEXT,
    status      VARCHAR(32),
    downtime_window JSON   -- F3 — operator-declared cutover/maintenance window
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ingest_runs (
    id          VARCHAR(64) PRIMARY KEY,
    source      VARCHAR(32),
    mode        VARCHAR(16),
    raw_count   INT,
    started_at  VARCHAR(40),
    finished_at VARCHAR(40),
    error       TEXT
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_tasks (
    id          VARCHAR(64) PRIMARY KEY,
    prompt      TEXT,
    mode        VARCHAR(16),
    status      VARCHAR(32),
    created_at  VARCHAR(40),
    finished_at VARCHAR(40),
    output      LONGTEXT,
    error       TEXT
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS code_profiles (
    app_id            VARCHAR(128) PRIMARY KEY,
    repo_url          VARCHAR(512),
    branch            VARCHAR(128),
    scan_id           VARCHAR(64),
    scanner           VARCHAR(128),
    scanned_at        VARCHAR(40),
    language          VARCHAR(32),
    runtime           VARCHAR(64),
    framework         VARCHAR(128),
    cloud_readiness   DOUBLE,
    migration_pattern VARCHAR(16),
    refactor_effort   VARCHAR(8),
    findings          LONGTEXT,
    code_deps         JSON,
    network_endpoints JSON,
    required_changes  JSON,
    blockers          JSON,
    summary           TEXT,
    updated_at        VARCHAR(40)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- F9 — code profile provenance (repo / runtime-derived). ALTER in place.
ALTER TABLE code_profiles ADD COLUMN IF NOT EXISTS source VARCHAR(24);

-- path A — agent (codex) grounded code pass, run by the executor after the
-- rule engine. Separate from findings/blockers so the rule set provenance
-- stays clean. _row_to_profile null-safes pre-ALTER rows. ALTER in place.
ALTER TABLE code_profiles ADD COLUMN IF NOT EXISTS agent_findings LONGTEXT;
ALTER TABLE code_profiles ADD COLUMN IF NOT EXISTS agent_blockers JSON;
ALTER TABLE code_profiles ADD COLUMN IF NOT EXISTS agent_summary TEXT;

CREATE TABLE IF NOT EXISTS app_strategies (
    app_id        VARCHAR(128) PRIMARY KEY,
    strategy      VARCHAR(24),
    rationale     TEXT,
    target        VARCHAR(64),
    confidence    DOUBLE,
    effort        VARCHAR(8),
    key_changes   JSON,
    source        VARCHAR(16),
    assigned_at   VARCHAR(40),
    updated_at    VARCHAR(40)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS change_jobs (
    id          VARCHAR(64) PRIMARY KEY,
    app_id      VARCHAR(128),
    kind        VARCHAR(32),
    repo_url    VARCHAR(512),
    branch      VARCHAR(128),
    status      VARCHAR(32),
    patch_ref   VARCHAR(512),
    summary     TEXT,
    error       TEXT,
    created_at  VARCHAR(40),
    finished_at VARCHAR(40),
    KEY idx_cjob_app (app_id),
    KEY idx_cjob_status (status)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS executor_tasks (
    id          VARCHAR(64) PRIMARY KEY,   -- tsk-... (the dispatch unit)
    kind        VARCHAR(32),               -- scan|comb|modify|db-scan|...
    payload     JSON,                      -- the full task body incl callback push targets
    executor_id VARCHAR(64),              -- target executor, NULL = pool (any executor)
    status      VARCHAR(16),              -- pending|claimed|running|done|error
    claimed_by  VARCHAR(64),              -- executor id that claimed (resolved from token)
    claimed_at  VARCHAR(40),
    lease_until VARCHAR(40),               -- claim expires here -> requeue to pending
    created_at  VARCHAR(40),
    finished_at VARCHAR(40),
    error       TEXT,
    result_ref  VARCHAR(512),             -- patch_ref / PR url / artifact ref
    summary     TEXT,
    KEY idx_etask_status (status),
    KEY idx_etask_target (executor_id),
    KEY idx_etask_lease (lease_until)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS executor_heartbeats (
    -- pull-mode connectivity: the last time an executor reached into
    -- idc-migrate (polled /claim, renewed /lease, or posted /complete).
    -- idc-migrate never initiates a connection, so liveness is inferred
    -- from this inbound activity, not from any outbound probe.
    executor_id   VARCHAR(64) PRIMARY KEY,
    last_seen_at  VARCHAR(40)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS repos (
    -- a git url as a first-class object, global and url-unique: the same git
    -- url is ONE row, scanned once, shared by every host/app that deploys it.
    -- host:git-url is N:N via host_repos, and app:repo is derived from the
    -- app's hosts. Phase 1 = entity + N:N host mapping, Phase 2 = code-profile
    -- re-key to repo_id.
    repo_id     VARCHAR(64)  PRIMARY KEY,   -- repo-<8>
    url         VARCHAR(512) NOT NULL,
    branch      VARCHAR(128),
    name        VARCHAR(255),               -- derived from the url path, editable
    created_at  VARCHAR(40),
    updated_at  VARCHAR(40),
    UNIQUE KEY uk_repos_url (url)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS host_repos (
    -- the N:N edge: a host (server) deploys a set of repos, and a repo is
    -- deployed on a set of hosts. server_id references servers.id (stable,
    -- hostname-derived) and repo_id references repos.repo_id. Deleting a repo
    -- cascades its edges (see delete_repo) and a removed server's edges are
    -- filtered out on read by joining servers.
    server_id   VARCHAR(64) NOT NULL,
    repo_id     VARCHAR(64) NOT NULL,
    PRIMARY KEY (server_id, repo_id),
    KEY idx_hrep_repo (repo_id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS repo_scans (
    -- a discovery scan: the operator pastes a git group/org url, the executor
    -- enumerates the repositories inside it and pushes the list back here, the
    -- operator picks which to register as first-class Repo rows. One row per
    -- scan, and repos_json holds the discovered [{url,name,branch}] list.
    scan_id      VARCHAR(64) PRIMARY KEY,   -- scn-<8>
    url          VARCHAR(512),              -- the group/org git url that was scanned
    status       VARCHAR(16),               -- pending | done | error
    executor_id  VARCHAR(64),               -- executor that ran it (NULL = pool)
    repos_json   JSON,                      -- discovered repos list (empty until done)
    error        TEXT,
    created_at   VARCHAR(40),
    finished_at  VARCHAR(40),
    KEY idx_rscan_status (status)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS questions (
    id          VARCHAR(64) PRIMARY KEY,
    app_id      VARCHAR(128),
    job_id      VARCHAR(64),       -- ChangeJob that raised it (optional)
    kind        VARCHAR(16),       -- value | choice | confirm
    prompt      TEXT,             -- the human-facing question
    options     JSON,             -- for choice: ["${DB_HOST}", "${DB_HOST_RO}"]
    context     JSON,             -- {file, line, evidence, category, old, new, ...}
    status      VARCHAR(16),      -- pending | answered | skipped
    answer      TEXT,             -- operator's answer once status=answered
    answered_by VARCHAR(128),
    created_at  VARCHAR(40),
    answered_at VARCHAR(40),
    KEY idx_q_app (app_id),
    KEY idx_q_status (status)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS cost_estimates (
    server_id       VARCHAR(64) PRIMARY KEY,
    target_product  VARCHAR(32),
    spec            VARCHAR(128),
    region          VARCHAR(32),
    monthly_usd     DOUBLE,
    yearly_usd      DOUBLE,
    basis           VARCHAR(16),      -- measured / estimated
    pricing_source  VARCHAR(16),      -- public / contract
    computed_at     VARCHAR(40)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS migration_jobs (
    id               VARCHAR(64) PRIMARY KEY,
    server_id        VARCHAR(64),
    app_id           VARCHAR(128),
    wave_id          VARCHAR(64),
    kind             VARCHAR(16),     -- host / db / code
    executor_ref     VARCHAR(255),    -- SMS/DTS job id / ChangeJob id / external ref
    status           VARCHAR(24),     -- planned/replicating/tested/ready_for_cutover/cut_over/finalized/rolled_back
    stage_history    JSON,            -- [{status, at, note, by}] append-only audit
    validation_gates JSON,            -- [{name, kind, must_pass, result, at}]
    created_at       VARCHAR(40),
    updated_at       VARCHAR(40),
    KEY idx_mjob_server (server_id),
    KEY idx_mjob_wave (wave_id),
    KEY idx_mjob_status (status)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS business_case_snapshots (
    id          VARCHAR(64) PRIMARY KEY,
    created_at  VARCHAR(40),
    payload     LONGTEXT              -- full JSON of the business-case run
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- F5 — heterogeneous DB conversion profiles (one row per DB host, keyed by
-- db_server_id). Pushed by the executor's DB-scan step and consumed by
-- codeintel.enrich_match_db to bias match confidence + wave deferral.
CREATE TABLE IF NOT EXISTS db_profiles (
    db_server_id       VARCHAR(64) PRIMARY KEY,
    source_engine      VARCHAR(32),
    target_engine      VARCHAR(32),
    difficulty         VARCHAR(4),       -- A / B / C
    est_man_days       DOUBLE,
    review_objects     JSON,
    blockers           JSON,
    reverse_replication BOOLEAN,
    auto_convert_pct   DOUBLE,
    scan_id            VARCHAR(64),
    scanned_at         VARCHAR(40),
    summary            TEXT,
    updated_at         VARCHAR(40)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- F8 — Landing Zone archetype status (per archetype: not_ready/applied/finalized).
-- Operator persists the LZ lifecycle so the wave-launch placement gate
-- (check_lz_gate) can block workload waves until the target archetype's LZ
-- Terraform is applied + finalized.
CREATE TABLE IF NOT EXISTS lz_status (
    archetype   VARCHAR(16) PRIMARY KEY,
    status      VARCHAR(16) NOT NULL,
    updated_at  VARCHAR(40),
    updated_by  VARCHAR(64)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- F8 (Phase A) — operator-authored LZ designs (LLM-driven LZ design). The
-- active row (is_active=1, at most one, enforced by set_active_lz_design)
-- is the source of truth for /api/lz/archetypes, lz_context, iac-emit,
-- lz_readiness and check_lz_gate. When no active row exists, the built-in
-- LZ_BLUEPRINTS is the default design. archetypes/requirements/conversation/
-- onprem_cidrs ride JSON columns (same shape as LZ_BLUEPRINTS[arch]).
CREATE TABLE IF NOT EXISTS lz_designs (
    design_id    VARCHAR(64) PRIMARY KEY,
    name         VARCHAR(128) NOT NULL,
    archetypes   JSON,
    requirements JSON,
    conversation JSON,
    onprem_cidrs JSON,
    is_active    BOOLEAN DEFAULT FALSE,
    created_at   VARCHAR(40),
    updated_at   VARCHAR(40),
    updated_by   VARCHAR(64)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- scale tier (small/medium/large) this LZ design was authored for — drives the
-- default strategy (LZ_SCALE_TIERS). Added in place, pre-scale rows stay empty.
ALTER TABLE lz_designs ADD COLUMN IF NOT EXISTS scale VARCHAR(16) DEFAULT '';

-- one-line AI description (MigraQ authors it during the LZ interview, surfaced
-- in the Saved designs table). Added in place, pre-AI rows stay empty.
ALTER TABLE lz_designs ADD COLUMN IF NOT EXISTS summary VARCHAR(512) DEFAULT '';

-- F8 (Phase B+) — network designs (VPC + subnets per account + server placement).
-- The active row (is_active=1, at most one) drives /api/lz/network/design. Built
-- on top of the active LZ design's per-archetype VPC cidrs. Placements are keyed
-- by stable hostname (survive rebuild). accounts/vpcs/placements/policy/overrides
-- ride JSON columns.
CREATE TABLE IF NOT EXISTS network_designs (
    design_id    VARCHAR(64) PRIMARY KEY,
    name         VARCHAR(128) NOT NULL,
    accounts     JSON,
    vpcs         JSON,
    placements   JSON,
    policy       JSON,
    overrides    JSON,
    onprem_cidrs JSON,
    validation   JSON,
    is_active    BOOLEAN DEFAULT FALSE,
    created_at   VARCHAR(40),
    updated_at   VARCHAR(40),
    updated_by   VARCHAR(64)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- data-gap (Gap1) — latest shadow-IT / CMDB-drift discovery snapshot. Single
-- row (id='latest') upserted by POST /api/discovery/scan so the web tab shows
-- the last scan without re-running. Payload is the full discover_drift dict.
CREATE TABLE IF NOT EXISTS discovery_snapshots (
    id          VARCHAR(16) PRIMARY KEY,
    created_at  VARCHAR(40),
    payload     LONGTEXT
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- runtime system config (key/value) — currently holds the operator-editable
-- executor connection (executor_url / executor_token / executor_enabled /
-- executor_timeout) so the web panel can manage it without a redeploy/env edit.
-- Values are strings — callers coerce (bool via "true"/"false", int via int()).
CREATE TABLE IF NOT EXISTS system_config (
    k           VARCHAR(64) PRIMARY KEY,
    v           TEXT
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- F4 server columns migrated in place via ALTER IF NOT EXISTS
ALTER TABLE servers ADD COLUMN IF NOT EXISTS sizing_basis VARCHAR(16);
ALTER TABLE servers ADD COLUMN IF NOT EXISTS assessment_confidence DOUBLE;
ALTER TABLE servers ADD COLUMN IF NOT EXISTS warranty_status VARCHAR(16);
ALTER TABLE servers ADD COLUMN IF NOT EXISTS hardware_eol VARCHAR(16);
ALTER TABLE servers ADD COLUMN IF NOT EXISTS warranty_bucket VARCHAR(16);
ALTER TABLE servers ADD COLUMN IF NOT EXISTS os_eol_bucket VARCHAR(16);

-- F6 — db_profiles conversion artifact (None for assess-mode profiles). JSON
-- so the whole DBConversion (ddl[] + objects[] + report_md) rides one column.
ALTER TABLE db_profiles ADD COLUMN IF NOT EXISTS conversion JSON;

-- F3 — waves downtime window (operator-declared cutover/maintenance slot).
-- Added via ALTER for existing DBs (the waves table predates this field).
ALTER TABLE waves ADD COLUMN IF NOT EXISTS downtime_window JSON;

-- F8 (Phase B+) — per-server VPC (archetype) override, riding the network design
-- alongside the subnet overrides. Same JSON-column-on-existing-table pattern as
-- the waves downtime_window above. ADD COLUMN IF NOT EXISTS tolerates re-runs.
ALTER TABLE network_designs ADD COLUMN IF NOT EXISTS archetype_overrides JSON;

-- F8 (Phase B+) — per-server account override (reattribute a server to a
-- different account WITHOUT moving its VPC). Same pattern as archetype_overrides.
ALTER TABLE network_designs ADD COLUMN IF NOT EXISTS account_overrides JSON;

-- one-line AI description of the carving policy (MigraQ authors it as the
-- policy ``notes``, surfaced in the Saved network designs table). Pre-AI / default
-- rows stay empty.
ALTER TABLE network_designs ADD COLUMN IF NOT EXISTS summary VARCHAR(512) DEFAULT '';

-- F7 — legacy / unsupported-OS disposition (one row per EOL host, keyed by the
-- stable hostname). The executor's containerize/replatform/rewrite/retain call
-- replaces eol.apply_os_eol's fixed "replatform" string with a real rec.
CREATE TABLE IF NOT EXISTS legacy_dispositions (
    server_id        VARCHAR(64) PRIMARY KEY,   -- hostname lowercased (stable)
    disposition      VARCHAR(16),               -- containerize/replatform/rewrite/retain
    rationale        TEXT,
    confidence       DOUBLE,
    target_base_image VARCHAR(64),
    effort_days      DOUBLE,
    prereqs          JSON,
    scan_id          VARCHAR(64),
    scanned_at       VARCHAR(40),
    summary          TEXT,
    updated_at       VARCHAR(40)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- F9 — generated doc artifacts (cutover playbook / as-built / persisted runbook).
-- One row per (doc_type, scope_id). scope_id is a wave id. Pushed by the
-- executor, rendered/exported by the web. LONGTEXT for the markdown body.
CREATE TABLE IF NOT EXISTS doc_artifacts (
    doc_type         VARCHAR(16) NOT NULL,      -- runbook / cutover / as_built
    scope_id         VARCHAR(64) NOT NULL,      -- wave id (or other scope key)
    doc_md           LONGTEXT,
    scan_id          VARCHAR(64),
    scanned_at       VARCHAR(40),
    summary          TEXT,
    updated_at       VARCHAR(40),
    PRIMARY KEY (doc_type, scope_id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- F5 — generated IaC artifacts (landing-zone / workload Terraform + guardrail
-- checks). One row per scope_id (lz:<arch> | wl:<server_id>). The guardrails
-- JSON + guardrail_pass flag drive check_lz_gate (block launch until pass).
CREATE TABLE IF NOT EXISTS iac_artifacts (
    scope_id         VARCHAR(64) PRIMARY KEY,
    scope            VARCHAR(16),               -- landing_zone / workload
    modules          JSON,                      -- [{path, content}]
    guardrails       JSON,                      -- [{pillar, rule, status, finding, severity}]
    guardrail_pass   BOOLEAN,
    plan_summary     TEXT,
    target           JSON,                      -- {cloud, region}
    scan_id          VARCHAR(64),
    scanned_at       VARCHAR(40),
    summary          TEXT,
    updated_at       VARCHAR(40)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- F10 — post-migration optimization recommendations (right_size/reserved/
-- anomaly/perf). One row per (server_id, kind) so a host carries at most one
-- rec of each kind. Pushed by the executor after a host is finalized, and the
-- right_size/reserved savings roll into the cost module savings estimate.
CREATE TABLE IF NOT EXISTS postmig_recs (
    server_id        VARCHAR(64) NOT NULL,      -- hostname lowercased (stable)
    kind             VARCHAR(16) NOT NULL,      -- right_size/reserved/anomaly/perf
    from_spec        VARCHAR(64),
    to_spec          VARCHAR(64),
    reason           TEXT,
    monthly_saving_usd DOUBLE,
    confidence       DOUBLE,
    severity         VARCHAR(16),
    detail           TEXT,
    scan_id          VARCHAR(64),
    scanned_at       VARCHAR(40),
    summary          TEXT,
    updated_at       VARCHAR(40),
    PRIMARY KEY (server_id, kind)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- F11 — automated testing. test_cases: generated cases per app (keyed by
-- app_id+name). test_runs: one execution per run id (results JSON, phase
-- pre/post). test_diffs: latest pre-vs-post comparison per app (regressions
-- count drives the test_regression cutover gate).
CREATE TABLE IF NOT EXISTS test_cases (
    app_id           VARCHAR(64) NOT NULL,
    name             VARCHAR(128) NOT NULL,
    kind             VARCHAR(16),
    endpoint         VARCHAR(255),
    method           VARCHAR(16),
    request          JSON,
    expected         JSON,
    setup            TEXT,
    scan_id          VARCHAR(64),
    updated_at       VARCHAR(40),
    PRIMARY KEY (app_id, name)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS test_runs (
    id               VARCHAR(64) PRIMARY KEY,
    app_id           VARCHAR(64),
    phase            VARCHAR(8),               -- pre / post
    target           VARCHAR(255),
    results          JSON,                    -- [{case, status, duration_ms, ...}]
    passed           INT,
    failed           INT,
    errors           INT,
    run_at           VARCHAR(40),
    scan_id          VARCHAR(64)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS test_diffs (
    app_id           VARCHAR(64) PRIMARY KEY,
    pre_run_id       VARCHAR(64),
    post_run_id      VARCHAR(64),
    diff             JSON,                    -- [{case, pre_status, post_status, verdict, detail}]
    regressions      INT,
    scan_id          VARCHAR(64),
    scanned_at       VARCHAR(40),
    summary          TEXT,
    updated_at       VARCHAR(40)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
"""


# Task kinds idc-migrate enqueues for an executor to pull. Mirrors the old
# push-trigger action names so the executor's per-kind handlers map 1:1.
TASK_KINDS = (
    "scan", "comb", "modify", "db-scan", "runtime-containerize",
    "legacy-disposition", "cutover-playbook", "as-built", "iac-emit",
    "postmig-optimize", "test-gen", "test-run", "test-compare",
    "discover-repos",
)


def _lease_expiry(now_iso: str, lease_seconds: int) -> str:
    """``now_iso + lease_seconds`` as an ISO-8601 UTC ``...Z`` string. ``now_iso``
    follows the codebase convention (``datetime.utcnow().isoformat(...)+"Z"``).
    Falls back to a plain increment when the string won't parse so a caller's
    odd timestamp never crashes a claim."""
    try:
        base = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        return (base + timedelta(seconds=lease_seconds)) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return f"{now_iso}+{lease_seconds}s"


class Store:
    """MariaDB-backed store. ``url`` is an ``IDC_DB_URL`` string
    (``mysql://`` or ``mariadb://``)."""

    def __init__(self, url: str):
        self._mysql = self._parse(url)
        self.ph = "%s"
        self._pool: Optional[Queue] = None
        self._pool_lock = threading.Lock()
        self._pool_max = 8

    # -- url parsing / connection setup -----------------------------------
    @staticmethod
    def _parse(url) -> dict:
        s = str(url)
        if not s.startswith(("mysql://", "mariadb://")):
            raise ValueError(
                f"IDC_DB_URL must be mysql:// or mariadb:// (got {s!r}); "
                "the SQLite backend was removed")
        p = urllib.parse.urlparse(s)
        return {
            "host": p.hostname or "10.0.0.3",
            "port": p.port or 3306,
            "user": urllib.parse.unquote(p.username or ""),
            "password": urllib.parse.unquote(p.password or ""),
            "db": (p.path or "/").lstrip("/"),
        }

    def _new_mysql(self):
        import pymysql  # lazy import
        return pymysql.connect(
            host=self._mysql["host"], port=self._mysql["port"],
            user=self._mysql["user"], password=self._mysql["password"],
            database=self._mysql["db"], charset="utf8mb4",
            autocommit=True, cursorclass=pymysql.cursors.DictCursor,
        )

    # -- mariadb pool ------------------------------------------------------
    def _pool_get(self):
        with self._pool_lock:
            if self._pool is None:
                self._pool = Queue(maxsize=self._pool_max)
                for _ in range(2):
                    self._pool.put(self._new_mysql())
        try:
            conn = self._pool.get_nowait()
        except Empty:
            return self._new_mysql()
        # a pooled conn may have sat idle past MariaDB's wait_timeout (server
        # closed it). ping with reconnect=True re-establishes it; on failure,
        # drop it and make a fresh one instead of returning a dead conn.
        try:
            conn.ping(reconnect=True)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            return self._new_mysql()
        return conn

    def _pool_put(self, conn) -> None:
        try:
            if self._pool is not None and self._pool.qsize() < self._pool_max:
                self._pool.put(conn, timeout=1)
                return
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    # -- driver primitives -------------------------------------------------
    def _x(self, sql: str) -> str:
        """Translate ``?`` placeholders to pymysql's ``%s`` marker."""
        return sql.replace("?", "%s")

    def _cast_real(self, expr: str) -> str:
        """Numeric cast for json_extract results."""
        return f"CAST({expr} AS DECIMAL(20,4))"

    def _jq(self, extract_expr: str) -> str:
        """Unquote a json_extract *string* scalar for compare/group/facet.
        MariaDB's JSON_EXTRACT returns the JSON representation (strings come
        back double-quoted), so JSON_UNQUOTE strips that."""
        return f"JSON_UNQUOTE({extract_expr})"

    def _json_array_len(self, col: str) -> str:
        return f"JSON_LENGTH({col})"

    def _upsert_sql(self, table: str, cols: List[str], pk: str) -> str:
        cols = list(cols)
        placeholders = ", ".join(["?"] * len(cols))
        cols_csv = ", ".join(cols)
        set_cols = [c for c in cols if c != pk]
        set_clause = ", ".join(f"{c}=VALUES({c})" for c in set_cols)
        return (f"INSERT INTO {table}({cols_csv}) VALUES({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {set_clause}")

    # Rows per executemany batch. Bounds the assembled INSERT packet so a
    # 15K-server rebuild never trips max_allowed_packet; all chunks still share
    # the caller's single transaction (the win is collapsing N transactions,
    # not N statements — pymysql also rewrites each chunk to one multi-row INSERT).
    _BULK_BATCH = 1000

    def _bulk_upsert(self, cur, sql: str, rows: List[tuple]) -> None:
        """Chunked executemany inside the caller's open transaction."""
        for i in range(0, len(rows), self._BULK_BATCH):
            cur.executemany(sql, rows[i:i + self._BULK_BATCH])

    def _exec_schema(self, conn, schema: str) -> None:
        cur = conn.cursor()
        try:
            for stmt in (s.strip() for s in schema.split(";")):
                if not stmt:
                    continue
                try:
                    cur.execute(stmt)
                except Exception as e:
                    # tolerate duplicate index names on re-run (MariaDB 10.5
                    # has no CREATE INDEX IF NOT EXISTS)
                    if ("Duplicate key name" in str(e) or "already exists" in str(e)
                            or "Duplicate column" in str(e)):
                        continue
                    raise
        finally:
            try:
                cur.close()
            except Exception:
                pass

    # -- transaction / read helpers ---------------------------------------
    @contextmanager
    def tx(self):
        """Write transaction. Holds a pooled mariadb conn.

        On failure: rollback, and if the connection is dead (rollback itself
        raised, e.g. ``MySQL server has gone away``) close it instead of
        returning a poisoned conn to the pool."""
        conn = self._pool_get()
        cur = conn.cursor()
        conn_dead = False
        try:
            conn.begin()
            yield cur
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                conn_dead = True   # rollback failed -> conn is poisoned; drop it
            raise
        finally:
            try:
                cur.close()
            except Exception:
                pass
            if conn_dead:
                try:
                    conn.close()
                except Exception:
                    pass
            else:
                self._pool_put(conn)

    @contextmanager
    def _read_cursor(self):
        """A cursor for one or more reads. Don't use for writes."""
        conn = self._pool_get()
        cur = conn.cursor()
        try:
            yield cur
        finally:
            try:
                cur.close()
            except Exception:
                pass
            self._pool_put(conn)

    def _fetchall(self, sql: str, args: Iterable = ()) -> List[Dict[str, Any]]:
        with self._read_cursor() as cur:
            cur.execute(self._x(sql), list(args))
            return [dict(r) for r in cur.fetchall()]

    def _fetchone(self, sql: str, args: Iterable = ()) -> Optional[Dict[str, Any]]:
        with self._read_cursor() as cur:
            cur.execute(self._x(sql), list(args))
            r = cur.fetchone()
            return dict(r) if r is not None else None

    def _scalar(self, sql: str, args: Iterable = ()) -> Any:
        """First column of the first row (for COUNT/SUM etc.)."""
        r = self._fetchone(sql, args)
        if not r:
            return None
        return next(iter(r.values()))

    def close(self) -> None:
        with self._pool_lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            while True:
                try:
                    pool.get_nowait().close()
                except Empty:
                    break

    # -- raw assets --------------------------------------------------------
    def upsert_raw(self, assets: Iterable[Any]) -> int:
        sql = self._upsert_sql("raw_assets",
                               ["source", "source_id", "hostname", "fqdn", "ip",
                                "attrs", "ingested_at"], "source")
        n = 0
        with self.tx() as cur:
            for a in assets:
                cur.execute(self._x(sql),
                            (a.source, a.source_id, a.hostname, a.fqdn, a.ip,
                             dumps(a.attrs), a.ingested_at))
                n += 1
        return n

    def list_raw(self, source: Optional[str] = None) -> List[Any]:
        if source:
            return self._fetchall("SELECT * FROM raw_assets WHERE source=?", (source,))
        return self._fetchall("SELECT * FROM raw_assets")

    # -- servers -----------------------------------------------------------
    _SERVER_COLS = ["id", "hostname", "fqdn", "ips", "source_type", "role", "os",
                    "os_version", "cpu_cores", "cpu_model", "mem_gb", "disks",
                    "subnet", "vlan", "datacenter", "cluster", "env", "app_ids",
                    "tags", "business_criticality", "migration_tier",
                    "utilization", "source_refs", "status",
                    "sizing_basis", "assessment_confidence",
                    "warranty_status", "hardware_eol",
                    "warranty_bucket", "os_eol_bucket",
                    "created_at", "updated_at"]

    def upsert_server(self, s: Server) -> None:
        sql = self._upsert_sql("servers", self._SERVER_COLS, "id")
        with self.tx() as cur:
            cur.execute(self._x(sql),
                        (s.id, s.hostname, s.fqdn, dumps(s.ips), s.source_type,
                         s.role, s.os, s.os_version, s.cpu_cores, s.cpu_model,
                         s.mem_gb, dumps([d.to_dict() for d in s.disks]),
                         s.subnet, s.vlan, s.datacenter, s.cluster, s.env,
                         dumps(s.app_ids), dumps(s.tags),
                         s.business_criticality, s.migration_tier,
                         dumps(s.utilization.to_dict()), dumps(s.source_refs),
                         s.status, s.sizing_basis, s.assessment_confidence,
                         s.warranty_status, s.hardware_eol,
                         s.warranty_bucket, s.os_eol_bucket,
                         s.created_at, s.updated_at))

    def replace_servers(self, servers: Iterable[Server]) -> int:
        """Atomically replace every server row: DELETE + batched insert in one tx.

        The rebuild write hotspot. ``rebuild`` previously did one ``upsert_server``
        per server (15K own transactions for a 15K estate, each a full pool
        checkout/BEGIN/COMMIT round trip to the remote DB). This collapses that
        to a single transaction with chunked multi-row inserts."""
        sql = self._x(self._upsert_sql("servers", self._SERVER_COLS, "id"))
        rows = [(
            s.id, s.hostname, s.fqdn, dumps(s.ips), s.source_type, s.role, s.os,
            s.os_version, s.cpu_cores, s.cpu_model, s.mem_gb,
            dumps([d.to_dict() for d in s.disks]), s.subnet, s.vlan, s.datacenter,
            s.cluster, s.env, dumps(s.app_ids), dumps(s.tags),
            s.business_criticality, s.migration_tier, dumps(s.utilization.to_dict()),
            dumps(s.source_refs), s.status, s.sizing_basis, s.assessment_confidence,
            s.warranty_status, s.hardware_eol, s.warranty_bucket, s.os_eol_bucket,
            s.created_at, s.updated_at,
        ) for s in servers]
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM servers"))
            self._bulk_upsert(cur, sql, rows)
        return len(rows)

    def _row_to_server(self, r) -> Server:
        d = dict(r)
        d["ips"] = loads(d.get("ips")) or []
        d["disks"] = loads(d.get("disks")) or []
        d["app_ids"] = loads(d.get("app_ids")) or []
        d["tags"] = loads(d.get("tags")) or []
        d["utilization"] = loads(d.get("utilization")) or {}
        d["source_refs"] = loads(d.get("source_refs")) or []
        return Server.from_dict(d)

    def get_server(self, sid: str) -> Optional[Server]:
        r = self._fetchone("SELECT * FROM servers WHERE id=?", (sid,))
        return self._row_to_server(r) if r else None

    def list_servers(
        self,
        role: Optional[str] = None,
        env: Optional[str] = None,
        status: Optional[str] = None,
        source_type: Optional[str] = None,
        hostname_like: Optional[str] = None,
        limit: int = 5000,
    ) -> List[Server]:
        sql = "SELECT * FROM servers WHERE 1=1"
        args: List[Any] = []
        if role:
            sql += " AND role=?"; args.append(role)
        if env:
            sql += " AND env=?"; args.append(env)
        if status:
            sql += " AND status=?"; args.append(status)
        if source_type:
            sql += " AND source_type=?"; args.append(source_type)
        if hostname_like:
            sql += " AND (hostname LIKE ? OR fqdn LIKE ?)"
            args += [f"%{hostname_like}%", f"%{hostname_like}%"]
        sql += " ORDER BY hostname LIMIT ?"
        args.append(limit)
        return [self._row_to_server(r) for r in self._fetchall(sql, args)]

    def count_servers(self) -> int:
        return self._scalar("SELECT COUNT(*) FROM servers")

    def list_all_servers(self, limit: int = 200000) -> List[Server]:
        """Load the full estate (no pagination) — for planners/aggregations.
        Cap is a safety valve, not a page size."""
        rows = self._fetchall("SELECT * FROM servers LIMIT ?", (limit,))
        return [self._row_to_server(r) for r in rows]

    def resolve_server_ids(self, tokens: List[str]) -> Dict[str, Any]:
        """Map free-text host tokens (each a server_id, hostname, or fqdn,
        case-insensitive) to existing server_ids. Returns
        ``{matched: [server_id...], unresolved: [token...]}``. Used by the
        repo->hosts editor so the operator can type hostnames they know."""
        toks = [t.strip() for t in tokens if t and t.strip()]
        if not toks:
            return {"matched": [], "unresolved": []}
        # one pass over the estate; index by id/hostname/fqdn (lowercased)
        idx: Dict[str, str] = {}
        for s in self.list_all_servers():
            for k in (s.id, s.hostname, s.fqdn):
                if k:
                    idx[str(k).lower()] = s.id
        matched, unresolved = [], []
        for t in toks:
            sid = idx.get(t.lower())
            if sid:
                matched.append(sid)
            else:
                unresolved.append(t)
        # de-dup matched, preserve order
        return {"matched": list(dict.fromkeys(matched)), "unresolved": unresolved}

    def query_server_ids(self, f: "ServerFilter") -> List[str]:
        """All server ids matching a filter (no pagination) — for scoping."""
        where, args, join_matches = self._filters_sql(f)
        join = " LEFT JOIN matches ON matches.server_id=servers.id" if join_matches else ""
        rows = self._fetchall(
            f"SELECT servers.id AS id FROM servers{join} WHERE {where}", args)
        return [r["id"] for r in rows]

    # -- scalable query (filters + pagination + facets) -------------------
    def _filters_sql(self, f: "ServerFilter") -> tuple:
        """Build a WHERE clause + params from a ServerFilter (joins matches
        only when a target/conf filter is present)."""
        where = ["1=1"]
        args: List[Any] = []
        join_matches = bool(f.target_product or f.conf_max is not None or f.conf_min is not None)
        for col, val in (("role", f.role), ("env", f.env), ("os", f.os),
                         ("source_type", f.source_type),
                         ("business_criticality", f.criticality),
                         ("cluster", f.cluster), ("datacenter", f.datacenter)):
            if val:
                where.append(f"servers.{col}=?"); args.append(val)
        if f.status:
            where.append("servers.status=?"); args.append(f.status)
        if f.q:
            where.append("(servers.hostname LIKE ? OR servers.fqdn LIKE ? "
                         "OR servers.ips LIKE ? OR servers.tags LIKE ? OR servers.app_ids LIKE ?)")
            pat = f"%{f.q}%"
            args += [pat, pat, pat, pat, pat]
        if f.util_mem_min is not None:
            cm = self._cast_real("json_extract(servers.utilization,'$.mem_p95')")
            where.append(f"{cm} >= ?")
            args.append(f.util_mem_min)
        if f.util_cpu_min is not None:
            cm = self._cast_real("json_extract(servers.utilization,'$.cpu_p95')")
            where.append(f"{cm} >= ?")
            args.append(f.util_cpu_min)
        if f.util_disk_min is not None:
            cm = self._cast_real("json_extract(servers.utilization,'$.disk_used_pct')")
            where.append(f"{cm} >= ?")
            args.append(f.util_disk_min)
        if f.wave_id:
            # membership is a JSON array in waves.server_ids; resolve it once and
            # filter servers by primary-key IN(...). The old EXISTS+LIKE-on-JSON
            # was O(servers x wave_size): a 12K-member wave stores a ~180KB
            # server_ids string that got LIKE-scanned once per server row, which
            # timed out the wave-members view for big waves. IN on the indexed
            # servers.id is O(M) and stays sub-second at estate scale.
            row = self._fetchone("SELECT server_ids FROM waves WHERE id=?",
                                 (f.wave_id,))
            ids = loads(row["server_ids"]) if row else None
            if not ids:
                where.append("1=0")   # wave missing or has no members -> none
            else:
                where.append(f"servers.id IN ({','.join(['?'] * len(ids))})")
                args.extend(ids)
        # data-gap (gap-actionable) — facet/filter on the persisted derived buckets
        for col, val in (("warranty_bucket", f.warranty_bucket),
                         ("os_eol_bucket", f.os_eol_bucket)):
            if val:
                where.append(f"servers.{col}=?"); args.append(val)
        # app membership — servers.app_ids is a JSON array (e.g. ["APP_0069"]);
        # match the quoted element so APP_006 doesn't bleed into APP_0069.
        # MariaDB 10.5 has no JSON_TABLE/JSON_CONTAINS path, so a bounded LIKE
        # on the quoted array element is the portable precise filter.
        if f.app_id:
            where.append("servers.app_ids LIKE ?")
            args.append(f'%"{f.app_id}"%')
        # resume cursor for the bulk 7R batch (process the estate in ordered
        # chunks + commit per batch): servers.hostname > cursor, ordered asc.
        if f.hostname_after:
            where.append("servers.hostname > ?")
            args.append(f.hostname_after)
        if join_matches:
            if f.target_product:
                jq = self._jq("json_extract(matches.target,'$.product')")
                where.append(f"{jq}=?")
                args.append(f.target_product)
            if f.conf_min is not None:
                where.append("matches.confidence >= ?"); args.append(f.conf_min)
            if f.conf_max is not None:
                where.append("matches.confidence <= ?"); args.append(f.conf_max)
        return " AND ".join(where), args, join_matches

    def query_servers(self, f: "ServerFilter",
                      page: int = 1, page_size: int = 50,
                      order_by: str = "hostname", order_dir: str = "asc",
                      with_facets: bool = True) -> dict:
        where, args, join_matches = self._filters_sql(f)
        join = " LEFT JOIN matches ON matches.server_id=servers.id" if join_matches else ""
        allowed = {"hostname", "cpu_cores", "mem_gb", "env", "role", "os",
                   "business_criticality", "source_type", "updated_at", "created_at"}
        ob = order_by if order_by in allowed else "hostname"
        od = "DESC" if order_dir.lower() == "desc" else "ASC"
        offset = max(0, (page - 1) * page_size)
        total = self._scalar(
            f"SELECT COUNT(*) FROM servers{join} WHERE {where}", args)
        rows = self._fetchall(
            f"SELECT servers.* FROM servers{join} WHERE {where} "
            f"ORDER BY servers.{ob} {od} LIMIT ? OFFSET ?",
            args + [page_size, offset])
        items = [self._row_to_server(r) for r in rows]
        # facets are 6 GROUP-BY queries; callers that only need items+total
        # (e.g. the wave-members view) pass with_facets=False to skip them —
        # significant for big waves where the filter is a 12K-id IN clause.
        facets = self._facets(where, args, join_matches) if with_facets else {}
        return {"items": items, "total": total, "page": page,
                "page_size": page_size, "facets": facets}

    def _facets(self, where: str, args: list, join_matches: bool) -> dict:
        join = " LEFT JOIN matches ON matches.server_id=servers.id" if join_matches else ""
        out: Dict[str, Dict[str, int]] = {}
        for col in ("role", "env", "os", "source_type", "business_criticality",
                    "cluster", "warranty_bucket", "os_eol_bucket"):
            rows = self._fetchall(
                f"SELECT servers.{col} AS c, COUNT(*) AS n FROM servers{join} "
                f"WHERE {where} GROUP BY servers.{col}", args)
            out[col] = {r["c"] or "(none)": r["n"]
                        for r in rows if r["c"] is not None}
        # target product facet (always join for this one)
        jq = self._jq("json_extract(matches.target,'$.product')")
        rows = self._fetchall(
            f"SELECT {jq} AS c, COUNT(*) AS n "
            f"FROM servers LEFT JOIN matches ON matches.server_id=servers.id "
            f"WHERE {where} GROUP BY {jq}", args)
        out["target_product"] = {r["c"] or "(unmatched)": r["n"]
                                 for r in rows if r["c"] is not None}
        # utilization buckets (mem_p95)
        cm = self._cast_real("json_extract(servers.utilization,'$.mem_p95')")
        rows = self._fetchall(
            f"SELECT CASE WHEN {cm} >= 80 THEN 'high' "
            f"WHEN {cm} >= 50 THEN 'mid' ELSE 'low' END AS b, COUNT(*) AS n "
            f"FROM servers{join} WHERE {where} GROUP BY b", args)
        out["util_mem"] = {r["b"]: r["n"] for r in rows}
        return out

    def aggregations(self) -> dict:
        """Dashboard tiles + charts (cheap GROUP BYs over the whole estate)."""
        n = self._scalar("SELECT COUNT(*) FROM servers")
        cores = self._scalar("SELECT COALESCE(SUM(cpu_cores),0) FROM servers")
        mem = self._scalar("SELECT COALESCE(SUM(mem_gb),0) FROM servers")
        def dist(sql):
            return {r["c"]: r["n"] for r in self._fetchall(sql) if r["c"]}
        by_role = dist("SELECT role AS c, COUNT(*) AS n FROM servers GROUP BY role")
        by_env = dist("SELECT env AS c, COUNT(*) AS n FROM servers GROUP BY env")
        by_os = dist("SELECT os AS c, COUNT(*) AS n FROM servers GROUP BY os")
        jp = self._jq("json_extract(target,'$.product')")
        jr = self._jq("json_extract(target,'$.region')")
        by_target = dist(f"SELECT {jp} AS c, COUNT(*) AS n FROM matches GROUP BY {jp}")
        by_region = dist(f"SELECT {jr} AS c, COUNT(*) AS n FROM matches GROUP BY {jr}")
        # match confidence distribution
        conf = {"high(>=0.85)": 0, "medium(0.7-0.85)": 0, "low(<0.7)": 0}
        for c in self._fetchall("SELECT confidence AS c FROM matches"):
            v = c["c"] or 0
            if v >= 0.85:
                conf["high(>=0.85)"] += 1
            elif v >= 0.7:
                conf["medium(0.7-0.85)"] += 1
            else:
                conf["low(<0.7)"] += 1
        cm_mem = self._cast_real("json_extract(utilization,'$.mem_p95')")
        cm_disk = self._cast_real("json_extract(utilization,'$.disk_used_pct')")
        high_util = self._scalar(
            f"SELECT COUNT(*) FROM servers WHERE "
            f"{cm_mem} >= 80 OR {cm_disk} >= 80")
        waves = [{"id": r["id"], "name": r["name"], "stage": r["stage"], "n": r["n"]}
                 for r in self._fetchall(
            f"SELECT id, name, stage, COALESCE({self._json_array_len('server_ids')},0) AS n "
            "FROM waves ORDER BY stage, name")]
        # per-application rollup: servers / cores / mem grouped by app_id (the
        # JSON array in servers.app_ids, expanded in Python — MariaDB 10.5 lacks
        # JSON_TABLE), enriched with app name/tier/env (workloads) + 7R strategy
        # + target product (app_strategies). Sorted by server count desc so the
        # dashboard's "By application" card shows the biggest apps first.
        app_acc: Dict[str, list] = {}
        for r in self._fetchall("SELECT app_ids, cpu_cores, mem_gb FROM servers"):
            ids = loads(r["app_ids"]) if r["app_ids"] else None
            if not ids:
                continue
            cpu = r["cpu_cores"] or 0
            mem = r["mem_gb"] or 0
            for aid in ids:
                a = app_acc.setdefault(aid, [0, 0, 0])
                a[0] += 1; a[1] += cpu; a[2] += mem
        wl = {r["app_id"]: r for r in self._fetchall(
            "SELECT app_id, name, tier, env FROM workloads")}
        st = {r["app_id"]: r for r in self._fetchall(
            "SELECT app_id, strategy, target FROM app_strategies")}
        by_app = []
        for aid, (cnt, cores_a, mem_a) in sorted(app_acc.items(),
                                                 key=lambda kv: kv[1][0], reverse=True):
            w = wl.get(aid, {})
            s = st.get(aid, {})
            by_app.append({"app_id": aid, "name": w.get("name") or aid,
                           "tier": w.get("tier") or "", "env": w.get("env") or "",
                           "strategy": s.get("strategy") or "",
                           "target": s.get("target") or "", "servers": cnt,
                           "cores": int(cores_a or 0), "mem_gb": int(mem_a or 0)})
        return {"servers": n, "cores": cores, "mem_gb": mem,
                "by_role": by_role, "by_env": by_env, "by_os": by_os,
                "by_target": by_target, "by_region": by_region,
                "confidence": conf, "high_utilization": high_util, "waves": waves,
                "by_app": by_app}

    def stream_servers_csv(self, f: "ServerFilter", order_by: str = "hostname"):
        """Yield CSV rows lazily for streaming export (no full load).

        Uses a dedicated cursor on a pooled mariadb conn held long-term — a
        long stream doesn't block writes (the conn isn't returned to the pool
        until the generator closes)."""
        import csv as _csv, io
        where, args, join_matches = self._filters_sql(f)
        join = " LEFT JOIN matches ON matches.server_id=servers.id" if join_matches else ""
        allowed = {"hostname", "cpu_cores", "mem_gb", "env", "role", "os", "source_type"}
        ob = order_by if order_by in allowed else "hostname"
        buf = io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["id", "hostname", "fqdn", "role", "source_type", "os", "os_version",
                    "cpu_cores", "mem_gb", "disk_gb", "env", "criticality", "cluster",
                    "ip", "util_cpu", "util_mem", "util_disk",
                    "target_product", "target_spec", "region", "confidence", "method"])
        yield buf.getvalue()
        with self._read_cursor() as cur:
            BATCH = 1000
            offset = 0
            while True:
                cur.execute(self._x(
                    f"SELECT servers.id AS id, hostname, fqdn, role, source_type, os, "
                    f"os_version, cpu_cores, mem_gb, disks, env, business_criticality, "
                    f"cluster, ips, utilization "
                    f"FROM servers{join} WHERE {where} ORDER BY servers.{ob} LIMIT ? OFFSET ?"),
                    list(args) + [BATCH, offset])
                rows = cur.fetchall()
                if not rows:
                    break
                ids = [r["id"] for r in rows]
                m = {}
                if ids:
                    placeholders = ",".join(["?"] * len(ids))
                    jp = self._jq("json_extract(target,'$.product')")
                    js = self._jq("json_extract(target,'$.spec')")
                    jr = self._jq("json_extract(target,'$.region')")
                    cur.execute(self._x(
                        "SELECT server_id AS sid, "
                        f"{jp} AS product, {js} AS spec, {jr} AS region, "
                        "confidence AS conf, method AS method "
                        "FROM matches WHERE server_id IN (" + placeholders + ")"), ids)
                    for r in cur.fetchall():
                        m[r["sid"]] = r
                for r in rows:
                    disk = sum(d.get("size_gb", 0) for d in (loads(r["disks"]) or []))
                    ips = loads(r["ips"]) or []
                    util = loads(r["utilization"]) or {}
                    mm = m.get(r["id"])
                    buf = io.StringIO(); w = _csv.writer(buf)
                    w.writerow([r["id"], r["hostname"], r["fqdn"], r["role"],
                                r["source_type"], r["os"], r["os_version"],
                                r["cpu_cores"], r["mem_gb"], disk, r["env"],
                                r["business_criticality"], r["cluster"],
                                ";".join(ips), util.get("cpu_p95"),
                                util.get("mem_p95"), util.get("disk_used_pct"),
                                mm["product"] if mm else "", mm["spec"] if mm else "",
                                mm["region"] if mm else "", mm["conf"] if mm else "",
                                mm["method"] if mm else ""])
                    yield buf.getvalue()
                offset += BATCH

    # -- workloads ---------------------------------------------------------
    def upsert_workload(self, w: Workload) -> None:
        sql = self._upsert_sql("workloads",
                               ["app_id", "name", "tier", "env", "server_ids",
                                "depends_on", "notes"], "app_id")
        with self.tx() as cur:
            cur.execute(self._x(sql),
                        (w.app_id, w.name, w.tier, w.env, dumps(w.server_ids),
                         dumps(w.depends_on), w.notes))

    def replace_workloads(self, workloads: Iterable[Workload]) -> int:
        """Atomically replace every workload row: DELETE + batched insert in one tx."""
        sql = self._x(self._upsert_sql("workloads",
                         ["app_id", "name", "tier", "env", "server_ids",
                          "depends_on", "notes"], "app_id"))
        rows = [(w.app_id, w.name, w.tier, w.env, dumps(w.server_ids),
                 dumps(w.depends_on), w.notes) for w in workloads]
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM workloads"))
            self._bulk_upsert(cur, sql, rows)
        return len(rows)

    def list_workloads(self) -> List[Workload]:
        out = []
        for r in self._fetchall("SELECT * FROM workloads"):
            d = dict(r)
            d["server_ids"] = loads(d.get("server_ids")) or []
            d["depends_on"] = loads(d.get("depends_on")) or []
            out.append(Workload.from_dict(d))
        return out

    def get_workload(self, app_id: str) -> Optional[Workload]:
        """One workload by app_id (cheap single-row read)."""
        r = self._fetchone("SELECT * FROM workloads WHERE app_id=?", (app_id,))
        if not r:
            return None
        d = dict(r)
        d["server_ids"] = loads(d.get("server_ids")) or []
        d["depends_on"] = loads(d.get("depends_on")) or []
        return Workload.from_dict(d)

    def app_options(self) -> List[Dict[str, str]]:
        """Lightweight app catalog for UI dropdowns: just app_id + name + tier
        + env from ``workloads``, ordered by app_id. Cheaper than
        ``list_workloads`` (no JSON arrays decoded) and covers every app even
        if it has no servers yet."""
        return [dict(r) for r in self._fetchall(
            "SELECT app_id, name, tier, env FROM workloads ORDER BY app_id")]

    # -- matches -----------------------------------------------------------
    def upsert_match(self, m: Match) -> None:
        sql = self._upsert_sql("matches",
                               ["server_id", "target", "confidence", "method",
                                "rationale", "alternatives", "created_at"], "server_id")
        with self.tx() as cur:
            cur.execute(self._x(sql),
                        (m.server_id, dumps(m.target.to_dict()), m.confidence,
                         m.method, m.rationale, dumps(m.alternatives), m.created_at))

    def replace_matches_and_costs(self, matches: Iterable[Match],
                                  costs: Iterable[CostEstimate]) -> int:
        """Atomically replace all matches + cost_estimates in one tx.

        Both tables are derived together from the match pass (one row per
        server) and share the rebuild lifecycle, so they are wiped and
        rewritten together. ``rebuild`` previously issued two per-server
        transactions here (``upsert_cost`` + ``upsert_match`` × 15K)."""
        msql = self._x(self._upsert_sql("matches",
                          ["server_id", "target", "confidence", "method",
                           "rationale", "alternatives", "created_at"], "server_id"))
        csql = self._x(self._upsert_sql("cost_estimates", self._COST_COLS, "server_id"))
        mrows = [(m.server_id, dumps(m.target.to_dict()), m.confidence, m.method,
                  m.rationale, dumps(m.alternatives), m.created_at) for m in matches]
        crows = [(c.server_id, c.target_product, c.spec, c.region, c.monthly_usd,
                  c.yearly_usd, c.basis, c.pricing_source, c.computed_at)
                 for c in costs]
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM matches"))
            cur.execute(self._x("DELETE FROM cost_estimates"))
            self._bulk_upsert(cur, msql, mrows)
            self._bulk_upsert(cur, csql, crows)
        return len(mrows)

    def get_match(self, server_id: str) -> Optional[Match]:
        r = self._fetchone("SELECT * FROM matches WHERE server_id=?", (server_id,))
        if not r:
            return None
        d = dict(r)
        tgt = Target.from_dict(loads(d.get("target")) or {})
        alts = loads(d.get("alternatives")) or []
        return Match(server_id=d["server_id"], target=tgt, confidence=d["confidence"],
                     method=d["method"], rationale=d["rationale"], alternatives=alts,
                     created_at=d["created_at"])

    def list_matches(self) -> List[Match]:
        out = []
        for r in self._fetchall("SELECT * FROM matches ORDER BY created_at"):
            d = dict(r)
            tgt = Target.from_dict(loads(d.get("target")) or {})
            alts = loads(d.get("alternatives")) or []
            out.append(Match(server_id=d["server_id"], target=tgt, confidence=d["confidence"],
                             method=d["method"], rationale=d["rationale"], alternatives=alts,
                             created_at=d["created_at"]))
        return out

    # -- waves -------------------------------------------------------------
    def upsert_wave(self, w: Wave) -> None:
        sql = self._upsert_sql("waves",
                               ["id", "name", "stage", "server_ids", "depends_on",
                                "rationale", "status"], "id")
        with self.tx() as cur:
            cur.execute(self._x(sql),
                        (w.id, w.name, w.stage, dumps(w.server_ids),
                         dumps(w.depends_on), w.rationale, w.status))

    def list_waves(self) -> List[Wave]:
        out = []
        for r in self._fetchall("SELECT * FROM waves ORDER BY stage, name"):
            d = dict(r)
            d["server_ids"] = loads(d.get("server_ids")) or []
            d["depends_on"] = loads(d.get("depends_on")) or []
            d["downtime_window"] = loads(d.get("downtime_window")) or {}
            out.append(Wave.from_dict(d))
        return out

    def replace_waves(self, waves: Iterable[Wave]) -> None:
        """Atomically replace all waves: DELETE + inserts in one transaction."""
        sql = self._upsert_sql("waves",
                               ["id", "name", "stage", "server_ids", "depends_on",
                                "rationale", "status", "downtime_window"], "id")
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM waves"))
            for w in waves:
                cur.execute(self._x(sql),
                            (w.id, w.name, w.stage, dumps(w.server_ids),
                             dumps(w.depends_on), w.rationale, w.status,
                             dumps(w.downtime_window or {})))

    # -- ingest runs -------------------------------------------------------
    def record_ingest(self, run: IngestRun) -> None:
        with self.tx() as cur:
            cur.execute(self._x(
                "INSERT INTO ingest_runs(id,source,mode,raw_count,started_at,"
                "finished_at,error) VALUES(?,?,?,?,?,?,?)"),
                (run.id, run.source, run.mode, run.raw_count, run.started_at,
                 run.finished_at, run.error))

    def list_ingest_runs(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._fetchall(
            "SELECT * FROM ingest_runs ORDER BY started_at DESC LIMIT 50")]

    # -- system config (key/value) ----------------------------------------
    def get_config(self, k: str) -> Optional[str]:
        r = self._fetchone("SELECT v FROM system_config WHERE k=?", (k,))
        return r["v"] if r else None

    def set_config(self, k: str, v: str) -> None:
        with self.tx() as cur:
            cur.execute(self._x(
                "INSERT INTO system_config(k, v) VALUES(?, ?) "
                "ON DUPLICATE KEY UPDATE v=VALUES(v)"), (k, v))

    def get_config_map(self, keys: Iterable[str]) -> Dict[str, str]:
        if not keys:
            return {}
        ph = ",".join(["?"] * len(keys))
        return {r["k"]: r["v"] for r in
                self._fetchall(f"SELECT k, v FROM system_config WHERE k IN ({ph})",
                               list(keys))}

    # -- agent tasks -------------------------------------------------------
    def create_task(self, t: AgentTask) -> None:
        with self.tx() as cur:
            cur.execute(self._x(
                "INSERT INTO agent_tasks(id,prompt,mode,status,created_at,"
                "finished_at,output,error) VALUES(?,?,?,?,?,?,?,?)"),
                (t.id, t.prompt, t.mode, t.status, t.created_at,
                 t.finished_at, t.output, t.error))

    def update_task(self, task_id: str, status: str, output: str = "",
                    error: str = "", finished_at: str = "") -> None:
        with self.tx() as cur:
            cur.execute(self._x(
                "UPDATE agent_tasks SET status=?, output=?, error=?, "
                "finished_at=? WHERE id=?"),
                (status, output, error, finished_at, task_id))

    # NOTE: these are the AGENT-task (Copilot tab `/api/agent`) read methods,
    # named `get_agent_task` / `list_agent_tasks` (not `get_task`/`list_tasks`)
    # because the executor pull-dispatch layer below defines its OWN
    # `get_task` / `list_tasks` over the `executor_tasks` table. When both were
    # named `get_task`, the executor one (defined later in this class) SILENTLY
    # shadowed this one — so `/api/tasks/{id}` and the agent WS read
    # `executor_tasks` instead of `agent_tasks` and the agent task was never
    # found (404 / "unknown task") even though it existed. Distinct names keep
    # the two surfaces from colliding again.
    def get_agent_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        r = self._fetchone("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
        return dict(r) if r else None

    def list_agent_tasks(self, limit: int = 50) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._fetchall(
            "SELECT * FROM agent_tasks ORDER BY created_at DESC LIMIT ?", (limit,))]

    # -- code profiles (from external agent executor) ----------------------
    _PROFILE_COLS = ["app_id", "repo_url", "branch", "scan_id", "scanner",
                     "scanned_at", "language", "runtime", "framework",
                     "cloud_readiness", "migration_pattern", "refactor_effort",
                     "findings", "code_deps", "network_endpoints",
                     "required_changes", "blockers", "summary", "source",
                     "agent_findings", "agent_blockers", "agent_summary",
                     "updated_at"]

    def upsert_code_profile(self, p: CodeProfile) -> None:
        sql = self._upsert_sql("code_profiles", self._PROFILE_COLS, "app_id")
        with self.tx() as cur:
            cur.execute(self._x(sql), (
                p.app_id, p.repo_url, p.branch, p.scan_id, p.scanner,
                p.scanned_at, p.language, p.runtime, p.framework,
                p.cloud_readiness, p.migration_pattern, p.refactor_effort,
                dumps([f.to_dict() for f in p.findings]),
                dumps(p.code_deps), dumps(p.network_endpoints),
                dumps(p.required_changes), dumps(p.blockers),
                p.summary, p.source or "repo",
                dumps([f.to_dict() for f in p.agent_findings]),
                dumps(p.agent_blockers), p.agent_summary or "",
                p.updated_at))

    def _row_to_profile(self, d: Dict[str, Any]) -> CodeProfile:
        d = dict(d)
        d["findings"] = [ScanFinding.from_dict(x) for x in (loads(d.get("findings")) or [])]
        d["code_deps"] = loads(d.get("code_deps")) or []
        d["network_endpoints"] = loads(d.get("network_endpoints")) or []
        d["required_changes"] = loads(d.get("required_changes")) or []
        d["blockers"] = loads(d.get("blockers")) or []
        # path A — pre-ALTER rows have agent_*=NULL; default to empty (the
        # executor predates the codex pass). from_dict parses the dict-shaped
        # agent_findings into ScanFinding; null-safe via `or []`.
        d["agent_findings"] = [ScanFinding.from_dict(x) for x in
                               (loads(d.get("agent_findings")) or [])]
        d["agent_blockers"] = loads(d.get("agent_blockers")) or []
        d["agent_summary"] = d.get("agent_summary") or ""
        # F9 — pre-ALTER rows have source=NULL; default to "repo"
        if not d.get("source"):
            d["source"] = "repo"
        return CodeProfile.from_dict(d)

    def list_code_profiles(self) -> List[CodeProfile]:
        return [self._row_to_profile(r) for r in
                self._fetchall("SELECT * FROM code_profiles ORDER BY updated_at DESC")]

    def get_code_profile(self, app_id: str) -> Optional[CodeProfile]:
        r = self._fetchone("SELECT * FROM code_profiles WHERE app_id=?", (app_id,))
        return self._row_to_profile(r) if r else None

    def delete_code_profile(self, app_id: str) -> None:
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM code_profiles WHERE app_id=?"), (app_id,))

    # -- repos (git urls) — first-class source object, N:N with hosts --------
    _REPO_COLS = ["repo_id", "url", "branch", "name", "created_at", "updated_at"]

    def upsert_repo(self, r: Repo) -> None:
        sql = self._upsert_sql("repos", self._REPO_COLS, "repo_id")
        with self.tx() as cur:
            cur.execute(self._x(sql), (r.repo_id, r.url, r.branch, r.name,
                                      r.created_at, r.updated_at))

    def get_repo(self, repo_id: str) -> Optional[Repo]:
        r = self._fetchone("SELECT * FROM repos WHERE repo_id=?", (repo_id,))
        return Repo.from_dict(dict(r)) if r else None

    def get_repo_by_url(self, url: str) -> Optional[Repo]:
        r = self._fetchone("SELECT * FROM repos WHERE url=?", (url,))
        return Repo.from_dict(dict(r)) if r else None

    def list_repos(self) -> List[Repo]:
        return [Repo.from_dict(dict(r)) for r in
                self._fetchall("SELECT * FROM repos ORDER BY url ASC")]

    def delete_repo(self, repo_id: str) -> bool:
        """Delete a repo AND its host_repos edges (cascade) in one tx. Returns
        False if the repo didn't exist (caller -> 404)."""
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM host_repos WHERE repo_id=?"), (repo_id,))
            cur.execute(self._x("DELETE FROM repos WHERE repo_id=?"), (repo_id,))
            return cur.rowcount > 0

    def set_host_repos(self, server_id: str, repo_ids: List[str]) -> None:
        """Replace the set of repos a host deploys (the N:N edge from the host
        side): delete all existing edges for ``server_id`` then insert the new
        set. Empty list detaches the host from every repo."""
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM host_repos WHERE server_id=?"), (server_id,))
            if repo_ids:
                rows = [(server_id, rid) for rid in dict.fromkeys(repo_ids) if rid]
                cur.executemany(self._x(
                    "INSERT IGNORE INTO host_repos(server_id, repo_id) VALUES (?, ?)"),
                    rows)

    def set_repo_hosts(self, repo_id: str, server_ids: List[str]) -> None:
        """Replace the set of hosts that deploy a repo (the N:N edge from the
        repo side): delete all existing edges for ``repo_id`` then insert the
        new set. Empty list detaches the repo from every host."""
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM host_repos WHERE repo_id=?"), (repo_id,))
            if server_ids:
                rows = [(sid, repo_id) for sid in dict.fromkeys(server_ids) if sid]
                cur.executemany(self._x(
                    "INSERT IGNORE INTO host_repos(server_id, repo_id) VALUES (?, ?)"),
                    rows)

    def host_repos_for(self, server_id: str) -> List[str]:
        return [r["repo_id"] for r in self._fetchall(
            "SELECT repo_id FROM host_repos WHERE server_id=? ORDER BY repo_id",
            (server_id,))]

    def hosts_for_repo(self, repo_id: str) -> List[str]:
        """server_ids that deploy this repo, restricted to servers that still
        exist (a removed server's edge is filtered out here)."""
        return [r["server_id"] for r in self._fetchall(
            "SELECT hr.server_id FROM host_repos hr "
            "JOIN servers s ON s.id = hr.server_id "
            "WHERE hr.repo_id=? ORDER BY hr.server_id", (repo_id,))]

    def repos_for_app(self, app_id: str) -> List[str]:
        """Derived app->repos: the DISTINCT repos deployed on the app's hosts
        (workloads.server_ids -> host_repos). The app->host link already lives
        in workloads; app->repo is not stored separately (D2: derived)."""
        w = self.get_workload(app_id)
        if not w or not w.server_ids:
            return []
        ph = ",".join("?" * len(w.server_ids))
        return [r["repo_id"] for r in self._fetchall(
            f"SELECT DISTINCT repo_id FROM host_repos "
            f"WHERE server_id IN ({ph}) ORDER BY repo_id", tuple(w.server_ids))]

    def repo_host_counts(self) -> Dict[str, int]:
        """repo_id -> count of existing hosts that deploy it (one query)."""
        return {r["repo_id"]: int(r["n"]) for r in self._fetchall(
            "SELECT hr.repo_id, COUNT(*) AS n FROM host_repos hr "
            "JOIN servers s ON s.id = hr.server_id "
            "GROUP BY hr.repo_id")}

    def repo_hosts_map(self) -> Dict[str, List[str]]:
        """repo_id -> [server_id...] (existing hosts only), one query. For the
        enriched /api/repos list (hostnames + derived apps per repo)."""
        out: Dict[str, List[str]] = {}
        for r in self._fetchall(
            "SELECT hr.repo_id, hr.server_id FROM host_repos hr "
            "JOIN servers s ON s.id = hr.server_id "
            "ORDER BY hr.repo_id, hr.server_id"):
            out.setdefault(r["repo_id"], []).append(r["server_id"])
        return out

    def repo_host_enrichment(self) -> Dict[str, List[Dict[str, Any]]]:
        """repo_id -> [{server_id, hostname, app_ids}] for the hosts linked to
        each repo (one JOIN over host_repos x servers). Only touches servers
        actually linked to a repo — NOT the whole estate — so ``/api/repos``
        stays fast on a 13K-server estate (the old code called
        ``list_all_servers()`` which SELECT *'d every server + parsed 6 JSON
        cols per row just to read a hostname)."""
        out: Dict[str, List[Dict[str, Any]]] = {}
        for r in self._fetchall(
            "SELECT hr.repo_id, s.id AS server_id, s.hostname, s.app_ids "
            "FROM host_repos hr JOIN servers s ON s.id = hr.server_id "
            "ORDER BY hr.repo_id, s.hostname"):
            sid = r["server_id"]
            out.setdefault(r["repo_id"], []).append({
                "server_id": sid,
                "hostname": r["hostname"] or sid,
                "app_ids": loads(r["app_ids"]) if r["app_ids"] else [],
            })
        return out

    def repo_hosts_enriched(self, repo_id: str) -> List[Dict[str, Any]]:
        """[{server_id, hostname, fqdn}] for one repo's linked hosts (one JOIN).
        Used by GET /api/repos/{repo_id}/hosts — avoids loading the whole estate
        just to resolve one repo's hostnames/fqdns."""
        return [{"server_id": r["id"], "hostname": r["hostname"] or r["id"],
                 "fqdn": r["fqdn"] or ""}
                for r in self._fetchall(
                    "SELECT s.id, s.hostname, s.fqdn FROM host_repos hr "
                    "JOIN servers s ON s.id = hr.server_id WHERE hr.repo_id=? "
                    "ORDER BY s.hostname", (repo_id,))]

    def host_labels(self, server_ids: List[str]) -> Dict[str, str]:
        """server_id -> hostname for the given ids (one chunked IN-query).
        Missing/stale ids are omitted. For showing hostnames of a known small
        set (e.g. one app's hosts) WITHOUT loading the whole estate."""
        out: Dict[str, str] = {}
        ids = [s for s in server_ids if s]
        if not ids:
            return out
        for i in range(0, len(ids), 1000):
            chunk = ids[i:i + 1000]
            ph = ",".join(["?"] * len(chunk))
            for r in self._fetchall(
                    f"SELECT id, hostname FROM servers WHERE id IN ({ph})",
                    tuple(chunk)):
                out[r["id"]] = r["hostname"] or r["id"]
        return out

    # -- repo discovery scans (discover-repos executor action) ---------------
    _RSCAN_COLS = ["scan_id", "url", "status", "executor_id", "repos_json",
                   "error", "created_at", "finished_at"]

    def upsert_repo_scan(self, s: RepoScan) -> None:
        sql = self._upsert_sql("repo_scans", self._RSCAN_COLS, "scan_id")
        with self.tx() as cur:
            cur.execute(self._x(sql), (
                s.scan_id, s.url, s.status, (s.executor_id or None),
                dumps(s.repos) if s.repos is not None else None,
                (s.error or None), s.created_at, (s.finished_at or None)))

    def _row_to_repo_scan(self, d: Dict[str, Any]) -> RepoScan:
        d = dict(d)
        d["repos"] = loads(d.get("repos_json")) if d.get("repos_json") else []
        d.pop("repos_json", None)
        if d.get("executor_id") is None:
            d["executor_id"] = ""
        if d.get("error") is None:
            d["error"] = ""
        if d.get("finished_at") is None:
            d["finished_at"] = ""
        return RepoScan.from_dict(d)

    def get_repo_scan(self, scan_id: str) -> Optional[RepoScan]:
        r = self._fetchone("SELECT * FROM repo_scans WHERE scan_id=?", (scan_id,))
        return self._row_to_repo_scan(r) if r else None

    def list_repo_scans(self, limit: int = 50) -> List[RepoScan]:
        return [self._row_to_repo_scan(r) for r in self._fetchall(
            "SELECT * FROM repo_scans ORDER BY created_at DESC LIMIT ?", (limit,))]

    def complete_repo_scan(self, scan_id: str, status: str, repos, error: str,
                            executor_id: str, finished_at: str) -> bool:
        """Mark a discovery scan terminal with the discovered repos (or error).
        Returns False if the scan row doesn't exist (404 for the callback)."""
        with self.tx() as cur:
            cur.execute(self._x(
                "UPDATE repo_scans SET status=?, repos_json=?, error=?, "
                "executor_id=COALESCE(?, executor_id), finished_at=? "
                "WHERE scan_id=?"),
                (status, dumps(repos) if repos is not None else None,
                 (error or None), (executor_id or None), finished_at, scan_id))
            return cur.rowcount > 0

    # -- DB conversion profiles (F5 — heterogeneous DB migration assessment) -
    _DBP_COLS = ["db_server_id", "source_engine", "target_engine", "difficulty",
                 "est_man_days", "review_objects", "blockers",
                 "reverse_replication", "auto_convert_pct", "scan_id",
                 "scanned_at", "summary", "conversion", "updated_at"]

    def upsert_db_profile(self, d: DBConversionProfile) -> None:
        sql = self._upsert_sql("db_profiles", self._DBP_COLS, "db_server_id")
        conv = dumps(d.conversion.to_dict()) if d.conversion else None
        with self.tx() as cur:
            cur.execute(self._x(sql), (
                d.db_server_id, d.source_engine, d.target_engine, d.difficulty,
                d.est_man_days, dumps(d.review_objects), dumps(d.blockers),
                bool(d.reverse_replication), d.auto_convert_pct, d.scan_id,
                d.scanned_at, d.summary, conv, d.updated_at))

    def _row_to_db_profile(self, d: Dict[str, Any]) -> DBConversionProfile:
        d = dict(d)
        d["review_objects"] = loads(d.get("review_objects")) or []
        d["blockers"] = loads(d.get("blockers")) or []
        # MariaDB stores BOOLEAN as TINYINT (0/1); coerce back to a real bool
        # so the model stays typed regardless of driver.
        d["reverse_replication"] = bool(d.get("reverse_replication"))
        conv = d.get("conversion")
        if isinstance(conv, str) and conv:
            from ..core.models import DBConversion
            d["conversion"] = DBConversion.from_dict(loads(conv))
        elif conv is None:
            d["conversion"] = None
        return DBConversionProfile.from_dict(d)

    def list_db_profiles(self) -> List[DBConversionProfile]:
        return [self._row_to_db_profile(r) for r in
                self._fetchall("SELECT * FROM db_profiles ORDER BY updated_at DESC")]

    def get_db_profile(self, db_server_id: str) -> Optional[DBConversionProfile]:
        r = self._fetchone("SELECT * FROM db_profiles WHERE db_server_id=?", (db_server_id,))
        return self._row_to_db_profile(r) if r else None

    def delete_db_profile(self, db_server_id: str) -> None:
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM db_profiles WHERE db_server_id=?"), (db_server_id,))

    # -- Legacy / unsupported-OS disposition (F7) ------------------------------
    _LD_COLS = ["server_id", "disposition", "rationale", "confidence",
                "target_base_image", "effort_days", "prereqs", "scan_id",
                "scanned_at", "summary", "updated_at"]

    def upsert_legacy_disposition(self, d: LegacyDisposition) -> None:
        sql = self._upsert_sql("legacy_dispositions", self._LD_COLS, "server_id")
        with self.tx() as cur:
            cur.execute(self._x(sql), (
                d.server_id, d.disposition, d.rationale, d.confidence,
                d.target_base_image, d.effort_days, dumps(d.prereqs),
                d.scan_id, d.scanned_at, d.summary, d.updated_at))

    def _row_to_legacy_disposition(self, d: Dict[str, Any]) -> LegacyDisposition:
        d = dict(d)
        d["prereqs"] = loads(d.get("prereqs")) or []
        return LegacyDisposition.from_dict(d)

    def list_legacy_dispositions(self) -> List[LegacyDisposition]:
        return [self._row_to_legacy_disposition(r) for r in
                self._fetchall("SELECT * FROM legacy_dispositions ORDER BY updated_at DESC")]

    def get_legacy_disposition(self, server_id: str) -> Optional[LegacyDisposition]:
        r = self._fetchone("SELECT * FROM legacy_dispositions WHERE server_id=?",
                           (server_id,))
        return self._row_to_legacy_disposition(r) if r else None

    def delete_legacy_disposition(self, server_id: str) -> None:
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM legacy_dispositions WHERE server_id=?"),
                         (server_id,))

    # -- Doc artifacts (F9 — cutover playbook / as-built / persisted runbook) -
    _DOC_COLS = ["doc_type", "scope_id", "doc_md", "scan_id", "scanned_at",
                 "summary", "updated_at"]

    def upsert_doc_artifact(self, d: DocArtifact) -> None:
        # composite primary key (doc_type, scope_id) — build the upsert by hand
        # so the ON DUPLICATE KEY UPDATE clause excludes the PK columns.
        sql = ("INSERT INTO doc_artifacts (doc_type, scope_id, doc_md, scan_id, "
               "scanned_at, summary, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
               "ON DUPLICATE KEY UPDATE doc_md=VALUES(doc_md), "
               "scan_id=VALUES(scan_id), scanned_at=VALUES(scanned_at), "
               "summary=VALUES(summary), updated_at=VALUES(updated_at)")
        with self.tx() as cur:
            cur.execute(self._x(sql), (
                d.doc_type, d.scope_id, d.doc_md, d.scan_id, d.scanned_at,
                d.summary, d.updated_at))

    def _row_to_doc_artifact(self, d: Dict[str, Any]) -> DocArtifact:
        return DocArtifact.from_dict(dict(d))

    def list_doc_artifacts(self,
                           doc_type: Optional[str] = None) -> List[DocArtifact]:
        if doc_type:
            rows = self._fetchall(
                "SELECT * FROM doc_artifacts WHERE doc_type=? ORDER BY updated_at DESC",
                (doc_type,))
        else:
            rows = self._fetchall("SELECT * FROM doc_artifacts ORDER BY updated_at DESC")
        return [self._row_to_doc_artifact(r) for r in rows]

    def get_doc_artifact(self, doc_type: str, scope_id: str) -> Optional[DocArtifact]:
        r = self._fetchone(
            "SELECT * FROM doc_artifacts WHERE doc_type=? AND scope_id=?",
            (doc_type, scope_id))
        return self._row_to_doc_artifact(r) if r else None

    def delete_doc_artifact(self, doc_type: str, scope_id: str) -> None:
        with self.tx() as cur:
            cur.execute(self._x(
                "DELETE FROM doc_artifacts WHERE doc_type=? AND scope_id=?"),
                (doc_type, scope_id))

    # -- IaC artifacts (F5 — landing-zone/workload Terraform + guardrails) ----
    _IAC_COLS = ["scope_id", "scope", "modules", "guardrails", "guardrail_pass",
                 "plan_summary", "target", "scan_id", "scanned_at", "summary",
                 "updated_at"]

    def upsert_iac_artifact(self, a: IaCArtifact) -> None:
        sql = self._upsert_sql("iac_artifacts", self._IAC_COLS, "scope_id")
        with self.tx() as cur:
            cur.execute(self._x(sql), (
                a.scope_id, a.scope, dumps(a.modules),
                dumps([g.to_dict() for g in a.guardrails]),
                bool(a.guardrail_pass), a.plan_summary, dumps(a.target),
                a.scan_id, a.scanned_at, a.summary, a.updated_at))

    def _row_to_iac_artifact(self, d: Dict[str, Any]) -> IaCArtifact:
        d = dict(d)
        d["modules"] = loads(d.get("modules")) or []
        d["target"] = loads(d.get("target")) or {}
        gr_raw = loads(d.get("guardrails")) or []
        from ..core.models import GuardrailCheck
        d["guardrails"] = [GuardrailCheck.from_dict(g) for g in gr_raw]
        d["guardrail_pass"] = bool(d.get("guardrail_pass"))
        return IaCArtifact.from_dict(d)

    def list_iac_artifacts(self, scope: Optional[str] = None) -> List[IaCArtifact]:
        if scope:
            rows = self._fetchall(
                "SELECT * FROM iac_artifacts WHERE scope=? ORDER BY updated_at DESC",
                (scope,))
        else:
            rows = self._fetchall("SELECT * FROM iac_artifacts ORDER BY updated_at DESC")
        return [self._row_to_iac_artifact(r) for r in rows]

    def get_iac_artifact(self, scope_id: str) -> Optional[IaCArtifact]:
        r = self._fetchone("SELECT * FROM iac_artifacts WHERE scope_id=?",
                           (scope_id,))
        return self._row_to_iac_artifact(r) if r else None

    def delete_iac_artifact(self, scope_id: str) -> None:
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM iac_artifacts WHERE scope_id=?"),
                         (scope_id,))

    # -- Post-mig optimization recs (F10 — right_size/reserved/anomaly/perf) -
    _PM_COLS = ["server_id", "kind", "from_spec", "to_spec", "reason",
                "monthly_saving_usd", "confidence", "severity", "detail",
                "scan_id", "scanned_at", "summary", "updated_at"]

    def upsert_postmig_rec(self, r: PostMigRecommendation) -> None:
        # composite PK (server_id, kind) — build the upsert by hand so the ON
        # DUPLICATE KEY UPDATE clause excludes the PK columns.
        sql = ("INSERT INTO postmig_recs (server_id, kind, from_spec, to_spec, "
               "reason, monthly_saving_usd, confidence, severity, detail, "
               "scan_id, scanned_at, summary, updated_at) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
               "ON DUPLICATE KEY UPDATE from_spec=VALUES(from_spec), "
               "to_spec=VALUES(to_spec), reason=VALUES(reason), "
               "monthly_saving_usd=VALUES(monthly_saving_usd), "
               "confidence=VALUES(confidence), severity=VALUES(severity), "
               "detail=VALUES(detail), scan_id=VALUES(scan_id), "
               "scanned_at=VALUES(scanned_at), summary=VALUES(summary), "
               "updated_at=VALUES(updated_at)")
        with self.tx() as cur:
            cur.execute(self._x(sql), (
                r.server_id, r.kind, r.from_spec, r.to_spec, r.reason,
                r.monthly_saving_usd, r.confidence, r.severity, r.detail,
                r.scan_id, r.scanned_at, r.summary, r.updated_at))

    def _row_to_postmig_rec(self, d: Dict[str, Any]) -> PostMigRecommendation:
        return PostMigRecommendation.from_dict(dict(d))

    def list_postmig_recs(self,
                         server_id: Optional[str] = None) -> List[PostMigRecommendation]:
        if server_id:
            rows = self._fetchall(
                "SELECT * FROM postmig_recs WHERE server_id=? ORDER BY kind",
                (server_id,))
        else:
            rows = self._fetchall("SELECT * FROM postmig_recs ORDER BY updated_at DESC")
        return [self._row_to_postmig_rec(r) for r in rows]

    def get_postmig_rec(self, server_id: str,
                        kind: str) -> Optional[PostMigRecommendation]:
        r = self._fetchone(
            "SELECT * FROM postmig_recs WHERE server_id=? AND kind=?",
            (server_id, kind))
        return self._row_to_postmig_rec(r) if r else None

    def delete_postmig_recs(self, server_id: str) -> None:
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM postmig_recs WHERE server_id=?"),
                         (server_id,))

    # -- Automated testing (F11 — cases / runs / diffs) ----------------------
    def _upsert_test_case_sql(self, c: TestCase):
        """Return (sql, params) for a test_case upsert. Exposed so a caller can
        run the INSERT inside its OWN transaction (atomic clear + re-insert)."""
        sql = ("INSERT INTO test_cases (app_id, name, kind, endpoint, method, "
               "request, expected, setup, scan_id, updated_at) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
               "ON DUPLICATE KEY UPDATE kind=VALUES(kind), endpoint=VALUES(endpoint), "
               "method=VALUES(method), request=VALUES(request), "
               "expected=VALUES(expected), setup=VALUES(setup), "
               "scan_id=VALUES(scan_id), updated_at=VALUES(updated_at)")
        return self._x(sql), (
            c.app_id, c.name, c.kind, c.endpoint, c.method,
            dumps(c.request), dumps(c.expected), c.setup, c.scan_id,
            c.updated_at)

    def upsert_test_case(self, c: TestCase) -> None:
        # composite PK (app_id, name) — write the ON DUPLICATE KEY UPDATE
        # clause by hand so it excludes the PK columns.
        sql, params = self._upsert_test_case_sql(c)
        with self.tx() as cur:
            cur.execute(sql, params)

    def list_test_cases(self, app_id: Optional[str] = None) -> List[TestCase]:
        if app_id:
            rows = self._fetchall("SELECT * FROM test_cases WHERE app_id=? ORDER BY name",
                                  (app_id,))
        else:
            rows = self._fetchall("SELECT * FROM test_cases ORDER BY app_id, name")
        out = []
        for r in rows:
            d = dict(r)
            d["request"] = loads(d.get("request")) or {}
            d["expected"] = loads(d.get("expected")) or {}
            out.append(TestCase.from_dict(d))
        return out

    def get_test_case(self, app_id: str, name: str) -> Optional[TestCase]:
        r = self._fetchone("SELECT * FROM test_cases WHERE app_id=? AND name=?",
                           (app_id, name))
        if not r:
            return None
        d = dict(r)
        d["request"] = loads(d.get("request")) or {}
        d["expected"] = loads(d.get("expected")) or {}
        return TestCase.from_dict(d)

    def delete_test_cases(self, app_id: str) -> None:
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM test_cases WHERE app_id=?"), (app_id,))

    _TRUN_COLS = ["id", "app_id", "phase", "target", "results", "passed",
                  "failed", "errors", "run_at", "scan_id"]

    def upsert_test_run(self, r: TestRun) -> None:
        sql = self._upsert_sql("test_runs", self._TRUN_COLS, "id")
        with self.tx() as cur:
            cur.execute(self._x(sql), (
                r.id, r.app_id, r.phase, r.target,
                dumps([x.to_dict() for x in r.results]),
                r.passed, r.failed, r.errors, r.run_at, r.scan_id))

    def _row_to_test_run(self, d: Dict[str, Any]) -> TestRun:
        d = dict(d)
        res_raw = loads(d.get("results")) or []
        d["results"] = [TestResult.from_dict(x) for x in res_raw]
        return TestRun.from_dict(d)

    def list_test_runs(self, app_id: Optional[str] = None) -> List[TestRun]:
        if app_id:
            rows = self._fetchall("SELECT * FROM test_runs WHERE app_id=? ORDER BY run_at DESC",
                                  (app_id,))
        else:
            rows = self._fetchall("SELECT * FROM test_runs ORDER BY run_at DESC")
        return [self._row_to_test_run(r) for r in rows]

    def get_test_run(self, run_id: str) -> Optional[TestRun]:
        r = self._fetchone("SELECT * FROM test_runs WHERE id=?", (run_id,))
        return self._row_to_test_run(r) if r else None

    def latest_test_run(self, app_id: str, phase: str) -> Optional[TestRun]:
        r = self._fetchone(
            "SELECT * FROM test_runs WHERE app_id=? AND phase=? "
            "ORDER BY run_at DESC LIMIT 1", (app_id, phase))
        return self._row_to_test_run(r) if r else None

    def delete_test_runs(self, app_id: str) -> None:
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM test_runs WHERE app_id=?"), (app_id,))

    def upsert_test_diff(self, d: TestDiff) -> None:
        sql = ("INSERT INTO test_diffs (app_id, pre_run_id, post_run_id, diff, "
               "regressions, scan_id, scanned_at, summary, updated_at) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
               "ON DUPLICATE KEY UPDATE pre_run_id=VALUES(pre_run_id), "
               "post_run_id=VALUES(post_run_id), diff=VALUES(diff), "
               "regressions=VALUES(regressions), scan_id=VALUES(scan_id), "
               "scanned_at=VALUES(scanned_at), summary=VALUES(summary), "
               "updated_at=VALUES(updated_at)")
        with self.tx() as cur:
            cur.execute(self._x(sql), (
                d.app_id, d.pre_run_id, d.post_run_id,
                dumps([x.to_dict() for x in d.diff]), d.regressions,
                d.scan_id, d.scanned_at, d.summary, d.updated_at))

    def _row_to_test_diff(self, d: Dict[str, Any]) -> TestDiff:
        d = dict(d)
        items = loads(d.get("diff")) or []
        d["diff"] = [TestDiffItem.from_dict(x) for x in items]
        return TestDiff.from_dict(d)

    def list_test_diffs(self) -> List[TestDiff]:
        return [self._row_to_test_diff(r) for r in
                self._fetchall("SELECT * FROM test_diffs ORDER BY updated_at DESC")]

    def get_test_diff(self, app_id: str) -> Optional[TestDiff]:
        r = self._fetchone("SELECT * FROM test_diffs WHERE app_id=?", (app_id,))
        return self._row_to_test_diff(r) if r else None

    def delete_test_diff(self, app_id: str) -> None:
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM test_diffs WHERE app_id=?"), (app_id,))

    # -- LZ archetype status (F8 — Landing Zone placement gate) --
    def set_lz_status(self, archetype: str, status: str,
                      updated_by: str = "operator") -> None:
        """Upsert the lifecycle status of one LZ archetype (not_ready /
        applied / finalized). Drives the wave-launch placement gate."""
        sql = self._upsert_sql("lz_status",
                               ["archetype", "status", "updated_at", "updated_by"],
                               "archetype")
        with self.tx() as cur:
            cur.execute(self._x(sql), (archetype, status, _now(), updated_by))

    def get_lz_status(self, archetype: str) -> str:
        r = self._fetchone("SELECT status FROM lz_status WHERE archetype=?", (archetype,))
        return r["status"] if r else "not_ready"

    def list_lz_status(self) -> Dict[str, str]:
        return {r["archetype"]: r["status"]
                for r in self._fetchall("SELECT archetype, status FROM lz_status")}

    def delete_lz_status(self, archetype: str) -> None:
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM lz_status WHERE archetype=?"), (archetype,))

    # -- LZ designs (F8 Phase A — operator-authored, LLM-driven LZ design) --
    _LZ_DESIGN_COLS = ["design_id", "name", "summary", "archetypes", "requirements",
                       "conversation", "onprem_cidrs", "is_active", "scale",
                       "created_at", "updated_at", "updated_by"]

    def upsert_lz_design(self, d: LZDesign) -> None:
        """Insert or update an LZ design row (JSON columns for the structured
        fields). Does NOT touch is_active of other rows — activation is a
        separate atomic step (``set_active_lz_design``)."""
        sql = self._upsert_sql("lz_designs", self._LZ_DESIGN_COLS, "design_id")
        with self.tx() as cur:
            cur.execute(self._x(sql), (
                d.design_id, d.name, d.summary or "", dumps(d.archetypes),
                dumps(d.requirements), dumps(d.conversation), dumps(d.onprem_cidrs),
                bool(d.is_active), d.scale or "", d.created_at, d.updated_at,
                d.updated_by))

    def _row_to_lz_design(self, d: Dict[str, Any]) -> LZDesign:
        d = dict(d)
        for k in ("archetypes", "requirements", "conversation", "onprem_cidrs"):
            d[k] = loads(d.get(k)) or ({} if k not in ("conversation", "onprem_cidrs")
                                       else [])
        d["is_active"] = bool(d.get("is_active"))
        return LZDesign.from_dict(d)

    def get_lz_design(self, design_id: str) -> Optional[LZDesign]:
        r = self._fetchone("SELECT * FROM lz_designs WHERE design_id=?", (design_id,))
        return self._row_to_lz_design(r) if r else None

    def list_lz_designs(self) -> List[LZDesign]:
        return [self._row_to_lz_design(r) for r in self._fetchall(
            "SELECT * FROM lz_designs ORDER BY updated_at DESC")]

    def get_active_lz_design(self) -> Optional[LZDesign]:
        """The single active design (is_active=1), or None — callers fall back
        to the built-in LZ_BLUEPRINTS via ``idc.core.lz.default_lz_design``."""
        r = self._fetchone("SELECT * FROM lz_designs WHERE is_active=1 LIMIT 1")
        return self._row_to_lz_design(r) if r else None

    def set_active_lz_design(self, design_id: str, updated_by: str = "operator"
                              ) -> None:
        """Atomically make ``design_id`` the sole active design: clear every
        other row's is_active, set this one's, in one transaction. The single-
        active invariant is enforced here, not by a DB constraint (many rows
        may be is_active=0)."""
        with self.tx() as cur:
            cur.execute(self._x("UPDATE lz_designs SET is_active=0"))
            cur.execute(self._x(
                "UPDATE lz_designs SET is_active=1, updated_at=?, updated_by=? "
                "WHERE design_id=?"), (_now(), updated_by, design_id))

    def delete_lz_design(self, design_id: str) -> None:
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM lz_designs WHERE design_id=?"),
                         (design_id,))

    # -- network designs (F8 Phase B+ — VPC + subnets + server placement) --
    _NETWORK_DESIGN_COLS = ["design_id", "name", "summary", "accounts", "vpcs",
                            "placements", "policy", "overrides", "archetype_overrides",
                            "account_overrides", "onprem_cidrs", "validation",
                            "is_active", "created_at", "updated_at", "updated_by"]

    def upsert_network_design(self, nd: NetworkDesign) -> None:
        """Insert or update a network design row (JSON columns for the structured
        fields). Does NOT touch is_active of other rows — activation is a
        separate atomic step (``set_active_network_design``)."""
        sql = self._upsert_sql("network_designs", self._NETWORK_DESIGN_COLS,
                               "design_id")
        with self.tx() as cur:
            cur.execute(self._x(sql), (
                nd.design_id, nd.name, nd.summary or "", dumps(nd.accounts),
                dumps(nd.vpcs), dumps(nd.placements), dumps(nd.policy),
                dumps(nd.overrides), dumps(nd.archetype_overrides),
                dumps(nd.account_overrides), dumps(nd.onprem_cidrs),
                dumps(nd.validation), bool(nd.is_active), nd.created_at,
                nd.updated_at, nd.updated_by))

    def _row_to_network_design(self, d: Dict[str, Any]) -> NetworkDesign:
        d = dict(d)
        # JSON columns come back as strings; coerce to the right type per key.
        # vpcs + onprem_cidrs are LISTS — a naive ``loads(v) or {}`` would turn an
        # empty ``[]`` into ``{}`` (wrong type). Use per-key defaults.
        list_keys = ("vpcs", "onprem_cidrs")
        for k in ("accounts", "vpcs", "placements", "policy", "overrides",
                  "archetype_overrides", "account_overrides", "onprem_cidrs",
                  "validation"):
            v = d.get(k)
            d[k] = loads(v) or ([] if k in list_keys else {})
        d["is_active"] = bool(d.get("is_active"))
        return NetworkDesign.from_dict(d)

    def get_network_design(self, design_id: str) -> Optional[NetworkDesign]:
        r = self._fetchone("SELECT * FROM network_designs WHERE design_id=?",
                           (design_id,))
        return self._row_to_network_design(r) if r else None

    def list_network_designs(self) -> List[NetworkDesign]:
        return [self._row_to_network_design(r) for r in self._fetchall(
            "SELECT * FROM network_designs ORDER BY updated_at DESC")]

    def get_active_network_design(self) -> Optional[NetworkDesign]:
        r = self._fetchone("SELECT * FROM network_designs WHERE is_active=1 LIMIT 1")
        return self._row_to_network_design(r) if r else None

    def set_active_network_design(self, design_id: str,
                                   updated_by: str = "operator") -> None:
        """Atomically make ``design_id`` the sole active network design."""
        with self.tx() as cur:
            cur.execute(self._x("UPDATE network_designs SET is_active=0"))
            cur.execute(self._x(
                "UPDATE network_designs SET is_active=1, updated_at=?, "
                "updated_by=? WHERE design_id=?"),
                (_now(), updated_by, design_id))

    def delete_network_design(self, design_id: str) -> None:
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM network_designs WHERE design_id=?"),
                         (design_id,))

    # -- discovery snapshots (Gap1 — shadow-IT / CMDB-drift diff) --
    def save_discovery(self, payload: Dict[str, Any], created_at: str = "") -> None:
        """Upsert the latest discovery-diff snapshot (single row id='latest').
        POST /api/discovery/scan writes here so GET /api/discovery/diff can
        return the last scan without re-running the source."""
        if not created_at:
            from datetime import datetime
            created_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        sql = self._upsert_sql("discovery_snapshots",
                               ["id", "created_at", "payload"], "id")
        with self.tx() as cur:
            cur.execute(self._x(sql), ("latest", created_at, dumps(payload)))

    def get_discovery(self) -> Optional[Dict[str, Any]]:
        r = self._fetchone("SELECT * FROM discovery_snapshots WHERE id=?", ("latest",))
        if not r:
            return None
        d = dict(r)
        return {"id": d["id"], "created_at": d["created_at"],
                "payload": loads(d.get("payload"))}

    # -- app strategies (AI-assigned 7R, separate from executor profiles) --
    _STRATEGY_COLS = ["app_id", "strategy", "rationale", "target", "confidence",
                      "effort", "key_changes", "source", "assigned_at", "updated_at"]

    def upsert_app_strategy(self, s: AppStrategy) -> None:
        sql = self._upsert_sql("app_strategies", self._STRATEGY_COLS, "app_id")
        with self.tx() as cur:
            cur.execute(self._x(sql), (
                s.app_id, s.strategy, s.rationale, s.target, s.confidence,
                s.effort, dumps(s.key_changes), s.source, s.assigned_at, s.updated_at))

    def _row_to_strategy(self, d: Dict[str, Any]) -> AppStrategy:
        d = dict(d)
        d["key_changes"] = loads(d.get("key_changes")) or []
        return AppStrategy.from_dict(d)

    def list_app_strategies(self) -> List[AppStrategy]:
        return [self._row_to_strategy(r) for r in
                self._fetchall("SELECT * FROM app_strategies ORDER BY updated_at DESC")]

    def get_app_strategy(self, app_id: str) -> Optional[AppStrategy]:
        r = self._fetchone("SELECT * FROM app_strategies WHERE app_id=?", (app_id,))
        return self._row_to_strategy(r) if r else None

    def delete_app_strategy(self, app_id: str) -> None:
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM app_strategies WHERE app_id=?"), (app_id,))

    # -- change jobs (executor audit trail) --------------------------------
    _CJOB_COLS = ["id", "app_id", "kind", "repo_url", "branch", "status",
                  "patch_ref", "summary", "error", "created_at", "finished_at"]

    def upsert_change_job(self, j: ChangeJob) -> None:
        sql = self._upsert_sql("change_jobs", self._CJOB_COLS, "id")
        with self.tx() as cur:
            cur.execute(self._x(sql), (
                j.id, j.app_id, j.kind, j.repo_url, j.branch, j.status,
                j.patch_ref, j.summary, j.error, j.created_at, j.finished_at))

    # alias matching the create/update naming used for agent_tasks
    create_change_job = upsert_change_job
    update_change_job = upsert_change_job

    def get_change_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        r = self._fetchone("SELECT * FROM change_jobs WHERE id=?", (job_id,))
        return dict(r) if r else None

    def list_change_jobs(self, limit: int = 100,
                          status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Change jobs newest-first. When ``status`` is given, filter server-side
        and drop the LIMIT — readiness needs ALL done jobs to avoid false
        YELLOW on estates with >1000 change jobs (old done jobs fell out of the
        DESC window and the cutover gate lost its evidence)."""
        if status:
            return [dict(r) for r in self._fetchall(
                "SELECT * FROM change_jobs WHERE status=? ORDER BY created_at DESC",
                (status,))]
        return [dict(r) for r in self._fetchall(
            "SELECT * FROM change_jobs ORDER BY created_at DESC LIMIT ?", (limit,))]

    # -- executor tasks (pull-dispatch queue) ---------------------------------
    # The executor exposes nothing to the network; idc-migrate cannot push
    # triggers to it. Instead idc-migrate enqueues a task here and the executor
    # pulls + claims it via POST /api/executor/tasks/claim, runs it, and pushes
    # results back over the same /api/* feedback surface (unchanged). A task
    # targets one executor (executor_id set) or the pool (executor_id NULL,
    # claimable by any registered executor). A claim holds a lease; an expired
    # lease returns the task to pending so a dead executor doesn't strand work.
    _ETASK_COLS = ["id", "kind", "payload", "executor_id", "status", "claimed_by",
                   "claimed_at", "lease_until", "created_at", "finished_at",
                   "error", "result_ref", "summary"]

    def enqueue_task(self, task_id: str, kind: str, payload: dict,
                     executor_id: Optional[str], created_at: str) -> Dict[str, Any]:
        """Insert a pending task. ``executor_id`` None/'' -> pool (any executor).
        Returns the row dict (without re-reading)."""
        eid = (executor_id or "").strip() or None
        row = {
            "id": task_id, "kind": kind, "payload": dumps(payload),
            "executor_id": eid, "status": "pending", "claimed_by": None,
            "claimed_at": None, "lease_until": None, "created_at": created_at,
            "finished_at": None, "error": None, "result_ref": None,
            "summary": None,
        }
        sql = self._upsert_sql("executor_tasks", self._ETASK_COLS, "id")
        with self.tx() as cur:
            cur.execute(self._x(sql), (
                row["id"], row["kind"], row["payload"], row["executor_id"],
                row["status"], row["claimed_by"], row["claimed_at"],
                row["lease_until"], row["created_at"], row["finished_at"],
                row["error"], row["result_ref"], row["summary"]))
        row["payload"] = payload
        return row

    def _task_row(self, r) -> Optional[Dict[str, Any]]:
        if not r:
            return None
        d = dict(r)
        d["payload"] = loads(d.get("payload")) or {}
        return d

    def claim_next_task(self, executor_id: str, lease_seconds: int,
                        now: str) -> Optional[Dict[str, Any]]:
        """Atomically pop one eligible task for ``executor_id``: a task targeting
        this executor, or a pool task (executor_id NULL). Requeues any task whose
        lease expired first (so a dead executor's stranded work is reclaimable),
        then claims the oldest pending match. Returns the claimed row or None.

        Concurrency-safe via SELECT ... FOR UPDATE inside the write tx: two
        executors polling at once each get a distinct row (the first claim flips
        status to claimed before the second's SELECT sees it)."""
        with self.tx() as cur:
            # 1. requeue expired leases back to pending (same tx)
            cur.execute(self._x(
                "UPDATE executor_tasks SET status='pending', claimed_by=NULL, "
                "claimed_at=NULL, lease_until=NULL "
                "WHERE status='claimed' AND lease_until IS NOT NULL "
                "AND lease_until < ?"), (now,))
            # 2. pick the oldest pending task this executor may take
            cur.execute(self._x(
                "SELECT * FROM executor_tasks "
                "WHERE status='pending' "
                "AND (executor_id IS NULL OR executor_id=?) "
                "ORDER BY created_at ASC LIMIT 1 FOR UPDATE"), (executor_id,))
            r = cur.fetchone()
            if not r:
                return None
            d = dict(r)
            import datetime as _dt  # noqa: only used to build lease_until string
            # lease_until = now + lease_seconds (ISO-ish); caller passes `now`,
            # compute the expiry from the lease window.
            lease_until = _lease_expiry(now, lease_seconds)
            cur.execute(self._x(
                "UPDATE executor_tasks SET status='claimed', claimed_by=?, "
                "claimed_at=?, lease_until=? WHERE id=?"),
                (executor_id, now, lease_until, d["id"]))
            d["status"] = "claimed"
            d["claimed_by"] = executor_id
            d["claimed_at"] = now
            d["lease_until"] = lease_until
            d["payload"] = loads(d.get("payload")) or {}
            return d

    def renew_lease(self, task_id: str, executor_id: str, lease_seconds: int,
                    now: str) -> bool:
        """Extend a claim's lease (heartbeat). Only the owning executor may renew;
        a task that's already terminal or requeued (claimed_by != executor_id)
        is left untouched. Returns True if renewed.

        Ownership is verified by SELECT, not by UPDATE rowcount: pymysql's
        default counts *changed* rows, so a renewal that lands in the same
        second (lease string unchanged) would report 0 and look like a stale
        claim even though the owner is valid."""
        lease_until = _lease_expiry(now, lease_seconds)
        with self.tx() as cur:
            cur.execute(self._x(
                "SELECT id FROM executor_tasks "
                "WHERE id=? AND claimed_by=? AND status='claimed' FOR UPDATE"),
                (task_id, executor_id))
            if not cur.fetchone():
                return False
            cur.execute(self._x(
                "UPDATE executor_tasks SET lease_until=? WHERE id=?"),
                (lease_until, task_id))
            return True

    def complete_task(self, task_id: str, executor_id: str, status: str,
                       now: str, error: Optional[str] = None,
                       result_ref: Optional[str] = None,
                       summary: Optional[str] = None) -> bool:
        """Mark a claimed task terminal (done/error). Only the owning executor
        may complete it. Returns True on success, False if the task isn't owned
        by ``executor_id`` (already requeued/expired) — caller treats that as
        a no-op (the work was reclaimed)."""
        if status not in ("done", "error"):
            raise ValueError("status must be done or error")
        with self.tx() as cur:
            cur.execute(self._x(
                "UPDATE executor_tasks SET status=?, finished_at=?, error=?, "
                "result_ref=?, summary=? WHERE id=? AND claimed_by=?"),
                (status, now, error, result_ref, summary, task_id, executor_id))
            return cur.rowcount > 0

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self._task_row(self._fetchone(
            "SELECT * FROM executor_tasks WHERE id=?", (task_id,)))

    def list_tasks(self, limit: int = 200, status: Optional[str] = None,
                   executor_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Tasks newest-first (optionally filtered by status / target executor)."""
        sql = "SELECT * FROM executor_tasks"
        clauses, args = [], []
        if status:
            clauses.append("status=?"); args.append(status)
        if executor_id:
            clauses.append("executor_id=?"); args.append(executor_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [self._task_row(r) for r in self._fetchall(self._x(sql), tuple(args))]

    # -- executor heartbeats (pull-mode connectivity) -------------------------
    def touch_executor_seen(self, executor_id: str, now: str) -> None:
        """Record that ``executor_id`` just reached into idc-migrate — it polled
        ``/claim`` (even an idle poll with no task to hand over), renewed a
        ``/lease``, or posted ``/complete``. This is the pull-mode liveness
        signal: idc-migrate never reaches out, so "is the executor connected"
        is inferred from this inbound activity, not a probe. Upserts one row."""
        if not executor_id:
            return
        sql = self._upsert_sql("executor_heartbeats",
                               ["executor_id", "last_seen_at"], "executor_id")
        with self.tx() as cur:
            cur.execute(self._x(sql), (executor_id, now))

    def executor_activity(self, executor_id: str) -> Dict[str, Any]:
        """Inbound-activity summary for one executor (no outbound probe):
        the last time it touched idc-migrate, plus the counts of tasks it has
        claimed by terminal/in-flight status. ``last_seen`` is None when the
        executor has never polled (registered but silent)."""
        out: Dict[str, Any] = {"last_seen": None, "in_flight": 0,
                               "done": 0, "error": 0}
        if not executor_id:
            return out
        with self.tx() as cur:
            cur.execute(self._x(
                "SELECT last_seen_at FROM executor_heartbeats "
                "WHERE executor_id=?"), (executor_id,))
            row = cur.fetchone()
            if row:
                out["last_seen"] = row["last_seen_at"]
            cur.execute(self._x(
                "SELECT status, COUNT(*) AS n FROM executor_tasks "
                "WHERE claimed_by=? GROUP BY status"), (executor_id,))
            for r in cur.fetchall():
                st, n = r["status"], int(r["n"])
                if st == "claimed":
                    out["in_flight"] = n
                elif st == "done":
                    out["done"] = n
                elif st == "error":
                    out["error"] = n
        return out

    # -- questions (executor ↔ operator) -------------------------------------
    _Q_COLS = ["id", "app_id", "job_id", "kind", "prompt", "options", "context",
               "status", "answer", "answered_by", "created_at", "answered_at"]

    def upsert_question(self, q) -> None:
        from .models import Question  # local import avoids cycle at module load
        sql = self._upsert_sql("questions", self._Q_COLS, "id")
        with self.tx() as cur:
            cur.execute(self._x(sql), (
                q.id, q.app_id, q.job_id, q.kind, q.prompt,
                dumps(q.options), dumps(q.context),
                q.status, q.answer, q.answered_by, q.created_at, q.answered_at))

    def get_question(self, qid: str):
        from .models import Question
        r = self._fetchone("SELECT * FROM questions WHERE id=?", (qid,))
        if not r:
            return None
        d = dict(r)
        d["options"] = loads(d.get("options")) or []
        d["context"] = loads(d.get("context")) or {}
        return Question.from_dict(d)

    def list_questions(self, app_id: Optional[str] = None,
                       status: Optional[str] = None) -> list:
        from .models import Question
        clauses, args = [], []
        if app_id:
            clauses.append("app_id=?"); args.append(app_id)
        if status:
            clauses.append("status=?"); args.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._fetchall(
            f"SELECT * FROM questions{where} ORDER BY created_at DESC",
            tuple(args))
        out = []
        for r in rows:
            d = dict(r)
            d["options"] = loads(d.get("options")) or []
            d["context"] = loads(d.get("context")) or {}
            out.append(Question.from_dict(d))
        return out

    # -- cost estimates (F2 — TCO per server) ------------------------------
    _COST_COLS = ["server_id", "target_product", "spec", "region", "monthly_usd",
                  "yearly_usd", "basis", "pricing_source", "computed_at"]

    def upsert_cost(self, c: CostEstimate) -> None:
        sql = self._upsert_sql("cost_estimates", self._COST_COLS, "server_id")
        with self.tx() as cur:
            cur.execute(self._x(sql), (
                c.server_id, c.target_product, c.spec, c.region,
                c.monthly_usd, c.yearly_usd, c.basis, c.pricing_source,
                c.computed_at))

    def get_cost(self, server_id: str) -> Optional[CostEstimate]:
        r = self._fetchone("SELECT * FROM cost_estimates WHERE server_id=?", (server_id,))
        return CostEstimate.from_dict(dict(r)) if r else None

    def list_costs(self) -> List[CostEstimate]:
        return [CostEstimate.from_dict(dict(r)) for r in
                self._fetchall("SELECT * FROM cost_estimates ORDER BY server_id")]

    def delete_cost(self, server_id: str) -> None:
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM cost_estimates WHERE server_id=?"), (server_id,))

    # -- migration jobs (F6 — per-host migration execution state) ----------
    _MJOB_COLS = ["id", "server_id", "app_id", "wave_id", "kind", "executor_ref",
                  "status", "stage_history", "validation_gates", "created_at", "updated_at"]

    def upsert_migration_job(self, j: MigrationJob) -> None:
        sql = self._upsert_sql("migration_jobs", self._MJOB_COLS, "id")
        with self.tx() as cur:
            cur.execute(self._x(sql), (
                j.id, j.server_id, j.app_id, j.wave_id, j.kind, j.executor_ref,
                j.status, dumps(j.stage_history), dumps(j.validation_gates),
                j.created_at, j.updated_at))

    def _row_to_migration_job(self, d: Dict[str, Any]) -> MigrationJob:
        d = dict(d)
        d["stage_history"] = loads(d.get("stage_history")) or []
        d["validation_gates"] = loads(d.get("validation_gates")) or []
        return MigrationJob.from_dict(d)

    def get_migration_job(self, job_id: str) -> Optional[MigrationJob]:
        r = self._fetchone("SELECT * FROM migration_jobs WHERE id=?", (job_id,))
        return self._row_to_migration_job(r) if r else None

    def list_migration_jobs(self, wave_id: Optional[str] = None,
                            status: Optional[str] = None,
                            server_id: Optional[str] = None) -> List[MigrationJob]:
        clauses: List[str] = []
        args: List[Any] = []
        if wave_id:
            clauses.append("wave_id=?"); args.append(wave_id)
        if status:
            clauses.append("status=?"); args.append(status)
        if server_id:
            clauses.append("server_id=?"); args.append(server_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._fetchall(
            f"SELECT * FROM migration_jobs{where} ORDER BY updated_at DESC",
            tuple(args))
        return [self._row_to_migration_job(r) for r in rows]

    # -- business case snapshots (F2 — immutable run history) --------------
    def save_business_case(self, snapshot_id: str, payload: Dict[str, Any],
                           created_at: str = "") -> None:
        if not created_at:
            from datetime import datetime
            created_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        sql = self._upsert_sql("business_case_snapshots",
                               ["id", "created_at", "payload"], "id")
        with self.tx() as cur:
            cur.execute(self._x(sql), (snapshot_id, created_at, dumps(payload)))

    def get_business_case(self, snapshot_id: str) -> Optional[Dict[str, Any]]:
        r = self._fetchone("SELECT * FROM business_case_snapshots WHERE id=?", (snapshot_id,))
        if not r:
            return None
        d = dict(r)
        return {"id": d["id"], "created_at": d["created_at"],
                "payload": loads(d.get("payload"))}

    def list_business_cases(self, limit: int = 20) -> List[Dict[str, Any]]:
        return [{"id": r["id"], "created_at": r["created_at"]} for r in
                self._fetchall(
                    "SELECT id, created_at FROM business_case_snapshots "
                    "ORDER BY created_at DESC LIMIT ?", (limit,))]

    # -- stats -------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        s = {
            "servers": self._scalar("SELECT COUNT(*) FROM servers"),
            "raw_assets": self._scalar("SELECT COUNT(*) FROM raw_assets"),
            "matches": self._scalar("SELECT COUNT(*) FROM matches"),
            "waves": self._scalar("SELECT COUNT(*) FROM waves"),
            "workloads": self._scalar("SELECT COUNT(*) FROM workloads"),
            "code_profiles": self._scalar("SELECT COUNT(*) FROM code_profiles"),
            "app_strategies": self._scalar("SELECT COUNT(*) FROM app_strategies"),
            "change_jobs": self._scalar("SELECT COUNT(*) FROM change_jobs"),
            "cost_estimates": self._scalar("SELECT COUNT(*) FROM cost_estimates"),
            "migration_jobs": self._scalar("SELECT COUNT(*) FROM migration_jobs"),
            "db_profiles": self._scalar("SELECT COUNT(*) FROM db_profiles"),
        }
        by_role = {
            r["role"]: r["n"] for r in self._fetchall(
                "SELECT role, COUNT(*) AS n FROM servers GROUP BY role") if r["role"]
        }
        by_env = {
            r["env"]: r["n"] for r in self._fetchall(
                "SELECT env, COUNT(*) AS n FROM servers GROUP BY env") if r["env"]
        }
        s["by_role"] = by_role
        s["by_env"] = by_env
        return s


def open_store(url: str) -> Store:
    st = Store(url)
    # warm the pool + ensure schema on a real connection
    conn = st._pool_get()
    try:
        st._exec_schema(conn, SCHEMA)
    finally:
        st._pool_put(conn)
    return st