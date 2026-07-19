"""Business-case snapshot staleness guard.

A saved business-case snapshot is immutable, so once the estate beneath it
changes (new inventory load, a Rebuild, or any edit to servers/matches) it goes
stale and would display a *previous* estate's per-server list — e.g. the old
demo/scale fixture's ``db-``/``app-``/``web-`` hosts after the real
``inventory_draft`` was loaded. GET /api/business-case must detect that
(matched-server count drift / 7R-policy drift) and flag the response ``stale``
so the UI prompts an explicit Refresh. GET must NOT recompute synchronously —
estimate_portfolio is ~58s over 13,855 servers and would monopolize the GIL,
starving concurrent requests. These tests pin that behaviour.
"""
import pytest

pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient
import idc.backend.app as m

client = TestClient(m.app)

# far-future timestamp so the snapshot we save is ordered as the latest by
# list_business_cases (which orders by created_at DESC) regardless of any
# snapshots other tests have left in the shared test DB.
_FUTURE = "2099-12-31T23:59:59Z"


def _save_snapshot(sid: str, per_server_n: int, priced: int = 0, unpriced: int = 0):
    """Persist a snapshot whose payload claims ``per_server_n`` matched hosts."""
    payload = {
        "currency": "USD", "pricing_source": "fixture",
        "cloud_yearly": 0.0, "cloud_monthly": 0.0,
        "priced_servers": priced, "unpriced_servers": unpriced,
        "per_server": [{"server_id": f"srv-{i}"} for i in range(per_server_n)],
        "per_product": {}, "per_region": {}, "per_strategy": {},
    }
    m.STORE.save_business_case(sid, payload, created_at=_FUTURE)


def _cleanup(sid: str):
    m._BC_CACHE.clear()  # avoid cross-test in-process cache hits
    with m.STORE.tx() as cur:
        cur.execute(m.STORE._x("DELETE FROM business_case_snapshots WHERE id=?"), (sid,))


def test_stale_helper_detects_matched_count_drift(monkeypatch):
    """_business_case_stale is true when the snapshot's matched count != live."""
    monkeypatch.setattr(m.STORE, "stats", lambda: {"matches": 10})
    assert m._business_case_stale(
        {"per_server": [{} for _ in range(5)], "priced_servers": 5, "unpriced_servers": 0}) is True
    assert m._business_case_stale(
        {"per_server": [{} for _ in range(10)], "priced_servers": 10, "unpriced_servers": 0}) is False


def test_stale_helper_flags_emptied_estate(monkeypatch):
    """A snapshot with data over a now-empty estate (live matches == 0) is
    stale — the estate changed from N hosts to none, so the snapshot must not
    be served as if still current."""
    monkeypatch.setattr(m.STORE, "stats", lambda: {"matches": 0})
    assert m._business_case_stale(
        {"per_server": [{} for _ in range(5)], "priced_servers": 5, "unpriced_servers": 0}) is True
    # empty snapshot over empty estate -> not stale
    assert m._business_case_stale(
        {"per_server": [], "priced_servers": 0, "unpriced_servers": 0}) is False


def test_get_serves_stale_snapshot_with_flag_not_recompute(monkeypatch):
    """GET /api/business-case serves a stale saved snapshot AS-IS and flags it
    ``stale`` — it does NOT recompute (the ~58s recompute only runs on the
    explicit POST Refresh). _portfolio_inputs is stubbed so the test FAILS if
    GET tries to call it."""
    _save_snapshot("bc-stale-test", per_server_n=999, priced=999)
    monkeypatch.setattr(m.STORE, "stats", lambda: {"matches": 10})  # 999 != 10 -> stale

    def _boom(*a, **k):
        raise AssertionError("GET must not call _portfolio_inputs (no synchronous recompute)")
    monkeypatch.setattr(m, "_portfolio_inputs", _boom)
    m._BC_CACHE.clear()
    try:
        r = client.get("/api/business-case")
        assert r.status_code == 200
        payload = r.json()["payload"]
        assert payload.get("stale") is True
        # served as-is — the 999 stale rows are still there (NOT recomputed to [])
        assert len(payload["per_server"]) == 999
        assert "recomputed" not in payload
    finally:
        _cleanup("bc-stale-test")


