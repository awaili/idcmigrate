"""F7 — legacy / unsupported-OS disposition (containerize/replatform/rewrite/retain).

Covers the eol.apply_os_eol fold-in (reads a stored LegacyDisposition instead
of the fixed "replatform" string), the mock executor's decision logic, the
DB-backed push endpoint, and the rebuild fold-in.
"""
import pytest

from idc.core.eol import apply_os_eol
from idc.core.models import (
    LegacyDisposition, LD_CONTAINERIZE, LD_REPLATFORM, LD_REWRITE, LD_RETAIN,
    LD_RETIRE, LEGACY_DISPOSITIONS, Match, Server, Target, Workload,
)


def _server(os_="centos", ver="6"):
    return Server(hostname="web-1", role="app", os=os_, os_version=ver,
                  business_criticality="high")


def _match():
    return Match(server_id="s", confidence=0.9, rationale="base",
                 target=Target(product="CVM", spec="SA2.LARGE4", region="ap-bangkok"))


# -- fold-in (eol.apply_os_eol with a stored disposition) --------------------
def test_apply_os_eol_without_disposition_uses_fixed_string():
    m = _match()
    apply_os_eol(m, _server())
    assert m.confidence < 0.9                          # EOL dip fired
    assert any("replatform to a supported base image" in a for a in m.alternatives)


def test_apply_os_eol_with_disposition_uses_rec():
    m = _match()
    disp = LegacyDisposition(server_id="web-1", disposition=LD_CONTAINERIZE,
                             rationale="stateless web tier, no repo -> containerize",
                             target_base_image="tencentos-server-3.1:jdk17")
    apply_os_eol(m, _server(), disposition=disp)
    assert any("containerize" in a for a in m.alternatives)
    assert any("stateless web tier" in a for a in m.alternatives)
    assert any("tencentos-server-3.1:jdk17" in a for a in m.alternatives)
    # the fixed string is NOT appended when a real disposition is present
    assert not any("replatform to a supported base image" in a
                   for a in m.alternatives)


def test_apply_os_eol_active_os_is_noop_even_with_disposition():
    m = _match()
    s = Server(hostname="ok", role="app", os="ubuntu", os_version="22")  # active
    disp = LegacyDisposition(server_id="ok", disposition=LD_REPLATFORM)
    apply_os_eol(m, s, disposition=disp)
    assert m.confidence == 0.9                        # no dip
    assert m.alternatives == []                        # no rec appended (no EOL)


def test_apply_os_eol_empty_disposition_falls_back():
    """A disposition object with no .disposition falls back to the fixed string
    (defensive — don't emit an empty recommendation)."""
    m = _match()
    apply_os_eol(m, _server(), disposition=LegacyDisposition())
    assert any("replatform to a supported base image" in a for a in m.alternatives)


def test_legacy_disposition_roundtrip():
    d = LegacyDisposition(server_id="h1", disposition=LD_REWRITE,
                          rationale="tightly coupled", confidence=0.6,
                          target_base_image="", effort_days=25.0,
                          prereqs=["extract schema", "dual-run"])
    rt = LegacyDisposition.from_dict(d.to_dict())
    assert rt.disposition == LD_REWRITE
    assert rt.confidence == 0.6
    assert rt.effort_days == 25.0
    assert rt.prereqs == ["extract schema", "dual-run"]


# -- mock decision logic ----------------------------------------------------
def test_mock_legacy_disposition_stateless_no_repo_containerizes():
    from idc.executor_mock.app import _fake_legacy_disposition
    d = _fake_legacy_disposition("web-1", {"role": "app", "has_source_repo": False,
                                           "os": "centos 6",
                                           "os_eol_bucket": "expired"}, "sc")
    assert d["disposition"] == LD_CONTAINERIZE
    assert d["target_base_image"]


def test_mock_legacy_disposition_stateful_with_repo_rewrites():
    from idc.executor_mock.app import _fake_legacy_disposition
    d = _fake_legacy_disposition("db-1", {"role": "db", "has_source_repo": True,
                                          "os": "oracle linux 6",
                                          "os_eol_bucket": "expired"}, "sc")
    assert d["disposition"] == LD_REWRITE
    assert d["effort_days"] > 10


def test_mock_legacy_disposition_app_with_repo_replatforms():
    from idc.executor_mock.app import _fake_legacy_disposition
    d = _fake_legacy_disposition("app-1", {"role": "app", "has_source_repo": True,
                                           "os": "windows 2008"}, "sc")
    assert d["disposition"] == LD_REPLATFORM


