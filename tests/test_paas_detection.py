"""Tests for per-item PaaS detection (inventory analysis step 1) and the
Replatform routing it drives in the matcher.

Covers:
  * ``normalize.detect_paas`` — the deterministic first-pass classifier that
    flags a self-hosted PaaS platform host (zpaas mount) for Replatform.
  * the ``role == "paas"`` matcher branch → TKE managed (replatform, not rehost).
  * the Oracle DB branch → Replatform to TData (TencentDB for Oracle).
  * the loader's ``_parse_mounts`` carries the diskList signal into raw assets.
"""
from idc.core.match import match_server
from idc.core.models import RawAsset, Server
from idc.core.normalize import (
    detect_cache, detect_db_engine, detect_hadoop, detect_object_store,
    detect_paas, detect_middleware, detect_oracle_db, normalize, parse_mounts,
)
from idc.core.eol import os_long_term_eol, os_eol_bucket, EXPIRED, ACTIVE, UNKNOWN


# -- detect_paas -------------------------------------------------------------
def test_detect_paas_via_mount():
    assert detect_paas(["/zpaas"]) is True
    assert detect_paas(["/", "/usr", "/zpaas", "/data"]) is True
    assert detect_paas(["/zpaasssd"]) is True            # variant volume
    assert detect_paas(["/", "/opt", "/data03"]) is False  # no platform signal
    assert detect_paas([]) is False


def test_detect_paas_nested_path_segment():
    # a zpaas volume nested under /data still matches on the path segment
    assert detect_paas(["/data/zpaas/x"]) is True


def test_detect_paas_via_tag_and_hostname_fallback():
    assert detect_paas([], tags=["zpaas"]) is True
    assert detect_paas([], hostname="zpaas-node-01") is True
    assert detect_paas([], hostname="node-zpaas") is True
    # a host merely *mentioning* zpaas mid-name without the segment boundary
    # should NOT trip (avoid false positives like "zpaastext-01")
    assert detect_paas([], hostname="appzpaasx-01") is False


# -- normalize end-to-end ----------------------------------------------------
def _rvt(host, mounts=None):
    disks = [{"name": "disk0", "size_gb": 100}]
    if mounts is not None:
        # emulate the loader: a flat mounts list on the RVTools asset
        pass
    return RawAsset(source="rvtools", source_id=host, hostname=host, fqdn="", ip="",
                    attrs={"memory_mb": 4096, "disks": disks, "os": "centos",
                           "mounts": mounts or []})


def _sn(host):
    return RawAsset(source="servicenow", source_id=host, hostname=host, fqdn="", ip="",
                    attrs={"name": host, "os": "centos", "os_version": "7",
                           "cpus": 4, "ram_gb": 4, "environment": "prod",
                           "business_criticality": "", "virtual": "true"})


def _zbx(host, role="app"):
    return RawAsset(source="zabbix", source_id=host, hostname=host, fqdn=host, ip="",
                    attrs={"utilization": {"cpu_p95": 10, "mem_p95": 20},
                           "tags": {"env": "prod", "role": role}})


def test_normalize_flags_paas_from_mounts_over_app_role():
    host = "host-zpaas-01"
    assets = [_sn(host), _zbx(host, role="app"), _rvt(host, mounts=["/", "/zpaas", "/data"])]
    servers = normalize(assets)
    s = servers[0]
    assert s.role == "paas"
    # and the matcher routes a paas host to Replatform (TKE), not CVM rehost
    m = match_server(s)
    assert m.target.product == "TKE"
    assert "replatform" in m.rationale.lower() or "replatform" in (m.target.notes or "").lower()


def test_normalize_keeps_app_when_no_paas_signal():
    host = "host-app-01"
    assets = [_sn(host), _zbx(host, role="app"), _rvt(host, mounts=["/", "/opt", "/data"])]
    servers = normalize(assets)
    assert servers[0].role == "app"
    assert match_server(servers[0]).target.product == "CVM"


