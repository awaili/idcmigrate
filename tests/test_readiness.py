"""F10 — portfolio readiness heatmap tests.

Covers each of the six signal paths (lz_ready / db_conversion / code_refactor
/ deps_resolved / cutover_rehearsal / rollback_channel), the worst-case rollup
(red blocks cutover), and the backend endpoints.
"""
import pytest

from idc.core.readiness import wave_readiness, portfolio_readiness, _worst, GREEN, YELLOW, RED

# -- pure rollup ------------------------------------------------------------
def test_worst_case_rollup():
    assert _worst([GREEN, GREEN, GREEN]) == GREEN
    assert _worst([GREEN, YELLOW]) == YELLOW
    assert _worst([GREEN, YELLOW, RED]) == RED
    assert _worst([RED, GREEN]) == RED
    assert _worst([]) == "n/a"


# -- DB-backed signal paths -------------------------------------------------
pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient
import idc.backend.app as m
from idc.core.db import open_store
from idc.config import get_settings
from idc.core.models import (
    _new_id, Server, Match, Target, Wave, Workload, CodeProfile, ScanFinding,
    DBConversionProfile, DB_DIFFICULTY_A, DB_DIFFICULTY_B, DB_DIFFICULTY_C,
    ChangeJob, STAGE_LZ, STAGE_APP, STAGE_DB,
)
from idc.core.execute import launch_wave, advance, complete, MJS_TESTED, MJS_FINALIZED
from idc.core.lz import LZ_CORP, LZ_ONLINE, LZ_FINALIZED

client = TestClient(m.app)


def _store():
    return open_store(get_settings().db_url)


def _seed_wave(role="web", app_id="app-rd", stage=STAGE_APP, with_match=True):
    """Seed one wave with one server (+ its app binding). Returns
    (wave_id, server_id, app_id, hostname). The server is role=web -> online
    archetype unless role overridden."""
    st = _store()
    sid = _new_id("srv")
    wid = _new_id("wave")
    host = f"rdy-{sid[-4:]}"
    s = Server(id=sid, hostname=host, fqdn=f"{host}.dc1", ips=["10.99.0.1"],
               role=role, app_ids=[app_id], tags=[], sizing_basis="estimated")
    st.upsert_server(s)
    if with_match:
        st.upsert_match(Match(server_id=sid,
                              target=Target(product="CVM", spec="SA5.SMALL2",
                                            region="ap-shanghai"),
                              confidence=0.9, method="rule", rationale="x"))
    st.upsert_workload(Workload(app_id=app_id, name=app_id, server_ids=[sid]))
    st.upsert_wave(Wave(id=wid, name=f"RD {host}", stage=stage,
                        server_ids=[sid], status="planned"))
    st.close()
    return wid, sid, app_id, host


def _cleanup(wave_ids, sids, app_ids=None, extra=None):
    extra = extra or {}
    st = _store()
    with st.tx() as cur:
        for jid in extra.get("mjobs", []):
            cur.execute(st._x("DELETE FROM migration_jobs WHERE id=?"), (jid,))
        for cjid in extra.get("cjobs", []):
            cur.execute(st._x("DELETE FROM change_jobs WHERE id=?"), (cjid,))
        for wid in wave_ids:
            cur.execute(st._x("DELETE FROM waves WHERE id=?"), (wid,))
        for a in (app_ids or []):
            cur.execute(st._x("DELETE FROM code_profiles WHERE app_id=?"), (a,))
        for h in extra.get("db_hosts", []):
            cur.execute(st._x("DELETE FROM db_profiles WHERE db_server_id=?"), (h,))
        for arch in extra.get("lz_archs", []):
            cur.execute(st._x("DELETE FROM lz_status WHERE archetype=?"), (arch,))
        for sid in sids:
            cur.execute(st._x("DELETE FROM matches WHERE server_id=?"), (sid,))
            cur.execute(st._x("DELETE FROM servers WHERE id=?"), (sid,))
        # clean workloads for the test app_ids
        for a in (app_ids or []):
            cur.execute(st._x("DELETE FROM workloads WHERE app_id=?"), (a,))
    st.close()


def test_signal_lz_ready_red_then_green_after_finalize():
    wid, sid, app, _ = _seed_wave(role="web")   # web -> online
    try:
        r = wave_readiness(_store(), wid)
        assert r["signals"]["lz_ready"]["level"] == RED   # online not finalized
        st = _store()
        st.set_lz_status(LZ_ONLINE, LZ_FINALIZED, updated_by="test")
        st.close()
        r = wave_readiness(_store(), wid)
        assert r["signals"]["lz_ready"]["level"] == GREEN
    finally:
        _cleanup([wid], [sid], app_ids=[app], extra={"lz_archs": [LZ_ONLINE]})


def test_signal_db_conversion_levels():
    wid, sid, app, host = _seed_wave(role="db")   # a db host
    st = _store()
    try:
        # unassessed -> yellow
        r = wave_readiness(st, wid)
        assert r["signals"]["db_conversion"]["level"] == YELLOW
        # grade C -> red
        st.upsert_db_profile(DBConversionProfile(
            db_server_id=host, source_engine="oracle", target_engine="tdsql",
            difficulty=DB_DIFFICULTY_C, est_man_days=40, reverse_replication=False))
        r = wave_readiness(st, wid)
        assert r["signals"]["db_conversion"]["level"] == RED
        # grade A -> green
        st.upsert_db_profile(DBConversionProfile(
            db_server_id=host, source_engine="mysql", target_engine="cdb_mysql",
            difficulty=DB_DIFFICULTY_A, est_man_days=2, reverse_replication=False))
        r = wave_readiness(st, wid)
        assert r["signals"]["db_conversion"]["level"] == GREEN
        st.close()
    finally:
        _cleanup([wid], [sid], app_ids=[app], extra={"db_hosts": [host]})


