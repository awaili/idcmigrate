"""Hermetic tests for the pull-mode mock executor.

The mock no longer serves HTTP (the executor exposes nothing); it polls
idc-migrate for work. These tests exercise the per-kind workers + the poll
loop directly, stubbing the outbound ``_push`` / ``_post`` so no idc-migrate or
DB is needed. The ``_fake_*`` canned-data generators are pure and tested directly.
"""
import asyncio
import pytest

import idc.executor_mock.app as mock


@pytest.fixture
def captured(monkeypatch):
    """Record every outbound push + post instead of hitting the network."""
    pushes = []
    posts = []

    async def fake_push(path, body, method="POST", url=None):
        pushes.append((method, url or path, body))

    async def fake_post(path, body):
        posts.append((path, body))
        return {}

    monkeypatch.setattr(mock, "_push", fake_push)
    monkeypatch.setattr(mock, "_post", fake_post)
    return {"pushes": pushes, "posts": posts}


def _find_push(cap, path_contains, method):
    for mth, target, body in cap["pushes"]:
        if mth == method and path_contains in target:
            return body
    return None


# -- pure canned-data generators -------------------------------------------
def test_fake_db_profile_oracle_is_grade_c():
    p = mock._fake_db_profile("db-1", "oracle", "tdsql", "s1")
    assert p["difficulty"] == "C" and p["reverse_replication"] is True
    assert "PL/SQL" in p["blockers"][0]


def test_fake_db_profile_mysql_class_is_easy():
    p = mock._fake_db_profile("db-2", "mysql", "cdb_mysql", "s2")
    assert p["difficulty"] == "A" and p["est_man_days"] == 2.0


def test_fake_test_results_post_has_one_regression():
    pre = mock._fake_test_results("a", "pre", "http://x")
    post = mock._fake_test_results("a", "post", "http://x")
    assert all(r["status"] == "pass" for r in pre)
    assert sum(1 for r in post if r["status"] == "fail") == 1
    assert next(r for r in post if r["status"] == "fail")["case"] == \
        "regression_orders_total"


def test_fake_legacy_disposition_containerize_for_sourceless_web():
    d = mock._fake_legacy_disposition("web-1",
        {"role": "web", "has_source_repo": False, "os": "centos6"}, "s")
    assert d["disposition"] == "containerize"
    assert "jdk17" in d["target_base_image"]


def test_fake_legacy_disposition_retain_for_regulated():
    d = mock._fake_legacy_disposition("mf-1",
        {"role": "app", "has_source_repo": True, "tags": ["regulated"]}, "s")
    assert d["disposition"] == "retain"


# -- workers (push results back) --------------------------------------------
def test_run_scan_pushes_profile_and_change_jobs(captured):
    payload = {"app_id": "orders", "repo_url": "git@x:orders.git",
               "branch": "master", "callback": "http://mig/api/code-profiles/orders"}
    out = asyncio.run(mock._run_scan(payload, "tsk-1"))
    # a running heartbeat + a terminal change-job, both to /api/change-jobs
    cj = [b for mth, t, b in captured["pushes"]
          if mth == "POST" and t == "/api/change-jobs"]
    assert cj[0]["status"] == "running" and cj[1]["status"] == "done"
    # the profile is PUT to the callback URL idc-migrate baked in
    prof = _find_push(captured, "code-profiles", "PUT")
    assert prof["app_id"] == "orders" and prof["cloud_readiness"] == 0.42
    assert out["summary"].startswith("scanned orders")


def test_run_db_scan_convert_mode_attaches_conversion(captured):
    payload = {"db_server_id": "db-oracle", "source_engine": "oracle",
               "target_engine": "tdsql", "mode": "convert",
               "callback": "http://mig/api/db-profiles/db-oracle"}
    asyncio.run(mock._run_db_scan(payload, "tsk-2"))
    prof = _find_push(captured, "db-profiles", "PUT")
    assert prof["difficulty"] == "C"
    assert "conversion" in prof and "objects" in prof["conversion"]


