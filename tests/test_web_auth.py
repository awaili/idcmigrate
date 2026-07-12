"""Web auth — the shared-password login gate over the UI + API.

Auth is OFF when no password is configured (so the rest of the suite + a fresh
deploy stay open). These tests turn it ON via the DB system_config `web_password`
override (same path the runtime uses), exercise the gate + login flow, then clean
up so auth is off again for any later tests in the same process.
"""
import pytest

pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient
import idc.backend.app as m

client = TestClient(m.app)


@pytest.fixture
def _web_auth_on():
    """Turn web auth on with a known password + executor bearer, then off again."""
    m.STORE.set_config("web_password", "test-pw")
    m.STORE.set_config("executor_token", "exec-tok")
    try:
        yield {"password": "test-pw", "bearer": "exec-tok"}
    finally:
        with m.STORE.tx() as cur:
            cur.execute(m.STORE._x(
                "DELETE FROM system_config WHERE k IN ('web_password','executor_token')"))


def test_auth_off_when_no_password():
    # no web_password configured (fixture cleaned up / not set) -> open
    with m.STORE.tx() as cur:
        cur.execute(m.STORE._x("DELETE FROM system_config WHERE k='web_password'"))
    assert client.get("/api/stats").status_code == 200


def test_auth_gates_api_and_redirects_pages(_web_auth_on):
    assert client.get("/api/stats").status_code == 401
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_public_paths_stay_open(_web_auth_on):
    assert client.get("/login").status_code == 200
    assert client.get("/executor").status_code == 200
    assert client.get("/assets/app.css", follow_redirects=False).status_code != 401


def test_login_wrong_password_rejected(_web_auth_on):
    r = client.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert r.status_code == 401


def test_login_then_session_authes_api(_web_auth_on):
    r = client.post("/login", data={"password": _web_auth_on["password"]},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    # the TestClient cookie jar now carries the session -> API is open
    assert client.get("/api/stats").status_code == 200
    # logout ends the session
    assert client.post("/logout", follow_redirects=False).status_code == 303
    assert client.get("/api/stats").status_code == 401


def test_executor_bearer_bypasses_session_gate(_web_auth_on):
    # a fresh client with no session cookie but a valid executor bearer gets in
    # ONLY on the executor-contract surface (not operator endpoints like /api/stats)
    c = TestClient(m.app)
    r = c.get("/api/change-jobs", headers={"Authorization": "Bearer " + _web_auth_on["bearer"]})
    assert r.status_code == 200
    # bearer is NOT honored on operator-only endpoints
    assert c.get("/api/stats", headers={"Authorization": "Bearer " + _web_auth_on["bearer"]}).status_code == 401
    # wrong bearer is rejected even on the contract surface
    assert c.get("/api/change-jobs", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_change_password_requires_current_and_session(_web_auth_on):
    # unauthed -> 401
    assert client.post("/api/auth/password",
                       data={"password": "newpass", "current": _web_auth_on["password"]}).status_code == 401
    # log in, then change it (with the current password)
    client.post("/login", data={"password": _web_auth_on["password"]}, follow_redirects=False)
    # wrong current -> 401
    assert client.post("/api/auth/password", data={"password": "newpass123", "current": "wrong"}).status_code == 401
    r = client.post("/api/auth/password", data={"password": "newpass123", "current": _web_auth_on["password"]})
    assert r.status_code == 200 and r.json()["updated"] is True
    # the new password now logs in (on a fresh client)
    c = TestClient(m.app)
    assert c.post("/login", data={"password": "newpass123"}, follow_redirects=False).status_code == 303
    # the old one no longer does
    assert c.post("/login", data={"password": _web_auth_on["password"]}, follow_redirects=False).status_code == 401