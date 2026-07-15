"""Regression guards for the migration-correctness audit fixes.

Each test pins one defect that was fixed so it cannot silently regress:
  * match: baremetal routing, data-disk sum, DB engine detection, oracle FP, AZ
  * normalize: union-find entity resolution, RVTools OS, source_type, 0.0 util
  * plan: multi-app server in one wave only
  * llm_plan: cap_waves survives a cyclic policy; validate_schema rejects cycles
  * codeintel: retain+retire mix does not migrate
  * config: ``#`` inside an unquoted value is not truncated
"""
from idc.core.match import (
    _data_disk_gb, _is_oracle_db, _db_engine, _az, match_server, warranty_bucket,
    WARRANTY_EXPIRED,
)
from idc.core.models import Server, Disk, RawAsset, Workload
from idc.core.normalize import normalize
from idc.core.plan import plan_waves
from idc.core.llm_plan import cap_waves, validate_plan, WavePolicy, WaveSpec
from idc.core.codeintel import non_migrating_servers
from idc.config import load_dotenv


# -- match.py ---------------------------------------------------------------
def _srv(**kw) -> Server:
    return Server(**kw)


def test_baremetal_non_hadoop_not_routed_to_emr():
    """C1: a bare-metal DB/web must NOT go to EMR (only role==hadoop does)."""
    bm_db = _srv(hostname="bm-oracle-01", source_type="baremetal", role="db",
                 cpu_cores=8, mem_gb=32, tags=["oracle-db"])
    m = match_server(bm_db)
    assert m.target.product != "EMR"
    assert m.target.product == "TData"  # Oracle → Replatform to TData

    bm_web = _srv(hostname="bm-web-01", source_type="baremetal", role="web",
                  cpu_cores=4, mem_gb=8)
    assert match_server(bm_web).target.product == "CVM"


def test_hadoop_still_routed_to_emr():
    hd = _srv(hostname="hdop-nn-01", source_type="vm", role="hadoop",
              cpu_cores=8, mem_gb=32)
    assert match_server(hd).target.product == "EMR"


def test_data_disk_aggregates_all_data_disks():
    """H1: 4x500GB data disks -> 2000GB CBS, not 500GB (the largest single)."""
    s = _srv(disks=[
        Disk(name="sda", size_gb=50, fs="/"),
        Disk(name="sdb", size_gb=500, fs="/data"),
        Disk(name="sdc", size_gb=500, fs="/data2"),
        Disk(name="sdd", size_gb=500, fs="/data3"),
        Disk(name="sde", size_gb=500, fs="/data4"),
    ])
    assert _data_disk_gb(s) == 2000


def test_db_engine_detection_postgres():
    """H2: a PostgreSQL host maps to TencentDB for PostgreSQL, not MySQL."""
    s = _srv(hostname="pg-orders-01", role="db", cpu_cores=4, mem_gb=16,
             tags=["postgres"])
    m = match_server(s)
    assert m.target.product == "TencentDB for PostgreSQL"


def test_db_engine_detection_sqlserver():
    s = _srv(hostname="mssql-01", role="db", cpu_cores=4, mem_gb=16, tags=["mssql"])
    assert match_server(s).target.product == "TencentDB for SQL Server"


def test_oracle_false_positive_rejected():
    """M4: oracle-backup / oracle-client are NOT Oracle DBs."""
    assert _is_oracle_db([], "db-oracle-01") is True
    assert _is_oracle_db([], "oracle-01") is True
    assert _is_oracle_db([], "oracle-backup-01") is False
    assert _is_oracle_db([], "oracle-client-99") is False
    assert _is_oracle_db(["oracle-client"], "web-01") is False
    assert _is_oracle_db(["oracle-db"], "web-01") is True


def test_db_engine_helper_no_oracle_overload():
    # _db_engine deliberately does NOT return "oracle" (ambiguous token)
    assert _db_engine([], "db-oracle-01") == ""


