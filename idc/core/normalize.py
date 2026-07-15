"""Cross-source normalization and entity resolution.

Takes RawAssets from all four sources and produces unified ``Server`` records.
ServiceNow CMDB is the authoritative source for identity & classification
(role, env, criticality, apps); RVTools enriches VM sizing/disks; Zabbix and
Prometheus provide utilization. Per-field provenance is kept in
``Server.source_refs`` so every claim is auditable.

Also loads the app/workload dependency file (``fixtures/apps.csv`` or a custom
path) and resolves hostnames → server ids into ``Workload`` records.
"""
from __future__ import annotations

import csv
import hashlib
import os
import random
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..config import FIXTURES
from .models import (
    Disk,
    NetDep,
    RawAsset,
    Server,
    Utilization,
    Workload,
)


def _key(hostname: str, fqdn: str, ip: str = "") -> str:
    # Hostname is the primary identity within an org; fqdn/ip are fallbacks.
    # Keying on fqdn-first would split a host when some sources lack fqdn
    # (e.g. Prometheus/RVTools only carry hostname).
    return (hostname or fqdn or ip).strip().lower()


def _tok(v: str) -> str:
    return (v or "").strip().lower()


# Source precedence for the merge: ServiceNow (CMDB) is authoritative for
# identity & classification, RVTools enriches sizing, Zabbix/Prometheus add
# utilization. The merge uses first-wins per field, so the group MUST be
# iterated in precedence order — otherwise alphabetical ingest order
# (prometheus, rvtools, servicenow, zabbix) lets RVTools/Prometheus win the
# hostname/role over CMDB.
_SOURCE_PRECEDENCE = {"servicenow": 0, "rvtools": 1, "zabbix": 2, "prometheus": 3}


class _UnionFind:
    """Union-find over identity tokens (hostname / fqdn / ip).

    A single physical host is reported by several sources that may each
    carry a different identity field: ServiceNow has hostname+ip, RVTools has
    hostname, Prometheus has ``instance=ip:port``. Keying on a single field
    (the old ``_key`` picked hostname OR fqdn OR ip) split one host into
    several phantom servers and silently lost utilization. Unioning every
    token an asset carries — and letting shared tokens merge across assets —
    keeps one physical host as one Server.
    """

    def __init__(self):
        self.parent: Dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            self.parent[root] = self.parent[self.parent[root]]
            root = self.parent[root]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _to_int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _to_float(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# Per-item PaaS detection — the FIRST classification pass of inventory analysis.
# A self-hosted PaaS / middleware platform exposes itself through dedicated
# mount points (the on-prem estate's ``zpaas`` platform carries ``/zpaas`` and
# ``/zpaasssd`` volumes). Such a host is NOT a plain app VM to lift-and-shift:
# its workloads should replatform onto a managed PaaS (TKE / cloud-native).
# ``mounts`` is the list of mount-point paths carried from the raw disk list;
# ``tags``/``hostname`` are secondary signals. Keys on the specific platform
# token so a host with a generic ``/opt`` or ``/data`` is NOT mis-flagged.
_PAAS_MOUNT_MARKERS = ("zpaas",)


def parse_mounts(disk_list: str) -> List[str]:
    """Extract mount-point paths from an inventory ``diskList`` field.

    The TSV carries two shapes:
      * Linux:   ``/:13G,/usr:9.8G,/data03:1.5T,/zpaas:40G``  (mount:size)
      * Windows: ``Hard disk 1:13GB;Hard disk 2:17GB``        (no mount path)
    Only the Linux shape exposes mount points; we keep the left of the last
    ``:`` for tokens that start with ``/``. This is the signal ``detect_paas``
    keys on (a ``/zpaas`` volume). Pure + side-effect-free so it is safe to
    import from tests (unlike ``scripts.load_inventory_draft``, which sets
    ``IDC_APPS_PATH`` at import time).
    """
    out: List[str] = []
    for tok in (disk_list or "").split(","):
        tok = tok.strip()
        if not tok or not tok.startswith("/"):
            continue
        mount = tok.rsplit(":", 1)[0].strip()
        if mount and mount not in out:
            out.append(mount)
    return out


def parse_disks(disk_list: str, aggregate_gb: Optional[float] = 0) -> List[Dict[str, Any]]:
    """Parse an inventory ``diskList`` into per-partition disk dicts.

    The TSV carries two shapes (the same field ``parse_mounts`` reads for the
    PaaS signal, but here we keep the SIZES so the host drawer can show each
    partition, not just the aggregate):

      * Windows/VMware (``;``-separated, no mount path):
        ``Hard disk 1:13GB;Hard disk 2:17GB``
      * Linux (``-``-separated, mount path first):
        ``/:13G,/usr:9.8G,/boot:1014M,/data03:1.5T``

    Each token is ``name:size`` (``rsplit`` on the last ``:`` so Linux paths
    like ``/opt/app/zabbix-agent:2.0G`` parse correctly). A ``/``-prefixed name
    is a mount — it becomes both the disk ``name`` and ``fs`` (the mount point).
    A bare name (``Hard disk N``) stays as ``name`` with empty ``fs``. Sizes
    accept G/GB/T/TB/M/MB (T → ×1024, M → ÷1024, rounded to int GB like the
    rest of the model).

    Returns one dict per partition in ``{name, size_gb, kind, fs}`` shape (what
    ``normalize`` consumes from ``attrs["disks"]``). Falls back to a single
    aggregate disk (``disk0``) when the list is empty/unparseable AND an
    ``aggregate_gb`` is given, so a host with only a total still shows one row.
    """
    disks: List[Dict[str, Any]] = []
    for tok in re.split(r"[;,]", disk_list or ""):
        tok = tok.strip()
        if not tok or ":" not in tok:
            continue
        name, _, size_raw = tok.rpartition(":")
        name = name.strip()
        size_raw = size_raw.strip()
        if not name:
            continue
        size_gb = _disk_size_gb(size_raw)
        if not size_gb:          # None or 0 — a 0-GB partition is just noise
            continue
        is_mount = name.startswith("/")
        disks.append({"name": name, "size_gb": size_gb, "kind": "",
                      "fs": name if is_mount else ""})
    if not disks and aggregate_gb:
        ag = int(round(aggregate_gb)) if aggregate_gb else 0
        if ag:
            disks = [{"name": "disk0", "size_gb": ag, "kind": "", "fs": ""}]
    return disks


def _disk_size_gb(size_raw: str) -> Optional[int]:
    """``'13GB'`` → 13, ``'9.8G'`` → 10, ``'1.5T'`` → 1536, ``'1014M'`` → 1."""
    m = re.match(r"^\s*([0-9]*\.?[0-9]+)\s*([GMKTBgmtkb]*)\s*$", size_raw or "")
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).lower()
    if unit in ("t", "tb"):
        val *= 1024
    elif unit in ("m", "mb"):
        val /= 1024
    elif unit in ("k", "kb"):
        val /= (1024 * 1024)
    # G/GB/empty -> already GB
    return int(round(val))