# -- Oracle → TData Replatform ------------------------------------------------
def test_oracle_db_replatforms_to_tdata():
    s = Server(hostname="db-oracle-01", role="db", cpu_cores=8, mem_gb=32,
               tags=["oracle-db"])
    m = match_server(s)
    assert m.target.product == "TData"
    assert m.target.extras.get("replatform") is True
    # CVM BYOL survives as the fallback alternative, not the primary
    assert any("CVM" in a or "BYOL" in a for a in m.alternatives)


# -- loader mount parsing ----------------------------------------------------
def test_parse_mounts_extracts_linux_mounts_only():
    linux = "/:13G,/usr:9.8G,/boot:1014M,/data03:1.5T,/zpaas:40G,/zpaasssd:20G"
    assert "/zpaas" in parse_mounts(linux)
    assert "/zpaasssd" in parse_mounts(linux)
    assert "/" in parse_mounts(linux)
    # Windows-style diskList has no mount paths
    assert parse_mounts("Hard disk 1:13GB;Hard disk 2:17GB") == []
    assert parse_mounts("") == []


# -- parse_disks: per-partition disk + size for the host drawer ---------------
def test_parse_disks_windows_hard_disk_tokens():
    from idc.core.normalize import parse_disks
    ds = parse_disks("Hard disk 1:13GB;Hard disk 2:17GB;Hard disk 3:30GB")
    assert [d["name"] for d in ds] == ["Hard disk 1", "Hard disk 2", "Hard disk 3"]
    assert [d["size_gb"] for d in ds] == [13, 17, 30]
    assert all(d["fs"] == "" for d in ds)        # no mount path for Windows


def test_parse_disks_linux_mount_tokens_with_units():
    from idc.core.normalize import parse_disks
    ds = parse_disks("/:13G,/usr:9.8G,/boot:1014M,/data03:1.5T,/opt/app/zabbix-agent:2.0G")
    by = {d["name"]: d for d in ds}
    assert by["/"]["size_gb"] == 13
    assert by["/usr"]["size_gb"] == 10              # 9.8 -> round
    assert by["/boot"]["size_gb"] == 1              # 1014M -> 1GB
    assert by["/data03"]["size_gb"] == 1536         # 1.5T -> 1536GB
    assert by["/opt/app/zabbix-agent"]["size_gb"] == 2   # rpartition on last ':'
    # a /-prefixed name is itself the mount -> fs == name
    assert by["/data03"]["fs"] == "/data03"


def test_parse_disks_empty_falls_back_to_aggregate():
    from idc.core.normalize import parse_disks
    assert parse_disks("") == []
    # unparseable token + no aggregate -> nothing
    assert parse_disks("Hard disk 1") == []
    # unparseable + aggregate -> single disk0
    ds = parse_disks("Hard disk 1", aggregate_gb=59.7)
    assert ds == [{"name": "disk0", "size_gb": 60, "kind": "", "fs": ""}]
    # empty list but aggregate given -> aggregate disk0
    assert parse_disks("", aggregate_gb=200)[0]["size_gb"] == 200


def test_parse_disks_ignores_tokens_without_size():
    from idc.core.normalize import parse_disks
    ds = parse_disks("Hard disk 1:13GB;no-colon;/:0G")
    assert len(ds) == 1
    assert ds[0]["name"] == "Hard disk 1"


# -- upgrade_linux_disklist: relabel VMDK-only diskList -> Linux mounts -------
def test_upgrade_linux_disklist_relabels_vmdks_preserving_size():
    from idc.core.normalize import upgrade_linux_disklist, parse_disks
    out = upgrade_linux_disklist("Hard disk 1:13GB;Hard disk 2:17GB;Hard disk 3:30GB",
                                 "HOST_00002")
    # comma-separated so parse_mounts picks the paths up
    assert ";" not in out
    ds = parse_disks(out)
    assert len(ds) == 3
    assert [d["size_gb"] for d in ds] == [13, 17, 30]      # sizes preserved exactly
    assert all(d["fs"] == d["name"] for d in ds)           # every disk now has a mount
    assert ds[0]["name"] == "/"                            # first disk is always root