def test_run_modify_raises_question_for_unresolved_change(captured):
    # one change with empty old/new (idc-migrate couldn't extract the literal)
    payload = {"app_id": "orders", "repo_url": "r", "branch": "", "mode": "plan",
               "changes": [{"category": "secrets_in_repo", "file": "app.yml",
                            "line": 13, "old": "", "new": "", "title": "secret"}],
               "callback": ""}
    out = asyncio.run(mock._run_modify(payload, "tsk-3"))
    q = _find_push(captured, "questions", "POST")
    assert q["kind"] == "choice" and q["job_id"] == "tsk-3"
    assert out["result_ref"] == "mock-patch-tsk-3"  # plan mode -> patch ref


def test_run_modify_execute_mode_uses_pr_ref(captured):
    payload = {"app_id": "a", "repo_url": "r", "branch": "", "mode": "execute",
               "changes": [{"old": "1.2.3.4", "new": "${H}", "file": "f", "line": 1}],
               "callback": ""}
    out = asyncio.run(mock._run_modify(payload, "tsk-4"))
    assert out["result_ref"] == "mock-pr-tsk-4"
    # resolved change -> no question raised
    assert _find_push(captured, "questions", "POST") is None


def test_run_test_run_uses_payload_run_id(captured):
    payload = {"app_id": "a", "phase": "post", "target": "http://cloud",
               "run_id": "trun-premint", "callback": ""}
    out = asyncio.run(mock._run_test_run(payload, "tsk-5"))
    run = _find_push(captured, "test-runs", "PUT")
    assert run["id"] == "trun-premint"          # honored idc-migrate's pre-mint
    assert run["failed"] == 1                   # post phase -> one regression
    assert out["result_ref"] == "trun-premint"


# -- dispatch + poll loop ---------------------------------------------------
def test_handle_dispatches_scan_and_comb_to_same_worker(captured):
    t_scan = {"task_id": "tsk-a", "kind": "scan", "payload": {"app_id": "x"},
              "created_at": "2026-07-15T00:00:00Z"}
    out = asyncio.run(mock._handle(t_scan))
    assert "scanned x" in out["summary"]
    # comb shares the scan worker shape (produces a profile)
    t_comb = {"task_id": "tsk-b", "kind": "comb", "payload": {"app_id": "y"}}
    out2 = asyncio.run(mock._handle(t_comb))
    assert "scanned y" in out2["summary"]


def test_handle_unknown_kind_raises():
    with pytest.raises(ValueError):
        asyncio.run(mock._handle({"task_id": "tsk", "kind": "bogus", "payload": {}}))


def test_poll_once_idle_returns_false(monkeypatch):
    async def idle_claim(path, body):
        return {"task_id": None, "status": "idle"}
    monkeypatch.setattr(mock, "_post", idle_claim)
    assert asyncio.run(mock.poll_once()) is False


def test_poll_once_handles_task_and_completes(monkeypatch):
    """poll_once claims a task, runs it, and POSTs /tasks/{id}/complete."""
    posts = []

    async def fake_post(path, body):
        posts.append((path, body))
        if path.endswith("/claim"):
            return {"task_id": "tsk-h", "kind": "scan", "status": "claimed",
                    "payload": {"app_id": "z"}, "created_at": "2026-07-15T00:00:00Z"}
        return {}

    async def fake_push(path, body, method="POST", url=None):
        pass
    monkeypatch.setattr(mock, "_post", fake_post)
    monkeypatch.setattr(mock, "_push", fake_push)
    handled = asyncio.run(mock.poll_once())
    assert handled is True
    complete = [b for p, b in posts if p.endswith("/complete")]
    assert complete and complete[0]["status"] == "done"
    assert "scanned z" in complete[0]["summary"]


def test_poll_once_marks_error_on_worker_failure(monkeypatch):
    """A worker exception must not strand a claimed task — it's marked error."""
    async def fake_post(path, body):
        if path.endswith("/claim"):
            return {"task_id": "tsk-e", "kind": "bogus-kind", "status": "claimed",
                    "payload": {}, "created_at": "2026-07-15T00:00:00Z"}
        return {}
    monkeypatch.setattr(mock, "_post", fake_post)

    async def fake_push(*a, **k):
        pass
    monkeypatch.setattr(mock, "_push", fake_push)
    asyncio.run(mock.poll_once())   # must not raise