# Generic Linux mount-point vocabulary observed on the real estate's plain app
# VMs (e.g. ``/:13G,/usr:9.8G,/data:100G``). None of these segments trip the
# estate's detectors — detect_paas keys on ``zpaas``, detect_oracle_db on
# Oracle-internal OFA names (oradata/redolog/fra…), detect_db_engine on
# postgres/mysql/…, detect_middleware on kafka/tomcat/… — so a bare ``/data`` or
# ``/opt`` never reclassifies a host (the real Linux-shape app hosts carry
# exactly these and stay role "app"). Used by upgrade_linux_disklist to relabel
# the VMware-layer VMDK labels ("Hard disk N:GB") a Linux guest surfaces when the
# virtualization platform cannot see the guest's mount points.
_LINUX_MOUNT_POOL = (
    "/usr", "/var", "/opt", "/data", "/home", "/boot", "/tmp",
    "/usr/local", "/data01", "/data02", "/data03", "/data04",
)


def _host_seed(hostname: str) -> int:
    return int.from_bytes(hashlib.sha256((hostname or "").encode()).digest()[:8], "big")


def upgrade_linux_disklist(disk_list: str, hostname: str = "") -> str:
    """Relabel a VMDK-only ``diskList`` into Linux ``mount:size`` tokens.

    RVtools/VMware reports a Linux guest's disks as generic ``Hard disk N:GB``
    VMDK labels — the virtualization layer does not see the guest's mount
    points, so such a host shows an empty Mount column in the host drawer. This
    replaces each generic label with a deterministic Linux mount path: the first
    disk becomes ``/`` (every Linux has a root), the rest take a hostname-seeded
    pick from :data:`_LINUX_MOUNT_POOL`. The exact size of every disk is
    preserved; only the bare ``Hard disk N`` label is replaced. A ``diskList``
    that already carries ``/``-prefixed tokens (real captured mounts) is returned
    unchanged, and so is one with no parseable sizes.

    Output is comma-separated so :func:`parse_mounts` (which splits on ``,``)
    picks the paths up, and :func:`parse_disks` then sets each disk's ``fs`` to
    its mount — populating the drawer's Mount column. Pure + deterministic
    (seeded by hostname) + side-effect-free, so it is safe to import from tests.
    """
    raw = disk_list or ""
    tokens = [t.strip() for t in re.split(r"[;,]", raw) if t.strip()]
    if not tokens:
        return raw
    if any(t.startswith("/") for t in tokens):   # already Linux-shape (real mounts)
        return raw
    if not any(":" in t for t in tokens):         # no sizes -> nothing to relabel
        return raw
    pool = list(_LINUX_MOUNT_POOL)
    random.Random(_host_seed(hostname)).shuffle(pool)
    mounts = ["/", *pool]
    out = []
    for i, tok in enumerate(tokens):
        name, _, size = tok.rpartition(":")
        name, size = name.strip(), size.strip()
        if not name or not size:
            continue
        if i < len(mounts):
            m = mounts[i]
        else:                                    # more disks than the pool covers
            m = f"/data{(i - len(mounts) + 5):02d}"
        out.append(f"{m}:{size}")
    return ",".join(out)