def test_upgrade_linux_disklist_leaves_real_mounts_untouched():
    from idc.core.normalize import upgrade_linux_disklist
    real = "/:13G,/usr:9.8G,/data03:1.5T"
    assert upgrade_linux_disklist(real, "HOST_00008") == real


def test_upgrade_linux_disklist_empty_and_sizeless():
    from idc.core.normalize import upgrade_linux_disklist
    assert upgrade_linux_disklist("", "HOST") == ""
    assert upgrade_linux_disklist("Hard disk 1", "HOST") == "Hard disk 1"  # no size


def test_upgrade_linux_disklist_is_deterministic():
    from idc.core.normalize import upgrade_linux_disklist
    src = "Hard disk 1:200GB;Hard disk 2:300GB"
    a = upgrade_linux_disklist(src, "HOST_00002")
    b = upgrade_linux_disklist(src, "HOST_00002")
    assert a == b
    # different hosts get different layouts (the pool is hostname-seeded)
    c = upgrade_linux_disklist(src, "HOST_00003")
    assert c != a


def test_upgrade_linux_disklist_uses_only_detector_safe_paths():
    # the synthesized mounts must never reclassify a plain app host: no PaaS /
    # Oracle / DB-engine / cache / middleware / Hadoop / object-store marker.
    from idc.core.normalize import (upgrade_linux_disklist, parse_mounts,
                                     detect_paas, detect_oracle_db,
                                     detect_db_engine, detect_middleware,
                                     detect_cache, detect_hadoop,
                                     detect_object_store)
    mounts = parse_mounts(upgrade_linux_disklist(
        "Hard disk 1:200GB;Hard disk 2:300GB;Hard disk 3:400GB", "HOST_00002"))
    assert mounts and all(m.startswith("/") for m in mounts)
    assert detect_paas(mounts) is False
    assert detect_oracle_db(mounts) is False
    assert detect_db_engine(mounts) == ""
    assert detect_middleware(mounts) == ""
    assert detect_cache(mounts) == ""
    assert detect_hadoop(mounts) == ""
    assert detect_object_store(mounts) == ""


# -- Oracle DB detection from disk layout -----------------------------------
def test_detect_oracle_db_from_ofa_mounts():
    # the live estate's OFA layout: /db.../oracle/<SID>/oradata1, redolog, flashback
    mounts = ["/opt/app/oracle", "/dbaafPR1/oracle/AAF/oradata1",
              "/dbaafPR1/oracle/AAF/redolog_A0", "/dbaafPR1/oracle/AAF/oraundo1",
              "/dbaafPR1/oracle/AAF/flashback"]
    assert detect_oracle_db(mounts) is True
    # individual DB-structure markers
    assert detect_oracle_db(["/oradata"]) is True
    assert detect_oracle_db(["/u01/oradata2"]) is True
    assert detect_oracle_db(["/fra"]) is True
    assert detect_oracle_db(["/redo/redolog_B1"]) is True


def test_detect_oracle_db_requires_db_structure_not_just_oracle_base():
    # /opt/app/oracle alone is the Oracle base / client tools — NOT a DB
    assert detect_oracle_db(["/opt/app/oracle", "/opt/app/oracle/tfa"]) is False
    # generic mounts are not Oracle
    assert detect_oracle_db(["/", "/opt", "/data", "/u01"]) is False
    assert detect_oracle_db([]) is False


