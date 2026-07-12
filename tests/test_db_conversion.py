"""F5 — DB heterogeneous conversion assessment (Oracle/SQLServer → TDSQL/CDB).

Covers the pure-function core (``codeintel.enrich_match_db`` +
``wave_risk_basis`` DB signal + ``db_difficulty_rank``), the
``DBConversionProfile`` model roundtrip, and the DB-backed push endpoint +
rebuild fold-in (grade → match confidence dip + replatform force).
"""
import pytest

from idc.core.codeintel import (
    db_difficulty_rank, enrich_match_db, wave_risk_basis,
)
from idc.core.models import (
    DBConversionProfile, DB_DIFFICULTY_A, DB_DIFFICULTY_B, DB_DIFFICULTY_C,
    Match, Server, Target, Wave,
)


def _match(conf=0.9):
    return Match(server_id="s1",
                 target=Target(product="CVM", spec="SA5.LARGE8",
                              region="ap-bangkok"),
                 confidence=conf, method="rule", rationale="Oracle DB -> CVM BYOL")


def _dbp(difficulty, **kw):
    return DBConversionProfile(db_server_id=kw.get("db_server_id", "s1"),
                               source_engine="oracle",
                               target_engine="tdsql", difficulty=difficulty,
                               est_man_days=kw.get("est_man_days", 40.0),
                               reverse_replication=kw.get("reverse_replication", True),
                               review_objects=kw.get("review_objects", ["PKG_ORDERS"]),
                               blockers=kw.get("blockers", ["PL/SQL no equiv"]))


# -- model roundtrip --------------------------------------------------------
def test_db_profile_roundtrip():
    d = _dbp(DB_DIFFICULTY_C, db_server_id="h1")
    rt = DBConversionProfile.from_dict(d.to_dict())
    assert rt.db_server_id == "h1"
    assert rt.difficulty == "C"
    assert rt.reverse_replication is True
    assert rt.review_objects == ["PKG_ORDERS"]
    assert rt.est_man_days == 40.0


def test_db_difficulty_rank():
    assert db_difficulty_rank(_dbp(DB_DIFFICULTY_A)) == 0
    assert db_difficulty_rank(_dbp(DB_DIFFICULTY_B)) == 1
    assert db_difficulty_rank(_dbp(DB_DIFFICULTY_C)) == 2
    assert db_difficulty_rank(None) == 0
    assert db_difficulty_rank(DBConversionProfile(db_server_id="x")) == 0


# -- enrich_match_db --------------------------------------------------------
def test_grade_c_dips_confidence_and_forces_replatform():
    m = _match(0.9)
    enrich_match_db(m, _dbp(DB_DIFFICULTY_C))
    assert m.confidence == pytest.approx(0.5, abs=0.01)   # 0.9 - 0.40
    assert m.method == "hybrid"
    assert "replatform" in m.rationale
    assert "difficulty=C" in m.rationale
    assert "reverse replication" in m.rationale


def test_grade_b_modest_dip_no_pattern_change():
    m = _match(0.9)
    # empty review_objects so the "manual review" note is NOT emitted — we only
    # want the grade-B confidence dip, no pattern change
    enrich_match_db(m, _dbp(DB_DIFFICULTY_B, est_man_days=12, reverse_replication=False,
                            review_objects=[]))
    assert m.confidence == pytest.approx(0.75, abs=0.01)  # 0.9 - 0.15
    assert m.method == "rule"          # unchanged (B does not force replatform)
    assert "difficulty=B" in m.rationale
    assert "replatform" not in m.rationale


def test_grade_a_no_dip_annotates_only():
    m = _match(0.9)
    enrich_match_db(m, _dbp(DB_DIFFICULTY_A, est_man_days=2,
                            reverse_replication=False, review_objects=[],
                            blockers=[]))
    assert m.confidence == 0.9         # unchanged
    assert m.method == "rule"
    assert "difficulty=A" not in (m.rationale or "")  # no dip -> no note; A is silent
    # but the man-day estimate is still surfaced
    assert "2 man-day" in m.rationale


def test_no_profile_is_noop():
    m = _match()
    enrich_match_db(m, None)
    assert m.confidence == 0.9
    assert m.rationale == "Oracle DB -> CVM BYOL"


def test_confidence_floor_respected():
    m = _match(0.2)
    enrich_match_db(m, _dbp(DB_DIFFICULTY_C))   # -0.40 would go negative
    assert m.confidence >= 0.05


# -- wave_risk_basis DB signal ----------------------------------------------
def _server(sid, crit="high"):
    return Server(id=sid, hostname=sid, fqdn=f"{sid}.dc1", ips=["10.0.0.1"],
                  role="db", business_criticality=crit)


def test_wave_risk_hard_db_bumps_score():
    s = _server("s1")
    w = Wave(id="w1", name="db-wave", stage="2_data", server_ids=["s1"])
    base = wave_risk_basis(w, [s], [_match()], [])
    hard = wave_risk_basis(w, [s], [_match()], [], [_dbp(DB_DIFFICULTY_C)])
    assert hard["score"] > base["score"]
    assert hard["signals"]["hard_db_count"] == 1
    assert hard["signals"]["db_man_days"] == 40.0
    assert any("hard-DB-conversion" in f for f in hard["factors"])


