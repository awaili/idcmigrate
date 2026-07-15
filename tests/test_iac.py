"""F5 — IaC generation: landing-zone/workload Terraform + Well-Architected
guardrail checks, and the launch gate that consumes them.

Covers the IaCArtifact/GuardrailCheck models, ``codeintel.check_iac_guardrails``
(the gate verdict), the mock executor's HCL emit + guardrail pass/fail, the
DB-backed push/retrieve endpoints, and the ``check_lz_gate`` integration
(guardrails block launch only when an artifact exists — back-compat).
"""
import pytest

from idc.core.codeintel import check_iac_guardrails, iac_guardrail_summary
from idc.core.models import (
    GR_FAIL, GR_PASS, GR_WARN, GuardrailCheck, IaCArtifact,
    IAC_SCOPE_LZ, IAC_SCOPE_WORKLOAD, PILLAR_COST, PILLAR_SECURITY,
)


# -- guardrail aggregation --------------------------------------------------
def _art(grs):
    return IaCArtifact(scope_id="lz:corp", scope=IAC_SCOPE_LZ, guardrails=grs)


def test_check_guardrails_pass_when_no_fails():
    a = _art([GuardrailCheck(pillar=PILLAR_SECURITY, rule="r1", status=GR_PASS,
                            severity="high")])
    v = check_iac_guardrails(a)
    assert v["ok"] is True
    assert v["failing"] == []


def test_check_guardrails_blocks_on_medium_or_high_fail():
    a = _art([
        GuardrailCheck(pillar=PILLAR_SECURITY, rule="r1", status=GR_PASS,
                       severity="high"),
        GuardrailCheck(pillar=PILLAR_COST, rule="required_tags", status=GR_FAIL,
                       finding="vpc-corp missing cost_center tag", severity="medium"),
    ])
    v = check_iac_guardrails(a)
    assert v["ok"] is False
    assert len(v["failing"]) == 1
    assert v["failing"][0].rule == "required_tags"


def test_check_guardrails_low_fail_does_not_block():
    """A low-severity fail is a warning, not a blocking failure."""
    a = _art([GuardrailCheck(pillar=PILLAR_COST, rule="r", status=GR_FAIL,
                             severity="low")])
    assert check_iac_guardrails(a)["ok"] is True


def test_check_guardrails_warn_does_not_block():
    a = _art([GuardrailCheck(pillar=PILLAR_SECURITY, rule="r", status=GR_WARN)])
    v = check_iac_guardrails(a)
    assert v["ok"] is True
    assert v["warning_count"] == 1


def test_check_guardrails_none_artifact_is_noop():
    v = check_iac_guardrails(None)
    assert v["ok"] is True
    assert v["no_artifact"] is True


def test_iac_guardrail_summary():
    assert iac_guardrail_summary(None) == ""
    a = _art([])
    assert iac_guardrail_summary(a) == "guardrails PASS"
    a2 = _art([GuardrailCheck(pillar=PILLAR_COST, rule="r", status=GR_FAIL,
                              severity="medium")])
    assert "1 guardrail fail" in iac_guardrail_summary(a2)


def test_iac_artifact_roundtrip():
    a = IaCArtifact(scope_id="lz:corp", scope=IAC_SCOPE_LZ,
                   modules=[{"path": "main.tf", "content": "resource ..."}],
                   guardrails=[GuardrailCheck(pillar=PILLAR_SECURITY, rule="r",
                                             status=GR_PASS)],
                   guardrail_pass=True, plan_summary="Plan: 2 to add",
                   target={"cloud": "tencent", "region": "ap-bangkok"})
    rt = IaCArtifact.from_dict(a.to_dict())
    assert rt.scope_id == "lz:corp"
    assert rt.modules[0]["path"] == "main.tf"
    assert rt.guardrails[0].status == GR_PASS
    assert rt.target["region"] == "ap-bangkok"


