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


# -- mock worker (pull mode): the runtime-containerize worker infers a profile --
def test_mock_runtime_containerize_worker_pushes_profile(monkeypatch):
    """Pull mode: the mock's _run_runtime_containerize worker (dispatched when
    an executor claims a runtime-containerize task) infers a runtime-derived
    CodeProfile + a Dockerfile patch_ref and pushes them back."""
    import asyncio
    import idc.executor_mock.app as mock
    pushes = []

    async def fake_push(path, body, method="POST", url=None):
        pushes.append((method, url or path, body))
    monkeypatch.setattr(mock, "_push", fake_push)

    payload = {"app_id": "app-rt", "server_id": "srv-rt",
               "inventory": {"process": "java -jar app.jar", "ports": [8080]},
               "mode": "plan", "callback": "http://mig/api/code-profiles/app-rt"}
    out = asyncio.run(mock._run_runtime_containerize(payload, "tsk-rt"))
    # a runtime-derived profile is PUT back
    prof = next(b for mth, t, b in pushes
                if mth == "PUT" and "code-profiles" in t)
    assert prof["app_id"] == "app-rt" and prof["source"] == "runtime-derived"
    # plan mode -> a scaffold patch ref (not a PR)
    assert out["result_ref"].startswith("dockerfile-scaffold-")


def test_handle_dispatches_runtime_containerize(monkeypatch):
    """_handle routes a runtime-containerize task to its worker."""
    import asyncio
    import idc.executor_mock.app as mock

    async def fake_push(*a, **k):
        pass
    monkeypatch.setattr(mock, "_push", fake_push)
    task = {"task_id": "tsk-rc", "kind": "runtime-containerize",
            "payload": {"app_id": "a", "server_id": "s", "mode": "plan"},
            "created_at": "2026-07-15T00:00:00Z"}
    out = asyncio.run(mock._handle(task))
    assert "inferred Dockerfile scaffold" in out["summary"]


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
    """Plan mode (dry-run) previews the scaffold — no confirm gate, the task is
    enqueued for an executor to pull."""
    app_id = f"app-rt-{_new_id('a')[-4:]}"
    import idc.backend.app as bapp
    bapp.settings.executor_enabled = True
    try:
        _seed_runtime_profile(app_id)
        r = client.post("/api/executor/trigger", json={
            "app_id": app_id, "repo_url": "", "branch": "",
            "action": "modify", "mode": "plan"})
        # plan mode: NOT the runtime-confirm 409 — the task is enqueued (200).
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending"
        tid = r.json()["task_id"]
        t = m.STORE.get_task(tid)
        assert t["kind"] == "modify" and t["payload"]["mode"] == "plan"
        with m.STORE.tx() as cur:
            cur.execute(m.STORE._x("DELETE FROM executor_tasks WHERE id=?"), (tid,))
    finally:
        _cleanup_profile(app_id)
        bapp.settings.executor_enabled = False