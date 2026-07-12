"""F3 — conversational what-if pure functions + cost.what_if wiring.

Covers the F3 surface the copilot tab / agent calls without re-running ingest:
  * ``match.right_size(server, strategy)``  — re-size from live utilization
  * ``match.re_region(server, region)``    — swap target region
  * ``cost.what_if(..., region=, sizing=, byol=)`` — portfolio delta, composable
  * backend ``/api/what-if/right-size`` + ``/api/what-if/re-region`` per-server
    delta endpoints (mocked store).
"""
import math

import pytest

from idc.config import get_settings, reset_settings
from idc.core.cost import estimate_server, load_pricebook, what_if
from idc.core.match import (
    RIGHT_SIZE_HEADROOM, _sizing_cpu_mem, match_server, re_region, right_size,
)
from idc.core.models import Server, Target, Utilization, Match


def _srv(cpu=16, mem=64, cpu_p95=None, mem_p95=None, role="app",
         datacenter="dc1", app_ids=("app1",)):
    return Server(
        hostname="h1", fqdn="h1.dc1.corp", ips=["10.0.0.1"], role=role,
        cpu_cores=cpu, mem_gb=mem, datacenter=datacenter, app_ids=list(app_ids),
        sizing_basis="measured" if cpu_p95 else "estimated",
        utilization=Utilization(cpu_p95=cpu_p95, mem_p95=mem_p95,
                                source="zabbix" if cpu_p95 else ""),
    )


# -- _sizing_cpu_mem --------------------------------------------------------
def test_sizing_as_is_uses_specs():
    s = _srv(cpu=16, mem=64, cpu_p95=25, mem_p95=30)
    assert _sizing_cpu_mem(s, "as_is") == (16, 64)


def test_sizing_measured_shrinks_to_utilization_with_headroom():
    s = _srv(cpu=16, mem=64, cpu_p95=25, mem_p95=30)
    cpu, mem = _sizing_cpu_mem(s, "measured")
    assert cpu == math.ceil(16 * 0.25 * RIGHT_SIZE_HEADROOM)
    assert mem == math.ceil(64 * 0.30 * RIGHT_SIZE_HEADROOM)
    # right_size alias == measured
    assert _sizing_cpu_mem(s, "right_size") == _sizing_cpu_mem(s, "measured")


def test_sizing_measured_never_exceeds_specs():
    # 100% util + headroom would overshoot, but we cap at on-prem capacity
    s = _srv(cpu=8, mem=32, cpu_p95=99, mem_p95=99)
    cpu, mem = _sizing_cpu_mem(s, "measured")
    assert cpu <= 8 and mem <= 32


def test_sizing_measured_falls_back_to_specs_without_util():
    s = _srv(cpu=16, mem=64)  # no utilization
    assert _sizing_cpu_mem(s, "measured") == (16, 64)


# -- right_size / re_region -------------------------------------------------
def test_right_size_measured_yields_smaller_spec_and_lower_cost():
    reset_settings()
    book = load_pricebook(get_settings(), refresh=True)
    s = _srv(cpu=16, mem=64, cpu_p95=25, mem_p95=30)
    as_is = match_server(s, "as_is")
    measured = right_size(s, "measured")
    assert as_is.target.spec == "SA5.4XLARGE32"
    assert measured.target.spec == "SA5.2XLARGE16"   # shrunk
    assert "what-if sizing=measured" in measured.rationale
    y_as = estimate_server(s, as_is, book).yearly_usd
    y_me = estimate_server(s, measured, book).yearly_usd
    assert y_me < y_as


def test_right_size_as_is_matches_match_server():
    s = _srv(cpu=16, mem=64, cpu_p95=25, mem_p95=30)
    base = match_server(s)
    rs = right_size(s, "as_is")
    assert rs.target.spec == base.target.spec
    assert rs.target.region == base.target.region


def test_re_region_swaps_region_keeps_spec():
    s = _srv(cpu=16, mem=64, cpu_p95=25, mem_p95=30)
    m = re_region(s, "ap-bangkok")
    assert m.target.region == "ap-bangkok"
    assert m.target.az == "ap-bangkok-3"
    # spec is preserved (as_is sizing)
    assert m.target.spec == match_server(s, "as_is").target.spec
    assert "what-if region=ap-bangkok" in m.rationale


def test_right_size_and_re_region_are_pure_original_untouched():
    s = _srv(cpu=16, mem=64, cpu_p95=25, mem_p95=30)
    base = match_server(s)
    _ = right_size(s, "measured")
    _ = re_region(s, "ap-bangkok")
    # base match not mutated by the what-if helpers
    assert base.target.spec == "SA5.4XLARGE32"
    assert base.target.region == "ap-bangkok"


# -- cost.what_if composable overrides --------------------------------------
def test_what_if_no_overrides_returns_no_alternate():
    reset_settings()
    book = load_pricebook(get_settings(), refresh=True)
    s = _srv(cpu=16, mem=64, cpu_p95=25, mem_p95=30)
    m = match_server(s)
    out = what_if([s], [m], book)
    assert out["alternate"] is None and out["delta"] is None
    assert out["applied"] == {"region": None, "sizing": None, "byol": None}