def test_mock_legacy_disposition_regulated_retains():
    from idc.executor_mock.app import _fake_legacy_disposition
    d = _fake_legacy_disposition("main-1", {"role": "app", "has_source_repo": True,
                                           "tags": ["regulated"]}, "sc")
    assert d["disposition"] == LD_RETAIN


def test_legacy_dispositions_vocab():
    assert set(LEGACY_DISPOSITIONS) == {LD_CONTAINERIZE, LD_REPLATFORM,
                                        LD_REWRITE, LD_RETAIN, LD_RETIRE}


# -- DB-backed push endpoint + rebuild fold-in -------------------------------
pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient
import idc.backend.app as bm
from idc.core.db import open_store
from idc.config import get_settings
from idc.core.models import _new_id

client = TestClient(bm.app)


@pytest.fixture(autouse=True)
def _executor_token(monkeypatch):
    monkeypatch.setattr(bm.settings, "executor_token", "test-token")


def _bearer():
    return {"Authorization": "Bearer test-token"}


def _seed_eol_server(host=None):
    st = open_store(get_settings().db_url)
    sid = _new_id("srv")
    host = host or f"eolhost-{sid[-4:]}"
    s = Server(id=sid, hostname=host, fqdn=f"{host}.dc1.corp", ips=["10.0.0.7"],
               role="app", os="centos", os_version="6", app_ids=[],
               sizing_basis="estimated")
    st.upsert_server(s)
    st.close()
    return sid, host


def _cleanup(sid, host):
    st = open_store(get_settings().db_url)
    with st.tx() as cur:
        cur.execute(st._x("DELETE FROM legacy_dispositions WHERE server_id=?"), (host,))
        cur.execute(st._x("DELETE FROM matches WHERE server_id=?"), (sid,))
        cur.execute(st._x("DELETE FROM servers WHERE id=?"), (sid,))
    st.close()


def test_put_and_get_legacy_disposition_endpoint():
    sid, host = _seed_eol_server()
    try:
        body = {"server_id": host, "disposition": LD_CONTAINERIZE,
                "rationale": "stateless", "confidence": 0.7,
                "target_base_image": "tencentos-server-3.1:jdk17",
                "effort_days": 5.0, "prereqs": ["capture runtime inventory"],
                "scan_id": "sc-1", "summary": "containerize"}
        r = client.put(f"/api/legacy-dispositions/{host}", json=body, headers=_bearer())
        assert r.status_code == 200, r.text
        g = client.get(f"/api/legacy-dispositions/{host}").json()
        assert g["disposition"] == LD_CONTAINERIZE
        assert g["prereqs"] == ["capture runtime inventory"]
        assert any(d["server_id"] == host
                   for d in client.get("/api/legacy-dispositions").json())
    finally:
        _cleanup(sid, host)


def test_put_legacy_disposition_rejects_invalid_disposition():
    sid, host = _seed_eol_server()
    try:
        r = client.put(f"/api/legacy-dispositions/{host}",
                       json={"server_id": host, "disposition": "lift-n-shift"},
                       headers=_bearer())
        assert r.status_code == 400
    finally:
        _cleanup(sid, host)


def test_get_legacy_disposition_unknown_404():
    assert client.get("/api/legacy-dispositions/ghost-host").status_code == 404


def test_rebuild_folds_legacy_disposition_into_match():
    """A stored containerize disposition for an EOL host should land in the
    host's match alternatives (via apply_os_eol) on the next rebuild."""
    from idc.core import rebuild, run_ingest
    st = open_store(get_settings().db_url)
    host = "app-orders-01"   # a fixture app host on centos (EOL after rebuild)
    pushed = False
    try:
        run_ingest(st, get_settings(), source="all")
        r = client.put(f"/api/legacy-dispositions/{host}", json={
            "server_id": host, "disposition": LD_CONTAINERIZE,
            "rationale": "stateless web tier -> containerize",
            "confidence": 0.7, "target_base_image": "tencentos-server-3.1:jdk17",
            "effort_days": 5.0, "prereqs": [], "scan_id": "sc-1",
        }, headers=_bearer())
        assert r.status_code == 200, r.text
        pushed = True
        rebuild(st, do_match=True, do_plan=False)
        servers = {s.id: s for s in st.list_all_servers()}
        m = next((x for x in st.list_matches()
                  if servers.get(x.server_id) and
                  servers[x.server_id].hostname.lower() == host), None)
        if m is None:
            pytest.skip("fixture host app-orders-01 not matched on this estate")
        assert any("containerize" in a for a in (m.alternatives or []))
        assert any("stateless web tier" in a for a in (m.alternatives or []))
    finally:
        st.close()
        if pushed:
            _cleanup_disposition(host)