def test_signal_code_refactor_levels():
    wid, sid, app, _ = _seed_wave(role="app")
    try:
        st = _store()
        # no profile -> yellow
        r = wave_readiness(st, wid)
        assert r["signals"]["code_refactor"]["level"] == YELLOW
        # profile with blockers -> red
        st.upsert_code_profile(CodeProfile(app_id=app, cloud_readiness=0.4,
                                           migration_pattern="replatform",
                                           blockers=["x blocker"]))
        r = wave_readiness(st, wid)
        assert r["signals"]["code_refactor"]["level"] == RED
        # clean profile + done changejob -> green
        st.upsert_code_profile(CodeProfile(app_id=app, cloud_readiness=0.9,
                                            migration_pattern="rehost",
                                            blockers=[]))
        cjid = _new_id("cjob")
        st.upsert_change_job(ChangeJob(id=cjid, app_id=app, kind="modify",
                                        status="done", patch_ref="pr-1"))
        r = wave_readiness(st, wid)
        assert r["signals"]["code_refactor"]["level"] == GREEN
        st.close()
        _cleanup([], [], extra={"cjobs": [cjid]})
    finally:
        _cleanup([wid], [sid], app_ids=[app])


def test_signal_cutover_rehearsal_red_to_green():
    wid, sid, app, _ = _seed_wave(role="web")
    st = _store()
    job_ids = []
    try:
        # not launched -> red
        r = wave_readiness(st, wid)
        assert r["signals"]["cutover_rehearsal"]["level"] == RED
        # launch + advance to tested -> green (planned -> replicating -> tested)
        servers = st.list_all_servers()
        matches = st.list_matches()
        from idc.core.models import MJS_REPLICATING
        jobs = launch_wave(st, wid, servers, matches, workloads=[], kind="host")
        job_ids = [j.id for j in jobs]
        for j in jobs:
            advance(j, MJS_REPLICATING, store=st, by="test")
            advance(j, MJS_TESTED, store=st, by="test")
        r = wave_readiness(st, wid)
        assert r["signals"]["cutover_rehearsal"]["level"] == GREEN
    finally:
        st.close()
        _cleanup([wid], [sid], app_ids=[app], extra={"mjobs": job_ids})


def test_signal_rollback_channel_red_without_reverse_replication():
    wid, sid, app, host = _seed_wave(role="db")
    st = _store()
    try:
        # db host with grade A but reverse_replication=False -> rollback red
        st.upsert_db_profile(DBConversionProfile(
            db_server_id=host, source_engine="mysql", target_engine="cdb_mysql",
            difficulty=DB_DIFFICULTY_A, est_man_days=2, reverse_replication=False))
        r = wave_readiness(st, wid)
        assert r["signals"]["rollback_channel"]["level"] == RED
        # enable reverse replication -> green
        st.upsert_db_profile(DBConversionProfile(
            db_server_id=host, source_engine="mysql", target_engine="cdb_mysql",
            difficulty=DB_DIFFICULTY_A, est_man_days=2, reverse_replication=True))
        r = wave_readiness(st, wid)
        assert r["signals"]["rollback_channel"]["level"] == GREEN
        st.close()
    finally:
        _cleanup([wid], [sid], app_ids=[app], extra={"db_hosts": [host]})


def test_rollup_is_worst_case_and_can_cutover_only_when_all_green():
    wid, sid, app, _ = _seed_wave(role="web")
    st = _store()
    try:
        r = wave_readiness(st, wid)
        assert r["rollup"] == RED          # cutover_rehearsal red (not launched)
        assert r["can_cutover"] is False
        st.close()
    finally:
        _cleanup([wid], [sid], app_ids=[app])


def test_portfolio_readiness_returns_all_waves():
    wid, sid, app, _ = _seed_wave(role="web")
    st = _store()
    try:
        rows = portfolio_readiness(st)
        assert any(r["wave_id"] == wid for r in rows)
        assert all("signals" in r and "rollup" in r for r in rows)
        st.close()
    finally:
        _cleanup([wid], [sid], app_ids=[app])


def test_unknown_wave_returns_error():
    r = wave_readiness(_store(), "wave-nonexistent")
    assert "error" in r


# -- backend endpoints ------------------------------------------------------
def test_readiness_endpoints():
    wid, sid, app, _ = _seed_wave(role="web")
    try:
        r = client.get("/api/readiness")
        assert r.status_code == 200, r.text
        assert any(w["wave_id"] == wid for w in r.json())
        r = client.get(f"/api/readiness/{wid}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["wave_id"] == wid
        assert set(body["signals"].keys()) == {
            "lz_ready", "db_conversion", "code_refactor",
            "deps_resolved", "cutover_rehearsal", "rollback_channel"}
    finally:
        _cleanup([wid], [sid], app_ids=[app])


def test_readiness_unknown_wave_404():
    assert client.get("/api/readiness/wave-nonexistent").status_code == 404