def test_az_is_valid_for_bangkok():
    """M1: ap-bangkok has zones 1/2 only — never emit zone 3."""
    assert _az("ap-bangkok") == "ap-bangkok-1"
    assert _az("ap-singapore") == "ap-singapore-1"  # default zone 1


def test_warranty_expired_on_eol_day():
    """M1: on the EOL date itself the warranty is EXPIRED, not EXPIRING."""
    s = _srv(hardware_eol="2026-07-13")
    assert warranty_bucket(s, "2026-07-13") == WARRANTY_EXPIRED


# -- normalize.py -----------------------------------------------------------
def test_union_find_merges_prometheus_ip_with_cmdb_hostname():
    """C5/H6: Prometheus instance=ip:port merges with the CMDB host keyed by
    hostname, so utilization attaches to the real server (no phantom)."""
    assets = [
        RawAsset(source="servicenow", source_id="sn-1", hostname="db-prod-01",
                 ip="10.0.0.5", attrs={"os": "centos", "cpus": 4, "ram_gb": 16,
                                       "environment": "prod"}),
        # the prometheus adapter strips the :port and exposes the IP, so the
        # asset that reaches normalize is keyed by 10.0.0.5 (no port):
        RawAsset(source="prometheus", source_id="10.0.0.5",
                 hostname="10.0.0.5", ip="10.0.0.5",
                 attrs={"utilization": {"cpu_p95": 42.0, "mem_p95": 60.0}}),
    ]
    servers = normalize(assets)
    assert len(servers) == 1, "prometheus ip:port must merge with the CMDB host"
    assert servers[0].hostname == "db-prod-01"
    assert servers[0].utilization.cpu_p95 == 42.0


def test_rvtools_os_used_when_cmdb_has_none():
    """C4: when ServiceNow carries no OS, RVTools' vSphere OS is used, not
    'unknown' (which used to block EOL detection)."""
    assets = [
        RawAsset(source="servicenow", source_id="sn-1", hostname="web-01",
                 attrs={"environment": "prod"}),
        RawAsset(source="rvtools", source_id="rv-1", hostname="web-01",
                 attrs={"os": "CentOS 7.9", "cpus": 2, "memory_mb": 4096}),
    ]
    servers = normalize(assets)
    assert servers[0].os == "centos"


def test_rvtools_does_not_overwrite_cmdb_baremetal():
    """H4: a CMDB baremetal host keeps source_type=baremetal even if RVTools
    also reports it (hostname collision)."""
    assets = [
        RawAsset(source="servicenow", source_id="sn-1", hostname="bm-01",
                 attrs={"environment": "prod", "virtual": "false"}),
        RawAsset(source="rvtools", source_id="rv-1", hostname="bm-01",
                 attrs={"os": "centos", "cpus": 8, "memory_mb": 16384}),
    ]
    servers = normalize(assets)
    assert servers[0].source_type == "baremetal"


def test_zero_utilization_not_treated_as_missing():
    """H5: a genuine 0.0 cpu reading is kept, not overwritten by a later
    source's missing data."""
    assets = [
        RawAsset(source="zabbix", source_id="zx-1", hostname="idle-01",
                 attrs={"utilization": {"cpu_p95": 0.0, "mem_p95": 0.0}}),
        RawAsset(source="prometheus", source_id="prom-1", hostname="idle-01",
                 attrs={"utilization": {"mem_p95": 5.0}}),  # no cpu_p95
    ]
    servers = normalize(assets)
    assert servers[0].utilization.cpu_p95 == 0.0


def test_unknown_env_defaults_to_unknown_not_prod():
    """M2: a host with no environment is 'unknown' (conservative), not 'prod'."""
    assets = [RawAsset(source="servicenow", source_id="sn-1", hostname="x-01",
                      attrs={})]
    s = normalize(assets)[0]
    assert s.env == "unknown"
    assert s.business_criticality == "low"  # not rushed into prod criticality


