"""F8 — Landing Zone parameterized blueprint + archetype placement gate.

Covers the deterministic core: ``archetype_for`` classification (tag > role >
default), the LZ blueprint definitions (Tencent VPC/CAM/SG/tag-policy per
archetype), ``lz_readiness`` (derived from the LZ wave's MigrationJobs), and
``check_lz_gate`` (wave-launch placement gate). Plus the backend endpoints
and the F6 ``launch_wave`` LZ-gate wiring (warn vs enforce).
"""
import pytest

from idc.core.lz import (
    LZ_BLUEPRINTS, LZ_CORP, LZ_DMZ, LZ_ONLINE, LZ_ARCHETYPES,
    TAG_CORP, TAG_DMZ, TAG_ONLINE,
    archetype_for, archetypes_for_servers, wave_archetypes,
)
from idc.core.models import Match, Server, Target


def _srv(role="", tags=None, sid="s1"):
    return Server(id=sid, hostname=f"host-{sid}", fqdn=f"{sid}.dc1",
                  ips=["10.0.0.1"], role=role, tags=tags or [])


def _mat(server):
    return Match(server_id=server.id, target=Target(product="CVM", region="ap-shanghai"),
                 confidence=0.9, method="rule", rationale="x")


# -- classifier --------------------------------------------------------------
def test_explicit_tag_wins_over_role():
    assert archetype_for(_srv(role="db", tags=[TAG_DMZ])) == LZ_DMZ
    assert archetype_for(_srv(role="web", tags=[TAG_CORP])) == LZ_CORP
    assert archetype_for(_srv(role="db", tags=[TAG_ONLINE])) == LZ_ONLINE


def test_regulated_hints_route_to_dmz():
    for hint in ("regulated", "pci", "hipaa", "sox", "sensitive", "isolated"):
        assert archetype_for(_srv(role="app", tags=[hint])) == LZ_DMZ


def test_web_role_routes_to_online():
    assert archetype_for(_srv(role="web")) == LZ_ONLINE
    assert archetype_for(_srv(role="frontend")) == LZ_ONLINE


def test_default_is_corp():
    assert archetype_for(_srv(role="db")) == LZ_CORP
    assert archetype_for(_srv(role="app")) == LZ_CORP
    assert archetype_for(_srv()) == LZ_CORP


def test_tag_precedence_dmz_over_online():
    # both dmz + online tags -> dmz wins (checked first)
    assert archetype_for(_srv(tags=[TAG_ONLINE, TAG_DMZ])) == LZ_DMZ


def test_archetypes_for_servers_batch():
    ss = [_srv(role="web", sid="a"), _srv(role="db", sid="b"),
          _srv(tags=[TAG_DMZ], sid="c")]
    out = archetypes_for_servers(ss)
    assert out == {"a": LZ_ONLINE, "b": LZ_CORP, "c": LZ_DMZ}


# -- blueprint definitions --------------------------------------------------
def test_blueprints_cover_all_archetypes_with_tencent_resources():
    for a in LZ_ARCHETYPES:
        bp = LZ_BLUEPRINTS[a]
        assert bp["archetype"] == a
        # every archetype has the Tencent placement primitives
        assert "vpc" in bp and "peering" in bp and "security_groups" in bp
        assert "cam_roles" in bp and "tag_policy" in bp
        assert "policy_as_code" in bp and len(bp["policy_as_code"]) >= 1


def test_dmz_blueprint_is_isolated():
    dmz = LZ_BLUEPRINTS[LZ_DMZ]
    assert dmz["internet_egress"]["nat"] is False
    assert dmz["peering"]["to_dc"] is False
    assert dmz["peering"]["to_online"] is False


def test_online_blueprint_has_public_ingress():
    online = LZ_BLUEPRINTS[LZ_ONLINE]
    assert online["internet_egress"]["public_clb"] is True
    assert online["internet_egress"]["cdn"] is True


# -- wave_archetypes mix ----------------------------------------------------
def test_wave_archetypes_mix():
    ss = [_srv(role="web", sid="a"), _srv(role="db", sid="b"),
          _srv(tags=[TAG_DMZ], sid="c"), _srv(role="app", sid="d")]
    mix = wave_archetypes(["a", "b", "c", "d"], ss)
    assert mix == {LZ_CORP: 2, LZ_ONLINE: 1, LZ_DMZ: 1}