def test_normalize_detects_oracle_db_from_mounts_then_tdata():
    host = "HOST_00112"
    mounts = ["/opt/app/oracle", "/dbaafPR1/oracle/AAF/oradata1",
              "/dbaafPR1/oracle/AAF/redolog_A0", "/dbaafPR1/oracle/AAF/oraundo1"]
    assets = [_sn(host), _zbx(host, role="app"), _rvt(host, mounts=mounts)]
    servers = normalize(assets)
    s = servers[0]
    assert s.role == "db"
    assert "oracle-db" in s.tags
    m = match_server(s)
    assert m.target.product == "TData"
    assert m.target.extras.get("replatform") is True


def test_oracle_db_takes_precedence_over_eol_rehost_container():
    # an Oracle DB on a long-term EOL OS → TData (managed, escapes the OS),
    # NOT rehost-container.
    host = "HOST_00007"
    mounts = ["/opt/app/oracle", "/db/oracle/X/oradata1"]
    # OS = centos 7 (long-term EOL)
    sn = RawAsset(source="servicenow", source_id=host, hostname=host, fqdn="", ip="",
                  attrs={"name": host, "os": "centos", "os_version": "7",
                         "cpus": 8, "ram_gb": 64, "environment": "prod",
                         "business_criticality": "", "virtual": "true"})
    assets = [sn, _zbx(host, role="app"), _rvt(host, mounts=mounts)]
    s = normalize(assets)[0]
    assert s.role == "db"
    m = match_server(s)
    assert m.target.product == "TData"
    assert m.target.extras.get("rehost_container") is None


# -- middleware detection ----------------------------------------------------
def test_detect_middleware_via_disk_layout_and_name():
    # disk/mount layout is the strongest signal on the live estate
    assert detect_middleware(mounts=["/", "/data/appdata/zookeeper", "/opt"]) == "zookeeper"
    assert detect_middleware(mounts=["/kafka", "/kafka-logs"]) == "kafka"
    assert detect_middleware(mounts=["/data/kafka3"]) == "kafka"     # prefix match
    assert detect_middleware(mounts=["/opt/tomcat", "/webapps"]) == "tomcat"
    # hostname + tag fallback
    assert detect_middleware(hostname="app-tomcat-01") == "tomcat"
    assert detect_middleware(hostname="kafka-broker-03") == "kafka"
    assert detect_middleware(tags=["kafka"]) == "kafka"
    # not middleware — plain app / db / data-store names
    assert detect_middleware(hostname="app-orders-01") == ""
    assert detect_middleware(hostname="db-mysql-01") == ""
    assert detect_middleware(mounts=["/oradata", "/mongo", "/redis"]) == ""
    # bounded segment match — "was"-as-substring must not trip on "news-app-01"
    assert detect_middleware(hostname="news-app-01") == ""


def test_normalize_middleware_refines_only_app_role():
    host = "app-tomcat-01"
    assets = [_sn(host), _zbx(host, role="app"), _rvt(host, mounts=["/", "/opt"])]
    servers = normalize(assets)
    s = servers[0]
    assert s.role == "middleware"
    assert "mw:tomcat" in s.tags                 # kind is identified + tagged
    m = match_server(s)
    assert m.target.product == "CVM"            # TKE worker node (priced)
    assert m.target.extras.get("rehost_container") is True
    assert "rehost-container" in m.rationale.lower()
    assert "tomcat" in m.rationale.lower()       # the middleware is named
    assert any("containerize on TKE" in a for a in m.alternatives)  # managed alt


def test_normalize_detects_middleware_from_disk_layout():
    # a HOST_NNNNN app host whose only signal is the /kafka mount. Middleware
    # with a distinct managed service replatforms to it FIRST (CKafka), with
    # rehost-container as the fallback alternative.
    host = "HOST_00099"
    assets = [_sn(host), _zbx(host, role="app"),
              _rvt(host, mounts=["/", "/opt", "/kafka", "/kafka/data"])]
    s = normalize(assets)[0]
    assert s.role == "middleware"
    assert "mw:kafka" in s.tags
    m = match_server(s)
    assert m.target.product == "Tencent CKafka"
    assert m.target.extras.get("replatform") is True
    assert "kafka" in m.rationale.lower()
    # rehost-container survives as the fallback alternative, not the primary
    assert any("rehost-container" in a.lower() or "TKE worker" in a for a in m.alternatives)


