"""F1 — network dependency discovery tests.

Pure-function tests for discover_network_deps (fixture source) and
merge_network_deps (edge folding + unresolved reporting + dedup), plus one
DB-backed rebuild integration test confirming netdep edges land in persisted
workloads and netdep provenance lands on persisted servers.
"""
import os

import pytest

from idc.config import get_settings, reset_settings
from idc.core.ingest.netdep import discover_network_deps
from idc.core.models import NetDep, Server, Workload
from idc.core.normalize import merge_network_deps


def _srv(hostname, ip, app_ids=None):
    s = Server(hostname=hostname, fqdn=f"{hostname}.dc1.corp", ips=[ip] if ip else [],
               app_ids=app_ids or [])
    return s


# -- discover_network_deps (fixture source) --------------------------------
def test_discover_off_returns_empty():
    s = _srv("app-orders-01", "10.10.1.11")
    os.environ["IDC_NETDEP_SOURCE"] = "off"
    reset_settings()
    assert discover_network_deps(get_settings(), [s]) == []


def test_discover_collector_reads_fixture_and_resolves_src():
    # default source is collector -> reads fixtures/netdep.json
    os.environ["IDC_NETDEP_SOURCE"] = "collector"
    reset_settings()
    s = _srv("app-orders-01", "10.10.1.11")
    deps = discover_network_deps(get_settings(), [s])
    # the fixture has app-orders-01 -> 10.10.3.11:3306 and -> 203.0.113.5:443
    orders = [d for d in deps if d.src_host == "app-orders-01"]
    assert any(d.dst_ip == "10.10.3.11" and d.dst_port == 3306 for d in orders)
    assert any(d.dst_ip == "203.0.113.5" and d.dst_port == 443 for d in orders)
    # src_server_id resolved for the known host
    assert all(d.src_server_id for d in orders)
    # unknown src host gets an empty src_server_id, not a crash
    assert any(d.src_server_id == "" for d in deps), "fixture includes a grafana edge too"


def test_discover_prometheus_falls_back_to_fixture_when_no_metric():
    # no IDC_PROM_URL -> _from_prometheus returns [] -> fixture fallback
    os.environ["IDC_NETDEP_SOURCE"] = "prometheus"
    os.environ.pop("IDC_PROM_URL", None)
    reset_settings()
    s = _srv("app-orders-01", "10.10.1.11")
    deps = discover_network_deps(get_settings(), [s])
    assert any(d.dst_ip == "10.10.3.11" for d in deps), "should fall back to fixture"


# -- merge_network_deps ----------------------------------------------------
def _estate():
    servers = [
        _srv("app-orders-01", "10.10.1.11", app_ids=["app-orders"]),
        _srv("web-portal-01", "10.10.2.11", app_ids=["app-portal"]),
        _srv("mon-grafana-01", "10.10.9.11", app_ids=["app-observability"]),
        _srv("db-mysql-01", "10.10.3.11", app_ids=[]),  # infra, no app
    ]
    workloads = [
        Workload(app_id="app-orders", tier="backend", server_ids=[servers[0].id],
                 depends_on=["app-users"]),
        Workload(app_id="app-portal", tier="frontend", server_ids=[servers[1].id],
                 depends_on=["app-users", "app-orders"]),
        Workload(app_id="app-observability", tier="infra", server_ids=[servers[2].id],
                 depends_on=[]),
    ]
    return servers, workloads


def _nd(servers, src_host, dst_ip, dst_port=3306):
    s = next(x for x in servers if x.hostname == src_host)
    return NetDep(src_server_id=s.id, src_host=src_host, dst_ip=dst_ip,
                  dst_port=dst_port, source="collector")


def test_merge_adds_new_app_edge():
    servers, wls = _estate()
    # grafana (app-observability) -> app-orders-01 (app-orders) — NEW edge
    wls, report = merge_network_deps(servers, wls, [_nd(servers, "mon-grafana-01", "10.10.1.11", 9100)])
    obs = next(w for w in wls if w.app_id == "app-observability")
    assert "app-orders" in obs.depends_on
    assert {"src_app": "app-observability", "dst_app": "app-orders"} in report["added"]


def test_merge_confirms_existing_edge_without_duplicating():
    servers, wls = _estate()
    # portal -> orders-01 (app-orders) — already in app-portal.depends_on
    wls, report = merge_network_deps(servers, wls, [_nd(servers, "web-portal-01", "10.10.1.11", 8080)])
    portal = next(w for w in wls if w.app_id == "app-portal")
    assert portal.depends_on.count("app-orders") == 1  # not duplicated
    assert {"src_app": "app-portal", "dst_app": "app-orders"} in report["confirmed"]


def test_merge_reports_unresolved_external_and_infra_dsts():
    servers, wls = _estate()
    deps = [
        _nd(servers, "app-orders-01", "203.0.113.5", 443),   # external, not in estate
        _nd(servers, "app-orders-01", "10.10.3.11", 3306),   # db-mysql-01, no app (infra)
    ]
    wls, report = merge_network_deps(servers, wls, deps)
    reasons = {r["reason"] for r in report["unresolved"]}
    assert any("external/unmanaged" in r for r in reasons)
    assert any("no app (infra)" in r for r in reasons)
    # no app-level edges added for these
    assert report["added"] == []


def test_merge_appends_netdep_provenance_to_src_server():
    servers, wls = _estate()
    src = next(s for s in servers if s.hostname == "app-orders-01")
    before = len(src.source_refs)
    wls, _ = merge_network_deps(servers, wls, [_nd(servers, "app-orders-01", "203.0.113.5", 443)])
    after = len(src.source_refs)
    assert after == before + 1
    netdep_refs = [r for r in src.source_refs if r.get("source") == "netdep"]
    assert netdep_refs and netdep_refs[-1]["ip"] == "203.0.113.5"


# -- rebuild integration (DB-backed) --------------------------------------
pytest.importorskip("idc.backend.app")
from idc.core.db import open_store
from idc.core import rebuild, run_ingest


def test_rebuild_persists_netdep_edges_and_provenance():
    os.environ["IDC_NETDEP_SOURCE"] = "collector"
    reset_settings()
    st = open_store(get_settings().db_url)
    try:
        run_ingest(st, get_settings(), source="all")
        out = rebuild(st, do_match=True, do_plan=True)
        # netdep report surfaced in rebuild output
        assert "netdep" in out, "rebuild should surface the netdep report"
        report = out["netdep"]
        # the fixture adds app-observability -> app-orders (new edge)
        added_pairs = {(e["src_app"], e["dst_app"]) for e in report["added"]}
        assert ("app-observability", "app-orders") in added_pairs
        # persisted workload carries the new edge
        wls = {w.app_id: w for w in st.list_workloads()}
        assert "app-orders" in wls["app-observability"].depends_on
        # persisted src server carries netdep provenance
        servers = st.list_all_servers()
        grafana = next(s for s in servers if s.hostname == "mon-grafana-01")
        assert any(r.get("source") == "netdep" for r in grafana.source_refs)
        # unresolved dsts reported (external 203.0.113.5 + infra db-mysql-01)
        reasons = " ".join(r["reason"] for r in report["unresolved"])
        assert "external/unmanaged" in reasons or "no app (infra)" in reasons
    finally:
        st.close()
        os.environ.pop("IDC_NETDEP_SOURCE", None)
        reset_settings()