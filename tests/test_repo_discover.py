"""Tests for repo discovery (the "Scan a git group" feature on the Code tab).

The operator pastes a git GROUP/ORG url; idc-migrate enqueues a ``discover-repos``
task; an executor pulls it, enumerates the repositories inside the url (with its
own git/SCM creds — idc-migrate never touches git), and pushes the list back to
``PUT /api/repos/discover/{scan_id}/result``; the operator picks which to
register via ``POST /api/repos/bulk`` (idempotent — existing urls are skipped).

HTTP tests run against the real FastAPI app + dedicated test DB (auth is off
on a fresh test DB, so the session gate is open). The executor bearer is honored
on the callback route via a monkeypatched ``settings.executor_token``.

The mock-worker test is hermetic (no DB): it stubs the outbound push and checks
the discovered list is PUT back to the callback URL.
"""
import asyncio

import pytest

pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient

import idc.backend.app as bm
import idc.executor_mock.app as mock

client = TestClient(bm.app)


@pytest.fixture
def captured(monkeypatch):
    """Record every outbound push + post instead of hitting the network (mirrors
    tests/test_executor_mock.py's fixture, which is module-local there too)."""
    pushes = []

    async def fake_push(path, body, method="POST", url=None):
        pushes.append((method, url or path, body))

    async def fake_post(path, body):
        return {}

    monkeypatch.setattr(mock, "_push", fake_push)
    monkeypatch.setattr(mock, "_post", fake_post)
    return {"pushes": pushes}


@pytest.fixture(autouse=True)
def _executor(monkeypatch):
    """Enable the default executor with a known bearer so triggers enqueue and
    the callback's ``_check_executor_auth`` accepts the push-back."""
    monkeypatch.setattr(bm.settings, "executor_enabled", True)
    monkeypatch.setattr(bm.settings, "executor_token", "disc-tok")


@pytest.fixture
def _clean():
    """Wipe repo_scans + repos + executor_tasks around each test (leftover
    pending discover-repos tasks would otherwise be claimed before this test's
    own task — claim is oldest-first)."""
    def _r():
        with bm.STORE.tx() as cur:
            cur.execute(bm.STORE._x("DELETE FROM executor_tasks"))
            cur.execute(bm.STORE._x("DELETE FROM repo_scans"))
            cur.execute(bm.STORE._x("DELETE FROM host_repos"))
            cur.execute(bm.STORE._x("DELETE FROM repos"))
    _r()
    yield
    _r()


def _bearer():
    return {"Authorization": "Bearer disc-tok"}


