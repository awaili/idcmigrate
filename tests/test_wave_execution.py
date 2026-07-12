"""F7 — wave execution rollup + handoff manifest tests."""
import pytest

from idc.core.execute import (
    wave_execution_status, handoff_rows, _dominant_status,
)
from idc.core.models import (
    MigrationJob, Server, Match, Target, Wave,
    MJS_PLANNED, MJS_REPLICATING, MJS_FINALIZED, MJS_ROLLED_BACK,
)


# -- _dominant_status (bottleneck logic) -----------------------------------
def test_dominant_all_finalized_is_finalized():
    counts = {"planned": 0, "replicating": 0, "tested": 0, "ready_for_cutover": 0,
              "cut_over": 0, "finalized": 3, "rolled_back": 0, "not_launched": 0}
    assert _dominant_status(counts) == "done"


def test_dominant_bottleneck_when_mixed():
    # one server at replicating, two finalized -> wave is "running"
    counts = {"planned": 0, "replicating": 1, "tested": 0, "ready_for_cutover": 0,
              "cut_over": 0, "finalized": 2, "rolled_back": 0, "not_launched": 0}
    assert _dominant_status(counts) == "running"


def test_dominant_all_rolled_back():
    counts = {"planned": 0, "replicating": 0, "tested": 0, "ready_for_cutover": 0,
              "cut_over": 0, "finalized": 0, "rolled_back": 2, "not_launched": 0}
    assert _dominant_status(counts) == "rolled-back"


def test_dominant_not_launched_is_planned():
    counts = {"planned": 0, "replicating": 0, "tested": 0, "ready_for_cutover": 0,
              "cut_over": 0, "finalized": 0, "rolled_back": 0, "not_launched": 3}
    assert _dominant_status(counts) == "planned"


# -- wave_execution_status + handoff (DB-backed) --------------------------
pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient
import idc.backend.app as m
from idc.core.db import open_store
from idc.config import get_settings
from idc.core.models import _new_id

client = TestClient(m.app)


def _setup_wave(n=2):
    st = open_store(get_settings().db_url)
    wid = _new_id("wave")
    sids = []
    for i in range(n):
        sid = _new_id("srv")
        sids.append(sid)
        st.upsert_server(Server(id=sid, hostname=f"pytest-f7-{sid[-4:]}",
                                ips=[f"10.99.0.{i+1}"], app_ids=[], sizing_basis="estimated"))
        st.upsert_match(Match(server_id=sid, target=Target(product="CVM",
                              spec="SA5.SMALL2", region="ap-shanghai",
                              extras={"port": 22}), confidence=0.9))
    st.upsert_wave(Wave(id=wid, name="pytest-f7-wave", stage="3_application",
                        server_ids=sids, status="planned"))
    st.close()
    return wid, sids


def _cleanup(wid, sids, job_ids):
    st = open_store(get_settings().db_url)
    with st.tx() as cur:
        for jid in job_ids:
            cur.execute(st._x("DELETE FROM migration_jobs WHERE id=?"), (jid,))
        for sid in sids:
            cur.execute(st._x("DELETE FROM matches WHERE server_id=?"), (sid,))
            cur.execute(st._x("DELETE FROM servers WHERE id=?"), (sid,))
        cur.execute(st._x("DELETE FROM waves WHERE id=?"), (wid,))
    st.close()


def test_wave_execution_status_rollup():
    wid, sids = _setup_wave(2)
    job_ids = []
    try:
        # launch jobs for the wave via the API
        r = client.post(f"/api/waves/{wid}/execute?kind=host")
        assert r.status_code == 200, r.text
        job_ids = [j["id"] for j in r.json()["jobs"]]
        # rollup: both planned (just launched)
        r = client.get(f"/api/waves/{wid}/execution")
        assert r.status_code == 200, r.text
        roll = r.json()
        assert roll["server_count"] == 2
        assert roll["status_counts"]["planned"] == 2
        assert roll["dominant_status"] == "planned"
        assert len(roll["per_server"]) == 2
        assert roll["per_server"][0]["target"] == "CVM"

        # advance one to replicating -> wave is "running" (any in-progress)
        client.post(f"/api/migration-jobs/{job_ids[0]}/advance",
                    json={"to": MJS_REPLICATING, "by": "test"})
        roll = client.get(f"/api/waves/{wid}/execution").json()
        assert roll["status_counts"]["replicating"] == 1
        assert roll["status_counts"]["planned"] == 1
        assert roll["dominant_status"] == "running"
    finally:
        _cleanup(wid, sids, job_ids)


def test_handoff_csv_shape():
    wid, sids = _setup_wave(1)
    job_ids = []
    try:
        client.post(f"/api/waves/{wid}/execute?kind=host")
        job_ids = [j["id"] for j in client.get(f"/api/migration-jobs?wave_id={wid}").json()]
        r = client.get(f"/api/waves/{wid}/handoff.csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers.get("content-type", "")
        body = r.text
        assert "wave_id" in body.splitlines()[0]  # header row
        assert "CVM" in body and "host" in body  # target + tool columns
    finally:
        _cleanup(wid, sids, job_ids)


def test_wave_execution_unknown_wave():
    r = client.get("/api/waves/wave-nonexistent/execution")
    # wave not found -> returns error dict (200 with error field), not a crash
    assert r.status_code == 200
    assert "error" in r.json()