# -- DB-backed: readiness + gate + launch wiring -----------------------------
pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient
import idc.backend.app as m
from idc.core.db import open_store
from idc.config import get_settings
from idc.core.models import _new_id, Wave, STAGE_LZ, STAGE_APP
from idc.core.execute import launch_wave, LZNotReady

client = TestClient(m.app)


def _setup_lz_and_workload(app_role="web", app_tag=None):
    """Seed one LZ server (k8s) + one workload server, build LZ + app waves.
    Returns (lz_wave_id, app_wave_id, lz_sid, app_sid). The app wave's archetype
    is determined by app_role/app_tag (web -> online, db -> corp, dmz tag -> dmz)."""
    st = open_store(get_settings().db_url)
    lz_sid = _new_id("srv")
    app_sid = _new_id("srv")
    lz_s = Server(id=lz_sid, hostname=f"lz-{lz_sid[-4:]}", ips=["10.99.0.1"],
                  role="k8s", app_ids=[], tags=[])
    app_s = Server(id=app_sid, hostname=f"app-{app_sid[-4:]}", ips=["10.99.0.2"],
                   role=app_role, app_ids=[], tags=[app_tag] if app_tag else [])
    st.upsert_server(lz_s)
    st.upsert_server(app_s)
    st.upsert_match(Match(server_id=app_sid, target=Target(product="CVM", spec="SA5.SMALL2",
                          region="ap-shanghai"), confidence=0.9))
    lz_wid = _new_id("wave")
    app_wid = _new_id("wave")
    st.upsert_wave(Wave(id=lz_wid, name="LZ", stage=STAGE_LZ, server_ids=[lz_sid],
                        status="planned"))
    st.upsert_wave(Wave(id=app_wid, name="App", stage=STAGE_APP, server_ids=[app_sid],
                        depends_on=[lz_wid], status="planned"))
    st.close()
    return lz_wid, app_wid, lz_sid, app_sid


def _cleanup(wave_ids, sids, job_ids, archetypes=None):
    st = open_store(get_settings().db_url)
    with st.tx() as cur:
        for jid in job_ids:
            cur.execute(st._x("DELETE FROM migration_jobs WHERE id=?"), (jid,))
        for wid in wave_ids:
            cur.execute(st._x("DELETE FROM waves WHERE id=?"), (wid,))
        for sid in sids:
            cur.execute(st._x("DELETE FROM matches WHERE server_id=?"), (sid,))
            cur.execute(st._x("DELETE FROM servers WHERE id=?"), (sid,))
        for a in (archetypes or []):
            cur.execute(st._x("DELETE FROM lz_status WHERE archetype=?"), (a,))
    st.close()


def _all_servers_matches():
    st = open_store(get_settings().db_url)
    try:
        return st.list_all_servers(), st.list_matches(), st
    except Exception:
        st.close()
        raise


def test_lz_readiness_defaults_not_ready():
    lz_wid, app_wid, lz_sid, app_sid = _setup_lz_and_workload(app_role="web")
    job_ids = []
    try:
        servers, matches, st = _all_servers_matches()
        from idc.core.lz import lz_readiness, LZ_NOT_READY
        rdy = lz_readiness(st, servers, matches)
        # no lz_status rows -> every archetype defaults to not_ready
        for a in LZ_ARCHETYPES:
            assert rdy[a]["status"] == LZ_NOT_READY
            assert rdy[a]["ready"] is False
        # workload_count reflects the estate classification (app is web -> online)
        assert rdy[LZ_ONLINE]["workload_count"] >= 1
        st.close()
    finally:
        _cleanup([lz_wid, app_wid], [lz_sid, app_sid], job_ids)


def test_lz_gate_blocks_workload_wave_until_archetype_finalized():
    lz_wid, app_wid, lz_sid, app_sid = _setup_lz_and_workload(app_role="web")
    job_ids = []
    try:
        servers, matches, st = _all_servers_matches()
        from idc.core.lz import check_lz_gate
        # app wave targets online; online LZ not finalized -> gate blocks
        gate = check_lz_gate(st, app_wid, servers, matches)
        assert gate["ok"] is False
        assert LZ_ONLINE in gate["blocking_archetypes"]
        st.close()
    finally:
        _cleanup([lz_wid, app_wid], [lz_sid, app_sid], job_ids)


