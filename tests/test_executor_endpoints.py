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
    _reset()
    yield
    _reset()


def test_executor_config_get_shape_and_token_never_returned(_clean_exec_config):
    r = client.get("/api/executor/config")
    assert r.status_code == 200
    d = r.json()
    for k in ("url", "token_set", "enabled", "timeout", "status"):
        assert k in d
    assert "token" not in d          # the secret itself is never returned


def test_executor_config_put_persists_and_applies_live(_clean_exec_config):
    r = client.put("/api/executor/config",
                   json={"url": "http://127.0.0.1:8090", "enabled": True, "timeout": 120})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["url"] == "http://127.0.0.1:8090"
    assert d["enabled"] is True
    assert d["timeout"] == 120
    # persisted to the DB
    assert m.STORE.get_config("executor_url") == "http://127.0.0.1:8090"
    assert m.STORE.get_config("executor_enabled") == "true"
    assert m.STORE.get_config("executor_timeout") == "120"
    # applied to the live config -> the status probe sees the new url
    assert client.get("/api/executor/status").json()["url"] == "http://127.0.0.1:8090"
    assert client.get("/api/executor/config").json()["url"] == "http://127.0.0.1:8090"


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


def test_executor_test_probes_without_persisting(_clean_exec_config):
    before = client.get("/api/executor/config").json()["url"]
    r = client.post("/api/executor/test", json={"url": "http://127.0.0.1:1"})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["reachable"] is False        # nothing on :1
    # the probe must NOT persist
    assert client.get("/api/executor/config").json()["url"] == before
    assert m.STORE.get_config("executor_url") is None