# -- enqueue + scan row ---------------------------------------------------
def test_discover_enqueues_scan(_clean):
    r = client.post("/api/repos/discover",
                    json={"url": "https://gitlab.example.com/mygroup"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scan_id"].startswith("scn-")
    assert body["task_id"].startswith("tsk-")
    assert body["status"] == "pending"
    assert body["url"] == "https://gitlab.example.com/mygroup"

    # the scan row exists, pending, with the url, no repos yet
    sc = client.get(f"/api/repos/discover/{body['scan_id']}").json()
    assert sc["status"] == "pending" and sc["url"] == "https://gitlab.example.com/mygroup"
    assert sc["repos"] == []

    # a discover-repos task was enqueued for an executor to pull
    tasks = client.get("/api/executor/tasks").json()
    assert any(t["kind"] == "discover-repos" for t in tasks)
    t = next(t for t in tasks if t["kind"] == "discover-repos")
    assert t["payload"]["scan_id"] == body["scan_id"]
    assert t["payload"]["url"] == "https://gitlab.example.com/mygroup"


def test_discover_rejects_bad_url(_clean):
    assert client.post("/api/repos/discover", json={"url": "not a url"}).status_code == 400
    assert client.post("/api/repos/discover", json={"url": ""}).status_code == 400


def test_discover_requires_executor_enabled(monkeypatch, _clean):
    monkeypatch.setattr(bm.settings, "executor_enabled", False)
    r = client.post("/api/repos/discover", json={"url": "https://gitlab.example.com/g"})
    assert r.status_code == 409


# -- executor callback stores results -------------------------------------
def test_discover_callback_stores_repos(_clean):
    sid = client.post("/api/repos/discover",
                      json={"url": "https://gitlab.example.com/mygroup"}).json()["scan_id"]
    repos = [{"url": "https://gitlab.example.com/mygroup/api.git", "name": "api",
              "branch": "main", "description": "api service"},
             {"url": "https://gitlab.example.com/mygroup/web.git", "name": "web",
              "branch": "main", "description": "web service"}]
    r = client.put(f"/api/repos/discover/{sid}/result", json={"repos": repos},
                   headers=_bearer())
    assert r.status_code == 200, r.text
    assert r.json() == {"scan_id": sid, "status": "done", "count": 2}

    sc = client.get(f"/api/repos/discover/{sid}").json()
    assert sc["status"] == "done"
    assert [r2["name"] for r2 in sc["repos"]] == ["api", "web"]
    assert sc["repos"][0]["url"].endswith("api.git")
    assert sc["finished_at"]   # stamped terminal


def test_discover_callback_rejects_missing_bearer(_clean):
    sid = client.post("/api/repos/discover",
                      json={"url": "https://gitlab.example.com/g"}).json()["scan_id"]
    # no Authorization header -> 401 (the callback is executor-only)
    r = client.put(f"/api/repos/discover/{sid}/result", json={"repos": []})
    assert r.status_code == 401
    # wrong bearer -> 401
    r2 = client.put(f"/api/repos/discover/{sid}/result", json={"repos": []},
                    headers={"Authorization": "Bearer wrong"})
    assert r2.status_code == 401


def test_discover_callback_unknown_scan_404(_clean):
    r = client.put("/api/repos/discover/scn-nope/result", json={"repos": []},
                   headers=_bearer())
    assert r.status_code == 404


def test_discover_callback_error_marks_scan_error(_clean):
    sid = client.post("/api/repos/discover",
                      json={"url": "https://gitlab.example.com/g"}).json()["scan_id"]
    r = client.put(f"/api/repos/discover/{sid}/result",
                   json={"repos": [], "error": "401 from gitlab — bad token"},
                   headers=_bearer())
    assert r.status_code == 200
    sc = client.get(f"/api/repos/discover/{sid}").json()
    assert sc["status"] == "error"
    assert "bad token" in sc["error"]


def test_discover_list_history(_clean):
    a = client.post("/api/repos/discover", json={"url": "https://gitlab.example.com/g1"}).json()["scan_id"]
    b = client.post("/api/repos/discover", json={"url": "https://gitlab.example.com/g2"}).json()["scan_id"]
    lst = client.get("/api/repos/discover").json()
    assert {s["scan_id"] for s in lst} >= {a, b}
    assert all(s["url"].startswith("https://gitlab.example.com/") for s in lst)


def test_discover_task_is_claimable_with_callback(_clean, monkeypatch):
    """The real executor path: an executor polls /api/executor/tasks/claim and
    gets the discover-repos task with the scan_id + callback baked into the
    payload. Then it pushes results back to the callback and completes."""
    monkeypatch.setattr(bm.settings, "public_url", "https://mig.example.com")
    sid = client.post("/api/repos/discover",
                      json={"url": "https://gitlab.example.com/mygroup"}).json()["scan_id"]
    # claim as the default executor (bearer identifies it)
    claimed = client.post("/api/executor/tasks/claim", json={},
                          headers=_bearer()).json()
    assert claimed["task_id"] and claimed["kind"] == "discover-repos"
    p = claimed["payload"]
    assert p["scan_id"] == sid and p["url"] == "https://gitlab.example.com/mygroup"
    # the callback URL idc-migrate baked in points at this scan's result route
    assert p["callback"] == f"https://mig.example.com/api/repos/discover/{sid}/result"

    # executor pushes the discovered repos back to the callback route, completes
    repos = [{"url": "https://gitlab.example.com/mygroup/api.git", "name": "api",
              "branch": "main", "description": "api"}]
    cr = client.put(f"/api/repos/discover/{sid}/result", json={"repos": repos},
                    headers=_bearer())
    assert cr.status_code == 200 and cr.json()["count"] == 1
    # complete the task (the executor's bookkeeping)
    comp = client.post(f"/api/executor/tasks/{claimed['task_id']}/complete",
                       json={"status": "done", "summary": "discover-repos done"},
                       headers=_bearer())
    assert comp.status_code == 200
    # the scan is now done with the discovered repo
    sc = client.get(f"/api/repos/discover/{sid}").json()
    assert sc["status"] == "done" and sc["repos"][0]["name"] == "api"


# -- bulk register (idempotent) -------------------------------------------
def test_bulk_register_creates_skips_invalid(_clean):
    # pre-register one so it's "already exists"
    client.post("/api/repos", json={"url": "https://gitlab.example.com/mygroup/web.git"})
    r = client.post("/api/repos/bulk", json={"repos": [
        {"url": "https://gitlab.example.com/mygroup/api.git", "branch": "main", "name": "api"},
        {"url": "https://gitlab.example.com/mygroup/web.git"},          # already exists -> skip
        {"url": "https://gitlab.example.com/mygroup/worker.git", "name": "worker"},
        {"url": "not a url"},                                            # invalid
    ]})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["created_count"] == 2
    assert sorted(b["created"]) == ["https://gitlab.example.com/mygroup/api.git",
                                    "https://gitlab.example.com/mygroup/worker.git"]
    assert b["skipped"] == ["https://gitlab.example.com/mygroup/web.git"]
    assert b["invalid"] == ["not a url"]

    repos = {r2["url"]: r2 for r2 in client.get("/api/repos").json()}
    assert repos["https://gitlab.example.com/mygroup/api.git"]["name"] == "api"
    assert repos["https://gitlab.example.com/mygroup/worker.git"]["name"] == "worker"
    # the web repo was NOT recreated (still the original single row)
    assert len([u for u in repos if u.endswith("web.git")]) == 1

    # re-bulk the same set -> all created ones are now skipped
    r2 = client.post("/api/repos/bulk", json={"repos": [
        {"url": "https://gitlab.example.com/mygroup/api.git"},
        {"url": "https://gitlab.example.com/mygroup/worker.git"},
    ]})
    assert r2.json()["created_count"] == 0
    assert sorted(r2.json()["skipped"]) == ["https://gitlab.example.com/mygroup/api.git",
                                            "https://gitlab.example.com/mygroup/worker.git"]


def test_bulk_register_empty(_clean):
    r = client.post("/api/repos/bulk", json={"repos": []})
    assert r.status_code == 200
    assert r.json() == {"created": [], "skipped": [], "invalid": [], "created_count": 0}


# -- mock worker (hermetic) ------------------------------------------------
def test_fake_discover_repos_group_yields_children():
    repos = mock._fake_discover_repos("https://gitlab.example.com/mygroup")
    assert len(repos) >= 3
    assert all(r["url"].endswith(".git") for r in repos)
    assert all(r["url"].startswith("https://gitlab.example.com/mygroup/") for r in repos)
    assert repos[0]["branch"] == "main" and "description" in repos[0]


def test_fake_discover_repos_single_repo_yields_itself():
    repos = mock._fake_discover_repos("https://gitlab.example.com/team/svc.git")
    assert len(repos) == 1
    assert repos[0]["url"] == "https://gitlab.example.com/team/svc.git"
    assert repos[0]["name"] == "svc"


def test_run_discover_repos_pushes_results_to_callback(captured):
    payload = {"scan_id": "scn-abc", "url": "https://gitlab.example.com/mygroup",
               "callback": "http://mig/api/repos/discover/scn-abc/result"}
    out = asyncio.run(mock._run_discover_repos(payload, "tsk-1"))
    # a terminal change-job heartbeat
    cj = [b for mth, t, b in captured["pushes"] if mth == "POST" and t == "/api/change-jobs"]
    assert cj[0]["status"] == "running" and cj[1]["status"] == "done"
    # the discovered list is PUT to the callback URL idc-migrate baked in
    res = next(b for mth, t, b in captured["pushes"]
               if mth == "PUT" and "repos/discover/scn-abc/result" in t)
    assert res["repos"]
    assert all(r["url"].endswith(".git") for r in res["repos"])
    assert out["summary"].startswith("discover-repos")


def test_handle_dispatches_discover_repos(captured):
    """_handle routes a discover-repos task to the worker (registry coverage)."""
    task = {"kind": "discover-repos", "task_id": "tsk-2",
            "payload": {"scan_id": "scn-x", "url": "https://gitlab.example.com/g",
                        "callback": "http://mig/api/repos/discover/scn-x/result"},
            "created_at": "2026-07-16T00:00:00Z"}
    out = asyncio.run(mock._handle(task))
    assert "discover-repos" in out["summary"]
    assert mock._WORKERS["discover-repos"] is mock._run_discover_repos