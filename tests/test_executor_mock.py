"""Hermetic tests for the mock external executor (no DB, no real callback).

The mock pushes results back to idc-migrate via best-effort HTTP; when no
idc-migrate is running those pushes just log and move on (they never raise
into the request path). So we can test the executor-side endpoints with a
TestClient alone.
"""
import pytest
from fastapi.testclient import TestClient

from idc.executor_mock.app import app


@pytest.fixture
def client():
    # token is whatever IDC_EXECUTOR_TOKEN was at import; tests don't auth
    return TestClient(app)


def test_scan_returns_202_with_job(client):
    r = client.post("/v1/scan", json={"app_id": "orders",
                                       "repo_url": "git@gitlab:t/orders.git",
                                       "branch": "master"})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending"
    assert body["job_id"].startswith("cjob-")


def test_job_lookup_roundtrip(client):
    r = client.post("/v1/scan", json={"app_id": "x", "repo_url": "r", "branch": ""})
    jid = r.json()["job_id"]
    j = client.get(f"/v1/jobs/{jid}").json()
    assert j["id"] == jid and j["app_id"] == "x" and j["kind"] == "scan"


def test_unknown_job_404(client):
    assert client.get("/v1/jobs/cjob-nope").status_code == 404


def test_modify_accepts_changes_and_returns_202(client):
    r = client.post("/v1/modify", json={
        "app_id": "orders", "repo_url": "r", "branch": "b", "mode": "plan",
        "scope": ["db_connection"],
        "changes": [{"category": "db_connection", "kind": "endpoint",
                      "file": "a", "line": 1, "old": "10.0.4.20:3306",
                      "new": "${DB_HOST}:3306"}],
        "notes": []})
    assert r.status_code == 202
    assert r.json()["job_id"].startswith("cjob-")


def test_comb_returns_202(client):
    r = client.post("/v1/comb", json={"app_id": "x", "repo_url": "r", "branch": ""})
    assert r.status_code == 202