def detect_paas(mounts: Iterable[str], tags: Iterable[str] = (),
                hostname: str = "") -> bool:
    """True when a host runs a self-hosted PaaS platform (→ replatform, not rehost).

    Pure + deterministic. Examines mount-point paths first (the strongest
    signal — a dedicated platform volume), then tags/hostname as fallback.
    Matches on path *segment prefix* so a ``/zpaas`` volume and its ``/zpaasssd``
    companion both hit.
    """
    for m in mounts or []:
        low = (m or "").strip().lower()
        # match on any path segment so both "/zpaas" and "/data/zpaas/x" hit
        segs = low.replace("/", " ").split()
        if any(seg.startswith(_PAAS_MOUNT_MARKERS) for seg in segs):
            return True
    for t in tags or []:
        if (t or "").lower().startswith(_PAAS_MOUNT_MARKERS):
            return True
    # hostname fallback: a node named like a PaaS platform component
    h = (hostname or "").lower()
    if h and any(f"-{m}-" in f"-{h}-" or h.startswith(m + "-") or h.endswith("-" + m)
                for m in _PAAS_MOUNT_MARKERS):
        return True
    return False


# Middleware / app-server / message-queue hosts — self-managed runtimes that
# should containerize (rehost-container onto TKE) rather than lift-and-shift the
# runtime VM. Detected ONLY as a refinement of the generic "app" role, so a host
# already classified db / cache / web / k8s / hadoop / paas keeps that role.
#
# Two signals, in order: the disk/mount layout (the live estate's CMDB has no
# installed-software column and hostnames are generic ``HOST_NNNNN``, so the OFA
# mount paths like ``/kafka`` / ``/data/appdata/zookeeper`` / ``/tomcat`` are the
# strongest signal) and hostname/tag tokens as fallback. ``detect_middleware``
# returns the middleware *kind* (e.g. ``"kafka"``, ``"tomcat"``) so the matcher
# can name it and propose a managed-service alternative — or ``""`` otherwise.
# token-prefix (or exact) -> canonical kind
_MIDDLEWARE_KINDS = {
    "kafka": "kafka", "rabbitmq": "rabbitmq", "activemq": "activemq",
    "rocketmq": "rocketmq", "pulsar": "pulsar", "ibmmq": "ibmmq",
    "zookeeper": "zookeeper", "zk": "zookeeper",
    "tomcat": "tomcat", "weblogic": "weblogic",
    "jboss": "jboss", "wildfly": "jboss", "websphere": "websphere", "jetty": "jetty",
    "elasticsearch": "elasticsearch", "elastic": "elasticsearch",
    "flink": "flink", "spark": "spark",
    "zmq": "zmq", "zeromq": "zmq",
}


def _match_kind(text: str) -> str:
    """Return the canonical middleware kind for a lowercased token, or "".

    Matches on token prefix (``spark-scratch`` / ``kafka3`` → kind) — safe here
    because the tokens are middleware-specific names, not generic words.
    """
    for tok, kind in _MIDDLEWARE_KINDS.items():
        if text == tok or text.startswith(tok):
            return kind
    return ""


# Oracle DB detection from the disk/mount layout (the live estate's CMDB has no
# installed-software column and hostnames are generic ``HOST_NNNNN``, so the
# only Oracle signal is the OFA mount layout: ``/db.../oracle/<SID>/oradata1``,
# ``/opt/app/oracle``, ``redolog_A/B``, ``flashback``, ``fra`` …). These are
# Oracle-internal names — high precision, not generic — so we require a
# *DB-structure* marker (datafiles / redo / undo / flashback), NOT just the
# ``oracle`` segment alone (that is the Oracle base / client tools, which could
# be an oracle-client/backup box, not a DB). Detection here sets role="db" +
# tag "oracle-db" so ``match._is_oracle_db`` routes the host to TData.
_ORACLE_DB_MOUNT_PREFIXES = (
    "oradata", "oraindx", "oraidx", "oraundo", "oratmp", "oratemp",
    "orarbs", "oraahf", "redolog", "flashback",
)
_ORACLE_DB_MOUNT_EXACT = ("fra",)   # Fast Recovery Area — short, match exactly


def detect_oracle_db(mounts: Iterable[str]) -> bool:
    """True when the disk layout carries an Oracle *DB* signature (datafiles /
    redo / undo / flashback / FRA) — the on-prem OFA mount layout."""
    for m in mounts or []:
        for seg in (m or "").lower().split("/"):
            seg = seg.strip()
            if not seg:
                continue
            if seg.startswith(_ORACLE_DB_MOUNT_PREFIXES) or seg in _ORACLE_DB_MOUNT_EXACT:
                return True
    return False


def detect_middleware(mounts: Iterable[str] = (), tags: Iterable[str] = (),
                      hostname: str = "") -> str:
    """The middleware *kind* a generic app host runs (e.g. ``"kafka"``), or "".

    Checks the disk/mount layout first (strongest on the live estate), then
    tags, then hostname. Pure + deterministic.
    """
    for m in mounts or []:
        for seg in (m or "").lower().split("/"):
            seg = seg.strip()
            if not seg:
                continue
            kind = _match_kind(seg)
            if kind:
                return kind
    for t in tags or []:
        kind = _match_kind((t or "").strip().lower())
        if kind:
            return kind
    h = (hostname or "").lower()
    if h:
        hh = f"-{h}-"
        for tok, kind in _MIDDLEWARE_KINDS.items():
            if f"-{tok}-" in hh or h.startswith(tok + "-") or h.endswith("-" + tok):
                return kind
    return ""