def test_get_returns_fresh_snapshot_unchanged(monkeypatch):
    """When the snapshot's matched count == live, GET returns it as-is (not
    stale)."""
    _save_snapshot("bc-fresh-test", per_server_n=10, priced=10)
    monkeypatch.setattr(m.STORE, "stats", lambda: {"matches": 10})  # 10 == 10 -> fresh
    m._BC_CACHE.clear()
    try:
        r = client.get("/api/business-case")
        assert r.status_code == 200
        payload = r.json()["payload"]
        assert payload.get("stale") is False
        assert "recomputed" not in payload
        assert len(payload["per_server"]) == 10
    finally:
        _cleanup("bc-fresh-test")


def test_get_flags_stale_when_7r_policy_changed_after_snapshot(monkeypatch):
    """A 7R-policy change (app_strategies / legacy_dispositions updated_at)
    AFTER the snapshot was generated makes the snapshot stale EVEN WHEN the
    matched-server count is unchanged — so GET flags it ``stale`` (the UI
    prompts Refresh, whose recompute regenerates 7R matching the Inventory).
    GET must NOT recompute synchronously."""
    live_n = int(m.STORE.stats().get("matches") or 0)
    payload = {
        "currency": "USD", "pricing_source": "fixture",
        "generated_at": "2020-01-01T00:00:00Z",
        "cloud_yearly": 0.0, "cloud_monthly": 0.0,
        "priced_servers": live_n, "unpriced_servers": 0,
        "per_server": [{"server_id": f"srv-{i}"} for i in range(live_n)],
        "per_product": {}, "per_region": {}, "per_strategy": {},
    }
    m.STORE.save_business_case("bc-7r-drift-test", payload, created_at=_FUTURE)
    from idc.core.models import LegacyDisposition
    m.STORE.upsert_legacy_disposition(LegacyDisposition(
        server_id="bc-7r-drift-test-host", disposition="retain",
        rationale="t", confidence=1.0, summary="t"))
    monkeypatch.setattr(m, "_portfolio_inputs",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("GET must not recompute")))
    m._BC_CACHE.clear()
    try:
        r = client.get("/api/business-case")
        assert r.status_code == 200
        p = r.json()["payload"]
        assert p.get("stale") is True   # 7R drift -> stale flag (no recompute)
        assert "recomputed" not in p
    finally:
        m.STORE.delete_legacy_disposition("bc-7r-drift-test-host")
        _cleanup("bc-7r-drift-test")


def test_get_fresh_when_no_7r_change_after_snapshot(monkeypatch):
    """A snapshot whose generated_at is AFTER the newest 7R-policy write is
    NOT stale by 7R drift — GET serves it as-is (stale=False)."""
    live_n = int(m.STORE.stats().get("matches") or 0)
    payload = {
        "currency": "USD", "pricing_source": "fixture",
        "generated_at": "2099-12-31T23:59:59Z",   # far future -> newer than any 7R write
        "cloud_yearly": 0.0, "cloud_monthly": 0.0,
        "priced_servers": live_n, "unpriced_servers": 0,
        "per_server": [{"server_id": f"srv-{i}"} for i in range(live_n)],
        "per_product": {}, "per_region": {}, "per_strategy": {},
    }
    m.STORE.save_business_case("bc-7r-fresh-test", payload, created_at=_FUTURE)
    m._BC_CACHE.clear()
    try:
        r = client.get("/api/business-case")
        assert r.status_code == 200
        p = r.json()["payload"]
        assert p.get("stale") is False   # generated_at newer than any 7R write -> fresh
        assert "recomputed" not in p
    finally:
        _cleanup("bc-7r-fresh-test")


def test_post_populates_cache_and_get_hits_it(monkeypatch):
    """POST /api/business-case recomputes + populates the in-process cache; the
    next GET with the same estate signature returns the cached payload
    instantly (no DB snapshot read, no recompute)."""
    m._BC_CACHE.clear()
    # stub the expensive inputs so the POST is fast + deterministic
    monkeypatch.setattr(m, "_portfolio_inputs", lambda: ([], [], {}, {}))
    try:
        r = client.post("/api/business-case", json={"save": False})
        assert r.status_code == 200, r.text
        posted = r.json()
        assert "stale" not in posted and "recomputed" not in posted
        # cache now populated for the current signature
        assert m._BC_CACHE, "POST should populate _BC_CACHE"
        g = client.get("/api/business-case")
        assert g.status_code == 200
        gp = g.json()["payload"]
        # cache hit returns the POSTed payload (same cloud_yearly), not a stale
        # saved snapshot, and no stale flag.
        assert gp.get("cloud_yearly") == posted.get("cloud_yearly")
        assert "stale" not in gp
    finally:
        m._BC_CACHE.clear()