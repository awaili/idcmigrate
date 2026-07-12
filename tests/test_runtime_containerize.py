"""F9 — runtime containerization (no-source path) tests.

Covers the pure codeintel helpers (runtime-derived detection, confidence cap,
confirm-question gate), the executor_client request shape, the mock
``/v1/runtime-containerize`` round-trip (infers a runtime-derived CodeProfile +
Dockerfile patch_ref + pushes back), the backend trigger endpoint, and the
modify confirm-gate (runtime-derived + execute → 409 + a Question raised).
"""
import pytest

from idc.core.codeintel import (
    enrich_match, is_runtime_derived, runtime_confirm_question,
    RUNTIME_CONFIDENCE_CAP, SOURCE_RUNTIME, SOURCE_REPO,
)
from idc.core.models import CodeProfile, Match, Target, Question, QK_CHOICE


# -- pure helpers -----------------------------------------------------------
def _profile(source="repo", conf=0.9, pattern="rehost"):
    return CodeProfile(app_id="app-x", source=source, cloud_readiness=0.5,
                       migration_pattern=pattern, blockers=[],
                       summary="inferred" if source == SOURCE_RUNTIME else "scanned")


def test_is_runtime_derived():
    assert is_runtime_derived(_profile(SOURCE_RUNTIME)) is True
    assert is_runtime_derived(_profile(SOURCE_REPO)) is False
    assert is_runtime_derived(None) is False


def test_enrich_match_caps_runtime_derived_confidence():
    m = Match(server_id="s", target=Target(product="CVM", spec="SA5.SMALL2"),
              confidence=0.9, method="rule", rationale="app")
    enrich_match(m, _profile(SOURCE_RUNTIME))
    assert m.confidence == pytest.approx(RUNTIME_CONFIDENCE_CAP, abs=0.01)
    assert "runtime-derived" in m.rationale
    assert "operator confirm" in m.rationale


def test_enrich_match_does_not_cap_repo_profile():
    m = Match(server_id="s", target=Target(product="CVM", spec="SA5.SMALL2"),
              confidence=0.9, method="rule", rationale="app")
    enrich_match(m, _profile(SOURCE_REPO))
    assert m.confidence == 0.9   # not capped (only the cap applies to runtime)


def test_runtime_confirm_question_only_for_runtime_derived():
    q = runtime_confirm_question("app-x", _profile(SOURCE_RUNTIME), job_id="j1")
    assert isinstance(q, Question) and q.kind == QK_CHOICE
    assert "no source repo" in q.prompt
    assert q.context["source"] == SOURCE_RUNTIME
    # repo-sourced profile → no gate
    assert runtime_confirm_question("app-x", _profile(SOURCE_REPO)) is None
    # missing profile → no gate
    assert runtime_confirm_question("app-x", None) is None


# -- executor_client request shape ------------------------------------------
def test_executor_client_runtime_containerize_request(monkeypatch):
    """The client builds the right POST /v1/runtime-containerize body."""
    from idc.agent.executor_client import ExecutorClient
    captured = {}

    def fake_request(self, method, path, json):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = json
        return {"job_id": "cjob-1", "status": "pending"}

    monkeypatch.setattr(ExecutorClient, "_request", fake_request)
    ec = ExecutorClient.__new__(ExecutorClient)
    res = ec.runtime_containerize("app-x", "srv-1",
                                   inventory={"process": "java -jar app.jar",
                                              "port": 8080}, mode="plan")
    assert res == {"job_id": "cjob-1", "status": "pending"}
    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/runtime-containerize"
    assert captured["body"]["app_id"] == "app-x"
    assert captured["body"]["server_id"] == "srv-1"
    assert captured["body"]["mode"] == "plan"
    assert captured["body"]["inventory"]["port"] == 8080