# Non-Oracle DB engine detection from the disk/mount layout. The live estate's
# CMDB carries no installed-software column and hostnames are generic
# ``HOST_NNNNN``, so the engine signal is the data-directory mount path
# (``/pgdata``, ``/mysqldata``, ``/mongo``, ``/mariabackup`` …). Oracle is
# handled separately by :func:`detect_oracle_db` (the OFA layout). Returns the
# engine *kind* (``"postgres"`` / ``"mysql"`` / ``"mongo"`` / ``"sqlserver"`` /
# ``"db2"`` / ``"cassandra"`` / …) so the matcher can route each to its Tencent
# managed service, or ``""``. Curated strong mount-segment prefixes — a generic
# ``/data`` or ``/opt`` never trips. Refines ONLY role in ("","app") so a host
# already classified db/cache/web/k8s/hadoop/paas/infra keeps its role.
_DB_ENGINE_KINDS = {
    # PostgreSQL
    "postgres": "postgres", "postgresql": "postgres", "pgdata": "postgres",
    "pg_backup": "postgres", "pg_wal": "postgres", "pgsql": "postgres",
    # MySQL / MariaDB
    "mysql": "mysql", "mysqldata": "mysql", "mariadb": "mysql",
    "mariadata": "mysql", "mariabackup": "mysql",
    # MongoDB
    "mongo": "mongo", "mongodata": "mongo", "mongobackup": "mongo",
    # SQL Server
    "mssql": "sqlserver", "sqlserver": "sqlserver", "mssqlserver": "sqlserver",
    # other engines (no first-class managed Tencent equivalent -> rehost)
    "db2": "db2", "sybase": "sybase",
    "cassandra": "cassandra", "clickhouse": "clickhouse", "tidb": "tidb",
    "influxdb": "influx", "influx": "influx", "tdengine": "tdengine",
    "opengauss": "opengauss", "dameng": "dameng", "kingbase": "kingbase",
}

# In-memory cache / store detection (-> role "cache" -> managed Redis / CVM).
_CACHE_KINDS = {"redis": "redis", "redisdata": "redis",
                "memcache": "memcache", "memcached": "memcache"}


def _seg_prefix_kind(text: str, kind_map: Dict[str, str]) -> str:
    """Canonical kind for a lowercased token by exact-or-prefix match, or ""."""
    s = (text or "").strip().lower()
    if not s:
        return ""
    for tok, kind in kind_map.items():
        if s == tok or s.startswith(tok):
            return kind
    return ""


def detect_db_engine(mounts: Iterable[str] = (), tags: Iterable[str] = (),
                     hostname: str = "") -> str:
    """The non-Oracle DB *engine* a generic app host runs, from the disk layout.

    Checks mounts first (strongest on the live estate), then tags, then
    hostname. Pure + deterministic. Returns ``""`` for Oracle (use
    :func:`detect_oracle_db`) and for plain app/data-store names. Mirror of
    :func:`detect_middleware` for the database family.
    """
    for m in mounts or []:
        for seg in (m or "").lower().split("/"):
            kind = _seg_prefix_kind(seg, _DB_ENGINE_KINDS)
            if kind:
                return kind
    for t in tags or []:
        kind = _seg_prefix_kind((t or "").strip().lower(), _DB_ENGINE_KINDS)
        if kind:
            return kind
    h = (hostname or "").lower()
    if h:
        hh = f"-{h}-"
        for tok, kind in _DB_ENGINE_KINDS.items():
            if f"-{tok}-" in hh or h.startswith(tok + "-") or h.endswith("-" + tok):
                return kind
    return ""


def detect_cache(mounts: Iterable[str] = (), tags: Iterable[str] = (),
                 hostname: str = "") -> str:
    """The cache/store *kind* (``"redis"`` / ``"memcache"``) a generic app host
    runs, from the disk layout, or ``""``."""
    for m in mounts or []:
        for seg in (m or "").lower().split("/"):
            kind = _seg_prefix_kind(seg, _CACHE_KINDS)
            if kind:
                return kind
    for t in tags or []:
        kind = _seg_prefix_kind((t or "").strip().lower(), _CACHE_KINDS)
        if kind:
            return kind
    h = (hostname or "").lower()
    if h:
        hh = f"-{h}-"
        for tok, kind in _CACHE_KINDS.items():
            if f"-{tok}-" in hh or h.startswith(tok + "-") or h.endswith("-" + tok):
                return kind
    return ""


# Hadoop / big-data platform detection from the disk layout (``/hadoop``,
# ``/hdfs``, ``/namenode``, ``/datanode`` …). The matcher routes role "hadoop"
# to Tencent EMR (managed). Runs AFTER middleware in the cascade so a node with
# both a middleware signal and a /hadoop mount (common — Kafka/ZK co-located
# with Hadoop) keeps its middleware classification; only hosts whose sole
# platform signal is Hadoop become role "hadoop".
_HADOOP_KINDS = {
    "hadoop": "hadoop", "hdfs": "hadoop", "namenode": "hadoop",
    "datanode": "hadoop", "yarn": "hadoop", "hbase": "hadoop",
    "hive": "hadoop", "mapred": "hadoop",
}