# -- mock HCL emit + guardrails --------------------------------------------
def test_mock_iac_emits_tencentcloud_resources_and_mixed_guardrails():
    from idc.executor_mock.app import _fake_iac_artifact
    art = _fake_iac_artifact("landing_zone", "lz:corp",
                             {"blueprint": {"vpc": {"name": "vpc-corp",
                                                    "cidr": "10.10.0.0/16"},
                                            "policy_as_code": ["deny public CLB in corp"]}},
                             "sc")
    main_tf = next(m["content"] for m in art["modules"] if m["path"] == "main.tf")
    assert 'resource "tencentcloud_vpc" "main"' in main_tf
    assert "10.10.0.0/16" in main_tf
    statuses = {g["status"] for g in art["guardrails"]}
    assert GR_PASS in statuses and GR_WARN in statuses   # pass + a non-blocking warn
    # the mock default is PASSABLE (warns don't block) so the demo can launch;
    # the blocking-fail path is exercised by directly pushing a medium-fail.
    assert art["guardrail_pass"] is True


def test_mock_iac_workload_scope():
    from idc.executor_mock.app import _fake_iac_artifact
    art = _fake_iac_artifact("workload", "wl:srv-1", {"match": {"product": "CVM"}},
                             "sc")
    assert art["scope"] == "workload"
    assert art["scope_id"] == "wl:srv-1"


# -- DB-backed push + retrieve endpoint --------------------------------------
pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient
import idc.backend.app as bm
from idc.core.db import open_store
from idc.config import get_settings

client = TestClient(bm.app)


@pytest.fixture(autouse=True)
def _executor_token(monkeypatch):
    monkeypatch.setattr(bm.settings, "executor_token", "test-token")


def _bearer():
    return {"Authorization": "Bearer test-token"}


def _cleanup(scope_id):
    st = open_store(get_settings().db_url)
    with st.tx() as cur:
        cur.execute(st._x("DELETE FROM iac_artifacts WHERE scope_id=?"), (scope_id,))
    st.close()


def test_put_and_get_iac_artifact_endpoint():
    body = {"scope_id": "lz:corp", "scope": "landing_zone",
            "modules": [{"path": "main.tf", "content": "resource ..."}],
            "guardrails": [
                {"pillar": "security", "rule": "no_public_clb", "status": "pass",
                 "finding": "", "severity": "high"},
                {"pillar": "cost", "rule": "required_tags", "status": "fail",
                 "finding": "vpc-corp missing cost_center", "severity": "medium"}],
            "guardrail_pass": False, "plan_summary": "Plan: 2 to add",
            "target": {"cloud": "tencent", "region": "ap-bangkok"},
            "scan_id": "sc-1", "summary": "IaC for lz:corp"}
    try:
        r = client.put("/api/iac-artifacts/lz:corp", json=body, headers=_bearer())
        assert r.status_code == 200, r.text
        assert r.json()["guardrail_pass"] is False
        g = client.get("/api/iac-artifacts/lz:corp").json()
        assert g["scope"] == "landing_zone"
        assert len(g["guardrails"]) == 2
        assert g["guardrails"][1]["status"] == "fail"
        assert g["target"]["region"] == "ap-bangkok"
        assert any(a["scope_id"] == "lz:corp"
                   for a in client.get("/api/iac-artifacts").json())
    finally:
        _cleanup("lz:corp")


def test_put_iac_rejects_invalid_scope():
    r = client.put("/api/iac-artifacts/x:y", json={"scope_id": "x:y",
                   "scope": "bogus"}, headers=_bearer())
    assert r.status_code == 400


def test_get_iac_unknown_404():
    assert client.get("/api/iac-artifacts/lz:ghost").status_code == 404


# -- check_lz_gate integration (guardrails block launch) -------------------
def _seed_archetype_server(host, arch_tag=None, role="app"):
    """Seed a server whose archetype is determined by tag/role."""
    st = open_store(get_settings().db_url)
    from idc.core.models import Server, _new_id
    sid = _new_id("srv")
    tags = [arch_tag] if arch_tag else []
    s = Server(id=sid, hostname=host, fqdn=f"{host}.dc1", ips=["10.0.0.5"],
               role=role, app_ids=[], sizing_basis="estimated", tags=tags)
    st.upsert_server(s)
    st.close()
    return sid


