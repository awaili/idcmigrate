"""Storage for idc-migrate — dual SQLite / MariaDB backend.

Stdlib ``sqlite3`` for the portable default (no server, used by tests and
offline runs); ``pymysql`` for MariaDB on the DB box at scale. The dialect is
picked from ``IDC_DB_URL``:

  * ``sqlite:///<path>``  or a bare filesystem path  → SQLite
  * ``mysql://user:pass@host:port/db`` / ``mariadb://...`` → MariaDB

Both backends share one ``Store`` API and the same method bodies; only a few
dialect primitives differ (placeholder, upsert clause, JSON cast, string
concat, DDL). Complex fields (disks, util, source_refs, target, ...) are
stored as JSON text columns; both ``json_extract`` (SQLite) and
``JSON_EXTRACT`` (MariaDB 10.2+) work on them.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .models import (
    AgentTask,
    IngestRun,
    Match,
    Server,
    Target,
    Wave,
    Workload,
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


# ---------------------------------------------------------------------------
# schema — one per dialect. JSON columns are TEXT on sqlite, JSON on MariaDB.
# ---------------------------------------------------------------------------
SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS raw_assets (
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    hostname    TEXT,
    fqdn        TEXT,
    ip          TEXT,
    attrs       TEXT,
    ingested_at TEXT,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_hostname ON raw_assets(hostname);
CREATE INDEX IF NOT EXISTS idx_raw_ip ON raw_assets(ip);

CREATE TABLE IF NOT EXISTS servers (
    id            TEXT PRIMARY KEY,
    hostname      TEXT,
    fqdn          TEXT,
    ips           TEXT,
    source_type   TEXT,
    role          TEXT,
    os            TEXT,
    os_version    TEXT,
    cpu_cores     INTEGER,
    cpu_model     TEXT,
    mem_gb        INTEGER,
    disks         TEXT,
    subnet        TEXT,
    vlan          TEXT,
    datacenter    TEXT,
    cluster       TEXT,
    env           TEXT,
    app_ids       TEXT,
    tags          TEXT,
    business_criticality TEXT,
    migration_tier TEXT,
    utilization   TEXT,
    source_refs   TEXT,
    status        TEXT,
    created_at    TEXT,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_srv_role ON servers(role);
CREATE INDEX IF NOT EXISTS idx_srv_env ON servers(env);
CREATE INDEX IF NOT EXISTS idx_srv_status ON servers(status);
CREATE INDEX IF NOT EXISTS idx_srv_os ON servers(os);
CREATE INDEX IF NOT EXISTS idx_srv_crit ON servers(business_criticality);
CREATE INDEX IF NOT EXISTS idx_srv_stype ON servers(source_type);
CREATE INDEX IF NOT EXISTS idx_srv_cluster ON servers(cluster);

CREATE TABLE IF NOT EXISTS workloads (
    app_id     TEXT PRIMARY KEY,
    name       TEXT,
    tier       TEXT,
    env        TEXT,
    server_ids TEXT,
    depends_on TEXT,
    notes      TEXT
);

CREATE TABLE IF NOT EXISTS matches (
    server_id  TEXT PRIMARY KEY,
    target     TEXT,
    confidence REAL,
    method     TEXT,
    rationale  TEXT,
    alternatives TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_match_conf ON matches(confidence);

CREATE TABLE IF NOT EXISTS waves (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    stage       TEXT,
    server_ids  TEXT,
    depends_on  TEXT,
    rationale   TEXT,
    status      TEXT
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id          TEXT PRIMARY KEY,
    source      TEXT,
    mode        TEXT,
    raw_count   INTEGER,
    started_at  TEXT,
    finished_at TEXT,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS agent_tasks (
    id          TEXT PRIMARY KEY,
    prompt      TEXT,
    mode        TEXT,
    status      TEXT,
    created_at  TEXT,
    finished_at TEXT,
    output      TEXT,
    error       TEXT
);
"""

# MariaDB: ids are short text (srv-/wave-/…), JSON fields use the JSON alias,
# sizes are VARCHAR to act as PRIMARY KEY / indexed cols (TEXT can't be PK in
# MySQL without a prefix length). Indexes are created without IF NOT EXISTS
# (MariaDB 10.5 lacks it) — _exec_schema tolerates "Duplicate key name".
SCHEMA_MARIADB = """
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
"""