def test_what_if_sizing_measured_negative_delta():
    reset_settings()
    book = load_pricebook(get_settings(), refresh=True)
    s = _srv(cpu=16, mem=64, cpu_p95=25, mem_p95=30)
    m = match_server(s)
    out = what_if([s], [m], book, sizing="measured")
    assert out["applied"]["sizing"] == "measured"
    assert out["delta"] < 0   # right-sized down -> cheaper
    assert out["alternate"]["cloud_yearly"] < out["baseline"]["cloud_yearly"]


def test_what_if_region_and_sizing_compose():
    reset_settings()
    book = load_pricebook(get_settings(), refresh=True)
    s = _srv(cpu=16, mem=64, cpu_p95=25, mem_p95=30)
    m = match_server(s)
    both = what_if([s], [m], book, region="ap-bangkok", sizing="measured")
    reg_only = what_if([s], [m], book, region="ap-bangkok")
    size_only = what_if([s], [m], book, sizing="measured")
    # Bangkok is the only target region, so a region override is a no-op
    # (delta 0) and region+sizing == sizing alone. Sizing still saves.
    assert reg_only["delta"] == 0
    assert size_only["delta"] < 0
    assert both["delta"] < reg_only["delta"]
    assert both["delta"] == size_only["delta"]


def test_what_if_sizing_without_util_is_no_op_delta():
    reset_settings()
    book = load_pricebook(get_settings(), refresh=True)
    s = _srv(cpu=16, mem=64)  # no utilization -> measured falls back to specs
    m = match_server(s)
    out = what_if([s], [m], book, sizing="measured")
    assert out["delta"] == 0  # same spec -> same cost


# -- backend per-server what-if endpoints (DB-gated) ------------------------
pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient
import idc.backend.app as m
from idc.core.db import open_store
from idc.core.models import _new_id

client = TestClient(m.app)


def _seed_server_with_match(cpu=16, mem=64, cpu_p95=25, mem_p95=30):
    st = open_store(get_settings().db_url)
    sid = _new_id("srv")
    s = Server(id=sid, hostname=f"pytest-f3-{sid[-4:]}",
               fqdn=f"f3-{sid[-4:]}.dc1.corp", ips=["10.99.0.99"], role="app",
               cpu_cores=cpu, mem_gb=mem, datacenter="dc1", app_ids=[],
               sizing_basis="measured" if cpu_p95 else "estimated",
               utilization=Utilization(cpu_p95=cpu_p95, mem_p95=mem_p95,
                                      source="zabbix" if cpu_p95 else ""))
    st.upsert_server(s)
    st.upsert_match(Match(server_id=sid,
                          target=Target(product="CVM", spec="SA5.4XLARGE32",
                                        region="ap-bangkok"),
                          confidence=0.85, method="rule", rationale="x"))
    st.close()
    return sid


def _cleanup(sid):
    st = open_store(get_settings().db_url)
    with st.tx() as cur:
        cur.execute(st._x("DELETE FROM matches WHERE server_id=?"), (sid,))
        cur.execute(st._x("DELETE FROM servers WHERE id=?"), (sid,))
    st.close()


def test_what_if_right_size_endpoint():
    sid = _seed_server_with_match()
    try:
        r = client.post("/api/what-if/right-size",
                        json={"server_id": sid, "strategy": "measured"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["before"]["spec"] == "SA5.4XLARGE32"
        assert body["after"]["spec"] == "SA5.2XLARGE16"   # right-sized down
        assert body["delta_yearly"] < 0
        assert "what-if sizing=measured" in body["rationale"]
    finally:
        _cleanup(sid)


def test_what_if_re_region_endpoint():
    sid = _seed_server_with_match(cpu_p95=None, mem_p95=None)
    try:
        r = client.post("/api/what-if/re-region",
                        json={"server_id": sid, "region": "ap-bangkok"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["before"]["region"] == "ap-bangkok"
        assert body["after"]["region"] == "ap-bangkok"
        assert body["after"]["spec"] == "SA5.4XLARGE32"   # spec preserved
        # Bangkok is the only target region, so re-region to ap-bangkok is a
        # no-op (same region -> delta 0); the endpoint still returns before/after.
        assert body["delta_yearly"] == 0
    finally:
        _cleanup(sid)


def test_what_if_unknown_server_404():
    r = client.post("/api/what-if/right-size",
                    json={"server_id": "srv-nope", "strategy": "measured"})
    assert r.status_code == 404


def test_cost_what_if_endpoint_sizing():
    sid = _seed_server_with_match()
    try:
        r = client.post("/api/cost/what-if", json={"sizing": "measured"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["applied"]["sizing"] == "measured"
        # the seeded server is over-sized -> alternate should be cheaper
        assert body["delta"] < 0
    finally:
        _cleanup(sid)