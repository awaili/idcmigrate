"""Expert-review fixes: the db_conversion_blocked cutover gate (F6) and the
F9 doc-context grounding, plus regression guards on the gate-lockout fix."""
import pytest

from idc.core.execute import _gate_db_conversion_blocked, run_validation_gates
from idc.core.models import (
    DBConversion, DBConversionObject, DBOBJ_BLOCKED, MigrationJob,
)


# -- F6 db_conversion_blocked gate ------------------------------------------
def test_db_gate_passes_when_no_getter():
    status, detail = _gate_db_conversion_blocked({"db_server_id": "h"}, settings=None)
    assert status == "pass"
    assert "not assessed" in detail


def test_db_gate_fails_on_blocked_objects():
    import idc.core.execute as ex
    from idc.core.models import DBConversionProfile
    prof = DBConversionProfile(db_server_id="db-1", conversion=DBConversion(
        target_engine="postgresql", objects=[
            DBConversionObject(name="ORDERS", status="auto_converted"),
            DBConversionObject(name="PKG_BILLING", status=DBOBJ_BLOCKED,
                               issue="UTL_FILE -> sidecar"),
        ]))
    token = ex._DB_CONV_GETTER.set(lambda h: prof if h == "db-1" else None)
    try:
        status, detail = _gate_db_conversion_blocked({"db_server_id": "db-1"},
                                                     settings=None)
        assert status == "fail"
        assert "1 blocked" in detail
    finally:
        ex._DB_CONV_GETTER.reset(token)


def test_db_gate_passes_when_conversion_clean():
    import idc.core.execute as ex
    from idc.core.models import DBConversionProfile
    prof = DBConversionProfile(db_server_id="db-1", conversion=DBConversion(
        target_engine="postgresql",
        objects=[DBConversionObject(name="ORDERS", status="auto_converted")]))
    token = ex._DB_CONV_GETTER.set(lambda h: prof if h == "db-1" else None)
    try:
        status, detail = _gate_db_conversion_blocked({"db_server_id": "db-1"},
                                                     settings=None)
        assert status == "pass"
    finally:
        ex._DB_CONV_GETTER.reset(token)


def test_db_gate_passes_when_assess_mode_no_conversion():
    """A grade-only (assess-mode) profile has no conversion → pass (no lockout)."""
    import idc.core.execute as ex
    from idc.core.models import DBConversionProfile
    prof = DBConversionProfile(db_server_id="db-1", difficulty="C")  # no conversion
    token = ex._DB_CONV_GETTER.set(lambda h: prof if h == "db-1" else None)
    try:
        status, _ = _gate_db_conversion_blocked({"db_server_id": "db-1"}, settings=None)
        assert status == "pass"
    finally:
        ex._DB_CONV_GETTER.reset(token)


def test_db_gate_wired_into_default_gates_for_db_kind():
    """db-kind default gates include the db-conversion-blocked must_pass gate."""
    from idc.core.execute import default_gates_for, MJK_DB
    from idc.core.models import Server
    s = Server(hostname="db-oracle-01", role="db", ips=["10.0.0.1"])
    gates = default_gates_for(MJK_DB, server=s)
    g = next((g for g in gates if g["kind"] == "db_conversion_blocked"), None)
    assert g is not None and g["must_pass"] is True
    assert g["db_server_id"] == "db-oracle-01"


def test_run_validation_gates_db_blocked_blocks_finalize():
    import idc.core.execute as ex
    from idc.core.models import DBConversionProfile
    from idc.config import Settings
    prof = DBConversionProfile(db_server_id="db-1", conversion=DBConversion(
        target_engine="postgresql",
        objects=[DBConversionObject(name="X", status=DBOBJ_BLOCKED)]))

    class _Store:
        def get_db_profile(self, host):
            return prof if host == "db-1" else None

    job = MigrationJob(server_id="s", app_id="a")
    job.validation_gates = [{"name": "db-conversion-blocked",
                             "kind": "db_conversion_blocked", "must_pass": True,
                             "db_server_id": "db-1"}]
    ok, gates = run_validation_gates(job, Settings(), store=_Store())
    assert ok is False
    assert gates[0]["result"] == "fail"


# -- F9 doc-context grounding (backend builds real context) -----------------
pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient
import idc.backend.app as bm
from idc.core.db import open_store
from idc.config import get_settings

client = TestClient(bm.app)


@pytest.fixture(autouse=True)
def _executor_token(monkeypatch):
    monkeypatch.setattr(bm.settings, "executor_token", "test-token")


def _cleanup_wave(wave_id, sid):
    st = open_store(get_settings().db_url)
    with st.tx() as cur:
        cur.execute(st._x("DELETE FROM waves WHERE id=?"), (wave_id,))
        cur.execute(st._x("DELETE FROM servers WHERE id=?"), (sid,))
        cur.execute(st._x("DELETE FROM matches WHERE server_id=?"), (sid,))
    st.close()


def test_wave_doc_context_assembles_real_members():
    """The backend trigger builds the wave context (members / targets /
    downtime_window) from the store, so the doc is grounded — not empty."""
    from idc.core.models import Server, Match, Target, Wave, STAGE_APP, Utilization, _new_id
    st = open_store(get_settings().db_url)
    sid = _new_id("srv")
    wid = _new_id("wave")
    s = Server(id=sid, hostname="web-ctx-01", role="app", ips=["10.0.0.9"],
               app_ids=["app-x"], sizing_basis="estimated",
               utilization=Utilization(cpu_p95=10, mem_p95=20, disk_used_pct=10))
    st.upsert_server(s)
    m = Match(server_id=sid, confidence=0.8, method="rule",
              target=Target(product="CVM", spec="SA2.LARGE4", region="ap-bangkok",
                             extras={"port": 8080}))
    st.upsert_match(m)
    w = Wave(id=wid, name="W ctx", stage=STAGE_APP, server_ids=[sid],
             downtime_window={"start": "2026-07-20T02:00Z", "end": "2026-07-20T04:00Z"})
    st.replace_waves([w])
    st.close()
    try:
        ctx = bm._wave_doc_context(wid)
        assert any(mem["server_id"] == sid and mem["hostname"] == "web-ctx-01"
                   and "CVM" in mem["target"] and 8080 in mem["ports"]
                   for mem in ctx["members"])
        assert ctx["downtime_window"]["start"] == "2026-07-20T02:00Z"
        assert "score" in ctx["risk_basis"]   # the deterministic risk basis is attached
    finally:
        _cleanup_wave(wid, sid)