def test_wave_risk_easy_db_does_not_bump():
    s = _server("s1", crit="low")
    w = Wave(id="w1", name="db-wave", stage="2_data", server_ids=["s1"])
    base = wave_risk_basis(w, [s], [_match()], [])
    easy = wave_risk_basis(w, [s], [_match()], [], [_dbp(DB_DIFFICULTY_A,
                                                          est_man_days=2)])
    assert easy["score"] == base["score"]   # grade A is not a hazard
    assert easy["signals"]["hard_db_count"] == 0


# -- DB-backed push endpoint + rebuild fold-in -------------------------------
pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient
import idc.backend.app as m
from idc.core.db import open_store
from idc.config import get_settings
from idc.core.models import _new_id

client = TestClient(m.app)


@pytest.fixture(autouse=True)
def _executor_token(monkeypatch):
    # the db-profiles push endpoint requires a bearer; set one for the suite.
    monkeypatch.setattr(m.settings, "executor_token", "test-token")


def _bearer():
    return {"Authorization": "Bearer test-token"}


def _seed_oracle_server():
    """Seed a DB server + push a C-grade DB profile for its hostname; return
    the server's hostname (the stable key the profile is stored under)."""
    st = open_store(get_settings().db_url)
    sid = _new_id("srv")
    host = f"oradb-{sid[-4:]}"
    s = Server(id=sid, hostname=host, fqdn=f"{host}.dc1.corp", ips=["10.0.0.9"],
               role="db", app_ids=[], sizing_basis="estimated",
               tags=["oracle"])
    st.upsert_server(s)
    st.close()
    # push a C-grade profile keyed by hostname (stable identity)
    r = client.put(f"/api/db-profiles/{host}", json={
        "db_server_id": host, "source_engine": "oracle", "target_engine": "tdsql",
        "difficulty": "C", "est_man_days": 40.0,
        "review_objects": ["PKG_ORDERS"], "blockers": ["PL/SQL no equiv"],
        "reverse_replication": True, "auto_convert_pct": 0.62, "scan_id": "sc-1",
        "summary": "Oracle -> TDSQL hard",
    }, headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 200, r.text
    return sid, host


def _cleanup(sid, host):
    st = open_store(get_settings().db_url)
    with st.tx() as cur:
        cur.execute(st._x("DELETE FROM db_profiles WHERE db_server_id=?"), (host,))
        cur.execute(st._x("DELETE FROM matches WHERE server_id=?"), (sid,))
        cur.execute(st._x("DELETE FROM servers WHERE id=?"), (sid,))
    st.close()


def test_put_and_get_db_profile_endpoint():
    sid, host = _seed_oracle_server()
    try:
        r = client.get(f"/api/db-profiles/{host}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["difficulty"] == "C"
        assert body["reverse_replication"] is True
        # list endpoint sees it
        assert any(d["db_server_id"] == host for d in client.get("/api/db-profiles").json())
    finally:
        _cleanup(sid, host)


def test_put_db_profile_body_id_mismatch_400():
    sid, host = _seed_oracle_server()
    try:
        r = client.put(f"/api/db-profiles/{host}", json={
            "db_server_id": "wrong", "source_engine": "oracle",
            "difficulty": "A"}, headers={"Authorization": "Bearer test-token"})
        assert r.status_code == 400
    finally:
        _cleanup(sid, host)


def test_db_profile_get_unknown_404():
    assert client.get("/api/db-profiles/ghost-host").status_code == 404


def test_rebuild_folds_db_profile_into_match():
    """Rebuild should look up the DB profile by hostname and dip the host's
    match confidence + force replatform (F5 enrich loop).

    Uses the fixture estate's ``db-oracle-01`` Oracle host so the full
    ingest → match → enrich_match_db path runs against real data."""
    from idc.core import rebuild, run_ingest
    from idc.core.db import open_store
    st = open_store(get_settings().db_url)
    host = "db-oracle-01"
    pushed = False
    try:
        run_ingest(st, get_settings(), source="all")
        # push a C-grade profile keyed by the fixture Oracle host's hostname
        r = client.put(f"/api/db-profiles/{host}", json={
            "db_server_id": host, "source_engine": "oracle", "target_engine": "tdsql",
            "difficulty": "C", "est_man_days": 40.0,
            "review_objects": ["PKG_ORDERS"], "blockers": ["PL/SQL no equiv"],
            "reverse_replication": True, "auto_convert_pct": 0.62, "scan_id": "sc-1",
            "summary": "Oracle -> TDSQL hard",
        }, headers={"Authorization": "Bearer test-token"})
        assert r.status_code == 200, r.text
        pushed = True
        rebuild(st, do_match=True, do_plan=False)
        # find the match whose server hostname is db-oracle-01
        servers = {s.id: s for s in st.list_all_servers()}
        m = next((x for x in st.list_matches()
                  if servers.get(x.server_id) and
                  servers[x.server_id].hostname.lower() == host), None)
        assert m is not None, "db-oracle-01 should be matched"
        # F5 enrich loop fired: C-grade dip + replatform force
        assert "difficulty=C" in m.rationale
        assert "replatform" in m.rationale
        # base rule confidence for Oracle is 0.7; -0.40 dip -> ~0.30 (then F4
        # coverage scaling may lower it further, so just check it's well below 0.7)
        assert m.confidence < 0.5
    finally:
        st.close()
        if pushed:
            _cleanup_db_profile(host)


def _cleanup_db_profile(host):
    st = open_store(get_settings().db_url)
    with st.tx() as cur:
        cur.execute(st._x("DELETE FROM db_profiles WHERE db_server_id=?"), (host,))
    st.close()