def test_launch_wave_enforce_lz_gate_raises_then_passes_after_finalize():
    lz_wid, app_wid, lz_sid, app_sid = _setup_lz_and_workload(app_role="web")
    job_ids = []
    try:
        servers, matches, st = _all_servers_matches()
        # app wave targets online; online LZ not ready -> enforce raises
        with pytest.raises(LZNotReady):
            launch_wave(st, app_wid, servers, matches, workloads=[],
                        kind="host", enforce_lz_gate=True)
        # non-enforce mode launches with a warning in stage_history
        jobs = launch_wave(st, app_wid, servers, matches, workloads=[],
                           kind="host", enforce_lz_gate=False)
        job_ids = [j.id for j in jobs]
        assert jobs and jobs[0].status == "planned"
        notes = " ".join(h.get("note", "") for h in jobs[0].stage_history)
        assert "LZ gate warning" in notes
        st.close()
    finally:
        _cleanup([lz_wid, app_wid], [lz_sid, app_sid], job_ids, archetypes=[LZ_ONLINE])


def test_lz_gate_passes_when_archetype_finalized():
    lz_wid, app_wid, lz_sid, app_sid = _setup_lz_and_workload(app_role="web")
    job_ids = []
    try:
        servers, matches, st = _all_servers_matches()
        from idc.core.lz import check_lz_gate, LZ_FINALIZED
        # mark the online archetype's LZ finalized -> gate now passes
        st.set_lz_status(LZ_ONLINE, LZ_FINALIZED, updated_by="test")
        gate = check_lz_gate(st, app_wid, servers, matches)
        assert gate["ok"] is True, gate
        assert gate["blocking_archetypes"] == []
        # and enforce-mode launch no longer raises
        jobs = launch_wave(st, app_wid, servers, matches, workloads=[],
                           kind="host", enforce_lz_gate=True)
        job_ids = [j.id for j in jobs]
        assert jobs
        st.close()
    finally:
        _cleanup([lz_wid, app_wid], [lz_sid, app_sid], job_ids, archetypes=[LZ_ONLINE])


def test_lz_gate_lz_wave_always_ok():
    lz_wid, app_wid, lz_sid, app_sid = _setup_lz_and_workload()
    job_ids = []
    try:
        servers, matches, st = _all_servers_matches()
        from idc.core.lz import check_lz_gate
        gate = check_lz_gate(st, lz_wid, servers, matches)
        assert gate["ok"] is True   # LZ wave is not gated on itself
        st.close()
    finally:
        _cleanup([lz_wid, app_wid], [lz_sid, app_sid], job_ids)


# -- backend endpoints ------------------------------------------------------
def test_lz_archetypes_endpoint():
    r = client.get("/api/lz/archetypes")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["archetypes"].keys()) == set(LZ_ARCHETYPES)
    assert "counts" in body and "per_server" in body


def test_lz_readiness_and_status_endpoints():
    try:
        # default: not_ready
        r = client.get("/api/lz/readiness")
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body.keys()) == set(LZ_ARCHETYPES)
        for a in LZ_ARCHETYPES:
            assert "ready" in body[a] and "status" in body[a]
        # mark dmz finalized via the status endpoint
        r = client.post(f"/api/lz/{LZ_DMZ}/status", json={"status": "finalized", "by": "test"})
        assert r.status_code == 200, r.text
        body = client.get("/api/lz/readiness").json()
        assert body[LZ_DMZ]["ready"] is True
        assert body[LZ_DMZ]["status"] == "finalized"
        # invalid status -> 400
        assert client.post(f"/api/lz/{LZ_DMZ}/status",
                           json={"status": "bogus"}).status_code == 400
    finally:
        st = open_store(get_settings().db_url)
        st.delete_lz_status(LZ_DMZ)
        st.close()


def test_lz_gate_endpoint_unknown_wave():
    r = client.get("/api/waves/wave-nonexistent/lz-gate")
    assert r.status_code == 200
    assert r.json()["ok"] is False