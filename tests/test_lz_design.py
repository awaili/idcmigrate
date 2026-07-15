"""F8 (Phase A) — operator-authored LZ design: deterministic validator, the
lz_designs store layer, active-design resolution, and the backend endpoints.

The LLM interview that PRODUCES a design is Phase B — here we prove the
foundation is sound and that the gate/emit/archetypes surface switch to the
operator's design when one is activated (and stay on the built-in default
otherwise). The built-in LZ_BLUEPRINTS is always the floor (strict superset).
"""
import pytest

from idc.core.lz import (
    LZ_BLUEPRINTS, LZ_CORP, LZ_DMZ, LZ_ONLINE, LZ_ARCHETYPES,
    validate_lz_design, default_lz_design,
)
from idc.core.models import LZDesign


def _arch(cidr, to=None):
    """A minimal-but-valid archetype blueprint for validator tests."""
    return {
        "archetype": "x",
        "summary": "test",
        "vpc": {"name": "vpc", "cidr": cidr, "description": "x"},
        "peering": dict(to or {}),
        "internet_egress": {"nat": False, "public_clb": False},
        "security_groups": ["sg-x"],
        "cam_roles": ["role-x"],
        "tag_policy": {"env": "required"},
        "policy_as_code": ["require env tag"],
    }


# -- validate_lz_design ------------------------------------------------------
def test_validate_accepts_builtin_design():
    assert validate_lz_design(default_lz_design())["ok"] is True


def test_validate_rejects_empty_archetypes():
    d = LZDesign(design_id="d", name="d", archetypes={})
    v = validate_lz_design(d)
    assert v["ok"] is False
    assert any("no archetypes" in e for e in v["errors"])


def test_validate_rejects_missing_required_keys():
    bp = _arch("10.10.0.0/16")
    bp.pop("security_groups")              # missing required key
    bp["policy_as_code"] = []             # empty policy_as_code
    d = LZDesign(design_id="d", name="d", archetypes={"corp": bp})
    v = validate_lz_design(d)
    assert v["ok"] is False
    assert any("missing required key 'security_groups'" in e for e in v["errors"])
    assert any("policy_as_code must be non-empty" in e for e in v["errors"])


def test_validate_rejects_cidr_overlap_between_archetypes():
    d = LZDesign(design_id="d", name="d", archetypes={
        "a": _arch("10.10.0.0/16"),
        "b": _arch("10.10.64.0/20"),  # overlaps 10.10.0.0/16
    })
    v = validate_lz_design(d)
    assert v["ok"] is False
    assert any("CIDR overlap" in e for e in v["errors"])


def test_validate_rejects_cidr_overlap_with_onprem():
    d = LZDesign(design_id="d", name="d", archetypes={"a": _arch("172.16.0.0/16")},
                 onprem_cidrs=["172.16.0.0/12"])  # on-prem contains the VPC
    v = validate_lz_design(d)
    assert v["ok"] is False
    assert any("on-prem" in e and "overlap" in e for e in v["errors"])


def test_validate_rejects_invalid_cidr():
    d = LZDesign(design_id="d", name="d", archetypes={"a": _arch("not-a-cidr")})
    v = validate_lz_design(d)
    assert v["ok"] is False
    assert any("invalid vpc.cidr" in e for e in v["errors"])


def test_validate_rejects_asymmetric_peering_in_custom_design():
    # a.to_b=True but b has no to_a (False) -> asymmetric
    d = LZDesign(design_id="d", name="d", archetypes={
        "a": _arch("10.10.0.0/16", to={"to_b": True}),
        "b": _arch("10.20.0.0/16", to={}),
    })
    v = validate_lz_design(d)
    assert v["ok"] is False
    assert any("asymmetric peering" in e for e in v["errors"])


def test_validate_accepts_symmetric_custom_design():
    d = LZDesign(design_id="d", name="d", archetypes={
        "hub": _arch("10.10.0.0/16", to={"to_spoke": True}),
        "spoke": _arch("10.20.0.0/16", to={"to_hub": True}),
    })
    assert validate_lz_design(d)["ok"] is True


# -- DB-backed: store round-trip + single-active invariant + gate switching --
pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient  # noqa: E402
import idc.backend.app as m  # noqa: E402
from idc.core.db import open_store  # noqa: E402
from idc.config import get_settings  # noqa: E402
from idc.core.models import _new_id, Match, Server, Target, Wave  # noqa: E402
from idc.core.models import STAGE_APP, STAGE_LZ  # noqa: E402

client = TestClient(m.app)


def _design_body(archetypes, onprem_cidrs=None, name="custom"):
    return {"name": name, "archetypes": archetypes,
            "onprem_cidrs": onprem_cidrs or [], "updated_by": "test"}


