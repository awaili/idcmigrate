"""HTTP endpoint tests for the executor-interaction surface.

These run against the real FastAPI app + MariaDB store, so they are skipped
unless ``idc.backend.app`` imports cleanly (i.e. the DB is reachable). They
cover the question/answer loop, resolve, and the read-only context pulls —
the pieces that have no pure-function test coverage elsewhere.
"""
import pytest

# importing the app opens the DB pool; if the DB is down this fails and we
# skip the whole module (hermetic CI without a DB shouldn't error).
pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient
import idc.backend.app as m

client = TestClient(m.app)
TEST_APP = "pytest-exec-test"


@pytest.fixture(autouse=True)
def _executor_token(monkeypatch):
    # the raise-question endpoint requires a bearer; set one for the suite.
    # _check_executor_auth reads the live config via _executor_settings(),
    # which layers DB overrides over the current settings.executor_token.
    monkeypatch.setattr(m.settings, "executor_token", "test-token")


def _bearer():
    return {"Authorization": "Bearer test-token"}


def test_list_questions_returns_list():
    r = client.get("/api/questions")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_get_unknown_question_404():
    assert client.get("/api/questions/qst-nope").status_code == 404


def test_workload_unknown_404():
    assert client.get("/api/workloads/ghost-app").status_code == 404


def test_app_targets_unknown_404():
    assert client.get(f"/api/apps/ghost-app/targets").status_code == 404


def test_resolve_returns_placeholder_for_unknown_app():
    # resolve doesn't 404 on unknown app — it returns a placeholder form
    r = client.get(f"/api/apps/{TEST_APP}/resolve?old=10.0.5.99&kind=ip")
    assert r.status_code == 200
    body = r.json()
    assert body["old"] == "10.0.5.99"
    assert body["new"].startswith("${")          # a placeholder, never a concrete host
    assert body["source"] in ("default", "unknown", "match-derived")


def test_resolve_requires_old_param():
    assert client.get(f"/api/apps/{TEST_APP}/resolve").status_code == 400


def test_question_raise_answer_flow():
    # 1. executor raises a question (bearer auth)
    r = client.post(f"/api/apps/{TEST_APP}/questions",
                    json={"kind": "choice",
                          "prompt": "What should 10.0.4.20 become?",
                          "options": ["${DB_HOST}"],
                          "context": {"file": "a", "line": 1, "old": "10.0.4.20", "new": ""}},
                    headers=_bearer())
    assert r.status_code == 200
    qid = r.json()["id"]
    assert r.json()["status"] == "pending"
    try:
        # 2. executor/operator can read it
        q = client.get(f"/api/questions/{qid}").json()
        assert q["status"] == "pending" and q["kind"] == "choice"
        # 3. operator answers
        a = client.post(f"/api/questions/{qid}/answer",
                        json={"answer": "${DB_HOST}", "answered_by": "pytest"})
        assert a.status_code == 200 and a.json()["status"] == "answered"
        # 4. subsequent answer is rejected (already answered)
        assert client.post(f"/api/questions/{qid}/answer",
                           json={"answer": "x"}).status_code == 409
        # 5. the answer is persisted
        assert client.get(f"/api/questions/{qid}").json()["answer"] == "${DB_HOST}"
    finally:
        # cleanup: mark skipped so it doesn't linger as pending (already answered
        # here, but the row stays — answered rows are harmless audit trail)
        pass


def test_question_raise_rejects_bad_kind():
    r = client.post(f"/api/apps/{TEST_APP}/questions",
                    json={"kind": "nope", "prompt": "x"}, headers=_bearer())
    assert r.status_code == 400


def test_question_raise_requires_bearer(monkeypatch):
    # no token configured → 401
    monkeypatch.setattr(m.settings, "executor_token", "")
    # re-setting to empty makes _check_executor_auth 401 (no token configured)
    r = client.post(f"/api/apps/{TEST_APP}/questions",
                    json={"kind": "value", "prompt": "x"})
    assert r.status_code == 401


def test_executor_trigger_disabled_409(monkeypatch):
    monkeypatch.setattr(m.settings, "executor_enabled", False)
    r = client.post("/api/executor/trigger",
                    json={"app_id": "x", "repo_url": "r", "action": "scan"})
    assert r.status_code == 409


