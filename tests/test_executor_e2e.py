"""End-to-end integration test: backend ↔ mock executor ↔ DB.

Proves the loop we built actually composes: the web backend triggers a scan on
the mock executor; the mock's background task pushes a CodeProfile back via the
callback; the profile lands in the store; the operator can read it.

This needs a running DB (it writes a profile) AND two real uvicorn servers
(the mock's push-back runs in an ``asyncio.create_task`` that only progresses
under a live event loop — in-process test transports won't drive it). So it
is gated behind ``IDC_E2E=1`` and skipped otherwise: the normal suite stays fast
and hermetic, and this runs only when you explicitly want the integration
check (``IDC_E2E=1 python -m pytest tests/test_executor_e2e.py -q``).
"""
import os
import socket
import subprocess
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("IDC_E2E") != "1",
    reason="set IDC_E2E=1 to run the backend↔mock↔DB integration test")

import httpx

APP = "e2e-loop-test"
TOKEN = "e2e-token"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait(url, timeout=20):
    """Poll until the server answers (any 2xx/4xx), or raise."""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        try:
            r = httpx.get(url, timeout=2)
            if r.status_code < 500:
                return
            last = f"{r.status_code}"
        except Exception as e:
            last = repr(e)
        time.sleep(0.4)
    raise RuntimeError(f"{url} not ready in {timeout}s: {last}")


@pytest.fixture(scope="module")
def servers():
    be_port, ex_port = _free_port(), _free_port()
    env = dict(os.environ)
    env.update({
        "IDC_EXECUTOR_URL": f"http://127.0.0.1:{ex_port}",
        "IDC_EXECUTOR_TOKEN": TOKEN,
        "IDC_EXECUTOR_ENABLED": "true",
    })
    be = subprocess.Popen(
        ["python", "-m", "idc.cli.main", "serve", "--host", "127.0.0.1", "--port", str(be_port)],
        env=env, stdout=open("/tmp/e2e-be.log", "w"), stderr=subprocess.STDOUT)
    ex = subprocess.Popen(
        ["python", "-m", "idc.cli.main", "executor-mock", "--host", "127.0.0.1", "--port", str(ex_port)],
        env={**env, "IDC_MOCK_CALLBACK": f"http://127.0.0.1:{be_port}"},
        stdout=open("/tmp/e2e-ex.log", "w"), stderr=subprocess.STDOUT)
    try:
        _wait(f"http://127.0.0.1:{be_port}/api/stats")
        _wait(f"http://127.0.0.1:{ex_port}/v1/jobs/none")  # 404 is fine → server up
        yield be_port, ex_port
    finally:
        for p in (ex, be):
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        # best-effort cleanup of the test profile we wrote
        try:
            httpx.delete(f"http://127.0.0.1:{be_port}/api/code-profiles/{APP}",
                         headers={"Authorization": f"Bearer {TOKEN}"}, timeout=5)
        except Exception:
            pass


def test_scan_loop_lands_profile_in_store(servers):
    be_port, ex_port = servers
    base = f"http://127.0.0.1:{be_port}"

    # 1. trigger a scan via the web backend → it forwards to the mock executor
    r = httpx.post(f"{base}/api/executor/trigger", json={
        "app_id": APP, "repo_url": "git@gitlab:t/e2e.git", "branch": "master",
        "action": "scan"}, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10)
    assert r.status_code == 200, r.text
    assert r.json()["status"] in ("pending", "done")

    # 2. the mock's background task pushes a CodeProfile back; poll until it lands
    profile = None
    t0 = time.time()
    while time.time() - t0 < 20:
        try:
            g = httpx.get(f"{base}/api/code-profiles/{APP}", timeout=5)
            if g.status_code == 200:
                profile = g.json()
                break
        except Exception:
            pass
        time.sleep(0.5)
    assert profile is not None, "mock never pushed a profile back to the store"
    # 3. the profile is the mock's fabricated one
    assert profile["app_id"] == APP
    assert profile["migration_pattern"] == "replatform"
    assert profile["cloud_readiness"] == pytest.approx(0.42)
    assert "user-svc" in profile["code_deps"]
    assert len(profile["findings"]) >= 2


def test_modify_loop_raises_question_when_change_unresolved(servers):
    be_port, ex_port = servers
    base = f"http://127.0.0.1:{be_port}"

    # seed a profile for APP first (modify builds changes from the stored profile)
    httpx.put(f"{base}/api/code-profiles/{APP}", json={
        "app_id": APP, "repo_url": "r", "branch": "b", "scanner": "e2e",
        "cloud_readiness": 0.5, "findings": [
            {"category": "secrets_in_repo", "severity": "high", "file": "a",
             "line": 7, "message": "secret",
             # evidence with no password=/secret= token → extractor returns "" →
             # the change is unresolved → mock raises a Question
             "evidence": "•••• (redacted)", "remediation": "x"}],
        "required_changes": [{"title": "secret", "category": "secrets_in_repo",
                              "file": "a", "line": 7, "effort": "low", "description": "d"}]},
        headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10)

    # modify: build_change_spec will emit a change with empty old (secret redacted)
    # → the mock should raise a Question
    r = httpx.post(f"{base}/api/executor/trigger", json={
        "app_id": APP, "repo_url": "r", "branch": "b", "action": "modify",
        "mode": "plan"}, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body.get("changes", [])) >= 1   # backend built a concrete change list

    # poll for the question the mock raised
    q = None
    t0 = time.time()
    while time.time() - t0 < 20:
        try:
            qs = httpx.get(f"{base}/api/apps/{APP}/questions?status=pending", timeout=5)
            if qs.status_code == 200 and qs.json():
                q = qs.json()[0]
                break
        except Exception:
            pass
        time.sleep(0.5)
    assert q is not None, "mock did not raise a question for the unresolved change"
    assert q["kind"] in ("value", "choice")
    assert q["context"]["category"] == "secrets_in_repo"

    # operator answers; the executor would poll and continue
    a = httpx.post(f"{base}/api/questions/{q['id']}/answer",
                   json={"answer": "${DB_PASSWORD}", "answered_by": "e2e"}, timeout=10)
    assert a.status_code == 200 and a.json()["status"] == "answered"