# Self-hosted object / file storage detection (→ replatform to managed
# storage, not lift-and-shift the storage node). ``minio``/``oss``/``s3`` →
# Tencent COS (object); ``gluster``/``ceph``/``nfs`` → Tencent CFS (file).
_OBJECT_STORE_KINDS = {
    "minio": "minio", "ossbucket": "oss", "s3bucket": "s3",
    "glusterfs": "gluster", "gluster": "gluster", "ceph": "ceph",
    "cephfs": "ceph", "nfsdata": "nfs", "nfsshare": "nfs", "nfsserver": "nfs",
}
_OBJECT_STORES = {"minio", "oss", "s3"}        # → COS (object storage)
_FILE_STORES = {"gluster", "ceph", "nfs"}      # → CFS (file storage)


def detect_hadoop(mounts: Iterable[str] = (), tags: Iterable[str] = (),
                  hostname: str = "") -> str:
    """``"hadoop"`` when the disk layout carries a Hadoop/HDFS signal, or ``""``."""
    for m in mounts or []:
        for seg in (m or "").lower().split("/"):
            kind = _seg_prefix_kind(seg, _HADOOP_KINDS)
            if kind:
                return kind
    for t in tags or []:
        kind = _seg_prefix_kind((t or "").strip().lower(), _HADOOP_KINDS)
        if kind:
            return kind
    h = (hostname or "").lower()
    if h:
        hh = f"-{h}-"
        for tok, kind in _HADOOP_KINDS.items():
            if f"-{tok}-" in hh or h.startswith(tok + "-") or h.endswith("-" + tok):
                return kind
    return ""


def detect_object_store(mounts: Iterable[str] = (), tags: Iterable[str] = (),
                        hostname: str = "") -> str:
    """The storage *kind* a generic app host runs (``"minio"`` / ``"gluster"``
    / ``"ceph"`` / ``"nfs"`` / ``"oss"`` / ``"s3"``), or ``""``. The matcher
    splits object (minio/oss/s3 → COS) from file (gluster/ceph/nfs → CFS)."""
    for m in mounts or []:
        for seg in (m or "").lower().split("/"):
            kind = _seg_prefix_kind(seg, _OBJECT_STORE_KINDS)
            if kind:
                return kind
    for t in tags or []:
        kind = _seg_prefix_kind((t or "").strip().lower(), _OBJECT_STORE_KINDS)
        if kind:
            return kind
    h = (hostname or "").lower()
    if h:
        hh = f"-{h}-"
        for tok, kind in _OBJECT_STORE_KINDS.items():
            if f"-{tok}-" in hh or h.startswith(tok + "-") or h.endswith("-" + tok):
                return kind
    return ""


def _infer_role(attrs: dict, tags: Iterable[str], os_full: str) -> str:
    tags = [t.lower() for t in tags]
    if "db" in tags or "mysql" in tags or "oracle" in tags or "postgresql" in tags:
        return "db"
    if "web" in tags or "nginx" in tags:
        return "web"
    if "cache" in tags or "redis" in tags:
        return "cache"
    if "hadoop" in tags:
        return "hadoop"
    if "k8s" in tags:
        return "k8s"
    if "monitoring" in tags or "grafana" in tags:
        return "monitoring"
    # hint from os/path
    s = (os_full or "").lower()
    if "mysql" in s:
        return "db"
    return "app"


def _role_from_servicenow(row: dict) -> str:
    # ServiceNow has no single 'role' col in our fixture; infer from name/tags.
    name = (row.get("name") or row.get("hostname") or "").lower()
    for tok, role in [("db-mysql", "db"), ("db-oracle", "db"), ("mysql", "db"),
                      ("oracle", "db"), ("redis", "cache"), ("cache", "cache"),
                      ("hdop", "hadoop"), ("hadoop", "hadoop"),
                      ("k8s-master", "k8s"), ("k8s-worker", "k8s"), ("k8s", "k8s"),
                      ("web-portal", "web"), ("nginx", "web"),
                      ("grafana", "monitoring"), ("mon-", "monitoring")]:
        if tok in name:
            return role
    return ""