def test_middleware_app_server_stays_rehost_container():
    # app-server runtimes (tomcat) have no distinct managed product →
    # containerize-on-TKE (rehost-container) IS the managed-runtime replatform
    host = "app-tomcat-99"
    assets = [_sn(host), _zbx(host, role="app"), _rvt(host, mounts=["/", "/opt/tomcat"])]
    s = normalize(assets)[0]
    assert s.role == "middleware"
    m = match_server(s)
    assert m.target.product == "CVM"
    assert m.target.extras.get("rehost_container") is True


def test_normalize_does_not_steal_db_for_middleware_name():
    # a db host keeps db even if its name mentions a middleware token
    host = "db-kafka-01"
    assets = [_sn(host), _zbx(host, role="db"), _rvt(host)]
    # SN role infer: "db-" prefix -> db via _role_from_servicenow
    servers = normalize(assets)
    assert servers[0].role == "db"


# -- long-term EOL/EOS OS → rehost-container ---------------------------------
def test_os_long_term_eol_threshold():
    # CentOS 7 EOL 2024-06-30 -> long expired by 2026-07
    s = Server(hostname="h", os="centos", os_version="7")
    assert os_eol_bucket(s) == EXPIRED
    assert os_long_term_eol(s) is True
    # active OS is not long-term EOL
    s_active = Server(hostname="h", os="ubuntu", os_version="22")
    assert os_eol_bucket(s_active) == ACTIVE
    assert os_long_term_eol(s_active) is False
    # unknown OS never trips
    assert os_long_term_eol(Server(hostname="h", os="weirdos", os_version="1")) is False


def test_long_term_eol_app_host_rehost_containers():
    s = Server(hostname="app-orders-01", role="app", os="centos", os_version="7",
               cpu_cores=4, mem_gb=8)
    m = match_server(s)
    assert m.target.product == "CVM"
    assert m.target.extras.get("rehost_container") is True
    assert "long-term eol" in m.rationale.lower()


def test_long_term_eol_db_stays_managed_not_container():
    # a DB on an EOL OS already escapes the OS via a managed target — the EOL
    # guard must NOT recontainerize it
    s = Server(hostname="db-mysql-01", role="db", os="centos", os_version="7",
               cpu_cores=4, mem_gb=8, tags=["mysql"])
    m = match_server(s)
    assert m.target.product == "CDB"
    assert m.target.extras.get("rehost_container") is None


# -- non-Oracle DB engine detection (PaaS incl. databases) -------------------
def test_detect_db_engine_from_mounts():
    assert detect_db_engine(mounts=["/", "/pgdata", "/pg_backup"]) == "postgres"
    assert detect_db_engine(mounts=["/", "/mysqldata"]) == "mysql"
    assert detect_db_engine(mounts=["/mariabackup", "/mariadata"]) == "mysql"
    assert detect_db_engine(mounts=["/data/appdata/redis/7001"]) == ""   # cache, not db
    assert detect_db_engine(mounts=["/data/cassandra-commit:50G"]) == "cassandra"
    assert detect_db_engine(mounts=["/db2data"]) == "db2"
    # negatives — plain app, Oracle OFA (handled elsewhere), middleware
    assert detect_db_engine(mounts=["/", "/opt", "/data03"]) == ""
    assert detect_db_engine(mounts=["/opt/app/oracle", "/dbaafPR1/oracle/AAF/oradata1"]) == ""
    assert detect_db_engine(mounts=["/var/lib/elasticsearch:40G"]) == ""
    assert detect_db_engine(mounts=[]) == ""


