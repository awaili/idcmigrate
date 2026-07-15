"""F9 — generated docs: cutover playbook + as-built (+ persisted runbook).

Covers the DocArtifact model + composite-key store roundtrip, the mock
executor's cutover-playbook / as-built generators (grounded in the wave's
data), and the DB-backed push/retrieve endpoints.
"""
import pytest

from idc.core.models import DocArtifact, DOC_AS_BUILT, DOC_CUTOVER, DOC_RUNBOOK


# -- model ------------------------------------------------------------------
def test_doc_artifact_roundtrip():
    d = DocArtifact(doc_type=DOC_CUTOVER, scope_id="w-3",
                    doc_md="# Cutover playbook", scan_id="cjob-x",
                    summary="cutover for w-3")
    rt = DocArtifact.from_dict(d.to_dict())
    assert rt.doc_type == DOC_CUTOVER
    assert rt.scope_id == "w-3"
    assert rt.doc_md == "# Cutover playbook"


def test_doc_types_vocab():
    assert set(DOC_TYPES := (DOC_RUNBOOK, DOC_CUTOVER, DOC_AS_BUILT)) == \
        {DOC_RUNBOOK, DOC_CUTOVER, DOC_AS_BUILT}


# -- mock generators (grounded in the wave's data) --------------------------
def test_mock_cutover_playbook_orders_members_and_handles_db():
    from idc.executor_mock.app import _fake_cutover_playbook
    ctx = {
        "members": [
            {"server_id": "db-1", "role": "db", "target": "CDB",
             "reverse_replication": True, "ports": [3306]},
            {"server_id": "app-1", "role": "app", "target": "CVM", "ports": [8080]},
        ],
        "downtime_window": {"start": "2026-07-20T02:00Z", "end": "2026-07-20T04:00Z"},
        "risk_basis": {"level": "high", "score": 7},
    }
    md = _fake_cutover_playbook("w-3", ctx)
    assert "Cutover playbook — wave w-3" in md
    assert "Downtime window" in md and "2026-07-20T02:00Z" in md
    # DB host gets the reverse-replication note; app host gets the snapshot path
    assert "reverse-replication" in md
    assert "db-1" in md and "app-1" in md
    # ordered: both members numbered
    assert "1. **db-1**" in md and "2. **app-1**" in md
    assert "Rollback" in md


def test_mock_as_built_renders_targets_and_gates():
    from idc.executor_mock.app import _fake_as_built
    ctx = {
        "targets": [{"server_id": "db-1", "role": "db",
                     "product": "CDB", "spec": "MYSQL_4C8G"}],
        "gate_results": [{"name": "connect", "status": "pass"},
                         {"name": "http", "status": "fail",
                          "detail": "503 on /health"}],
        "change_jobs": [{"app_id": "orders", "patch_ref": "PR-42", "status": "done"}],
        "stage_history": [{"status": "finalized"}],
    }
    md = _fake_as_built("w-3", ctx)
    assert "As-built — wave w-3" in md
    assert "MYSQL_4C8G" in md
    assert "PR-42" in md
    assert "fail" in md and "503 on /health" in md


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


def _cleanup(doc_type, scope_id):
    st = open_store(get_settings().db_url)
    with st.tx() as cur:
        cur.execute(st._x(
            "DELETE FROM doc_artifacts WHERE doc_type=? AND scope_id=?"),
            (doc_type, scope_id))
    st.close()


def test_put_and_get_cutover_doc_endpoint():
    body = {"doc_type": "cutover", "scope_id": "w-3",
            "doc_md": "# Cutover playbook — wave w-3",
            "scan_id": "cjob-1", "scanned_at": "2026-07-13T00:00:00Z",
            "summary": "cutover for w-3"}
    try:
        r = client.put("/api/docs/cutover/w-3", json=body, headers=_bearer())
        assert r.status_code == 200, r.text
        g = client.get("/api/docs/cutover/w-3").json()
        assert g["doc_type"] == "cutover"
        assert g["doc_md"].startswith("# Cutover playbook")
        # list sees it
        assert any(d["scope_id"] == "w-3" for d in client.get("/api/docs").json())
        # filtered list
        assert all(d["doc_type"] == "cutover"
                   for d in client.get("/api/docs?doc_type=cutover").json())
    finally:
        _cleanup("cutover", "w-3")


def test_put_as_built_slug_normalizes_to_as_built():
    """The URL slug is 'as-built' but the stored doc_type is 'as_built'
    (matches DOC_TYPES). PUT via slug, GET via slug, both work; stored is as_built."""
    body = {"doc_type": "as_built", "scope_id": "w-4",
            "doc_md": "# As-built", "scan_id": "cjob-2"}
    try:
        r = client.put("/api/docs/as-built/w-4", json=body, headers=_bearer())
        assert r.status_code == 200, r.text
        g = client.get("/api/docs/as-built/w-4").json()
        assert g["doc_type"] == "as_built"
        assert g["doc_md"] == "# As-built"
    finally:
        _cleanup("as_built", "w-4")


def test_put_doc_rejects_invalid_doc_type():
    r = client.put("/api/docs/bogus/w-9", json={"doc_type": "bogus",
                  "scope_id": "w-9", "doc_md": "x"}, headers=_bearer())
    assert r.status_code == 400


def test_get_doc_unknown_404():
    assert client.get("/api/docs/cutover/ghost").status_code == 404


def test_doc_upsert_overwrites_same_key():
    """Re-pushing the same (doc_type, scope_id) overwrites the markdown
    (composite-key upsert, not duplicate rows)."""
    try:
        client.put("/api/docs/cutover/w-5",
                   json={"doc_type": "cutover", "scope_id": "w-5", "doc_md": "v1"},
                   headers=_bearer())
        client.put("/api/docs/cutover/w-5",
                   json={"doc_type": "cutover", "scope_id": "w-5", "doc_md": "v2"},
                   headers=_bearer())
        docs = client.get("/api/docs?doc_type=cutover").json()
        w5 = [d for d in docs if d["scope_id"] == "w-5"]
        assert len(w5) == 1
        assert w5[0]["doc_md"] == "v2"
    finally:
        _cleanup("cutover", "w-5")