def normalize(assets: List[RawAsset]) -> List[Server]:
    """Merge raw assets into unified Server records.

    Entity resolution unions every identity token (hostname / fqdn / ip) an
    asset carries, so a host reported by ServiceNow (hostname+ip), RVTools
    (hostname) and Prometheus (ip) collapses into one Server instead of
    splitting into phantoms. Each resolved group is then merged in source
    precedence order (ServiceNow authoritative, then RVTools, Zabbix,
    Prometheus) with first-wins per field.
    """
    uf = _UnionFind()
    for a in assets:
        toks = [t for t in (_tok(a.hostname), _tok(a.fqdn), _tok(a.ip)) if t]
        for t in toks[1:]:
            uf.union(toks[0], t)

    by_root: Dict[str, List[RawAsset]] = defaultdict(list)
    _noid = 0
    for a in assets:
        toks = [t for t in (_tok(a.hostname), _tok(a.fqdn), _tok(a.ip)) if t]
        if not toks:
            # no identity at all — keep as its own singleton group
            by_root[f"__noid_{_noid}"].append(a)
            _noid += 1
            continue
        by_root[uf.find(toks[0])].append(a)

    servers: List[Server] = []
    for k, group in by_root.items():
        srv = Server()
        mounts: List[str] = []   # mount-point paths carried from the raw disk
        # list (RVTools attrs["mounts"]) — the PaaS-platform signal source.
        # authoritative: servicenow first, then rvtools for sizing, zbx/prom for util
        group = sorted(group, key=lambda a: _SOURCE_PRECEDENCE.get(a.source, 99))
        for a in group:
            srv.source_refs.append({"source": a.source, "source_id": a.source_id,
                                    "ip": a.ip, "attrs": a.attrs})
            if a.source == "servicenow":
                srv.hostname = srv.hostname or a.hostname
                srv.fqdn = srv.fqdn or a.fqdn
                if a.ip and a.ip not in srv.ips:
                    srv.ips.append(a.ip)
                # only set OS when CMDB actually carries one; the old
                # ``_clean_os(attrs.get("os", srv.os))`` set "unknown" on
                # empty, which then blocked RVTools' accurate vSphere OS.
                os_val = a.attrs.get("os")
                if os_val:
                    srv.os = _clean_os(os_val)
                srv.os_version = a.attrs.get("os_version") or srv.os_version
                srv.cpu_cores = _to_int(a.attrs.get("cpus")) or srv.cpu_cores
                srv.mem_gb = _to_int(a.attrs.get("ram_gb")) or srv.mem_gb
                srv.subnet = a.attrs.get("subnet") or srv.subnet
                srv.datacenter = a.attrs.get("location") or srv.datacenter
                srv.env = (a.attrs.get("environment") or srv.env).lower() or srv.env
                srv.business_criticality = (a.attrs.get("business_criticality")
                                            or srv.business_criticality)
                virt = str(a.attrs.get("virtual", "")).lower()
                if virt == "false":
                    srv.source_type = "baremetal"
                srv.role = srv.role or _role_from_servicenow(a.attrs)
            elif a.source == "rvtools":
                srv.hostname = srv.hostname or a.hostname
                if a.ip and a.ip not in srv.ips:
                    srv.ips.append(a.ip)
                os_full = a.attrs.get("os", "")
                if os_full and not srv.os:
                    srv.os = _clean_os(os_full)
                srv.cpu_cores = _to_int(a.attrs.get("cpus")) or srv.cpu_cores
                mem_mb = _to_int(a.attrs.get("memory_mb"))
                if mem_mb:
                    srv.mem_gb = max(srv.mem_gb, mem_mb // 1024 or 1)
                srv.datacenter = a.attrs.get("datacenter") or srv.datacenter
                srv.cluster = a.attrs.get("cluster") or srv.cluster
                # disks (dedup by name+size — RVTools re-export must not double
                # capacity by appending the same spindles again)
                seen_disks = {(d.name, d.size_gb) for d in srv.disks}
                for d in a.attrs.get("disks", []):
                    key = (d.get("name", ""), _to_int(d.get("size_gb")))
                    if key in seen_disks:
                        continue
                    seen_disks.add(key)
                    srv.disks.append(Disk(name=d.get("name", ""), size_gb=_to_int(d.get("size_gb")),
                                          kind=(d.get("kind") or "").lower(), fs=d.get("fs", "")))
                # RVTools only sees VMs, but don't overwrite a CMDB baremetal
                # classification on a hostname collision.
                srv.source_type = srv.source_type or "vm"
                # carry mount-point paths for PaaS detection (the diskList signal);
                # not persisted as disks (capacity already captured above) but
                # fed to detect_paas as the first classification pass below.
                # The real RVTools adapter puts the mount in each disk's ``fs``,
                # the loader puts a flat ``mounts`` list — collect both so PaaS
                # detection works for either import path.
                for d in a.attrs.get("disks", []) or []:
                    fs = (d.get("fs") or "").strip()
                    if fs and fs not in mounts:
                        mounts.append(fs)
                for m in a.attrs.get("mounts", []) or []:
                    if m and m not in mounts:
                        mounts.append(m)
            elif a.source == "zabbix":
                srv.hostname = srv.hostname or a.hostname
                srv.fqdn = srv.fqdn or a.fqdn
                if a.ip and a.ip not in srv.ips:
                    srv.ips.append(a.ip)
                tags = a.attrs.get("tags") or {}
                srv.env = srv.env or (tags.get("env") or "").lower()
                srv.role = srv.role or (tags.get("role") or "").lower()
                u = a.attrs.get("utilization") or {}
                if u:
                    srv.utilization = Utilization(
                        cpu_p95=_to_float(u.get("cpu_p95")),
                        mem_p95=_to_float(u.get("mem_p95")),
                        disk_used_pct=_to_float(u.get("disk_used_pct")),
                        net_rx_mbps=_to_float(u.get("net_rx_mbps")),
                        net_tx_mbps=_to_float(u.get("net_tx_mbps")),
                        source="zabbix",
                    )
            elif a.source == "prometheus":
                srv.hostname = srv.hostname or a.hostname
                u = a.attrs.get("utilization") or {}
                # distinguish "no data" (None) from a real zero reading (0.0):
                # the old ``not srv.utilization.cpu_p95`` treated an idle host's
                # 0.0 as missing and let the next source overwrite it.
                if u and srv.utilization.cpu_p95 is None:
                    srv.utilization = Utilization(
                        cpu_p95=_to_float(u.get("cpu_p95")),
                        mem_p95=_to_float(u.get("mem_p95")),
                        disk_used_pct=_to_float(u.get("disk_used_pct")),
                        net_rx_mbps=_to_float(u.get("net_rx_mbps")),
                        net_tx_mbps=_to_float(u.get("net_tx_mbps")),
                        source="prometheus",
                    )
        # fallbacks / inference
        # PaaS detection is the FIRST per-item classification pass: a self-hosted
        # PaaS platform host (zpaas mount) overrides the generic "app" role so the
        # matcher routes it to Replatform, not a CVM lift-and-shift. Runs before
        # the role fallback so a zbx/SN "app" tag never masks a real platform.
        if detect_paas(mounts, srv.tags, srv.hostname):
            srv.role = "paas"
        # Oracle DB detection from the OFA disk layout (oradata*/redolog/...).
        # Reclassifies a generic app host → role "db" + tag "oracle-db" so the
        # matcher routes it to TData (Replatform), not a CVM rehost. Runs before
        # middleware/EOL so a DB host never becomes rehost-container.
        elif srv.role in ("", "app") and detect_oracle_db(mounts):
            srv.role = "db"
            if "oracle-db" not in (srv.tags or []):
                srv.tags = list(srv.tags or []) + ["oracle-db"]
        # Non-Oracle DB engine detection (postgres/mysql/mongo/sqlserver/db2/
        # cassandra/...). A generic app host with a DB data-directory mount →
        # role "db" + "db:<engine>" tag so the matcher routes it to the right
        # Tencent managed service (TencentDB for PG/Mongo/MySQL/...) instead of a
        # plain CVM rehost. Same precedence tier as Oracle; runs before
        # middleware/cache so a DB host is never containerized or treated as a
        # cache. The bare engine token is also added for postgres/sqlserver/mysql
        # so the matcher's legacy ``_db_engine`` path still resolves them.
        elif srv.role in ("", "app"):
            eng = detect_db_engine(mounts, srv.tags, srv.hostname)
            if eng:
                srv.role = "db"
                tags = list(srv.tags or [])
                db_tag = f"db:{eng}"
                if db_tag not in tags:
                    tags.append(db_tag)
                if eng in ("postgres", "sqlserver", "mysql") and eng not in tags:
                    tags.append(eng)
                srv.tags = tags
            else:
                cache = detect_cache(mounts, srv.tags, srv.hostname)
                if cache:
                    srv.role = "cache"
                    if cache not in (srv.tags or []):
                        srv.tags = list(srv.tags or []) + [cache]
                else:
                    mw = detect_middleware(mounts, srv.tags, srv.hostname)
                    if mw:
                        srv.role = "middleware"
                        mw_tag = f"mw:{mw}"
                        if mw_tag not in (srv.tags or []):
                            srv.tags = list(srv.tags or []) + [mw_tag]
                    else:
                        # Hadoop / big-data node (sole signal = /hadoop etc.)
                        # → role "hadoop" → Tencent EMR.
                        if detect_hadoop(mounts, srv.tags, srv.hostname):
                            srv.role = "hadoop"
                            if "hadoop" not in (srv.tags or []):
                                srv.tags = list(srv.tags or []) + ["hadoop"]
                        else:
                            # Self-hosted object/file storage → tag (role stays
                            # "app"); the matcher intercepts the tag and routes
                            # to COS (object) / CFS (file) before the app branch.
                            os_kind = detect_object_store(mounts, srv.tags,
                                                          srv.hostname)
                            if os_kind:
                                tag = ("object-store"
                                       if os_kind in _OBJECT_STORES
                                       else "file-store")
                                tags = list(srv.tags or [])
                                for t in (tag, f"{tag}:{os_kind}"):
                                    if t not in tags:
                                        tags.append(t)
                                srv.tags = tags
        if not srv.role:
            srv.role = _infer_role({}, srv.tags, srv.os)
        if not srv.disks and srv.source_type == "vm":
            pass
        srv.role = srv.role or "app"
        # default env BEFORE criticality so the two stay consistent; an unknown
        # env is "unknown" (conservative — don't rush a shadow-IT host into the
        # prod cutover wave), not "prod".
        if not srv.env:
            srv.env = "unknown"
        if not srv.business_criticality:
            srv.business_criticality = "medium" if srv.env == "prod" else "low"
        srv.updated_at = srv.created_at
        servers.append(srv)
    return servers


def _clean_os(s: str) -> str:
    s = (s or "").strip().lower()
    if "centos" in s:
        return "centos"
    if "ubuntu" in s:
        return "ubuntu"
    if "oracle linux" in s or "oracle" in s:
        return "oracle linux"
    if "red hat" in s or "rhel" in s:
        return "rhel"
    if "windows" in s:
        return "windows"
    if "debian" in s:
        return "debian"
    if "suse" in s or "sles" in s:
        return "suse"
    return s or "unknown"


# ---------------------------------------------------------------------------
# workloads / app dependency graph
# ---------------------------------------------------------------------------
def load_workloads(path: Optional[str] = None) -> List[Workload]:
    if not path:
        # IDC_APPS_PATH lets a live box with imported data point at its own
        # apps file (app_id -> hostnames) instead of the bundled fixture's
        # apps.csv, so rebuild reproduces the imported app grouping.
        path = os.environ.get("IDC_APPS_PATH") or str(FIXTURES / "apps.csv")
    p = path
    if not os.path.exists(p):
        return []
    out: List[Workload] = []
    with open(p, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.append(Workload(
                app_id=row["app_id"], name=row.get("name", ""),
                tier=row.get("tier", ""), env=(row.get("env") or "").lower(),
                server_ids=[],  # resolved later by resolve_workloads()
                depends_on=[x for x in (row.get("depends_on") or "").split(";") if x],
                notes=row.get("notes", ""),
                # stash hostnames temporarily via attrs-free field
            ))
            out[-1].server_ids = [h.strip() for h in (row.get("server_hostnames") or "").split(";") if h.strip()]
    return out


def resolve_workloads(workloads: List[Workload], servers: List[Server]) -> List[Workload]:
    """Convert server_hostnames (stored in server_ids slot) → resolved server ids."""
    by_host = {s.hostname.lower(): s.id for s in servers}
    by_fqdn = {s.fqdn.lower(): s.id for s in servers if s.fqdn}
    for w in workloads:
        resolved = []
        for h in w.server_ids:
            hid = by_host.get(h.lower()) or by_fqdn.get(h.lower())
            if hid:
                resolved.append(hid)
        w.server_ids = resolved
    return workloads


def merge_network_deps(servers: List[Server], workloads: List[Workload],
                       netdeps: List[NetDep]) -> tuple:
    """Fold runtime network edges (F1) into Workload.depends_on + provenance.

    For each NetDep:
      * resolve dst_ip/hostname -> Server (by ip, then hostname/fqdn);
      * append a ``source="netdep"`` provenance entry to the src Server's
        ``source_refs`` (audit trail of observed edges, including unresolved
        dsts);
      * if both src+dst servers belong to apps, add dst_app -> src_app's
        ``depends_on`` (deduped, no self-loops). An edge already present in the
        static app graph is recorded as ``confirmed`` (not duplicated).

    Dsts that don't resolve to an app (external endpoint, unmanaged host, or
    infra server with no app) are collected in the ``unresolved`` report
    rather than dropped — the plan's "don't silently lose signal" rule.

    Returns ``(workloads, report)`` where report = ``{added, confirmed,
    unresolved}`` (lists of dicts). ``workloads`` is mutated in place.
    """
    ip_to_server: Dict[str, Server] = {}
    host_to_server: Dict[str, Server] = {}
    for s in servers:
        for ip in s.ips:
            if ip:
                ip_to_server[ip] = s
        for k in (s.hostname, s.fqdn):
            if k:
                host_to_server[k.lower()] = s
    srv_by_id = {s.id: s for s in servers}
    app_by_server = {s.id: list(s.app_ids) for s in servers}
    wl_by_app = {w.app_id: w for w in workloads}

    def resolve_dst(dst: str) -> Optional[Server]:
        if not dst:
            return None
        if dst in ip_to_server:
            return ip_to_server[dst]
        return host_to_server.get(dst.lower())

    report = {"added": [], "confirmed": [], "unresolved": []}
    for nd in netdeps:
        src_srv = srv_by_id.get(nd.src_server_id)
        if not src_srv:
            # src host unknown to the estate — nothing to attach the edge to
            report["unresolved"].append(
                {"src": nd.src_host, "dst": f"{nd.dst_ip}:{nd.dst_port}",
                 "reason": "src host not in estate"})
            continue
        dst_srv = resolve_dst(nd.dst_ip)
        # provenance on the src server (audit trail of every observed edge)
        src_srv.source_refs.append({
            "source": "netdep", "source_id": nd.source,
            "ip": nd.dst_ip,
            "attrs": {"dst_port": nd.dst_port, "observed_at": nd.observed_at,
                      "dst_resolved": dst_srv.id if dst_srv else None},
        })
        src_apps = app_by_server.get(src_srv.id, [])
        if not src_apps:
            report["unresolved"].append(
                {"src": src_srv.hostname, "dst": f"{nd.dst_ip}:{nd.dst_port}",
                 "reason": "src server has no app"})
            continue
        if not dst_srv:
            report["unresolved"].append(
                {"src": src_srv.hostname, "dst": f"{nd.dst_ip}:{nd.dst_port}",
                 "reason": "dst not in estate (external/unmanaged)"})
            continue
        dst_apps = app_by_server.get(dst_srv.id, [])
        if not dst_apps:
            report["unresolved"].append(
                {"src": src_srv.hostname, "dst": dst_srv.hostname,
                 "reason": "dst server has no app (infra)"})
            continue
        for sa in src_apps:
            w = wl_by_app.get(sa)
            if not w:
                continue
            for da in dst_apps:
                if sa == da:
                    continue
                if da in w.depends_on:
                    report["confirmed"].append({"src_app": sa, "dst_app": da})
                else:
                    w.depends_on.append(da)
                    report["added"].append({"src_app": sa, "dst_app": da})
    return workloads, report