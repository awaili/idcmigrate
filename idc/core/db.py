"""SQLite-backed store for idc-migrate.

Stdlib ``sqlite3`` only — no ORM dependency. Complex fields (disks, util,
source_refs, target, app_ids, ...) are stored as JSON text columns. JSON1
functions are available in modern sqlite (3.34+ confirmed on this box).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
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

SCHEMA = """
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


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn: Optional[sqlite3.Connection] = None
        # The shared connection is used across FastAPI threadpool threads
        # (check_same_thread=False). sqlite3 connection objects are not safe
        # for concurrent use, so every access is serialized through this lock.
        self._lock = threading.RLock()

    # -- connection --------------------------------------------------------
    def _open_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        return conn

    def connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self._conn = self._open_conn()
            return self._conn

    @contextmanager
    def tx(self):
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
    def _read(self) -> Iterator[sqlite3.Connection]:
        """Hold the lock for the duration of a read so it cannot interleave
        with a write transaction on the shared connection."""
        with self._lock:
            yield self.connect()

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    # -- raw assets --------------------------------------------------------
    def upsert_raw(self, assets: Iterable[Any]) -> int:
        n = 0
        with self.tx() as cur:
            for a in assets:
                cur.execute(
                    """INSERT INTO raw_assets(source,source_id,hostname,fqdn,ip,attrs,ingested_at)
                       VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(source,source_id) DO UPDATE SET
                         hostname=excluded.hostname, fqdn=excluded.fqdn, ip=excluded.ip,
                         attrs=excluded.attrs, ingested_at=excluded.ingested_at""",
                    (a.source, a.source_id, a.hostname, a.fqdn, a.ip,
                     dumps(a.attrs), a.ingested_at),
                )
                n += 1
        return n

    def list_raw(self, source: Optional[str] = None) -> List[sqlite3.Row]:
        with self._read() as conn:
            if source:
                return conn.execute("SELECT * FROM raw_assets WHERE source=?", (source,)).fetchall()
            return conn.execute("SELECT * FROM raw_assets").fetchall()

    # -- servers -----------------------------------------------------------
    def upsert_server(self, s: Server) -> None:
        with self.tx() as cur:
            cur.execute(
                """INSERT INTO servers(id,hostname,fqdn,ips,source_type,role,os,os_version,
                       cpu_cores,cpu_model,mem_gb,disks,subnet,vlan,datacenter,cluster,env,
                       app_ids,tags,business_criticality,migration_tier,utilization,source_refs,
                       status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     hostname=excluded.hostname, fqdn=excluded.fqdn, ips=excluded.ips,
                     source_type=excluded.source_type, role=excluded.role, os=excluded.os,
                     os_version=excluded.os_version, cpu_cores=excluded.cpu_cores,
                     cpu_model=excluded.cpu_model, mem_gb=excluded.mem_gb, disks=excluded.disks,
                     subnet=excluded.subnet, vlan=excluded.vlan, datacenter=excluded.datacenter,
                     cluster=excluded.cluster, env=excluded.env, app_ids=excluded.app_ids,
                     tags=excluded.tags, business_criticality=excluded.business_criticality,
                     migration_tier=excluded.migration_tier, utilization=excluded.utilization,
                     source_refs=excluded.source_refs, status=excluded.status,
                     updated_at=excluded.updated_at""",
                (s.id, s.hostname, s.fqdn, dumps(s.ips), s.source_type, s.role, s.os, s.os_version,
                 s.cpu_cores, s.cpu_model, s.mem_gb, dumps([d.to_dict() for d in s.disks]),
                 s.subnet, s.vlan, s.datacenter, s.cluster, s.env, dumps(s.app_ids), dumps(s.tags),
                 s.business_criticality, s.migration_tier, dumps(s.utilization.to_dict()),
                 dumps(s.source_refs), s.status, s.created_at, s.updated_at),
            )

    def _row_to_server(self, r: sqlite3.Row) -> Server:
        d = dict(r)
        d["ips"] = loads(d.get("ips")) or []
        d["disks"] = loads(d.get("disks")) or []
        d["app_ids"] = loads(d.get("app_ids")) or []
        d["tags"] = loads(d.get("tags")) or []
        d["utilization"] = loads(d.get("utilization")) or {}
        d["source_refs"] = loads(d.get("source_refs")) or []
        return Server.from_dict(d)

    def get_server(self, sid: str) -> Optional[Server]:
        with self._read() as conn:
            r = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
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
        with self._read() as conn:
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
            return [self._row_to_server(r) for r in conn.execute(sql, args).fetchall()]

    def count_servers(self) -> int:
        with self._read() as conn:
            return conn.execute("SELECT COUNT(*) FROM servers").fetchone()[0]

    def list_all_servers(self, limit: int = 200000) -> List[Server]:
        """Load the full estate (no pagination) — for planners/aggregations.
        Cap is a safety valve, not a page size."""
        with self._read() as conn:
            return [self._row_to_server(r) for r in conn.execute(
                "SELECT * FROM servers LIMIT ?", (limit,)).fetchall()]

    def query_server_ids(self, f: "ServerFilter") -> List[str]:
        """All server ids matching a filter (no pagination) — for scoping."""
        with self._read() as conn:
            where, args, join_matches = self._filters_sql(f)
            join = " LEFT JOIN matches ON matches.server_id=servers.id" if join_matches else ""
            return [r[0] for r in conn.execute(
                f"SELECT servers.id FROM servers{join} WHERE {where}", args).fetchall()]

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
            where.append("CAST(json_extract(servers.utilization,'$.mem_p95') AS REAL) >= ?")
            args.append(f.util_mem_min)
        if f.util_cpu_min is not None:
            where.append("CAST(json_extract(servers.utilization,'$.cpu_p95') AS REAL) >= ?")
            args.append(f.util_cpu_min)
        if f.util_disk_min is not None:
            where.append("CAST(json_extract(servers.utilization,'$.disk_used_pct') AS REAL) >= ?")
            args.append(f.util_disk_min)
        if f.wave_id:
            # membership stored as a JSON array text in waves.server_ids
            where.append("EXISTS (SELECT 1 FROM waves WHERE waves.id=? "
                         "AND waves.server_ids LIKE '%\"'||servers.id||'\"%')")
            args.append(f.wave_id)
        if join_matches:
            # handled in query_servers by adding the JOIN
            if f.target_product:
                where.append("json_extract(matches.target,'$.product')=?")
                args.append(f.target_product)
            if f.conf_min is not None:
                where.append("matches.confidence >= ?"); args.append(f.conf_min)
            if f.conf_max is not None:
                where.append("matches.confidence <= ?"); args.append(f.conf_max)
        return " AND ".join(where), args, join_matches

    def query_servers(self, f: "ServerFilter",
                      page: int = 1, page_size: int = 50,
                      order_by: str = "hostname", order_dir: str = "asc") -> dict:
        with self._read() as conn:
            where, args, join_matches = self._filters_sql(f)
            join = " LEFT JOIN matches ON matches.server_id=servers.id" if join_matches else ""
            # total
            total = conn.execute(
                f"SELECT COUNT(*) FROM servers{join} WHERE {where}", args).fetchone()[0]
            # order
            allowed = {"hostname", "cpu_cores", "mem_gb", "env", "role", "os",
                       "business_criticality", "source_type", "updated_at", "created_at"}
            ob = order_by if order_by in allowed else "hostname"
            od = "DESC" if order_dir.lower() == "desc" else "ASC"
            offset = max(0, (page - 1) * page_size)
            rows = conn.execute(
                f"SELECT servers.* FROM servers{join} WHERE {where} "
                f"ORDER BY servers.{ob} {od} LIMIT ? OFFSET ?",
                args + [page_size, offset]).fetchall()
            items = [self._row_to_server(r) for r in rows]
            facets = self._facets(conn, where, args, join_matches)
            return {"items": items, "total": total, "page": page,
                    "page_size": page_size, "facets": facets}

    def _facets(self, conn, where: str, args: list, join_matches: bool) -> dict:
        join = " LEFT JOIN matches ON matches.server_id=servers.id" if join_matches else ""
        out: Dict[str, Dict[str, int]] = {}
        for col in ("role", "env", "os", "source_type", "business_criticality", "cluster"):
            rows = conn.execute(
                f"SELECT servers.{col}, COUNT(*) FROM servers{join} "
                f"WHERE {where} GROUP BY servers.{col}", args).fetchall()
            out[col] = {r[0] or "(none)": r[1] for r in rows if r[0] is not None}
        # target product facet (always join for this one)
        rows = conn.execute(
            f"SELECT json_extract(matches.target,'$.product'), COUNT(*) "
            f"FROM servers LEFT JOIN matches ON matches.server_id=servers.id "
            f"WHERE {where} GROUP BY json_extract(matches.target,'$.product')", args).fetchall()
        out["target_product"] = {r[0] or "(unmatched)": r[1] for r in rows if r[0] is not None}
        # utilization buckets (mem_p95)
        rows = conn.execute(
            f"SELECT CASE WHEN CAST(json_extract(servers.utilization,'$.mem_p95') AS REAL) >= 80 THEN 'high' "
            f"WHEN CAST(json_extract(servers.utilization,'$.mem_p95') AS REAL) >= 50 THEN 'mid' "
            f"ELSE 'low' END b, COUNT(*) FROM servers{join} WHERE {where} GROUP BY b", args).fetchall()
        out["util_mem"] = {r[0]: r[1] for r in rows}
        return out

    def aggregations(self) -> dict:
        """Dashboard tiles + charts (cheap GROUP BYs over the whole estate)."""
        with self._read() as conn:
            n = conn.execute("SELECT COUNT(*) FROM servers").fetchone()[0]
            cores = conn.execute("SELECT COALESCE(SUM(cpu_cores),0) FROM servers").fetchone()[0]
            mem = conn.execute("SELECT COALESCE(SUM(mem_gb),0) FROM servers").fetchone()[0]
            by_role = {r[0]: r[1] for r in conn.execute(
                "SELECT role, COUNT(*) FROM servers GROUP BY role").fetchall() if r[0]}
            by_env = {r[0]: r[1] for r in conn.execute(
                "SELECT env, COUNT(*) FROM servers GROUP BY env").fetchall() if r[0]}
            by_os = {r[0]: r[1] for r in conn.execute(
                "SELECT os, COUNT(*) FROM servers GROUP BY os").fetchall() if r[0]}
            by_target = {r[0]: r[1] for r in conn.execute(
                "SELECT json_extract(target,'$.product'), COUNT(*) FROM matches "
                "GROUP BY json_extract(target,'$.product')").fetchall() if r[0]}
            by_region = {r[0]: r[1] for r in conn.execute(
                "SELECT json_extract(target,'$.region'), COUNT(*) FROM matches "
                "GROUP BY json_extract(target,'$.region')").fetchall() if r[0]}
            # match confidence distribution
            conf = {"high(>=0.85)": 0, "medium(0.7-0.85)": 0, "low(<0.7)": 0}
            for c in conn.execute("SELECT confidence FROM matches").fetchall():
                v = c[0] or 0
                if v >= 0.85: conf["high(>=0.85)"] += 1
                elif v >= 0.7: conf["medium(0.7-0.85)"] += 1
                else: conf["low(<0.7)"] += 1
            # high-utilization candidates (mem or disk >= 80)
            high_util = conn.execute(
                "SELECT COUNT(*) FROM servers WHERE "
                "CAST(json_extract(utilization,'$.mem_p95') AS REAL) >= 80 "
                "OR CAST(json_extract(utilization,'$.disk_used_pct') AS REAL) >= 80").fetchone()[0]
            # waves summary
            waves = [{"id": r[0], "name": r[1], "stage": r[2], "n": r[3]}
                     for r in conn.execute(
                "SELECT id, name, stage, COALESCE(json_array_length(server_ids),0) "
                "FROM waves ORDER BY stage, name").fetchall()]
            return {"servers": n, "cores": cores, "mem_gb": mem,
                    "by_role": by_role, "by_env": by_env, "by_os": by_os,
                    "by_target": by_target, "by_region": by_region,
                    "confidence": conf, "high_utilization": high_util, "waves": waves}

    def stream_servers_csv(self, f: "ServerFilter", order_by: str = "hostname"):
        """Yield CSV rows lazily for streaming export (no full load).

        Uses a dedicated connection (not the shared one) so a long stream
        doesn't hold the access lock and block other requests."""
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
        conn = self._open_conn()
        try:
            # fetch in batches to keep memory bounded
            BATCH = 1000
            offset = 0
            while True:
                rows = conn.execute(
                    f"SELECT servers.id,hostname,fqdn,role,source_type,os,os_version,cpu_cores,"
                    f"mem_gb,disks,env,business_criticality,cluster,ips,utilization "
                    f"FROM servers{join} WHERE {where} ORDER BY servers.{ob} LIMIT ? OFFSET ?",
                    args + [BATCH, offset]).fetchall()
                if not rows:
                    break
                # need matches joined for target/confidence — fetch per batch
                ids = [r[0] for r in rows]
                m = {}
                if ids:
                    placeholders = ",".join("?" * len(ids))
                    sql = ("SELECT server_id, json_extract(target,'$.product'),"
                           "json_extract(target,'$.spec'),json_extract(target,'$.region'),"
                           "confidence,method FROM matches WHERE server_id IN (" + placeholders + ")")
                    m = {r[0]: r for r in conn.execute(sql, ids).fetchall()}
                for r in rows:
                    sid, hn, fqdn, role, st, osn, osv, cpu, mem, disks_j, env, crit, cl, ips_j, util_j = r
                    disk = sum(d.get("size_gb", 0) for d in (loads(disks_j) or []))
                    ips = loads(ips_j) or []
                    util = loads(util_j) or {}
                    mm = m.get(sid)
                    buf = io.StringIO(); w = _csv.writer(buf)
                    w.writerow([sid, hn, fqdn, role, st, osn, osv, cpu, mem, disk, env, crit, cl,
                                ";".join(ips), util.get("cpu_p95"), util.get("mem_p95"),
                                util.get("disk_used_pct"),
                                mm[1] if mm else "", mm[2] if mm else "",
                                mm[3] if mm else "", mm[4] if mm else "", mm[5] if mm else ""])
                    yield buf.getvalue()
                offset += BATCH
        finally:
            conn.close()

    # -- workloads ---------------------------------------------------------
    def upsert_workload(self, w: Workload) -> None:
        with self.tx() as cur:
            cur.execute(
                """INSERT INTO workloads(app_id,name,tier,env,server_ids,depends_on,notes)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(app_id) DO UPDATE SET
                     name=excluded.name, tier=excluded.tier, env=excluded.env,
                     server_ids=excluded.server_ids, depends_on=excluded.depends_on,
                     notes=excluded.notes""",
                (w.app_id, w.name, w.tier, w.env, dumps(w.server_ids), dumps(w.depends_on), w.notes),
            )

    def list_workloads(self) -> List[Workload]:
        with self._read() as conn:
            out = []
            for r in conn.execute("SELECT * FROM workloads").fetchall():
                d = dict(r)
                d["server_ids"] = loads(d.get("server_ids")) or []
                d["depends_on"] = loads(d.get("depends_on")) or []
                out.append(Workload.from_dict(d))
            return out

    # -- matches -----------------------------------------------------------
    def upsert_match(self, m: Match) -> None:
        with self.tx() as cur:
            cur.execute(
                """INSERT INTO matches(server_id,target,confidence,method,rationale,
                       alternatives,created_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(server_id) DO UPDATE SET
                     target=excluded.target, confidence=excluded.confidence,
                     method=excluded.method, rationale=excluded.rationale,
                     alternatives=excluded.alternatives, created_at=excluded.created_at""",
                (m.server_id, dumps(m.target.to_dict()), m.confidence, m.method,
                 m.rationale, dumps(m.alternatives), m.created_at),
            )

    def get_match(self, server_id: str) -> Optional[Match]:
        with self._read() as conn:
            r = conn.execute("SELECT * FROM matches WHERE server_id=?", (server_id,)).fetchone()
            if not r:
                return None
            d = dict(r)
            tgt = Target.from_dict(loads(d.get("target")) or {})
            alts = loads(d.get("alternatives")) or []
            return Match(server_id=d["server_id"], target=tgt, confidence=d["confidence"],
                         method=d["method"], rationale=d["rationale"], alternatives=alts,
                         created_at=d["created_at"])

    def list_matches(self) -> List[Match]:
        with self._read() as conn:
            out = []
            for r in conn.execute("SELECT * FROM matches ORDER BY created_at").fetchall():
                d = dict(r)
                tgt = Target.from_dict(loads(d.get("target")) or {})
                alts = loads(d.get("alternatives")) or []
                out.append(Match(server_id=d["server_id"], target=tgt, confidence=d["confidence"],
                                 method=d["method"], rationale=d["rationale"], alternatives=alts,
                                 created_at=d["created_at"]))
            return out

    # -- waves -------------------------------------------------------------
    def upsert_wave(self, w: Wave) -> None:
        with self.tx() as cur:
            cur.execute(
                """INSERT INTO waves(id,name,stage,server_ids,depends_on,rationale,status)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, stage=excluded.stage, server_ids=excluded.server_ids,
                     depends_on=excluded.depends_on, rationale=excluded.rationale,
                     status=excluded.status""",
                (w.id, w.name, w.stage, dumps(w.server_ids), dumps(w.depends_on),
                 w.rationale, w.status),
            )

    def list_waves(self) -> List[Wave]:
        with self._read() as conn:
            out = []
            for r in conn.execute("SELECT * FROM waves ORDER BY stage, name").fetchall():
                d = dict(r)
                d["server_ids"] = loads(d.get("server_ids")) or []
                d["depends_on"] = loads(d.get("depends_on")) or []
                out.append(Wave.from_dict(d))
            return out

    # -- ingest runs -------------------------------------------------------
    def record_ingest(self, run: IngestRun) -> None:
        with self.tx() as cur:
            cur.execute(
                """INSERT INTO ingest_runs(id,source,mode,raw_count,started_at,finished_at,error)
                   VALUES(?,?,?,?,?,?,?)""",
                (run.id, run.source, run.mode, run.raw_count, run.started_at,
                 run.finished_at, run.error),
            )

    def list_ingest_runs(self) -> List[Dict[str, Any]]:
        with self._read() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM ingest_runs ORDER BY started_at DESC LIMIT 50").fetchall()]

    # -- agent tasks -------------------------------------------------------
    def create_task(self, t: AgentTask) -> None:
        with self.tx() as cur:
            cur.execute(
                """INSERT INTO agent_tasks(id,prompt,mode,status,created_at,finished_at,output,error)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (t.id, t.prompt, t.mode, t.status, t.created_at, t.finished_at, t.output, t.error),
            )

    def update_task(self, task_id: str, status: str, output: str = "",
                    error: str = "", finished_at: str = "") -> None:
        with self.tx() as cur:
            cur.execute(
                """UPDATE agent_tasks SET status=?, output=?, error=?, finished_at=? WHERE id=?""",
                (status, output, error, finished_at, task_id),
            )

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._read() as conn:
            r = conn.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
            return dict(r) if r else None

    def list_tasks(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._read() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM agent_tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]

    # -- stats -------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        with self._read() as conn:
            s = {
                "servers": conn.execute("SELECT COUNT(*) FROM servers").fetchone()[0],
                "raw_assets": conn.execute("SELECT COUNT(*) FROM raw_assets").fetchone()[0],
                "matches": conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0],
                "waves": conn.execute("SELECT COUNT(*) FROM waves").fetchone()[0],
                "workloads": conn.execute("SELECT COUNT(*) FROM workloads").fetchone()[0],
            }
            by_role = {
                r["role"]: r["n"] for r in conn.execute(
                    "SELECT role, COUNT(*) n FROM servers GROUP BY role").fetchall() if r["role"]
            }
            by_env = {
                r["env"]: r["n"] for r in conn.execute(
                    "SELECT env, COUNT(*) n FROM servers GROUP BY env").fetchall() if r["env"]
            }
            s["by_role"] = by_role
            s["by_env"] = by_env
            return s


def open_store(path: str | Path) -> Store:
    st = Store(path)
    st.connect()
    return st