def test_detect_cache_from_mounts():
    assert detect_cache(mounts=["/newdata/appdata/redis/7001"]) == "redis"
    assert detect_cache(mounts=["/redisdata"]) == "redis"
    assert detect_cache(mounts=["/", "/opt", "/data"]) == ""
    assert detect_cache(mounts=[]) == ""


def test_normalize_detects_postgres_then_managed_pg():
    host = "HOST_00240"
    mounts = ["/", "/boot", "/tmp", "/pg_backup", "/opt", "/var"]
    assets = [_sn(host), _zbx(host, role="app"), _rvt(host, mounts=mounts)]
    s = normalize(assets)[0]
    assert s.role == "db"
    assert "db:postgres" in s.tags
    m = match_server(s)
    assert m.target.product == "TencentDB for PostgreSQL"


def test_normalize_detects_mongo_then_managed_mongodb():
    host = "HOST_00114"
    mounts = ["/", "/usr", "/data", "/mongo", "/opt"]
    assets = [_sn(host), _zbx(host, role="app"), _rvt(host, mounts=mounts)]
    s = normalize(assets)[0]
    assert s.role == "db"
    assert "db:mongo" in s.tags
    m = match_server(s)
    assert m.target.product == "TencentDB for MongoDB"
    assert m.target.extras.get("replatform") is True


def test_normalize_detects_redis_then_cache_role():
    host = "HOST_00092"
    mounts = ["/", "/usr", "/data", "/newdata/appdata/redis/7001"]
    assets = [_sn(host), _zbx(host, role="app"), _rvt(host, mounts=mounts)]
    s = normalize(assets)[0]
    assert s.role == "cache"
    assert "redis" in s.tags
    m = match_server(s)
    # unknown criticality on the live estate -> conservative CVM self-hosted
    # Redis, with managed Redis offered as the alternative
    assert m.target.product in ("CVM", "TencentDB for Redis")
    assert any("Redis" in a for a in m.alternatives) or m.target.product == "TencentDB for Redis"


def test_normalize_detects_cassandra_then_cvm_rehost():
    host = "HOST_02342"
    mounts = ["/", "/data/cassandra-commit", "/data/cassandra-data", "/var"]
    assets = [_sn(host), _zbx(host, role="app"), _rvt(host, mounts=mounts)]
    s = normalize(assets)[0]
    assert s.role == "db"
    assert "db:cassandra" in s.tags
    m = match_server(s)
    # no first-class managed Tencent equivalent -> CVM rehost, not a forced replatform
    assert m.target.product == "CVM"
    assert m.target.extras.get("rehost_container") is None


def test_db_engine_takes_precedence_over_middleware_for_influx_host():
    # a host with both an ES mount and an influxdb mount -> DB wins (influx is a
    # database); the db pass runs before middleware in the normalize ladder
    host = "HOST_13649"
    mounts = ["/var/lib/elasticsearch", "/var/lib/influxdb", "/opt"]
    assets = [_sn(host), _zbx(host, role="app"), _rvt(host, mounts=mounts)]
    s = normalize(assets)[0]
    assert s.role == "db"
    assert "db:influx" in s.tags


def test_recently_expired_not_long_term_stays_rehost():
    # Windows 2016 EOL 2027-01 — active as of 2026-07, so NOT long-term; a plain
    # app host stays a normal CVM rehost, not rehost-container.
    s = Server(hostname="app-x-01", role="app", os="windows", os_version="2016",
               cpu_cores=4, mem_gb=8)
    m = match_server(s)
    assert m.target.product == "CVM"
    assert m.target.extras.get("rehost_container") is None


# -- hadoop / object-store / redis-managed / TData-pricing fixes -------------
def test_detect_hadoop_from_mounts():
    assert detect_hadoop(mounts=["/", "/hadoop:50G", "/data1"]) == "hadoop"
    assert detect_hadoop(mounts=["/data/dfs/namenode", "/data/dfs/datanode"]) == "hadoop"
    assert detect_hadoop(mounts=["/", "/opt", "/data03"]) == ""
    assert detect_hadoop(mounts=[]) == ""


