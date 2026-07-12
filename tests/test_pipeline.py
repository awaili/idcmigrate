"""Tests for ingest adapters + normalize + match + plan (pure, no network)."""
import os
import tempfile

import pytest

from idc.config import get_settings, reset_settings
from idc.core import _offline_settings
from idc.core.ingest import all_sources, get_adapter
from idc.core.normalize import normalize, load_workloads, resolve_workloads
from idc.core.match import match_servers, match_server
from idc.core.plan import plan_waves
from idc.core.models import SOURCE_PROMETHEUS, SOURCE_RVTOOLS, SOURCE_SERVICENOW, SOURCE_ZABBIX

EXPECTED_TOTAL = 15  # servers defined in fixtures/_generate.py


@pytest.fixture(scope="module")
def all_assets():
    s = get_settings()
    out = []
    for src in all_sources():
        res = get_adapter(src).fetch(s)
        assert res.ok, f"{src} fixture failed: {res.error}"
        out.extend(res.assets)
    return out


def test_adapter_counts():
    s = get_settings()
    counts = {SOURCE_SERVICENOW: 15, SOURCE_RVTOOLS: 9,
              SOURCE_ZABBIX: 15, SOURCE_PROMETHEUS: 15}
    for src, n in counts.items():
        res = get_adapter(src).fetch(s)
        assert len(res.assets) == n, f"{src}: {len(res.assets)} != {n}"
        assert res.mode == "fixture"


def test_offline_path_override_reads_uploaded_file():
    """A path override makes the adapter read that file in fixture mode, even
    when live API creds are configured (mimics the web upload flow)."""
    row = ("sys_id,name,hostname,fqdn,ip_address,os,os_version,ram_gb,cpus,"
           "company,environment,business_criticality,location,virtual,subnet,serial_number\n"
           "abc,db-mysql-99,db-mysql-99,db-mysql-99.dc1,10.0.0.99,centos 7,7,8,4,"
           "acme,prod,high,dc1,false,10.0.0.0/24,SN99\n")
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        f.write(row)
        path = f.name
    try:
        base = get_settings()
        # pretend live API creds are set — override must still force offline
        import dataclasses
        creds = dataclasses.replace(base, sn_base="https://x", sn_token="t")
        assert creds.has_servicenow()  # sanity: would go online without override
        s = _offline_settings(creds, SOURCE_SERVICENOW, path)
        assert not s.has_servicenow()  # override cleared creds → offline
        assert s.sn_path == path
        res = get_adapter(SOURCE_SERVICENOW).fetch(s)
        assert res.ok and res.mode == "fixture"
        assert len(res.assets) == 1
        assert res.assets[0].hostname == "db-mysql-99"
    finally:
        os.unlink(path)


def test_offline_settings_missing_file_reports_error():
    s = _offline_settings(get_settings(), SOURCE_SERVICENOW, "/no/such/file.csv")
    res = get_adapter(SOURCE_SERVICENOW).fetch(s)
    assert not res.ok
    assert "not found" in res.error


def test_normalize_unique_servers(all_assets):
    servers = normalize(all_assets)
    assert len(servers) == EXPECTED_TOTAL, f"dedup failed: {len(servers)}"
    by_host = {s.hostname: s for s in servers}
    # a VM should merge all 4 sources
    vm = by_host["db-mysql-01"]
    sources = {r["source"] for r in vm.source_refs}
    assert sources == {SOURCE_SERVICENOW, SOURCE_RVTOOLS, SOURCE_ZABBIX, SOURCE_PROMETHEUS}
    # a baremetal hadoop node is not in RVTools → 3 sources
    bm = by_host["hdop-nn-01"]
    assert SOURCE_RVTOOLS not in {r["source"] for r in bm.source_refs}
    # role inference from ServiceNow name
    assert by_host["db-mysql-01"].role == "db"
    assert by_host["cache-redis-01"].role == "cache"
    assert by_host["k8s-master-01"].role == "k8s"
    assert by_host["hdop-nn-01"].role == "hadoop"
    # utilization merged from zabbix/prometheus
    assert vm.utilization.cpu_p95 is not None and vm.utilization.cpu_p95 > 0
    # sizing from RVTools
    assert vm.cpu_cores == 8 and vm.mem_gb == 32
    assert vm.disks and any(d.size_gb == 500 for d in vm.disks)