# -- plan.py ----------------------------------------------------------------
def test_multi_app_server_appears_in_only_one_wave():
    """H3: a server shared by two apps lands in exactly one wave."""
    shared = _srv(id="srv-shared", hostname="shared-01", role="app",
                  app_ids=["app-a", "app-b"])
    a = _srv(id="srv-a", hostname="a-01", role="app", app_ids=["app-a"])
    b = _srv(id="srv-b", hostname="b-01", role="app", app_ids=["app-b"])
    workloads = [
        Workload(app_id="app-a", name="A", server_ids=["srv-a", "srv-shared"]),
        Workload(app_id="app-b", name="B", server_ids=["srv-b", "srv-shared"],
                 depends_on=["app-a"]),
    ]
    waves = plan_waves([shared, a, b], workloads)
    val = validate_plan(waves, [shared, a, b])
    assert val["ok"], f"plan invalid: {val['errors']}"
    # the shared server is in exactly one wave
    owners = [w.name for w in waves if "srv-shared" in w.server_ids]
    assert len(owners) == 1, f"shared server in waves: {owners}"


# -- llm_plan.py ------------------------------------------------------------
def test_cap_waves_survives_cyclic_wave_dag():
    """C1: cap_waves must not infinite-recurse on a cyclic wave DAG."""
    from idc.core.models import Wave
    w1 = Wave(name="A", server_ids=["s1"], depends_on=["w2"])
    w2 = Wave(name="B", server_ids=["s2"], depends_on=["w1"])
    # should return without raising (and without RecursionError)
    out, _ = cap_waves([w1, w2], 1)
    assert len(out) <= 1


def test_validate_schema_rejects_cyclic_policy():
    """C1: a cyclic policy (A->B->A) is rejected at validation time."""
    pol = WavePolicy(stages=[
        WaveSpec(label="A", depends_on=["B"]),
        WaveSpec(label="B", depends_on=["A"]),
    ])
    errs = pol.validate_schema()
    assert any("cycle" in e for e in errs), errs


# -- codeintel.py -----------------------------------------------------------
def test_retain_plus_retire_mix_does_not_migrate():
    """M2: a host whose apps are all non-migrating (one retain, one retire) is
    NOT migrated AND NOT decommissioned — it is retained on-prem. A retain app
    must stay on its host; decommissioning the shared host would destroy the
    retain app. The retire app is retired at the app layer separately."""
    from idc.core.models import AppStrategy, PATTERN_RETAIN, PATTERN_RETIRE
    s = _srv(id="srv-1", hostname="h-1", app_ids=["keep", "drop"])
    workloads = [
        Workload(app_id="keep", server_ids=["srv-1"]),
        Workload(app_id="drop", server_ids=["srv-1"]),
    ]
    strategies = {"keep": AppStrategy(app_id="keep", strategy=PATTERN_RETAIN),
                  "drop": AppStrategy(app_id="drop", strategy=PATTERN_RETIRE)}
    retain, retire = non_migrating_servers([s], workloads, [], strategies=strategies)
    assert "srv-1" in retain
    assert "srv-1" not in retire


# -- config.py --------------------------------------------------------------
def test_dotenv_preserves_hash_in_unquoted_value(tmp_path, monkeypatch):
    """M2: an unquoted value containing '#' is not truncated at the '#'."""
    env = tmp_path / ".env"
    env.write_text("IDC_WEB_PASSWORD=ab#cd\nIDC_DB_URL='x#y'\n")
    monkeypatch.setenv("IDC_TEST_FORCE", "1")
    # ensure the keys aren't already set
    monkeypatch.delenv("IDC_WEB_PASSWORD", raising=False)
    monkeypatch.delenv("IDC_DB_URL", raising=False)
    load_dotenv(env)
    import os
    assert os.environ["IDC_WEB_PASSWORD"] == "ab#cd"
    assert os.environ["IDC_DB_URL"] == "x#y"
    # and a real inline comment (whitespace before #) is still stripped
    env.write_text("IDC_NOTE=hello # a note\n")
    monkeypatch.delenv("IDC_NOTE", raising=False)
    load_dotenv(env)
    assert os.environ["IDC_NOTE"] == "hello"