def _cleanup_designs(ids):
    st = open_store(get_settings().db_url)
    with st.tx() as cur:
        for did in ids:
            cur.execute(st._x("DELETE FROM lz_designs WHERE design_id=?"), (did,))
    st.close()


def test_store_roundtrip_and_single_active_invariant():
    st = open_store(get_settings().db_url)
    ids = []
    try:
        d1 = LZDesign(design_id=_new_id("lzd"), name="d1",
                      archetypes={"corp": _arch("10.10.0.0/16")})
        d2 = LZDesign(design_id=_new_id("lzd"), name="d2",
                      archetypes={"corp": _arch("10.20.0.0/16")})
        ids = [d1.design_id, d2.design_id]
        st.upsert_lz_design(d1)
        st.upsert_lz_design(d2)
        assert len(st.list_lz_designs()) >= 2
        assert st.get_active_lz_design() is None  # none active yet
        st.set_active_lz_design(d1.design_id, updated_by="test")
        assert st.get_active_lz_design().design_id == d1.design_id
        # activating d2 must DEACTIVATE d1 (single-active invariant)
        st.set_active_lz_design(d2.design_id, updated_by="test")
        active = st.get_active_lz_design()
        assert active.design_id == d2.design_id
        got = st.get_lz_design(d1.design_id)
        assert got is not None and got.is_active is False
    finally:
        _cleanup_designs(ids)
        st.close()


def test_archetypes_endpoint_falls_back_to_builtin_when_no_active_design():
    r = client.get("/api/lz/archetypes")
    assert r.status_code == 200
    body = r.json()
    assert set(body["archetypes"].keys()) == set(LZ_ARCHETYPES)
    assert body["design"]["is_builtin"] is True


def test_active_design_drives_archetypes_readiness_and_gate():
    """Activating a custom design (built-in NAMES so the classifier still maps
    servers to them) switches /api/lz/archetypes blueprints + the gate to the
    operator's design, while keeping the built-in classifier working."""
    # a custom design using built-in names but a different corp CIDR
    archs = {
        LZ_CORP: dict(LZ_BLUEPRINTS[LZ_CORP], vpc={
            "name": "vpc-corp-custom", "cidr": "10.99.0.0/16",
            "description": "operator-authored corp"}),
        LZ_ONLINE: LZ_BLUEPRINTS[LZ_ONLINE],
        LZ_DMZ: LZ_BLUEPRINTS[LZ_DMZ],
    }
    # seed a workload server (web -> online) + waves so the gate is exercisable
    st = open_store(get_settings().db_url)
    app_sid = _new_id("srv")
    lz_sid = _new_id("srv")
    lz_wid = _new_id("wave")
    app_wid = _new_id("wave")
    did = _new_id("lzd")
    try:
        st.upsert_server(Server(id=app_sid, hostname=f"app-{app_sid[-4:]}",
                                ips=["10.9.0.2"], role="web", app_ids=[], tags=[]))
        st.upsert_server(Server(id=lz_sid, hostname=f"lz-{lz_sid[-4:]}",
                                ips=["10.9.0.1"], role="k8s", app_ids=[], tags=[]))
        st.upsert_match(Match(server_id=app_sid,
                              target=Target(product="CVM", region="ap-bangkok"),
                              confidence=0.9))
        st.upsert_wave(Wave(id=lz_wid, name="LZ", stage=STAGE_LZ,
                            server_ids=[lz_sid], status="planned"))
        st.upsert_wave(Wave(id=app_wid, name="App", stage=STAGE_APP,
                            server_ids=[app_sid], depends_on=[lz_wid],
                            status="planned"))
        # create + activate the custom design via the endpoints
        cr = client.post("/api/lz/designs", json=_design_body(archs, name="custom"))
        assert cr.status_code == 200, cr.text
        # the endpoint assigns a design_id; fetch it from the list
        listed = client.get("/api/lz/designs").json()
        custom = next(d for d in listed["designs"] if d["name"] == "custom")
        assert custom["is_active"] is False
        ar = client.post(f"/api/lz/designs/{custom['design_id']}/activate",
                         json={"by": "test"})
        assert ar.status_code == 200, ar.text
        # /api/lz/archetypes now reflects the custom corp CIDR
        body = client.get("/api/lz/archetypes").json()
        assert body["design"]["is_builtin"] is False
        assert body["archetypes"][LZ_CORP]["vpc"]["cidr"] == "10.99.0.0/16"
        # the gate still blocks the app wave (online not finalized) — and the
        # archetype_mix reflects the built-in classifier (web -> online)
        gate = client.get(f"/api/waves/{app_wid}/lz-gate").json()
        assert gate["ok"] is False
        assert LZ_ONLINE in gate["blocking_archetypes"]
        assert gate["archetype_mix"].get(LZ_ONLINE, 0) == 1
        # finalize online via the status endpoint (now validated against the
        # active design's archetypes, which still include online)
        sr = client.post(f"/api/lz/{LZ_ONLINE}/status",
                         json={"status": "finalized", "by": "test"})
        assert sr.status_code == 200, sr.text
        gate2 = client.get(f"/api/waves/{app_wid}/lz-gate").json()
        assert gate2["ok"] is True
    finally:
        # remove the custom design (deactivate first by activating builtin? we
        # can't activate builtin — clear active by deleting the row directly)
        st2 = open_store(get_settings().db_url)
        with st2.tx() as cur:
            cur.execute(st2._x("UPDATE lz_designs SET is_active=0"))
            cur.execute(st2._x("DELETE FROM lz_designs WHERE name='custom'"))
            cur.execute(st2._x("DELETE FROM lz_status WHERE archetype=?"),
                         (LZ_ONLINE,))
            for wid in (app_wid, lz_wid):
                cur.execute(st2._x("DELETE FROM waves WHERE id=?"), (wid,))
            for sid in (app_sid, lz_sid):
                cur.execute(st2._x("DELETE FROM matches WHERE server_id=?"), (sid,))
                cur.execute(st2._x("DELETE FROM servers WHERE id=?"), (sid,))
        st2.close()
        st.close()