def test_match_targets(all_assets):
    servers = normalize(all_assets)
    by_host = {s.hostname: s for s in servers}
    matches = {m.server_id: m for m in match_servers(servers)}

    def prod(h): return matches[by_host[h].id].target.product

    assert prod("db-mysql-01") == "CDB"
    assert prod("db-oracle-01") == "CVM"   # Oracle → lift-and-shift
    assert prod("k8s-master-01") == "TKE"
    assert prod("k8s-worker-01") == "CVM"  # TKE node
    assert prod("hdop-nn-01") == "EMR"
    assert prod("web-portal-01") == "CVM"
    assert prod("app-orders-01") == "CVM"
    # confidence in sane range
    for m in matches.values():
        assert 0.0 <= m.confidence <= 1.0
    assert matches[by_host["db-mysql-01"].id].confidence >= 0.85
    # region mapping: target landing zone is Thailand, so every DC -> ap-bangkok
    assert matches[by_host["hdop-nn-01"].id].target.region == "ap-bangkok"
    assert matches[by_host["db-mysql-01"].id].target.region == "ap-bangkok"


def test_match_k8s_master_vs_worker(all_assets):
    servers = normalize(all_assets)
    by_host = {s.hostname: s for s in servers}
    m_master = match_server(by_host["k8s-master-01"])
    m_worker = match_server(by_host["k8s-worker-01"])
    assert m_master.target.product == "TKE"
    assert m_worker.target.product == "CVM"
    assert m_worker.target.extras.get("tke_node_pool") is True


def test_plan_waves(all_assets):
    servers = normalize(all_assets)
    wls = resolve_workloads(load_workloads(), servers)
    waves = plan_waves(servers, wls)
    stages = [w.stage for w in waves]
    assert "1_landing_zone" in stages
    assert "2_data" in stages
    assert "4_cutover" in stages

    # every server assigned to exactly one wave
    assigned = [sid for w in waves for sid in w.server_ids]
    assert len(assigned) == len(set(assigned)) == EXPECTED_TOTAL

    # LZ = k8s + monitoring; Data = db + cache
    by_id = {s.id: s for s in servers}
    lz = next(w for w in waves if w.stage == "1_landing_zone")
    lz_hosts = {by_id[sid].hostname for sid in lz.server_ids}
    assert {"k8s-master-01", "k8s-worker-01", "k8s-worker-02", "mon-grafana-01"} == lz_hosts

    data = next(w for w in waves if w.stage == "2_data")
    data_hosts = {by_id[sid].hostname for sid in data.server_ids}
    assert {"db-mysql-01", "db-mysql-02", "db-oracle-01", "cache-redis-01"} == data_hosts

    # cutover = web portal, depends on users + orders waves
    cutover = next(w for w in waves if w.stage == "4_cutover")
    assert by_id[cutover.server_ids[0]].hostname == "web-portal-01"
    assert len(cutover.depends_on) >= 2

    # DAG: data depends on LZ; cutover depends on data
    by_id = {w.id: w for w in waves}
    assert by_id[data.depends_on[0]].stage == "1_landing_zone"
    assert any(by_id[d].stage == "2_data" for d in cutover.depends_on)


def test_plan_waves_max_waves_hard_cap(all_assets):
    """The deterministic path also honors max_waves (unifies the guarantee with
    the LLM planner): capped to <= cap, every server still placed once, DAG valid."""
    from idc.core.llm_plan import validate_plan
    servers = normalize(all_assets)
    wls = resolve_workloads(load_workloads(), servers)
    full = plan_waves(servers, wls)
    assert len(full) > 2
    capped = plan_waves(servers, wls, max_waves=2)
    assert len(capped) == 2
    # nothing lost, no dups
    sids = [sid for w in capped for sid in w.server_ids]
    assert len(sids) == len(set(sids)) == EXPECTED_TOTAL
    # default (0) = no cap, unchanged
    assert len(plan_waves(servers, wls, max_waves=0)) == len(full)
    # capped plan is still a valid DAG
    val = validate_plan(capped, servers)
    assert val["ok"]