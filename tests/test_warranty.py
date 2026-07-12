"""Data-gap — hardware warranty / end-of-support tests (Gap2).

Covers:
  * ``match.warranty_bucket`` — explicit status wins; derived from hardware_eol
    vs today (expired / expiring / active); unknown when neither is available;
    parse-failure tolerance.
  * ``cost.eol_onprem_premium_monthly`` + ``estimate_portfolio`` — the on-prem
    extended-support premium is 0 when onprem_rate=0 (out-of-box / existing F2
    tests unaffected) and a bucket fraction of onprem_rate otherwise; the
    portfolio summary + per-server rows carry eol_premium_yearly.
  * ``ingest.warranty.merge_warranty`` — matches by hostname/fqdn, records
    unmatched (shadow-IT candidates), no-op on empty path.
  * ``readiness._sig_hw_support`` — expired→red, expiring→yellow, active→green,
    unknown→n/a (so the rollup is unchanged for un-assessed waves).
  * backend ``PUT /api/servers/{sid}/warranty`` operator override.
"""
import pytest

from idc.core.match import (
    warranty_bucket, WARRANTY_ACTIVE, WARRANTY_EXPIRING, WARRANTY_EXPIRED,
    WARRANTY_UNKNOWN,
)
from idc.core.cost import (
    PriceBook, EOL_ONPREM_PREMIUM, eol_onprem_premium_monthly, estimate_portfolio,
)
from idc.core.ingest.warranty import merge_warranty
from idc.core.models import Server, Match, Target, Wave, _new_id, STAGE_APP
from idc.core.readiness import _sig_hw_support, wave_readiness, GREEN, YELLOW, RED, NA


def _srv(**kw):
    return Server(id=_new_id("srv"), hostname=kw.pop("hostname", "h"), **kw)


# -- warranty_bucket (pure) ------------------------------------------------
def test_warranty_explicit_status_wins():
    s = _srv(warranty_status="expired", hardware_eol="2099-01-01")
    assert warranty_bucket(s, "2026-07-12") == WARRANTY_EXPIRED
    s = _srv(warranty_status="active")
    assert warranty_bucket(s, "2026-07-12") == WARRANTY_ACTIVE


def test_warranty_derived_from_eol():
    assert warranty_bucket(_srv(hardware_eol="2024-03-01"), "2026-07-12") == WARRANTY_EXPIRED
    assert warranty_bucket(_srv(hardware_eol="2026-08-20"), "2026-07-12") == WARRANTY_EXPIRING  # <90d
    assert warranty_bucket(_srv(hardware_eol="2029-01-01"), "2026-07-12") == WARRANTY_ACTIVE


def test_warranty_unknown_when_no_data():
    assert warranty_bucket(_srv(), "2026-07-12") == WARRANTY_UNKNOWN
    assert warranty_bucket(_srv(hardware_eol="not-a-date"), "2026-07-12") == WARRANTY_UNKNOWN
    # explicit "unknown" is honored
    assert warranty_bucket(_srv(warranty_status="unknown"), "2026-07-12") == WARRANTY_UNKNOWN


def test_warranty_expiring_boundary():
    # exactly 90 days out -> still expiring (<=); 91 -> active
    assert warranty_bucket(_srv(hardware_eol="2026-10-10"), "2026-07-12") == WARRANTY_EXPIRING
    assert warranty_bucket(_srv(hardware_eol="2026-10-11"), "2026-07-12") == WARRANTY_ACTIVE


# -- eol on-prem premium (pure) -------------------------------------------
def test_eol_premium_zero_when_no_onprem_rate():
    book = PriceBook(onprem_rate=0.0)
    assert eol_onprem_premium_monthly(_srv(warranty_status="expired"), book) == 0.0


def test_eol_premium_bucket_fractions():
    book = PriceBook(onprem_rate=100.0)
    assert eol_onprem_premium_monthly(_srv(warranty_status="expired"), book) == pytest.approx(50.0)
    assert eol_onprem_premium_monthly(_srv(warranty_status="expiring"), book) == pytest.approx(20.0)
    assert eol_onprem_premium_monthly(_srv(warranty_status="active"), book) == 0.0
    assert eol_onprem_premium_monthly(_srv(), book) == 0.0  # unknown -> 0