def test_create_invalid_design_is_rejected_422():
    # overlapping CIDRs -> 422 with the error list
    body = _design_body({"a": _arch("10.10.0.0/16"), "b": _arch("10.10.0.0/24")})
    r = client.post("/api/lz/designs", json=body)
    assert r.status_code == 422
    assert "overlap" in r.text


def test_set_lz_status_rejects_archetype_not_in_active_design():
    # activate a custom design with a non-builtin archetype name, then ensure
    # 'corp' (not in it) is rejected and the custom name is accepted.
    hub = dict(_arch("10.10.0.0/16", to={"to_spoke": True}), archetype="hub")
    archs = {"hub": hub, "spoke": _arch("10.20.0.0/16", to={"to_hub": True})}
    cr = client.post("/api/lz/designs", json=_design_body(archs, name="custom2"))
    assert cr.status_code == 200, cr.text
    listed = client.get("/api/lz/designs").json()
    custom = next(d for d in listed["designs"] if d["name"] == "custom2")
    client.post(f"/api/lz/designs/{custom['design_id']}/activate", json={"by": "t"})
    try:
        # 'corp' is not in the active design -> 400
        assert client.post("/api/lz/corp/status",
                           json={"status": "finalized"}).status_code == 400
        # 'hub' IS in the active design -> 200
        assert client.post("/api/lz/hub/status",
                           json={"status": "finalized"}).status_code == 200
    finally:
        st = open_store(get_settings().db_url)
        with st.tx() as cur:
            cur.execute(st._x("UPDATE lz_designs SET is_active=0"))
            cur.execute(st._x("DELETE FROM lz_designs WHERE name='custom2'"))
            cur.execute(st._x("DELETE FROM lz_status WHERE archetype=?"), ("hub",))
        st.close()

def test_store_lz_design_summary_roundtrips():
    """The AI ``summary`` column persists + reads back (pre-AI rows stay '')."""
    st = open_store(get_settings().db_url)
    did = _new_id("lzd")
    try:
        d = LZDesign(design_id=did, name="with-summary",
                     summary="MigraQ: hub-and-spoke, 3 workload accounts",
                     archetypes={"corp": _arch("10.10.0.0/16")})
        st.upsert_lz_design(d)
        got = st.get_lz_design(did)
        assert got is not None
        assert got.summary == "MigraQ: hub-and-spoke, 3 workload accounts"
        # an unset summary stays empty (not None) after a round-trip
        d2 = LZDesign(design_id=_new_id("lzd"), name="no-summary",
                      archetypes={"corp": _arch("10.20.0.0/16")})
        st.upsert_lz_design(d2)
        assert st.get_lz_design(d2.design_id).summary == ""
    finally:
        _cleanup_designs([did])
        st.close()


def test_lz_designs_list_surfaces_summary():
    """POST /api/lz/designs with a summary -> GET /api/lz/designs returns it so
    the Saved designs table can show the AI description column."""
    body = _design_body({"corp": _arch("10.30.0.0/16")}, name="sum-ep")
    body["summary"] = "MigraQ authored this design for a regulated estate"
    cr = client.post("/api/lz/designs", json=body)
    assert cr.status_code == 200, cr.text
    try:
        listed = client.get("/api/lz/designs").json()
        row = next(d for d in listed["designs"] if d["design_id"] == cr.json()["design_id"])
        assert row["summary"] == "MigraQ authored this design for a regulated estate"
    finally:
        _cleanup_designs([cr.json()["design_id"]])
