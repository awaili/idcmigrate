"""F10 — post-migration optimization (right_size / reserved / anomaly / perf).

Covers the PostMigRecommendation model + composite-key store roundtrip, the
cost-savings rollup, the mock executor's rec generation, and the DB-backed
push/retrieve endpoints.
"""
import pytest

from idc.core.cost import postmig_savings
from idc.core.models import (
    PM_ANOMALY, PM_KINDS, PM_PERF, PM_RESERVED, PM_RIGHT_SIZE,
    PostMigRecommendation,
)


def _rec(kind, saving=0.0, **kw):
    return PostMigRecommendation(server_id="web-1", kind=kind,
                                 monthly_saving_usd=saving, **kw)


# -- model ------------------------------------------------------------------
def test_postmig_roundtrip():
    r = _rec(PM_RIGHT_SIZE, saving=120.0, from_spec="SA2.LARGE8",
             to_spec="SA2.MEDIUM4", reason="p95 cpu 8%", confidence=0.8)
    rt = PostMigRecommendation.from_dict(r.to_dict())
    assert rt.kind == PM_RIGHT_SIZE
    assert rt.monthly_saving_usd == 120.0
    assert rt.to_spec == "SA2.MEDIUM4"


def test_pm_kinds_vocab():
    assert set(PM_KINDS) == {PM_RIGHT_SIZE, PM_RESERVED, PM_ANOMALY, PM_PERF}


# -- cost rollup ------------------------------------------------------------
def test_postmig_savings_sums_right_size_and_reserved_only():
    recs = [
        _rec(PM_RIGHT_SIZE, saving=120.0),
        _rec(PM_RESERVED, saving=60.0),
        _rec(PM_ANOMALY),    # 0 saving
        _rec(PM_PERF),       # 0 saving
    ]
    s = postmig_savings(recs)
    assert s["monthly_saving_usd"] == 180.0
    assert s["yearly_saving_usd"] == 2160.0
    assert s["rec_count"] == 4


def test_postmig_savings_empty():
    assert postmig_savings([])["monthly_saving_usd"] == 0.0
    assert postmig_savings(None)["rec_count"] == 0


# -- mock rec generation ----------------------------------------------------
def test_mock_postmig_emits_all_four_kinds():
    from idc.executor_mock.app import _fake_postmig_recs
    recs = _fake_postmig_recs("web-1",
                              {"target": {"product": "CVM", "spec": "SA2.LARGE8"},
                               "metrics": {"cpu_p95": 8, "mem_p95": 22,
                                           "uptime_pct": 99.5}}, "sc")
    kinds = {r["kind"] for r in recs}
    assert kinds == {PM_RIGHT_SIZE, PM_RESERVED, PM_ANOMALY, PM_PERF}
    # right_size + reserved carry savings; anomaly/perf carry 0
    rs = next(r for r in recs if r["kind"] == PM_RIGHT_SIZE)
    assert rs["monthly_saving_usd"] > 0
    an = next(r for r in recs if r["kind"] == PM_ANOMALY)
    assert an["monthly_saving_usd"] == 0.0
    assert an["severity"] == "medium"


def test_mock_postmig_high_utilization_skips_right_size():
    """A busy host (cpu_p95 high) is NOT a right-size-down candidate."""
    from idc.executor_mock.app import _fake_postmig_recs
    recs = _fake_postmig_recs("db-1",
                              {"target": {"spec": "SA2.LARGE8"},
                               "metrics": {"cpu_p95": 85, "uptime_pct": 99}}, "sc")
    assert not any(r["kind"] == PM_RIGHT_SIZE for r in recs)


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


def _cleanup(server_id):
    st = open_store(get_settings().db_url)
    with st.tx() as cur:
        cur.execute(st._x("DELETE FROM postmig_recs WHERE server_id=?"), (server_id,))
    st.close()


def test_put_and_get_postmig_recs():
    sid = "pmhost-xyz"
    try:
        for kind, body in [
            (PM_RIGHT_SIZE, {"monthly_saving_usd": 120, "from_spec": "SA2.LARGE8",
                              "to_spec": "SA2.MEDIUM4", "confidence": 0.8}),
            (PM_ANOMALY, {"severity": "medium", "detail": "cpu spike 03:00Z",
                          "confidence": 0.6}),
        ]:
            r = client.put(f"/api/postmig-recs/{sid}/{kind}", json=body, headers=_bearer())
            assert r.status_code == 200, r.text
        # get one
        g = client.get(f"/api/postmig-recs/{sid}/{PM_RIGHT_SIZE}").json()
        assert g["to_spec"] == "SA2.MEDIUM4"
        assert g["monthly_saving_usd"] == 120
        # list filtered by server
        recs = client.get(f"/api/postmig-recs?server_id={sid}").json()
        assert {r["kind"] for r in recs} == {PM_RIGHT_SIZE, PM_ANOMALY}
        # savings endpoint rolls up the right_size saving (anomaly=0)
        s = client.get("/api/postmig-savings").json()
        assert s["monthly_saving_usd"] == 120.0
    finally:
        _cleanup(sid)


def test_put_postmig_rejects_invalid_kind():
    r = client.put("/api/postmig-recs/h/bogus", json={},
                   headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 400


def test_get_postmig_unknown_404():
    assert client.get(f"/api/postmig-recs/ghost/{PM_RIGHT_SIZE}").status_code == 404


def test_postmig_upsert_overwrites_same_kind():
    sid = "pmhost-ow"
    try:
        client.put(f"/api/postmig-recs/{sid}/{PM_RIGHT_SIZE}",
                   json={"monthly_saving_usd": 100}, headers=_bearer())
        client.put(f"/api/postmig-recs/{sid}/{PM_RIGHT_SIZE}",
                   json={"monthly_saving_usd": 250, "to_spec": "SA2.MEDIUM2"},
                   headers=_bearer())
        recs = client.get(f"/api/postmig-recs?server_id={sid}").json()
        assert len(recs) == 1
        assert recs[0]["monthly_saving_usd"] == 250
        assert recs[0]["to_spec"] == "SA2.MEDIUM2"
    finally:
        _cleanup(sid)