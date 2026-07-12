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
from queue import Empty, Queue
from typing import Any, Dict, Iterable, List, Optional

from .models import (
    AgentTask,
    AppStrategy,
    ChangeJob,
    CodeProfile,
    CostEstimate,
    DBConversionProfile,
    IngestRun,
    Match,
    MigrationJob,
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
    status      VARCHAR(32)
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
    kind        VARCHAR(16),
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
"""


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
        return {"servers": n, "cores": cores, "mem_gb": mem,
                "by_role": by_role, "by_env": by_env, "by_os": by_os,
                "by_target": by_target, "by_region": by_region,
                "confidence": conf, "high_utilization": high_util, "waves": waves}

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
            out.append(Wave.from_dict(d))
        return out

    def replace_waves(self, waves: Iterable[Wave]) -> None:
        """Atomically replace all waves: DELETE + inserts in one transaction."""
        sql = self._upsert_sql("waves",
                               ["id", "name", "stage", "server_ids", "depends_on",
                                "rationale", "status"], "id")
        with self.tx() as cur:
            cur.execute(self._x("DELETE FROM waves"))
            for w in waves:
                cur.execute(self._x(sql),
                            (w.id, w.name, w.stage, dumps(w.server_ids),
                             dumps(w.depends_on), w.rationale, w.status))

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

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        r = self._fetchone("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
        return dict(r) if r else None

    def list_tasks(self, limit: int = 50) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._fetchall(
            "SELECT * FROM agent_tasks ORDER BY created_at DESC LIMIT ?", (limit,))]

    # -- code profiles (from external agent executor) ----------------------
    _PROFILE_COLS = ["app_id", "repo_url", "branch", "scan_id", "scanner",
                     "scanned_at", "language", "runtime", "framework",
                     "cloud_readiness", "migration_pattern", "refactor_effort",
                     "findings", "code_deps", "network_endpoints",
                     "required_changes", "blockers", "summary", "source", "updated_at"]

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
                p.summary, p.source or "repo", p.updated_at))

    def _row_to_profile(self, d: Dict[str, Any]) -> CodeProfile:
        d = dict(d)
        d["findings"] = [ScanFinding.from_dict(x) for x in (loads(d.get("findings")) or [])]
        d["code_deps"] = loads(d.get("code_deps")) or []
        d["network_endpoints"] = loads(d.get("network_endpoints")) or []
        d["required_changes"] = loads(d.get("required_changes")) or []
        d["blockers"] = loads(d.get("blockers")) or []
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

    # -- DB conversion profiles (F5 — heterogeneous DB migration assessment) -
    _DBP_COLS = ["db_server_id", "source_engine", "target_engine", "difficulty",
                 "est_man_days", "review_objects", "blockers",
                 "reverse_replication", "auto_convert_pct", "scan_id",
                 "scanned_at", "summary", "updated_at"]

    def upsert_db_profile(self, d: DBConversionProfile) -> None:
        sql = self._upsert_sql("db_profiles", self._DBP_COLS, "db_server_id")
        with self.tx() as cur:
            cur.execute(self._x(sql), (
                d.db_server_id, d.source_engine, d.target_engine, d.difficulty,
                d.est_man_days, dumps(d.review_objects), dumps(d.blockers),
                bool(d.reverse_replication), d.auto_convert_pct, d.scan_id,
                d.scanned_at, d.summary, d.updated_at))

    def _row_to_db_profile(self, d: Dict[str, Any]) -> DBConversionProfile:
        d = dict(d)
        d["review_objects"] = loads(d.get("review_objects")) or []
        d["blockers"] = loads(d.get("blockers")) or []
        # MariaDB stores BOOLEAN as TINYINT (0/1); coerce back to a real bool
        # so the model stays typed regardless of driver.
        d["reverse_replication"] = bool(d.get("reverse_replication"))
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

    def list_change_jobs(self, limit: int = 100) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._fetchall(
            "SELECT * FROM change_jobs ORDER BY created_at DESC LIMIT ?", (limit,))]

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