def test_estimate_portfolio_eol_premium_only_when_onprem_rate():
    # one expired host + one active host
    s1 = _srv(hostname="e1", warranty_status="expired")
    s2 = _srv(hostname="e2", warranty_status="active")
    matches = [
        Match(server_id=s1.id, target=Target(product="CVM", spec="SA5.SMALL2", region="ap-bangkok"),
              confidence=0.9, method="rule", rationale="x"),
        Match(server_id=s2.id, target=Target(product="CVM", spec="SA5.SMALL2", region="ap-bangkok"),
              confidence=0.9, method="rule", rationale="x"),
    ]
    # no onprem_rate -> no premium line, savings None
    book0 = PriceBook(onprem_rate=0.0)
    out0 = estimate_portfolio([s1, s2], matches, book0)
    assert out0["eol_premium_yearly"] == 0.0
    assert out0["annual_savings"] is None
    # onprem_rate=100 -> expired adds 50/mo (600/yr); active adds 0
    book = PriceBook(onprem_rate=100.0)
    out = estimate_portfolio([s1, s2], matches, book)
    assert out["eol_premium_yearly"] == pytest.approx(600.0, abs=0.5)
    # per-server carries its own premium
    ps = {p["server_id"]: p for p in out["per_server"]}
    assert ps[s1.id]["eol_premium_yearly"] == pytest.approx(600.0, abs=0.5)
    assert ps[s2.id]["eol_premium_yearly"] == 0.0


# -- merge_warranty (pure) ------------------------------------------------
def test_merge_warranty_matches_and_reports_unmatched(tmp_path):
    s1 = _srv(hostname="db-oracle-01")
    s2 = _srv(hostname="db-mysql-01")
    p = tmp_path / "w.json"
    p.write_text('['
                 '{"hostname":"db-oracle-01","hardware_eol":"2024-03-15","warranty_status":"expired"},'
                 '{"hostname":"db-mysql-01","hardware_eol":"2029-01-01","warranty_status":"active"},'
                 '{"fqdn":"ghost.dc1","hardware_eol":"2023-05-01","warranty_status":"expired"}'
                 ']', encoding="utf-8")
    rep = merge_warranty([s1, s2], str(p))
    assert rep["matched"] == 2
    assert rep["records"] == 3
    assert "ghost.dc1" in rep["unmatched"]
    assert s1.warranty_status == "expired" and s1.hardware_eol == "2024-03-15"
    assert s2.warranty_status == "active"


def test_merge_warranty_noop_on_empty_path():
    s1 = _srv(hostname="h1")
    rep = merge_warranty([s1], "")
    assert rep == {"matched": 0, "records": 0, "unmatched": []}
    assert s1.warranty_status == "" and s1.hardware_eol == ""


def test_merge_warranty_warranty_status_optional_derives_from_eol():
    s = _srv(hostname="h1")
    import json as _j, tempfile, os
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _j.dump([{"hostname": "h1", "hardware_eol": "2024-01-01"}], f)
        merge_warranty([s], path)
        assert s.hardware_eol == "2024-01-01"
        assert s.warranty_status == ""  # not set; bucket derives at read time
        assert warranty_bucket(s, "2026-07-12") == WARRANTY_EXPIRED
    finally:
        os.unlink(path)