class Store:
    """Dialect-aware store. ``url`` is an IDC_DB_URL string or a filesystem
    path (interpreted as SQLite at that path)."""

    def __init__(self, url: str | Path):
        self.dialect, self._sqlite_path, self._mysql = self._parse(url)
        # placeholder char and per-dialect bits
        self.ph = "%s" if self.dialect == "mariadb" else "?"
        if self.dialect == "sqlite":
            self._conn: Optional[sqlite3.Connection] = None
            # sqlite3 connection objects are not safe for concurrent use; every
            # access is serialized through this lock (FastAPI threadpool).
            self._lock = threading.RLock()
        else:
            self._pool: Optional[Queue] = None
            self._pool_lock = threading.Lock()
            self._pool_max = 8

    # -- url parsing / connection setup -----------------------------------
    @staticmethod
    def _parse(url) -> tuple:
        s = str(url)
        if s.startswith(("mysql://", "mariadb://")):
            p = urllib.parse.urlparse(s)
            cfg = {
                "host": p.hostname or "10.0.0.3",
                "port": p.port or 3306,
                "user": urllib.parse.unquote(p.username or ""),
                "password": urllib.parse.unquote(p.password or ""),
                "db": (p.path or "/").lstrip("/"),
            }
            return "mariadb", None, cfg
        if s.startswith("sqlite:///"):
            return "sqlite", Path(s[len("sqlite:///"):]), None
        # bare path → sqlite at that path
        return "sqlite", Path(s), None

    def _open_sqlite(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._sqlite_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        self._exec_schema(conn, SCHEMA_SQLITE, sqlite=True)
        return conn

    def _new_mysql(self):
        import pymysql  # lazy: SQLite-only users don't need the driver
        return pymysql.connect(
            host=self._mysql["host"], port=self._mysql["port"],
            user=self._mysql["user"], password=self._mysql["password"],
            database=self._mysql["db"], charset="utf8mb4",
            autocommit=True, cursorclass=pymysql.cursors.DictCursor,
        )

    # sqlite shared connection (used only on the sqlite path)
    def connect(self) -> sqlite3.Connection:
        if self.dialect != "sqlite":
            raise RuntimeError("connect() is sqlite-only; use tx/_read on mariadb")
        with self._lock:
            if self._conn is None:
                self._conn = self._open_sqlite()
            return self._conn

    # -- mariadb pool ------------------------------------------------------
    def _pool_get(self):
        with self._pool_lock:
            if self._pool is None:
                self._pool = Queue(maxsize=self._pool_max)
                for _ in range(2):
                    self._pool.put(self._new_mysql())
        try:
            return self._pool.get_nowait()
        except Empty:
            return self._new_mysql()

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

    # -- dialect primitives ------------------------------------------------
    def _x(self, sql: str) -> str:
        """Translate ``?`` placeholders to the active driver's marker."""
        return sql.replace("?", "%s") if self.dialect == "mariadb" else sql

    def _cast_real(self, expr: str) -> str:
        """Numeric cast for json_extract results."""
        if self.dialect == "mariadb":
            return f"CAST({expr} AS DECIMAL(20,4))"
        return f"CAST({expr} AS REAL)"

    def _jq(self, extract_expr: str) -> str:
        """Unquote a json_extract *string* scalar for compare/group/facet.
        MariaDB's JSON_EXTRACT returns the JSON representation (strings come
        back double-quoted); SQLite returns the raw scalar unquoted."""
        if self.dialect == "mariadb":
            return f"JSON_UNQUOTE({extract_expr})"
        return extract_expr

    def _json_array_len(self, col: str) -> str:
        if self.dialect == "mariadb":
            return f"JSON_LENGTH({col})"
        return f"json_array_length({col})"

    def _upsert_sql(self, table: str, cols: List[str], pk: str) -> str:
        cols = list(cols)
        placeholders = ", ".join(["?"] * len(cols))
        cols_csv = ", ".join(cols)
        set_cols = [c for c in cols if c != pk]
        if self.dialect == "mariadb":
            set_clause = ", ".join(f"{c}=VALUES({c})" for c in set_cols)
            return (f"INSERT INTO {table}({cols_csv}) VALUES({placeholders}) "
                    f"ON DUPLICATE KEY UPDATE {set_clause}")
        set_clause = ", ".join(f"{c}=excluded.{c}" for c in set_cols)
        return (f"INSERT INTO {table}({cols_csv}) VALUES({placeholders}) "
                f"ON CONFLICT({pk}) DO UPDATE SET {set_clause}")

    def _exec_schema(self, conn, schema: str, sqlite: bool) -> None:
        cur = conn.cursor()
        try:
            for stmt in (s.strip() for s in schema.split(";")):
                if not stmt:
                    continue
                try:
                    cur.execute(stmt)
                except Exception as e:
                    # tolerate duplicate index names on re-run (MariaDB 10.5 has
                    # no CREATE INDEX IF NOT EXISTS)
                    if "Duplicate key name" in str(e) or "already exists" in str(e):
                        continue
                    raise
        finally:
            try:
                cur.close()
            except Exception:
                pass
        if sqlite:
            conn.commit()

    # -- transaction / read helpers (dialect-unified) ----------------------
    @contextmanager
    def tx(self):
        """Write transaction. Holds the sqlite lock / a pooled mariadb conn."""
        if self.dialect == "mariadb":
            conn = self._pool_get()
            cur = conn.cursor()
            try:
                conn.begin()
                yield cur
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
                self._pool_put(conn)
        else:
            with self._lock:
                conn = self.connect()
                cur = conn.cursor()
                try:
                    yield cur
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

    @contextmanager
    def _read_cursor(self):
        """A cursor for one or more reads. Don't use for writes."""
        if self.dialect == "mariadb":
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
        else:
            with self._lock:
                conn = self.connect()
                cur = conn.cursor()
                try:
                    yield cur
                finally:
                    cur.close()

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
        if self.dialect == "mariadb":
            with self._pool_lock:
                pool, self._pool = self._pool, None
            if pool is not None:
                while True:
                    try:
                        pool.get_nowait().close()
                    except Empty:
                        break
        else:
            with self._lock:
                if self._conn:
                    self._conn.close()
                    self._conn = None

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
                    "utilization", "source_refs", "status", "created_at",
                    "updated_at"]

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
                         s.status, s.created_at, s.updated_at))

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
            # membership stored as a JSON array text in waves.server_ids.
            # Pass the '%' wildcards as params (a literal % in the SQL breaks
            # pymysql's mogrify). MariaDB has CONCAT; sqlite uses ||.
            if self.dialect == "mariadb":
                where.append("EXISTS (SELECT 1 FROM waves WHERE waves.id=? "
                             "AND waves.server_ids LIKE CONCAT(?, servers.id, ?))")
            else:
                where.append("EXISTS (SELECT 1 FROM waves WHERE waves.id=? "
                             "AND waves.server_ids LIKE ? || servers.id || ?)")
            args.append(f.wave_id)
            args.append('%"')
            args.append('"%')
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
                      order_by: str = "hostname", order_dir: str = "asc") -> dict:
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
        facets = self._facets(where, args, join_matches)
        return {"items": items, "total": total, "page": page,
                "page_size": page_size, "facets": facets}

    def _facets(self, where: str, args: list, join_matches: bool) -> dict:
        join = " LEFT JOIN matches ON matches.server_id=servers.id" if join_matches else ""
        out: Dict[str, Dict[str, int]] = {}
        for col in ("role", "env", "os", "source_type", "business_criticality", "cluster"):
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

        Uses a dedicated cursor (not the shared sqlite connection / a pooled
        mariadb conn held long-term) so a long stream doesn't block writes."""
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

    # -- stats -------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        s = {
            "servers": self._scalar("SELECT COUNT(*) FROM servers"),
            "raw_assets": self._scalar("SELECT COUNT(*) FROM raw_assets"),
            "matches": self._scalar("SELECT COUNT(*) FROM matches"),
            "waves": self._scalar("SELECT COUNT(*) FROM waves"),
            "workloads": self._scalar("SELECT COUNT(*) FROM workloads"),
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


def open_store(url: str | Path) -> Store:
    st = Store(url)
    # eager connect: runs schema on first connection
    if st.dialect == "sqlite":
        st.connect()
    else:
        # warm the pool + ensure schema on a real connection
        conn = st._pool_get()
        try:
            st._exec_schema(conn, SCHEMA_MARIADB, sqlite=False)
        finally:
            st._pool_put(conn)
    return st