def _cleanup_disposition(host):
    st = open_store(get_settings().db_url)
    with st.tx() as cur:
        cur.execute(st._x("DELETE FROM legacy_dispositions WHERE server_id=?"), (host,))
    st.close()


def _S(sid, host, role="app", apps=None):
    return Server(id=sid, hostname=host, role=role, app_ids=apps or [],
                  business_criticality="high")


# -- F7 retain disposition pulls the host OUT of the migration sequence -----
def test_non_migrating_servers_retain_disposition_excludes_host():
    """A host the executor marked `retain` (regulated/mainframe) is pulled into
    the retain set even when its app has no 7R strategy — the host-level
    override means it does NOT enter an app wave."""
    from idc.core.codeintel import non_migrating_servers
    from idc.core.models import LegacyDisposition as LD
    s = _S("main-1", "main-1", apps=["app-x"])
    wls = [Workload(app_id="app-x", name="x", server_ids=["main-1"], depends_on=[])]
    disp = [LD(server_id="main-1", disposition=LD_RETAIN,
               rationale="regulated mainframe")]
    retain, retire = non_migrating_servers([s], wls, [], [],
                                           legacy_dispositions=disp)
    assert "main-1" in retain


def test_non_migrating_servers_retain_disposition_wins_over_migrating_app():
    """Even when the host's app would migrate (no retain strategy), a
    host-level retain disposition pulls the host out — the app migrates with
    its other hosts (the retain host is excluded from app waves)."""
    from idc.core.codeintel import non_migrating_servers
    from idc.core.models import LegacyDisposition as LD
    s_retain = _S("main-1", "main-1", apps=["app-x"])
    s_other = _S("app-1", "app-1", apps=["app-x"])
    wls = [Workload(app_id="app-x", name="x", server_ids=["main-1", "app-1"],
                   depends_on=[])]
    disp = [LD(server_id="main-1", disposition=LD_RETAIN)]
    retain, retire = non_migrating_servers([s_retain, s_other], wls, [], [],
                                           legacy_dispositions=disp)
    assert "main-1" in retain
    assert "app-1" not in retain      # the other host still migrates


def test_plan_waves_retain_disposition_host_goes_to_retain_wave():
    """End-to-end: a retain-dispositioned host lands in the trailing Retain
    wave, NOT in its app's wave (F7 host-level override)."""
    from idc.core.plan import plan_waves
    from idc.core.models import LegacyDisposition as LD
    s = _S("main-1", "main-1", apps=["app-x"])
    wls = [Workload(app_id="app-x", name="x", server_ids=["main-1"], depends_on=[])]
    disp = [LD(server_id="main-1", disposition=LD_RETAIN)]
    waves = plan_waves([s], wls, legacy_dispositions=disp)
    # the host is in the Retain (on-prem) wave, not an App wave
    in_retain = any("main-1" in (w.server_ids or []) and "Retain" in (w.name or "")
                    for w in waves)
    in_app = any("main-1" in (w.server_ids or []) and "Retain" not in (w.name or "")
                 and "Retire" not in (w.name or "") and "Discovery" not in (w.name or "")
                 for w in waves)
    assert in_retain
    assert not in_app


def test_plan_waves_rewrite_disposition_does_not_exclude():
    """rewrite is a MIGRATING pattern (deferred by pattern_rank at the app
    level) — a rewrite disposition does NOT pull the host out of migration."""
    from idc.core.plan import plan_waves
    from idc.core.models import LegacyDisposition as LD
    s = _S("legacy-1", "legacy-1", apps=["app-x"])
    wls = [Workload(app_id="app-x", name="x", server_ids=["legacy-1"], depends_on=[])]
    disp = [LD(server_id="legacy-1", disposition=LD_REWRITE)]
    waves = plan_waves([s], wls, legacy_dispositions=disp)
    in_retain = any("legacy-1" in (w.server_ids or []) and "Retain" in (w.name or "")
                    for w in waves)
    assert not in_retain   # rewrite stays in the migration sequence


# -- operator retain / retire from the inventory host drawer ------------------
def test_non_migrating_servers_retire_disposition_excludes_host():
    """A host the operator marked `retire` (decommission) is pulled into the
    retire set even when its app would otherwise migrate — the host-level
    override means it does NOT enter an app wave."""
    from idc.core.codeintel import non_migrating_servers
    from idc.core.models import LegacyDisposition as LD
    s = _S("oldbox-1", "oldbox-1", apps=["app-x"])
    wls = [Workload(app_id="app-x", name="x", server_ids=["oldbox-1"], depends_on=[])]
    disp = [LD(server_id="oldbox-1", disposition=LD_RETIRE,
               rationale="operator: sunset redundant host")]
    retain, retire = non_migrating_servers([s], wls, [], [],
                                           legacy_dispositions=disp)
    assert "oldbox-1" in retire
    assert "oldbox-1" not in retain