# -- readiness hw_support signal (pure) ----------------------------------
def test_hw_support_levels():
    wave_expired = Wave(id=_new_id("wave"), name="x", stage=STAGE_APP,
                        server_ids=["a"], status="planned")
    wave_expiring = Wave(id=_new_id("wave"), name="x", stage=STAGE_APP,
                         server_ids=["b"], status="planned")
    wave_active = Wave(id=_new_id("wave"), name="x", stage=STAGE_APP,
                       server_ids=["c"], status="planned")
    wave_unknown = Wave(id=_new_id("wave"), name="x", stage=STAGE_APP,
                        server_ids=["d"], status="planned")
    sa = Server(id="a", hostname="a", warranty_status="expired")
    sb = Server(id="b", hostname="b", hardware_eol="2026-08-20")
    sc = Server(id="c", hostname="c", warranty_status="active")
    sd = Server(id="d", hostname="d")  # nothing -> unknown
    servers = [sa, sb, sc, sd]
    assert _sig_hw_support(wave_expired, servers)["level"] == RED
    assert _sig_hw_support(wave_expiring, servers)["level"] == YELLOW
    assert _sig_hw_support(wave_active, servers)["level"] == GREEN
    # unknown -> YELLOW (conservative): an unassessed wave must not read green
    assert _sig_hw_support(wave_unknown, servers)["level"] == YELLOW


def test_hw_support_worst_across_mixed_members():
    wid = _new_id("wave")
    wave = Wave(id=wid, name="mix", stage=STAGE_APP,
                server_ids=["a", "b"], status="planned")
    servers = [
        Server(id="a", hostname="a", warranty_status="active"),
        Server(id="b", hostname="b", warranty_status="expired"),
    ]
    sig = _sig_hw_support(wave, servers)
    assert sig["level"] == RED  # expired dominates active


# -- DB-backed: wave_readiness hw_support + backend PUT -------------------
pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient
import idc.backend.app as m
from idc.core.db import open_store
from idc.config import get_settings

client = TestClient(m.app)


def _store():
    return open_store(get_settings().db_url)


def _seed_server(warranty_status="", hardware_eol=""):
    st = _store()
    sid = _new_id("srv")
    wid = _new_id("wave")
    host = f"war-{sid[-4:]}"
    s = Server(id=sid, hostname=host, fqdn=f"{host}.dc1", role="web",
               app_ids=["app-war"], sizing_basis="estimated",
               warranty_status=warranty_status, hardware_eol=hardware_eol)
    st.upsert_server(s)
    st.upsert_match(Match(server_id=sid,
                          target=Target(product="CVM", spec="SA5.SMALL2", region="ap-bangkok"),
                          confidence=0.9, method="rule", rationale="x"))
    st.upsert_workload(__import__("idc.core.models", fromlist=["Workload"]).Workload(
        app_id="app-war", name="app-war", server_ids=[sid]))
    st.upsert_wave(Wave(id=wid, name=f"W {host}", stage=STAGE_APP,
                        server_ids=[sid], status="planned"))
    st.close()
    return wid, sid, host


def _cleanup(wid, sid, app_id="app-war"):
    st = _store()
    with st.tx() as cur:
        cur.execute(st._x("DELETE FROM waves WHERE id=?"), (wid,))
        cur.execute(st._x("DELETE FROM matches WHERE server_id=?"), (sid,))
        cur.execute(st._x("DELETE FROM servers WHERE id=?"), (sid,))
        cur.execute(st._x("DELETE FROM workloads WHERE app_id=?"), (app_id,))
    st.close()


def test_wave_readiness_hw_support_expired_red():
    wid, sid, host = _seed_server(warranty_status="expired")
    try:
        st = _store()
        r = wave_readiness(st, wid)
        assert r["signals"]["hw_support"]["level"] == RED
        st.close()
    finally:
        _cleanup(wid, sid)


def test_wave_readiness_hw_support_unknown_is_yellow():
    # no warranty data -> hw_support YELLOW (conservative: unassessed must not
    # read green/ready on the cutover gate)
    wid, sid, host = _seed_server()
    try:
        st = _store()
        r = wave_readiness(st, wid)
        assert r["signals"]["hw_support"]["level"] == YELLOW
        st.close()
    finally:
        _cleanup(wid, sid)


