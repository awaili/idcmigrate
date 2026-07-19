"""F6 — migration execution state machine + validation gates tests."""
import pytest

from idc.core.execute import (
    InvalidTransition, GatesNotSatisfied, advance, revert, complete,
    run_validation_gates, launch_wave, default_gates_for,
)
from idc.core.models import (
    MigrationJob, Server, Match, Target, Workload, Wave,
    MJS_PLANNED, MJS_REPLICATING, MJS_TESTED, MJS_READY_FOR_CUTOVER,
    MJS_CUT_OVER, MJS_FINALIZED, MJS_ROLLED_BACK,
    MJK_HOST, MJK_DB,
)
from idc.config import get_settings, reset_settings


def _job(status=MJS_PLANNED, gates=None):
    return MigrationJob(server_id="srv-1", app_id="app1", wave_id="wave-1",
                        kind=MJK_HOST, status=status,
                        validation_gates=gates if gates is not None else [])


# -- state machine ---------------------------------------------------------
def test_legal_forward_transitions():
    j = _job()
    advance(j, MJS_REPLICATING)
    assert j.status == MJS_REPLICATING
    advance(j, MJS_TESTED)
    advance(j, MJS_READY_FOR_CUTOVER)
    advance(j, MJS_CUT_OVER)
    advance(j, MJS_FINALIZED)
    assert j.status == MJS_FINALIZED
    # stage_history recorded every transition
    assert len(j.stage_history) == 5
    assert j.stage_history[-1]["from"] == MJS_CUT_OVER


def test_illegal_transition_raises():
    j = _job()  # planned
    with pytest.raises(InvalidTransition):
        advance(j, MJS_FINALIZED)   # can't skip from planned to finalized
    with pytest.raises(InvalidTransition):
        advance(j, MJS_CUT_OVER)    # can't jump two stages


def test_revert_edges_exist():
    j = _job(MJS_REPLICATING)
    revert(j, MJS_PLANNED)
    assert j.status == MJS_PLANNED
    assert "revert" in j.stage_history[-1]["note"]


def test_abort_to_rolled_back_from_any_nonterminal():
    for frm in (MJS_PLANNED, MJS_REPLICATING, MJS_TESTED, MJS_READY_FOR_CUTOVER, MJS_CUT_OVER):
        j = _job(frm)
        advance(j, MJS_ROLLED_BACK)
        assert j.status == MJS_ROLLED_BACK


def test_terminal_state_has_no_outgoing():
    j = _job(MJS_FINALIZED)
    with pytest.raises(InvalidTransition):
        advance(j, MJS_PLANNED)


# -- gates + finalize ------------------------------------------------------
def test_advance_to_finalized_requires_must_pass_gates():
    gates = [{"name": "ssh", "kind": "connect", "must_pass": True, "result": "pending"}]
    j = _job(MJS_CUT_OVER, gates=gates)
    with pytest.raises(GatesNotSatisfied):
        advance(j, MJS_FINALIZED)
    assert j.status == MJS_CUT_OVER  # unchanged


def test_advance_to_finalized_passes_when_gates_pass():
    gates = [{"name": "ssh", "kind": "connect", "must_pass": True, "result": "pass"}]
    j = _job(MJS_CUT_OVER, gates=gates)
    advance(j, MJS_FINALIZED)
    assert j.status == MJS_FINALIZED


def test_complete_manual_override_finalizes_and_skips_pending_gates():
    gates = [{"name": "ssh", "kind": "connect", "must_pass": True, "result": "pending"},
             {"name": "disk", "kind": "volume_consistency", "must_pass": True, "result": "pending"}]
    j = _job(MJS_REPLICATING, gates=gates)
    complete(j, by="operator")
    assert j.status == MJS_FINALIZED
    assert all(g["result"] == "skipped" for g in gates)


def test_complete_on_terminal_raises():
    j = _job(MJS_FINALIZED)
    with pytest.raises(InvalidTransition):
        complete(j)


# -- gate evaluation -------------------------------------------------------
def test_gate_connect_pass(monkeypatch):
    class _FakeSocket:
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: _FakeSocket())
    j = _job(gates=[{"name": "ssh", "kind": "connect", "must_pass": True,
                     "host": "10.0.0.1", "port": 22}])
    ok, gates = run_validation_gates(j, get_settings())
    assert ok and gates[0]["result"] == "pass"