def test_non_migrating_servers_retire_disposition_no_app_binding():
    """A standalone host with no app binding can still be retired by the operator
    (the app-loop skips app-less hosts, but the host-level override catches it)."""
    from idc.core.codeintel import non_migrating_servers
    from idc.core.models import LegacyDisposition as LD
    s = _S("orphan-1", "orphan-1", apps=[])          # no app binding
    disp = [LD(server_id="orphan-1", disposition=LD_RETIRE)]
    retain, retire = non_migrating_servers([s], [], [], [],
                                           legacy_dispositions=disp)
    assert "orphan-1" in retire


def test_plan_waves_retire_disposition_host_goes_to_retire_wave():
    """End-to-end: a retire-dispositioned host lands in the trailing Retire
    wave, NOT in its app's wave."""
    from idc.core.plan import plan_waves
    from idc.core.models import LegacyDisposition as LD
    s = _S("oldbox-1", "oldbox-1", apps=["app-x"])
    wls = [Workload(app_id="app-x", name="x", server_ids=["oldbox-1"], depends_on=[])]
    disp = [LD(server_id="oldbox-1", disposition=LD_RETIRE)]
    waves = plan_waves([s], wls, legacy_dispositions=disp)
    in_retire = any("oldbox-1" in (w.server_ids or []) and "Retire" in (w.name or "")
                    for w in waves)
    in_app = any("oldbox-1" in (w.server_ids or [])
                 and "Retain" not in (w.name or "") and "Retire" not in (w.name or "")
                 and "Discovery" not in (w.name or "") for w in waves)
    assert in_retire
    assert not in_app


def _seed_server(host=None):
    st = open_store(get_settings().db_url)
    sid = _new_id("srv")
    host = host or f"disphost-{sid[-4:]}"
    s = Server(id=sid, hostname=host, fqdn=f"{host}.dc1.corp", ips=["10.0.0.9"],
               role="app", os="ubuntu", os_version="22", app_ids=[],
               sizing_basis="estimated")
    st.upsert_server(s)
    st.close()
    return sid, host


def _disp_cleanup(host):
    st = open_store(get_settings().db_url)
    with st.tx() as cur:
        cur.execute(st._x("DELETE FROM legacy_dispositions WHERE server_id=?"), (host,))
    st.close()


def test_operator_disposition_endpoint_set_retain_then_retire_then_clear():
    """PUT /api/servers/{sid}/disposition (session-auth, no executor bearer)
    writes retain/retire keyed by hostname and survives a read-back; clearing
    sends "" and removes the row."""
    sid, host = _seed_server()
    try:
        # no auth password configured in tests -> session not required, but the
        # endpoint must NOT need a bearer (operator action, not executor push)
        r = client.put(f"/api/servers/{sid}/disposition",
                       json={"disposition": "retain"})
        assert r.status_code == 200, r.text
        assert r.json()["disposition"] == "retain"
        # the detail endpoint exposes the disposition (keyed by hostname)
        d = client.get(f"/api/servers/{sid}").json()
        assert d["disposition"] == "retain"

        r = client.put(f"/api/servers/{sid}/disposition",
                       json={"disposition": "retire"})
        assert r.status_code == 200, r.text
        assert r.json()["disposition"] == "retire"
        d = client.get(f"/api/servers/{sid}").json()
        assert d["disposition"] == "retire"

        r = client.put(f"/api/servers/{sid}/disposition",
                       json={"disposition": ""})
        assert r.status_code == 200, r.text
        assert r.json()["cleared"] is True
        d = client.get(f"/api/servers/{sid}").json()
        assert d["disposition"] == ""
    finally:
        _disp_cleanup(host)
        st = open_store(get_settings().db_url)
        with st.tx() as cur:
            cur.execute(st._x("DELETE FROM servers WHERE id=?"), (sid,))
        st.close()


def test_operator_disposition_endpoint_rejects_invalid():
    sid, host = _seed_server()
    try:
        r = client.put(f"/api/servers/{sid}/disposition",
                       json={"disposition": "rehost"})
        assert r.status_code == 400
    finally:
        _disp_cleanup(host)
        st = open_store(get_settings().db_url)
        with st.tx() as cur:
            cur.execute(st._x("DELETE FROM servers WHERE id=?"), (sid,))
        st.close()


def test_operator_disposition_endpoint_404_unknown_server():
    r = client.put("/api/servers/srv-does-not-exist/disposition",
                   json={"disposition": "retain"})
    assert r.status_code == 404