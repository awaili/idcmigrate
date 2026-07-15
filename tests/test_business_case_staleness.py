"""Business-case snapshot staleness guard.

A saved business-case snapshot is immutable, so once the estate beneath it
changes (new inventory load, a Rebuild, or any edit to servers/matches) it goes
stale and would display a *previous* estate's per-server list — e.g. the old
demo/scale fixture's ``db-``/``app-``/``web-`` hosts after the real
``inventory_draft`` was loaded. GET /api/business-case must detect that
(matched-server count drift) and recompute from the current estate instead of
serving the stale snapshot. These tests pin that behaviour.
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


def test_get_recomputes_stale_snapshot(monkeypatch):
    """GET /api/business-case recomputes when the saved snapshot is stale, and
    flags the response so the UI can hint a save. _portfolio_inputs is stubbed
    to empty inputs so the recompute is deterministic + fast."""
    _save_snapshot("bc-stale-test", per_server_n=999, priced=999)
    monkeypatch.setattr(m.STORE, "stats", lambda: {"matches": 10})  # 999 != 10 -> stale
    monkeypatch.setattr(m, "_portfolio_inputs", lambda: ([], [], {}))
    try:
        r = client.get("/api/business-case")
        assert r.status_code == 200
        payload = r.json()["payload"]
        assert payload.get("recomputed") is True
        # recomputed from the (stubbed empty) live estate -> no per-server rows
        assert payload["per_server"] == []
    finally:
        _cleanup("bc-stale-test")


def test_get_returns_fresh_snapshot_unchanged(monkeypatch):
    """When the snapshot's matched count == live, GET returns it as-is (no
    recompute flag)."""
    _save_snapshot("bc-fresh-test", per_server_n=10, priced=10)
    monkeypatch.setattr(m.STORE, "stats", lambda: {"matches": 10})  # 10 == 10 -> fresh
    try:
        r = client.get("/api/business-case")
        assert r.status_code == 200
        payload = r.json()["payload"]
        assert "recomputed" not in payload
        assert len(payload["per_server"]) == 10
    finally:
        _cleanup("bc-fresh-test")