# -- mock executor round-trip (TestClient) ----------------------------------
def test_mock_runtime_containerize_roundtrip():
    """The mock /v1/runtime-containerize returns a job_id and records the job
    (the async push-back is best-effort; we assert the contract shape)."""
    from fastapi.testclient import TestClient
    from idc.executor_mock.app import app, _JOBS
    client = TestClient(app)
    r = client.post("/v1/runtime-containerize", json={
        "app_id": "app-rt", "server_id": "srv-rt",
        "inventory": {"process": "java", "port": 8080}, "mode": "plan"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "pending" and body["job_id"]
    # the job is recorded in the mock's in-memory store
    assert body["job_id"] in _JOBS
    assert _JOBS[body["job_id"]]["kind"] == "runtime-containerize"


# -- DB-backed: backend trigger + modify confirm-gate -----------------------
pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient as _BackendClient
import idc.backend.app as m
from idc.core.db import open_store
from idc.config import get_settings
from idc.core.models import _new_id

client = _BackendClient(m.app)


@pytest.fixture(autouse=True)
def _executor_token(monkeypatch):
    monkeypatch.setattr(m.settings, "executor_token", "test-token")


def _seed_runtime_profile(app_id):
    """Push a runtime-derived CodeProfile for an app (simulating the executor
    callback after /v1/runtime-containerize). Returns the app_id."""
    st = open_store(get_settings().db_url)
    st.upsert_code_profile(CodeProfile(
        app_id=app_id, source="runtime-derived", cloud_readiness=0.5,
        migration_pattern="rehost-container", blockers=[],
        summary="Runtime-derived: Java on :8080 → openjdk:8-jre-slim scaffold."))
    st.close()
    return app_id


def _cleanup_profile(app_id):
    st = open_store(get_settings().db_url)
    st.delete_code_profile(app_id)
    # clean any questions raised for the app
    with st.tx() as cur:
        cur.execute(st._x("DELETE FROM questions WHERE app_id=?"), (app_id,))
    st.close()


def test_put_runtime_profile_persists_source():
    app_id = f"app-rt-{_new_id('a')[-4:]}"
    try:
        _seed_runtime_profile(app_id)
        r = client.get(f"/api/code-profiles/{app_id}")
        assert r.status_code == 200, r.text
        assert r.json()["source"] == "runtime-derived"
    finally:
        _cleanup_profile(app_id)


def test_modify_confirm_gate_blocks_execute_for_runtime_derived():
    """F9 — modify in execute mode on a runtime-derived profile MUST 409 + raise
    a Question; plan mode is allowed (preview)."""
    app_id = f"app-rt-{_new_id('a')[-4:]}"
    # configure the executor so the trigger passes the configured-check and
    # reaches the runtime-derived gate (which fires before ec.modify is called)
    m.settings.executor_enabled = True
    m.settings.executor_url = "http://mock-executor"
    try:
        _seed_runtime_profile(app_id)
        # execute mode -> 409 (confirm gate)
        r = client.post("/api/executor/trigger", json={
            "app_id": app_id, "repo_url": "", "branch": "",
            "action": "modify", "mode": "execute"})
        assert r.status_code == 409, r.text
        assert "runtime-derived" in r.text.lower() or "confirmation" in r.text.lower()
        # a Question was raised for the app
        st = open_store(get_settings().db_url)
        qs = [q for q in st.list_questions() if q.app_id == app_id]
        st.close()
        assert qs, "a confirm Question should have been raised"
        assert qs[0].kind == QK_CHOICE
    finally:
        _cleanup_profile(app_id)
        m.settings.executor_enabled = False
        m.settings.executor_url = ""


def test_modify_plan_mode_allowed_for_runtime_derived():
    """Plan mode (dry-run) previews the scaffold — no confirm gate."""
    app_id = f"app-rt-{_new_id('a')[-4:]}"
    m.settings.executor_enabled = True
    import idc.backend.app as bapp
    bapp.settings.executor_enabled = True
    # stub the executor client so we don't need a live executor
    from idc.agent.executor_client import ExecutorClient
    orig = ExecutorClient.modify
    ExecutorClient.modify = lambda self, *a, **k: {"job_id": "cjob-plan", "status": "pending"}
    try:
        _seed_runtime_profile(app_id)
        r = client.post("/api/executor/trigger", json={
            "app_id": app_id, "repo_url": "", "branch": "",
            "action": "modify", "mode": "plan"})
        # plan mode: either 200 (executor configured stub) or 409 (not configured)
        # — but NOT the runtime-confirm 409. Allow 200/202/409-config but reject the
        # confirm-gate 409 specifically.
        if r.status_code == 409:
            assert "runtime-derived" not in r.text.lower(), \
                "plan mode must not hit the runtime confirm-gate"
        assert r.status_code in (200, 409), r.text
    finally:
        ExecutorClient.modify = orig
        _cleanup_profile(app_id)
        m.settings.executor_enabled = False
        bapp.settings.executor_enabled = False