def test_gate_connect_fail(monkeypatch):
    monkeypatch.setattr("socket.create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("no route")))
    j = _job(gates=[{"name": "ssh", "kind": "connect", "must_pass": True,
                     "host": "10.0.0.1", "port": 22}])
    ok, gates = run_validation_gates(j, get_settings())
    assert not ok and gates[0]["result"] == "fail"


def test_gate_http_pass(monkeypatch):
    import httpx
    class _Resp:
        status_code = 200
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    j = _job(gates=[{"name": "web", "kind": "http_response", "must_pass": True,
                     "url": "http://10.0.0.1/health"}])
    ok, gates = run_validation_gates(j, get_settings())
    assert ok and gates[0]["result"] == "pass"


def test_gate_manual_stays_pending():
    j = _job(gates=[{"name": "disk", "kind": "volume_consistency", "must_pass": True}])
    ok, gates = run_validation_gates(j, get_settings())
    assert not ok and gates[0]["result"] == "pending"


def test_gate_promql_pending_without_prometheus():
    reset_settings()  # no IDC_PROM_URL
    j = _job(gates=[{"name": "h", "kind": "app_health_promql", "must_pass": False,
                     "metric": "up"}])
    ok, gates = run_validation_gates(j, get_settings())
    # must_pass=False so ok is True even though the gate is pending
    assert ok and gates[0]["result"] == "pending"


# -- default gates + launch_wave -------------------------------------------
def test_default_gates_for_host_includes_connect_and_disk():
    s = Server(hostname="h", ips=["10.0.0.1"])
    m = Match(server_id="s", target=Target(product="CVM", extras={"port": 22}))
    gates = default_gates_for(MJK_HOST, server=s, match=m)
    kinds = {g["kind"] for g in gates}
    assert "connect" in kinds and "volume_consistency" in kinds
    assert any(g["must_pass"] for g in gates)


def test_default_gates_for_db_includes_db_reachable():
    s = Server(hostname="db", ips=["10.0.0.2"])
    m = Match(server_id="db", target=Target(product="CDB", extras={"port": 3306}))
    gates = default_gates_for(MJK_DB, server=s, match=m)
    assert any(g["name"] == "db-reachable" and g["port"] == 3306 for g in gates)


# -- DB-backed launch_wave + backend endpoints -----------------------------
pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient
import idc.backend.app as m
from idc.core.db import open_store
from idc.core.models import _new_id

client = TestClient(m.app)


def _make_wave_with_server():
    """Insert a wave + a server + match into the live store for a test."""
    st = open_store(get_settings().db_url)
    sid = _new_id("srv")
    wid = _new_id("wave")
    s = Server(id=sid, hostname="pytest-exec-" + sid[-4:], ips=["10.0.0.99"],
               app_ids=[], sizing_basis="estimated")
    st.upsert_server(s)
    st.upsert_match(Match(server_id=sid, target=Target(product="CVM", spec="SA5.SMALL2",
                          region="ap-bangkok", extras={"port": 22}), confidence=0.9))
    st.upsert_wave(Wave(id=wid, name="pytest-wave", stage="3_application",
                        server_ids=[sid], status="planned"))
    st.close()
    return wid, sid


def _cleanup(st, wid, sid, job_ids):
    with st.tx() as cur:
        for jid in job_ids:
            cur.execute(st._x("DELETE FROM migration_jobs WHERE id=?"), (jid,))
        cur.execute(st._x("DELETE FROM waves WHERE id=?"), (wid,))
        cur.execute(st._x("DELETE FROM matches WHERE server_id=?"), (sid,))
        cur.execute(st._x("DELETE FROM servers WHERE id=?"), (sid,))
    st.close()


def test_launch_wave_then_advance_validate_complete():
    wid, sid = _make_wave_with_server()
    st = open_store(get_settings().db_url)
    job_ids = []
    try:
        # launch
        r = client.post(f"/api/waves/{wid}/execute?kind=host")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["launched"] == 1
        job_id = body["jobs"][0]["id"]
        job_ids.append(job_id)
        assert body["jobs"][0]["status"] == MJS_PLANNED

        # advance planned -> replicating -> tested -> ready_for_cutover -> cut_over
        for to in (MJS_REPLICATING, MJS_TESTED, MJS_READY_FOR_CUTOVER, MJS_CUT_OVER):
            r = client.post(f"/api/migration-jobs/{job_id}/advance",
                            json={"to": to, "by": "test", "note": ""})
            assert r.status_code == 200, r.text
            assert r.json()["status"] == to

        # advance to finalized without satisfying gates -> 409
        r = client.post(f"/api/migration-jobs/{job_id}/advance",
                        json={"to": MJS_FINALIZED, "by": "test"})
        assert r.status_code == 409

        # complete (manual override) -> finalized
        r = client.post(f"/api/migration-jobs/{job_id}/complete",
                        json={"by": "test", "note": "operator confirmed"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == MJS_FINALIZED

        # list migration-jobs for the wave (paginated: {items,total,...})
        r = client.get(f"/api/migration-jobs?wave_id={wid}")
        assert r.status_code == 200
        assert any(j["id"] == job_id for j in r.json()["items"])
    finally:
        _cleanup(st, wid, sid, job_ids)


def test_validate_endpoint_runs_gates():
    wid, sid = _make_wave_with_server()
    st = open_store(get_settings().db_url)
    job_ids = []
    try:
        r = client.post(f"/api/waves/{wid}/execute?kind=host")
        job_id = r.json()["jobs"][0]["id"]
        job_ids.append(job_id)
        r = client.post(f"/api/migration-jobs/{job_id}/validate")
        assert r.status_code == 200, r.text
        body = r.json()
        # gates evaluated; connect gate likely fails (no real host) but result set
        assert "gates" in body and len(body["gates"]) > 0
        assert all("result" in g for g in body["gates"])
    finally:
        _cleanup(st, wid, sid, job_ids)


def test_revert_endpoint():
    wid, sid = _make_wave_with_server()
    st = open_store(get_settings().db_url)
    job_ids = []
    try:
        client.post(f"/api/waves/{wid}/execute?kind=host")
        r = client.get(f"/api/migration-jobs?wave_id={wid}")
        job_id = r.json()["items"][0]["id"]
        job_ids.append(job_id)
        client.post(f"/api/migration-jobs/{job_id}/advance", json={"to": MJS_REPLICATING})
        r = client.post(f"/api/migration-jobs/{job_id}/revert", json={"to": MJS_PLANNED})
        assert r.status_code == 200
        assert r.json()["status"] == MJS_PLANNED
    finally:
        _cleanup(st, wid, sid, job_ids)