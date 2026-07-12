"""Data-gap — portfolio data-gaps report + soft confidence gate (Gap3).

Covers:
  * ``datagaps.portfolio_data_gaps`` — confidence distribution, missing
    key-field counts, missing util / missing code-profile / unknown-warranty
    counts, and the worst-hosts ranking.
  * ``plan.plan_waves(min_assessment_confidence=...)`` — below-threshold hosts
    land in a trailing "Needs Discovery" wave and are excluded from the normal
    migration sequence; default 0.0 = off (no gate, existing behavior).
  * backend ``GET /api/data-gaps``.
"""
import pytest

from idc.core.datagaps import (
    portfolio_data_gaps, CONF_HIGH, CONF_MED, CONF_LOW, CONF_NONE,
)
from idc.core.models import (
    Server, Workload, Wave, CodeProfile, _new_id, STAGE_APP,
)
from idc.core.plan import plan_waves


# -- plan_waves soft confidence gate (pure) --------------------------------
def _gw_servers():
    # one well-known host (full key fields + confidence 0.9) and one thinly-
    # known host (no role/env/crit, confidence 0.2). Both bound to the same app
    # so the well-known one drives the app wave; the thinly-known one would
    # normally join it (same app) but the gate pulls it to Needs Discovery.
    good = Server(id="srv-good", hostname="good", role="web", os="centos",
                  cpu_cores=4, mem_gb=8, env="prod", business_criticality="high",
                  app_ids=["app1"], assessment_confidence=0.9)
    thin = Server(id="srv-thin", hostname="thin", app_ids=["app1"],
                  assessment_confidence=0.2)
    return [good, thin], Workload(app_id="app1", name="app1",
                                 server_ids=["srv-good", "srv-thin"])


def test_plan_waves_gate_off_by_default_no_discovery_wave():
    servers, wl = _gw_servers()
    waves = plan_waves(servers, [wl])
    names = [w.name for w in waves]
    assert not any("Needs Discovery" in n for n in names)
    # both servers are members of some wave (the app wave)
    members = {sid for w in waves for sid in (w.server_ids or [])}
    assert "srv-good" in members and "srv-thin" in members


def test_plan_waves_gate_pulls_low_confidence_to_needs_discovery():
    servers, wl = _gw_servers()
    waves = plan_waves(servers, [wl], min_assessment_confidence=0.5)
    discovery = [w for w in waves if "Needs Discovery" in w.name]
    assert len(discovery) == 1
    assert "srv-thin" in discovery[0].server_ids
    assert "srv-good" not in discovery[0].server_ids  # good host not gated
    # the gated host is NOT in any other wave
    other_members = {sid for w in waves if "Needs Discovery" not in w.name
                     for sid in (w.server_ids or [])}
    assert "srv-thin" not in other_members
    # the good host still migrates (in some non-discovery wave)
    assert "srv-good" in other_members


def test_plan_waves_gate_none_confidence_not_gated():
    # a host with assessment_confidence=None (not rebuilt) is NOT gated even
    # when the threshold is set — only a measured-low confidence triggers it.
    s = Server(id="s1", hostname="s1", role="web", os="centos", cpu_cores=2,
               mem_gb=4, env="prod", business_criticality="med",
               app_ids=["a"], assessment_confidence=None)
    wl = Workload(app_id="a", name="a", server_ids=["s1"])
    waves = plan_waves([s], [wl], min_assessment_confidence=0.9)
    assert not any("Needs Discovery" in w.name for w in waves)
    assert "s1" in {sid for w in waves for sid in (w.server_ids or [])}


# -- portfolio_data_gaps (DB-backed) ---------------------------------------
pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient
import idc.backend.app as m
from idc.core.db import open_store
from idc.config import get_settings

client = TestClient(m.app)


def _store():
    return open_store(get_settings().db_url)


def _cleanup(sids, app_ids=None):
    st = _store()
    with st.tx() as cur:
        for a in (app_ids or []):
            cur.execute(st._x("DELETE FROM code_profiles WHERE app_id=?"), (a,))
            cur.execute(st._x("DELETE FROM workloads WHERE app_id=?"), (a,))
        for sid in sids:
            cur.execute(st._x("DELETE FROM matches WHERE server_id=?"), (sid,))
            cur.execute(st._x("DELETE FROM servers WHERE id=?"), (sid,))
    st.close()


def test_portfolio_data_gaps_counts():
    st = _store()
    sids = []
    try:
        # well-known host: full fields + measured util + profile + active warranty
        good = Server(id=_new_id("srv"), hostname="dg-good", role="web", os="centos",
                      os_version="7",
                      cpu_cores=4, mem_gb=8, env="prod", business_criticality="high",
                      app_ids=["dg-app1"],
                      utilization=__import__("idc.core.models", fromlist=["Utilization"]).Utilization(
                          cpu_p95=10, mem_p95=20, source="zabbix"),
                      assessment_confidence=0.9, warranty_status="active")
        # thinly-known host: no role/env/crit, no util, unknown warranty
        thin = Server(id=_new_id("srv"), hostname="dg-thin", app_ids=["dg-noprofile"],
                      assessment_confidence=0.2)
        sids = [good.id, thin.id]
        st.upsert_server(good); st.upsert_server(thin)
        st.upsert_code_profile(CodeProfile(app_id="dg-app1", language="java",
                                           runtime="jdk", framework="spring",
                                           cloud_readiness=0.8))
        rep = portfolio_data_gaps(st, top=10)
        assert rep["total_servers"] >= 2
        # the thin host lands in low confidence; good in high
        assert rep["confidence"][CONF_LOW] >= 1
        assert rep["confidence"][CONF_HIGH] >= 1
        # role/env/business_criticality missing on the thin host
        assert rep["missing"]["role"] >= 1
        assert rep["missing"]["env"] >= 1
        # thin host has no util telemetry (sizing_basis != measured)
        assert rep["missing_utilization"] >= 1
        # thin host's app (dg-noprofile) has no profile
        assert rep["missing_code_profile"] >= 1
        # thin host warranty unknown
        assert rep["warranty"]["unknown"] >= 1
        assert rep["warranty"]["active"] >= 1
        # good host is centos 7 (expired OS); thin host has no os -> unknown
        assert rep["os_eol"]["expired"] >= 1
        assert rep["os_eol"]["unknown"] >= 1
        # worst_hosts sorted lowest-confidence-first; thin should be near top
        worst = rep["worst_hosts"]
        assert worst, "expected at least one worst host"
        # dg-thin (scored 0.2) ranks ahead of any None-confidence (not-rebuilt)
        # hosts in the estate; assert membership + its missing fields.
        thin_row = next((h for h in worst if h["hostname"] == "dg-thin"), None)
        assert thin_row is not None, "dg-thin should be in worst_hosts"
        assert "role" in thin_row["missing"]
        assert isinstance(rep["summary"], str) and "characterized" in rep["summary"]
    finally:
        _cleanup(sids, app_ids=["dg-app1", "dg-noprofile"])
        st.close()


def test_backend_data_gaps_endpoint():
    st = _store()
    sid = _new_id("srv")
    try:
        s = Server(id=sid, hostname="dg-api", app_ids=["dg-api-app"],
                   assessment_confidence=0.1)
        st.upsert_server(s)
        r = client.get("/api/data-gaps")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "confidence" in body and "missing" in body and "worst_hosts" in body
        assert body["total_servers"] >= 1
        assert body["confidence"][CONF_LOW] >= 1
    finally:
        _cleanup([sid], app_ids=["dg-api-app"])
        st.close()