def test_backend_put_warranty_persists():
    wid, sid, host = _seed_server()
    try:
        r = client.put(f"/api/servers/{sid}/warranty",
                       json={"warranty_status": "expired", "hardware_eol": "2024-03-15"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["warranty_status"] == "expired"
        assert body["hardware_eol"] == "2024-03-15"
        # B1 gap-actionable — PUT recomputes the derived bucket + persists it
        assert body["warranty_bucket"] == "expired"
        st = _store()
        s = st.get_server(sid)
        st.close()
        assert s.warranty_status == "expired" and s.hardware_eol == "2024-03-15"
        assert s.warranty_bucket == "expired"   # persisted, not just computed
        assert warranty_bucket(s, "2026-07-12") == WARRANTY_EXPIRED
    finally:
        _cleanup(wid, sid)


def test_persisted_buckets_roundtrip():
    """B1 gap-actionable — warranty_bucket + os_eol_bucket survive upsert+load."""
    from idc.core.models import _new_id
    st = _store()
    sid = _new_id("srv")
    try:
        s = Server(id=sid, hostname="bkt-rt", os="centos", os_version="7",
                   warranty_status="expired", hardware_eol="2024-01-01",
                   warranty_bucket="expired", os_eol_bucket="expired")
        st.upsert_server(s)
        got = st.get_server(sid)
        assert got.warranty_bucket == "expired"
        assert got.os_eol_bucket == "expired"
        # the GET /servers/{sid} surface prefers the persisted bucket
        r = client.get(f"/api/servers/{sid}")
        assert r.status_code == 200
        assert r.json()["os_eol_bucket"] == "expired"
    finally:
        st2 = _store()
        with st2.tx() as cur:
            cur.execute(st2._x("DELETE FROM servers WHERE id=?"), (sid,))
        st2.close()
        st.close()


def test_inventory_filter_and_facet_on_buckets():
    """B2 gap-actionable — /api/servers filters + facets on the persisted buckets."""
    from idc.core.models import _new_id
    st = _store()
    sids = []
    try:
        # unique env isolates to these two (estate envs are prod/staging/dev)
        s_exp = Server(id=_new_id("srv"), hostname="inv-exp", env="zzgap",
                       os="centos", os_version="7",
                       warranty_bucket="expired", os_eol_bucket="expired")
        s_act = Server(id=_new_id("srv"), hostname="inv-act", env="zzgap",
                       os="windows", os_version="2019",
                       warranty_bucket="active", os_eol_bucket="active")
        sids = [s_exp.id, s_act.id]
        st.upsert_server(s_exp); st.upsert_server(s_act)
        # filter narrows to the expired cohort (env isolates to my 2, os_eol narrows to 1)
        r = client.get("/api/servers?env=zzgap&os_eol_bucket=expired")
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert len(items) == 1 and items[0]["hostname"] == "inv-exp"
        # warranty filter narrows to the active cohort
        r2 = client.get("/api/servers?env=zzgap&warranty_bucket=active")
        items2 = r2.json()["items"]
        assert len(items2) == 1 and items2[0]["hostname"] == "inv-act"
        # facets (over the env=zzgap cohort) include both buckets
        r3 = client.get("/api/servers?env=zzgap")
        fc = r3.json()["facets"]
        assert fc.get("os_eol_bucket", {}).get("expired") == 1
        assert fc.get("os_eol_bucket", {}).get("active") == 1
        assert fc.get("warranty_bucket", {}).get("expired") == 1
    finally:
        st2 = _store()
        with st2.tx() as cur:
            for sid in sids:
                cur.execute(st2._x("DELETE FROM servers WHERE id=?"), (sid,))
        st2.close()
        st.close()


def test_backend_put_warranty_rejects_bad_status():
    wid, sid, host = _seed_server()
    try:
        r = client.put(f"/api/servers/{sid}/warranty",
                       json={"warranty_status": "bogus"})
        assert r.status_code == 400
    finally:
        _cleanup(wid, sid)


def test_backend_put_warranty_404_unknown_server():
    r = client.put("/api/servers/srv-nonexistent/warranty",
                   json={"warranty_status": "active"})
    assert r.status_code == 404