def test_detect_object_store_from_mounts():
    assert detect_object_store(mounts=["/data", "/minio", "/backup"]) == "minio"
    assert detect_object_store(mounts=["/", "/glusterfs", "/opt"]) == "gluster"
    assert detect_object_store(mounts=["/", "/opt", "/data"]) == ""
    assert detect_object_store(mounts=[]) == ""


def test_normalize_hadoop_then_emr():
    host = "HOST_09624"
    mounts = ["/", "/data1", "/hadoop:50G", "/data2"]
    assets = [_sn(host), _zbx(host, role="app"), _rvt(host, mounts=mounts)]
    s = normalize(assets)[0]
    assert s.role == "hadoop"
    assert "hadoop" in s.tags
    assert match_server(s).target.product == "EMR"


def test_normalize_hadoop_does_not_steal_middleware_host():
    # a zookeeper/kafka node co-located with a /hadoop mount keeps middleware
    # (hadoop runs AFTER middleware in the cascade)
    host = "app-kafka-01"
    mounts = ["/", "/kafka", "/hadoop:20G"]
    assets = [_sn(host), _zbx(host, role="app"), _rvt(host, mounts=mounts)]
    s = normalize(assets)[0]
    assert s.role == "middleware"
    assert "mw:kafka" in s.tags


def test_normalize_minio_then_cos():
    host = "HOST_01986"
    mounts = ["/", "/data:886G", "/minio", "/backup:493G"]
    assets = [_sn(host), _zbx(host, role="app"), _rvt(host, mounts=mounts)]
    s = normalize(assets)[0]
    assert "object-store" in s.tags
    m = match_server(s)
    assert m.target.product == "COS"
    assert m.target.extras.get("replatform") is True


def test_normalize_gluster_then_cfs():
    host = "HOST_10035"
    mounts = ["/", "/glusterfs", "/opt", "/data"]
    assets = [_sn(host), _zbx(host, role="app"), _rvt(host, mounts=mounts)]
    s = normalize(assets)[0]
    assert "file-store" in s.tags
    assert match_server(s).target.product == "CFS"


def test_redis_routes_to_managed_regardless_of_criticality():
    # empty criticality (the live-estate reality) still goes managed now
    s = Server(hostname="cache-redis-01", role="cache", cpu_cores=4, mem_gb=8,
               os="centos", os_version="7", tags=["redis"],
               business_criticality="")
    m = match_server(s)
    assert m.target.product == "TencentDB for Redis"
    # memcache with empty criticality still falls to CVM (no managed memcache)
    mc = Server(hostname="h", role="cache", cpu_cores=4, mem_gb=8,
                os="centos", os_version="7", tags=["memcache"],
                business_criticality="")
    assert match_server(mc).target.product == "CVM"


def test_tdata_prices_nonzero():
    from idc.core.cost import estimate_server, load_pricebook
    from idc.config import get_settings
    book = load_pricebook(get_settings())
    s = Server(hostname="db-oracle-01", role="db", cpu_cores=8, mem_gb=32,
               os="oracle linux", os_version="7", tags=["oracle-db"], env="prod")
    m = match_server(s)
    assert m.target.product == "TData"
    ce = estimate_server(s, m, book)
    assert ce.monthly_usd > 0          # was 0 before the TData->CDB alias


def test_cos_cfs_price_nonzero():
    from idc.core.cost import estimate_server, load_pricebook
    from idc.config import get_settings
    book = load_pricebook(get_settings())
    for tags, product in ((["object-store", "object-store:minio"], "COS"),
                         (["file-store", "file-store:gluster"], "CFS")):
        s = Server(hostname="h", role="app", cpu_cores=4, mem_gb=8,
                   os="centos", os_version="7", tags=tags, env="prod")
        m = match_server(s)
        assert m.target.product == product
        assert estimate_server(s, m, book).monthly_usd > 0