def _cleanup_server(sid, scope_id=None):
    st = open_store(get_settings().db_url)
    with st.tx() as cur:
        if scope_id:
            cur.execute(st._x("DELETE FROM iac_artifacts WHERE scope_id=?"), (scope_id,))
        cur.execute(st._x("DELETE FROM servers WHERE id=?"), (sid,))
    st.close()


def test_check_lz_gate_blocks_when_guardrails_failing():
    """A finalized archetype with a guardrail-failing IaC artifact blocks the
    wave from launching — the real guardrail gate, not just the operator flag."""
    from idc.core.lz import check_lz_gate, LZ_FINALIZED
    from idc.core.models import Server
    sid = _seed_archetype_server("corp-app-1", role="app")   # internal role -> corp
    try:
        st = open_store(get_settings().db_url)
        # mark the corp archetype finalized (the old gate would pass here)
        st.set_lz_status("corp", LZ_FINALIZED)
        # push a guardrail-FAILING IaC artifact for lz:corp
        st.upsert_iac_artifact(IaCArtifact(
            scope_id="lz:corp", scope=IAC_SCOPE_LZ,
            guardrails=[GuardrailCheck(pillar=PILLAR_COST, rule="tags",
                                       status=GR_FAIL, severity="medium",
                                       finding="missing tag")],
            guardrail_pass=False))
        servers = st.list_all_servers()
        # build a wave containing the corp server
        from idc.core.models import Wave, STAGE_APP
        w = Wave(name="W app", stage=STAGE_APP, server_ids=[sid])
        st.replace_waves([w])
        gate = check_lz_gate(st, w.id, servers, [])
        st.close()
        assert gate["ok"] is False
        assert "corp" in gate["guardrail_blocked"]
        assert "guardrails failing" in gate["reason"]
    finally:
        # reset corp status to not_ready so other tests aren't affected
        st = open_store(get_settings().db_url)
        st.set_lz_status("corp", "not_ready")
        with st.tx() as cur:
            cur.execute(st._x("DELETE FROM waves WHERE id=?"), (sid,)) if False else None
        st.close()
        _cleanup_server(sid, "lz:corp")


def test_check_lz_gate_passes_when_guardrails_pass():
    from idc.core.lz import check_lz_gate, LZ_FINALIZED
    sid = _seed_archetype_server("corp-app-2", role="app")
    try:
        st = open_store(get_settings().db_url)
        st.set_lz_status("corp", LZ_FINALIZED)
        st.upsert_iac_artifact(IaCArtifact(
            scope_id="lz:corp", scope=IAC_SCOPE_LZ,
            guardrails=[GuardrailCheck(pillar=PILLAR_SECURITY, rule="r",
                                       status=GR_PASS, severity="high")],
            guardrail_pass=True))
        servers = st.list_all_servers()
        from idc.core.models import Wave, STAGE_APP
        w = Wave(name="W app", stage=STAGE_APP, server_ids=[sid])
        st.replace_waves([w])
        gate = check_lz_gate(st, w.id, servers, [])
        st.close()
        assert gate["ok"] is True
    finally:
        st = open_store(get_settings().db_url)
        st.set_lz_status("corp", "not_ready")
        st.close()
        _cleanup_server(sid, "lz:corp")


def test_check_lz_gate_no_artifact_falls_back_to_finalized_flag():
    """No IaC artifact → the finalized flag alone decides (back-compat with the
    pre-F5 behavior). A finalized archetype with no artifact passes."""
    from idc.core.lz import check_lz_gate, LZ_FINALIZED
    sid = _seed_archetype_server("corp-app-3", role="app")
    try:
        st = open_store(get_settings().db_url)
        st.set_lz_status("corp", LZ_FINALIZED)
        # no iac artifact pushed
        servers = st.list_all_servers()
        from idc.core.models import Wave, STAGE_APP
        w = Wave(name="W app", stage=STAGE_APP, server_ids=[sid])
        st.replace_waves([w])
        gate = check_lz_gate(st, w.id, servers, [])
        st.close()
        assert gate["ok"] is True
    finally:
        st = open_store(get_settings().db_url)
        st.set_lz_status("corp", "not_ready")
        st.close()
        _cleanup_server(sid)