def test_strategy_batch_streams_ndjson(monkeypatch):
    """Batch 7R is an NDJSON stream (start/result/done) — not a browser-side
    N-call loop. Mocks the LLM so no gateway/DB write (apply=False)."""
    import json as _json
    def fake_seven_r(app_id, servers, wls, matches, profiles):
        return {"ok": True, "app_id": app_id, "strategy": "rehost",
                "rationale": "mock", "target": "CVM", "confidence": 0.7,
                "effort": "low", "key_changes": ["x"]}
    monkeypatch.setattr(m.LLM, "seven_r_strategy", fake_seven_r)
    r = client.post("/api/strategy/batch",
                    json={"apply": False, "app_ids": ["pytest-batch-a", "pytest-batch-b"]})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    lines = [l for l in r.text.split("\n") if l.strip()]
    types = [_json.loads(l)["type"] for l in lines]
    assert types == ["start", "result", "result", "done"]
    results = [_json.loads(l) for l in lines[1:3]]
    assert all(x["ok"] and x["strategy"] == "rehost" for x in results)
    assert _json.loads(lines[-1])["ok_count"] == 2
    # no apply -> nothing persisted
    assert m.STORE.get_app_strategy("pytest-batch-a") is None

# -- executor runtime config (manage-executor panel) ------------------------
@pytest.fixture
def _clean_exec_config():
    """Reset executor config to env defaults (no DB overrides) around each test,
    so PUTs don't leak between tests or into other suites. _executor_settings()
    reads the DB live, so deleting the rows is enough."""
    def _reset():
        with m.STORE.tx() as cur:
            cur.execute(m.STORE._x("DELETE FROM system_config WHERE k LIKE 'executor_%'"))
            cur.execute(m.STORE._x("DELETE FROM system_config WHERE k = 'public_url'"))
            cur.execute(m.STORE._x("DELETE FROM system_config WHERE k = 'executors'"))
    _reset()
    yield
    _reset()


def test_executor_config_get_shape_and_token_never_returned(_clean_exec_config):
    r = client.get("/api/executor/config")
    assert r.status_code == 200
    d = r.json()
    for k in ("url", "token_set", "enabled", "timeout", "public_url", "status"):
        assert k in d
    assert "token" not in d          # the secret itself is never returned


def test_executor_config_put_persists_and_applies_live(_clean_exec_config):
    r = client.put("/api/executor/config",
                   json={"url": "http://127.0.0.1:8090", "enabled": True, "timeout": 120,
                         "public_url": "https://mig.test"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["url"] == "http://127.0.0.1:8090"
    assert d["enabled"] is True
    assert d["timeout"] == 120
    assert d["public_url"] == "https://mig.test"
    # persisted to the DB
    assert m.STORE.get_config("executor_url") == "http://127.0.0.1:8090"
    assert m.STORE.get_config("executor_enabled") == "true"
    assert m.STORE.get_config("executor_timeout") == "120"
    assert m.STORE.get_config("public_url") == "https://mig.test"
    # applied to the live config -> the status probe sees the new url
    assert client.get("/api/executor/status").json()["url"] == "http://127.0.0.1:8090"
    assert client.get("/api/executor/config").json()["url"] == "http://127.0.0.1:8090"


def test_executor_config_put_rejects_bad_public_url(_clean_exec_config):
    # must be http(s)://... or empty — a bare host is rejected so the executor
    # doesn't get a malformed callback URL.
    r = client.put("/api/executor/config", json={"public_url": "mig.test"})
    assert r.status_code == 400
    assert m.STORE.get_config("public_url") is None


def test_trigger_enqueues_with_callback_when_public_url_set(_clean_exec_config, monkeypatch):
    """In pull mode a trigger enqueues a task (no outbound call). The task
    payload carries the feedback `callback` built from IDC_PUBLIC_URL so the
    executor knows where to push results back (docs §2.0③)."""
    import dataclasses
    s = dataclasses.replace(m.settings, executor_url="http://ex.test",
                            executor_enabled=True, public_url="https://mig.test")
    monkeypatch.setattr(m, "_executor_settings", lambda *a, **k: s)
    r = client.post("/api/db-scan", json={"db_server_id": "db-oracle-01",
                    "source_engine": "oracle", "target_engine": "tdsql",
                    "mode": "assess"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending"
    tid = body["task_id"]
    try:
        t = m.STORE.get_task(tid)
        assert t["kind"] == "db-scan"
        # the callback is the full push URL: {public_url}/api/db-profiles/{id}
        assert t["payload"]["callback"] == "https://mig.test/api/db-profiles/db-oracle-01"
    finally:
        with m.STORE.tx() as cur:
            cur.execute(m.STORE._x("DELETE FROM executor_tasks WHERE id=?"), (tid,))


def test_trigger_enqueues_empty_callback_when_public_url_unset(_clean_exec_config, monkeypatch):
    """Back-compat: with IDC_PUBLIC_URL unset the callback is empty and the
    executor falls back to its own IDC_CALLBACK_BASE env."""
    import dataclasses
    s = dataclasses.replace(m.settings, executor_url="http://ex.test",
                            executor_enabled=True, public_url="")
    monkeypatch.setattr(m, "_executor_settings", lambda *a, **k: s)
    r = client.post("/api/db-scan", json={"db_server_id": "db-1",
                    "source_engine": "mysql", "target_engine": "cdb_mysql"})
    assert r.status_code == 200, r.text
    tid = r.json()["task_id"]
    try:
        assert m.STORE.get_task(tid)["payload"]["callback"] == ""
    finally:
        with m.STORE.tx() as cur:
            cur.execute(m.STORE._x("DELETE FROM executor_tasks WHERE id=?"), (tid,))


def test_executor_client_cb_helper():
    """ExecutorClient.cb() builds the full callback URL from public_url, and
    returns '' when unset (so the trigger omits a usable callback)."""
    from idc.agent.executor_client import ExecutorClient
    import idc.config as cfg
    set1 = cfg.Settings(public_url="https://mig.example.com")
    assert ExecutorClient(set1).cb("/api/code-profiles/x") == \
        "https://mig.example.com/api/code-profiles/x"
    # trailing slash on the configured base is stripped
    set2 = cfg.Settings(public_url="https://mig.example.com/")
    assert ExecutorClient(set2).cb("/api/db-profiles/y") == \
        "https://mig.example.com/api/db-profiles/y"
    # unset -> empty (executor falls back to IDC_CALLBACK_BASE)
    assert ExecutorClient(cfg.Settings(public_url="")).cb("/api/x") == ""


def test_executor_config_token_masked_and_kept_when_omitted(_clean_exec_config):
    client.put("/api/executor/config", json={"token": "secret-token"})
    d = client.get("/api/executor/config").json()
    assert d["token_set"] is True
    assert "token" not in d
    # a later PUT without a token keeps the existing one
    client.put("/api/executor/config", json={"url": "http://x:9"})
    assert client.get("/api/executor/config").json()["token_set"] is True
    # the live auth check uses the saved token
    assert m._executor_settings().executor_token == "secret-token"


def test_executor_test_does_not_probe_or_persist(_clean_exec_config):
    """Pull mode: idc-migrate cannot reach the executor (it exposes nothing),
    so /api/executor/test no longer probes a URL — it just reports pull-mode
    status without persisting the candidate fields."""
    before = client.get("/api/executor/config").json()["url"]
    r = client.post("/api/executor/test", json={"url": "http://127.0.0.1:1"})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["reachable"] is None          # no outbound probe in pull mode
    # nothing persisted
    assert client.get("/api/executor/config").json()["url"] == before
    assert m.STORE.get_config("executor_url") is None


# -- named-executor registry (multiple executors) -------------------------
def test_registry_lists_default_only_when_empty(_clean_exec_config):
    r = client.get("/api/executors")
    assert r.status_code == 200
    lst = r.json()
    assert isinstance(lst, list) and len(lst) == 1
    assert lst[0]["id"] == "default"
    assert lst[0]["default"] is True
    assert "token" not in lst[0]            # token never returned
    assert "token_set" in lst[0]


def test_registry_add_named_executor_and_list(_clean_exec_config):
    r = client.put("/api/executors/db-spec",
                   json={"url": "http://127.0.0.1:8091", "token": "db-tok",
                         "enabled": True, "timeout": 300})
    assert r.status_code == 200, r.text
    entry = r.json()
    assert entry["id"] == "db-spec" and entry["url"] == "http://127.0.0.1:8091"
    assert entry["token_set"] is True and "token" not in entry
    # it appears in the list after the default
    lst = client.get("/api/executors").json()
    assert [e["id"] for e in lst] == ["default", "db-spec"]


def test_registry_delete_named_executor(_clean_exec_config):
    client.put("/api/executors/db-spec", json={"url": "http://x:9", "token": "t"})
    assert client.delete("/api/executors/db-spec").status_code == 200
    assert [e["id"] for e in client.get("/api/executors").json()] == ["default"]


def test_registry_cannot_delete_default(_clean_exec_config):
    assert client.delete("/api/executors/default").status_code == 400


def test_registry_rejects_default_id_on_put(_clean_exec_config):
    # the default is managed via /api/executor/config, not the registry surface
    r = client.put("/api/executors/default", json={"url": "http://x:9"})
    assert r.status_code == 400


def test_trigger_routes_to_named_executor(_clean_exec_config, monkeypatch):
    """A trigger with executor_id targets the NAMED executor: the enqueued
    task's executor_id is that id (only that executor's token may claim it)."""
    client.put("/api/executors/db-spec",
               json={"url": "http://127.0.0.1:8091", "token": "db-tok",
                     "enabled": True, "timeout": 300})
    monkeypatch.setattr(m.settings, "executor_enabled", True)
    monkeypatch.setattr(m.settings, "executor_url", "http://127.0.0.1:8090")
    r = client.post("/api/db-scan", json={"db_server_id": "db-1",
                    "source_engine": "mysql", "target_engine": "cdb_mysql",
                    "executor_id": "db-spec"})
    assert r.status_code == 200, r.text
    tid = r.json()["task_id"]
    try:
        t = m.STORE.get_task(tid)
        assert t["executor_id"] == "db-spec"     # targeted, not pool
    finally:
        with m.STORE.tx() as cur:
            cur.execute(m.STORE._x("DELETE FROM executor_tasks WHERE id=?"), (tid,))


def test_trigger_unknown_executor_404(_clean_exec_config, monkeypatch):
    monkeypatch.setattr(m.settings, "executor_enabled", True)
    monkeypatch.setattr(m.settings, "executor_url", "http://127.0.0.1:8090")
    r = client.post("/api/db-scan", json={"db_server_id": "db-1",
                    "source_engine": "mysql", "target_engine": "cdb_mysql",
                    "executor_id": "ghost"})
    assert r.status_code == 404


def test_trigger_defaults_to_pool_when_no_executor_id(_clean_exec_config, monkeypatch):
    """No executor_id → the pool (executor_id NULL): any registered executor
    may claim it. (Pull mode: there is no 'default URL' to drive — the default
    executor is just one of the executors that may claim pool tasks.)"""
    monkeypatch.setattr(m.settings, "executor_enabled", True)
    monkeypatch.setattr(m.settings, "executor_url", "http://127.0.0.1:8090")
    r = client.post("/api/db-scan", json={"db_server_id": "db-1",
                    "source_engine": "mysql", "target_engine": "cdb_mysql"})
    assert r.status_code == 200, r.text
    tid = r.json()["task_id"]
    try:
        assert m.STORE.get_task(tid)["executor_id"] is None   # pool
    finally:
        with m.STORE.tx() as cur:
            cur.execute(m.STORE._x("DELETE FROM executor_tasks WHERE id=?"), (tid,))


def test_test_run_pre_mints_and_bakes_run_id(_clean_exec_config, monkeypatch):
    """test_run_trigger pre-mints a run_id, bakes it into the enqueued task
    payload (so the executor pushes back under it), and surfaces it in the
    response (contract §1.11). Previously the mint was dead code."""
    monkeypatch.setattr(m.settings, "executor_enabled", True)
    monkeypatch.setattr(m.settings, "executor_url", "http://127.0.0.1:8090")
    r = client.post("/api/test-run", json={"app_id": "orders-svc",
                    "phase": "pre", "target": "http://10.0.0.1:8080"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending"
    rid = body["run_id"]
    assert rid.startswith("trun-")
    tid = body["task_id"]
    try:
        t = m.STORE.get_task(tid)
        assert t["kind"] == "test-run"
        assert t["payload"]["run_id"] == rid     # baked in for the executor
        assert t["payload"]["callback"] == ""    # test-run uses IDC_CALLBACK_BASE
    finally:
        with m.STORE.tx() as cur:
            cur.execute(m.STORE._x("DELETE FROM executor_tasks WHERE id=?"), (tid,))


def test_test_run_response_always_carries_run_id(_clean_exec_config, monkeypatch):
    """The pre-mint is authoritative now (no executor round-trip at trigger
    time), so the response always carries a trun- run_id."""
    monkeypatch.setattr(m.settings, "executor_enabled", True)
    monkeypatch.setattr(m.settings, "executor_url", "http://127.0.0.1:8090")
    r = client.post("/api/test-run", json={"app_id": "a", "phase": "post",
                    "target": "http://cloud:8080"})
    assert r.status_code == 200, r.text
    tid = r.json()["task_id"]
    try:
        assert r.json()["run_id"].startswith("trun-")
    finally:
        with m.STORE.tx() as cur:
            cur.execute(m.STORE._x("DELETE FROM executor_tasks WHERE id=?"), (tid,))


def test_test_run_rejects_bad_phase(_clean_exec_config, monkeypatch):
    """phase must be one of TEST_PHASES (pre/post); anything else is a 400
    (before any task is enqueued)."""
    monkeypatch.setattr(m.settings, "executor_enabled", True)
    monkeypatch.setattr(m.settings, "executor_url", "http://127.0.0.1:8090")
    r = client.post("/api/test-run", json={"app_id": "a", "phase": "bogus",
                    "target": "http://x"})
    assert r.status_code == 400


# -- pull dispatch: claim / complete / lease --------------------------------
# The executor exposes nothing; it pulls work + reports completion over these
# bearer-authed endpoints. The autouse _executor_token fixture sets the default
# executor token to "test-token" (so resolve_by_token maps it -> "default").
@pytest.fixture
def _wipe_tasks():
    """Drop any executor_tasks these pull-dispatch tests create."""
    yield
    with m.STORE.tx() as cur:
        cur.execute(m.STORE._x("DELETE FROM executor_tasks "
                               "WHERE id LIKE 'tsk-pytest-%'"))
        cur.execute(m.STORE._x("DELETE FROM change_jobs "
                               "WHERE id LIKE 'tsk-pytest-%'"))


def _bearer(tok="test-token"):
    return {"Authorization": f"Bearer {tok}"}


def _enq(kind="scan", executor_id=None, payload=None):
    """Enqueue a task directly via the store for pull-dispatch tests."""
    tid = "tsk-pytest-" + kind
    m.STORE.enqueue_task(tid, kind, payload or {"app_id": "x"},
                         executor_id, "2026-07-15T00:00:00Z")
    return tid


def test_claim_requires_bearer(_wipe_tasks, _clean_exec_config):
    assert client.post("/api/executor/tasks/claim", json={}).status_code == 401


def test_claim_invalid_token_401(_wipe_tasks, _clean_exec_config):
    r = client.post("/api/executor/tasks/claim", json={},
                    headers=_bearer("wrong-token"))
    assert r.status_code == 401


def test_claim_idle_when_queue_empty(_wipe_tasks, _clean_exec_config):
    r = client.post("/api/executor/tasks/claim", json={}, headers=_bearer())
    assert r.status_code == 200
    assert r.json() == {"task_id": None, "status": "idle"}


def test_claim_pool_task_claimable_by_default_executor(_wipe_tasks, _clean_exec_config):
    tid = _enq("scan", executor_id=None)          # pool task
    r = client.post("/api/executor/tasks/claim", json={}, headers=_bearer())
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == tid
    assert body["kind"] == "scan"
    assert body["payload"]["app_id"] == "x"
    assert body["status"] == "claimed"
    # the task is now claimed by the default executor (token resolved to it)
    assert m.STORE.get_task(tid)["claimed_by"] == "default"


def test_claim_targeted_task_only_owner_can_claim(_wipe_tasks, _clean_exec_config):
    """A task targeting a named executor is claimable ONLY by that executor's
    token; the default executor's token cannot grab it."""
    client.put("/api/executors/db-spec",
               json={"url": "http://x:9", "token": "db-tok", "enabled": True})
    tid = _enq("db-scan", executor_id="db-spec")
    # default executor -> idle (not eligible)
    r = client.post("/api/executor/tasks/claim", json={}, headers=_bearer())
    assert r.json()["task_id"] is None
    # db-spec executor (its token) -> claims it
    r2 = client.post("/api/executor/tasks/claim", json={},
                     headers=_bearer("db-tok"))
    assert r2.json()["task_id"] == tid
    assert m.STORE.get_task(tid)["claimed_by"] == "db-spec"


def test_complete_marks_done_and_mirrors_change_job(_wipe_tasks, _clean_exec_config):
    tid = _enq("scan", executor_id=None)
    client.post("/api/executor/tasks/claim", json={}, headers=_bearer())
    r = client.post(f"/api/executor/tasks/{tid}/complete",
                    json={"status": "done", "summary": "ok",
                          "result_ref": "patch-1"}, headers=_bearer())
    assert r.status_code == 200 and r.json()["status"] == "done"
    t = m.STORE.get_task(tid)
    assert t["status"] == "done" and t["result_ref"] == "patch-1"
    # the terminal state is mirrored into the change-jobs audit trail
    cj = m.STORE.get_change_job(tid)
    assert cj is not None and cj["status"] == "done" and cj["summary"] == "ok"


def test_complete_wrong_owner_409(_wipe_tasks, _clean_exec_config):
    """A task claimed by db-spec cannot be completed by the default executor."""
    client.put("/api/executors/db-spec",
               json={"url": "http://x:9", "token": "db-tok", "enabled": True})
    tid = _enq("scan", executor_id=None)             # pool, claimed by db-spec
    client.post("/api/executor/tasks/claim", json={}, headers=_bearer("db-tok"))
    r = client.post(f"/api/executor/tasks/{tid}/complete",
                    json={"status": "done"}, headers=_bearer())   # default token
    assert r.status_code == 409


def test_lease_renewal_only_by_owner(_wipe_tasks, _clean_exec_config):
    tid = _enq("scan", executor_id=None)
    client.post("/api/executor/tasks/claim", json={}, headers=_bearer())
    # owner renews -> 200
    r = client.post(f"/api/executor/tasks/{tid}/lease", json={},
                    headers=_bearer())
    assert r.status_code == 200 and r.json()["status"] == "claimed"
    # requeue it (force lease expiry) then another executor can't renew the
    # old claim
    with m.STORE.tx() as cur:
        cur.execute(m.STORE._x(
            "UPDATE executor_tasks SET lease_until='2020-01-01T00:00:00Z' "
            "WHERE id=?"), (tid,))
    # a fresh claim (now the expired lease is requeued) by db-spec takes it
    client.put("/api/executors/db-spec",
               json={"url": "http://x:9", "token": "db-tok", "enabled": True})
    r2 = client.post("/api/executor/tasks/claim", json={}, headers=_bearer("db-tok"))
    assert r2.json()["task_id"] == tid
    # the old owner (default) renewing now -> 409 (no longer owns it)
    r3 = client.post(f"/api/executor/tasks/{tid}/lease", json={},
                     headers=_bearer())
    assert r3.status_code == 409


def test_expired_lease_requeues_task(_wipe_tasks, _clean_exec_config):
    """A claimed task whose lease expired is returned to pending on the next
    claim (so a dead executor doesn't strand the work)."""
    tid = _enq("scan", executor_id=None)
    client.post("/api/executor/tasks/claim", json={}, headers=_bearer())
    # force the lease into the past
    with m.STORE.tx() as cur:
        cur.execute(m.STORE._x(
            "UPDATE executor_tasks SET lease_until='2020-01-01T00:00:00Z' "
            "WHERE id=?"), (tid,))
    # another executor claims -> the expired task is requeued + reclaimed
    client.put("/api/executors/db-spec",
               json={"url": "http://x:9", "token": "db-tok", "enabled": True})
    r = client.post("/api/executor/tasks/claim", json={}, headers=_bearer("db-tok"))
    assert r.json()["task_id"] == tid
    assert m.STORE.get_task(tid)["claimed_by"] == "db-spec"


def test_list_and_get_task(_wipe_tasks, _clean_exec_config):
    tid = _enq("scan", executor_id=None)
    lst = client.get("/api/executor/tasks").json()
    assert any(t["id"] == tid for t in lst)
    one = client.get(f"/api/executor/tasks/{tid}").json()
    assert one["id"] == tid and one["kind"] == "scan"
    assert client.get("/api/executor/tasks/tsk-nope").status_code == 404
