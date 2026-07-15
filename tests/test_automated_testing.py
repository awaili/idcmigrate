"""F11 — automated testing: test-gen / run / compare pre-vs-post + the
test_regression cutover gate.

Covers the TestCase/TestRun/TestResult/TestDiff models, the mock executor's
gen + run + compare (incl. one regression to exercise the gate), the
test_regression gate verdict (pending → pass/fail from the stored diff), and
the DB-backed push/retrieve endpoints.
"""
import pytest

from idc.core.execute import run_validation_gates, _gate_test_regression
from idc.core.models import (
    MigrationJob, Server, TestCase, TestDiff, TestDiffItem, TestRun, TestResult,
    TEST_FUNCTIONAL, TEST_INTEGRATION, TEST_REGRESSION,
    TEST_PHASE_POST, TEST_PHASE_PRE,
    TV_NEW_FAILURE, TV_PASS, TV_REGRESSION,
)


# -- models -----------------------------------------------------------------
def test_test_case_roundtrip():
    c = TestCase(app_id="orders", name="health", kind=TEST_FUNCTIONAL,
                 endpoint="/health", method="GET",
                 expected={"status": 200}, setup="none")
    rt = TestCase.from_dict(c.to_dict())
    assert rt.kind == TEST_FUNCTIONAL
    assert rt.expected == {"status": 200}


def test_test_run_roundtrip_with_results():
    r = TestRun(app_id="orders", phase=TEST_PHASE_PRE, target="http://onprem",
                results=[TestResult(case="health", status="pass", duration_ms=35,
                                    response_summary="200 OK")],
                passed=1, scan_id="sc")
    rt = TestRun.from_dict(r.to_dict())
    assert rt.phase == TEST_PHASE_PRE
    assert len(rt.results) == 1
    assert rt.results[0].status == "pass"
    assert rt.results[0].duration_ms == 35


def test_test_diff_roundtrip():
    d = TestDiff(app_id="orders", pre_run_id="r1", post_run_id="r2",
                 diff=[TestDiffItem(case="health", pre_status="pass",
                                    post_status="pass", verdict=TV_PASS),
                       TestDiffItem(case="orders_total", pre_status="pass",
                                    post_status="fail", verdict=TV_REGRESSION,
                                    detail="500 NPE")],
                 regressions=1)
    rt = TestDiff.from_dict(d.to_dict())
    assert rt.regressions == 1
    assert rt.diff[1].verdict == TV_REGRESSION


# -- mock gen / run / compare ----------------------------------------------
def test_mock_test_gen_emits_cases():
    from idc.executor_mock.app import _fake_test_cases
    cases = _fake_test_cases("orders",
                             {"code_profile": {"framework": "spring"}})
    names = {c["name"] for c in cases}
    assert "health" in names
    assert all(c["app_id"] == "orders" for c in cases)


def test_mock_test_run_post_introduces_a_failure():
    from idc.executor_mock.app import _fake_test_results
    pre = _fake_test_results("orders", TEST_PHASE_PRE, "http://onprem")
    post = _fake_test_results("orders", TEST_PHASE_POST, "http://cloud")
    assert all(r["status"] == "pass" for r in pre)
    # the post run flips the regression case to fail
    assert any(r["status"] == "fail" for r in post)
    failed = next(r for r in post if r["status"] == "fail")
    assert failed["case"] == "regression_orders_total"


def test_mock_test_compare_produces_one_regression():
    from idc.executor_mock.app import _fake_test_results
    from idc.core.models import TV_VERDICTS
    # the mock builds a 1-regression diff; verify the verdict vocabulary
    assert TV_REGRESSION in TV_VERDICTS and TV_NEW_FAILURE in TV_VERDICTS
    # actually exercise the compare contract that _run_test_compare relies on:
    # pre is all-pass, post flips exactly one case to fail. A case that was
    # pass in pre and fail in post is a regression — so the diff the mock would
    # build has exactly one regression. This fails if _fake_test_results stops
    # flipping exactly one case (the gate would then get a wrong verdict).
    pre = {r["case"]: r["status"] for r in _fake_test_results("orders", TEST_PHASE_PRE, "http://onprem")}
    post = {r["case"]: r["status"] for r in _fake_test_results("orders", TEST_PHASE_POST, "http://cloud")}
    assert all(s == "pass" for s in pre.values())
    regressions = [c for c in post if pre.get(c) == "pass" and post[c] == "fail"]
    assert len(regressions) == 1
    assert regressions[0] == "regression_orders_total"


# -- test_regression gate ---------------------------------------------------
def test_gate_test_regression_pass_when_no_diff_getter():
    # no contextvar set → PASS (don't lock out estates that don't use testing)
    status, detail = _gate_test_regression({"app_id": "a"}, settings=None)
    assert status == "pass"
    assert "not configured" in detail


def test_gate_test_regression_fails_on_regression(monkeypatch):
    import idc.core.execute as ex
    diff = TestDiff(app_id="a", regressions=1,
                    diff=[TestDiffItem(case="x", verdict=TV_REGRESSION)])
    token = ex._TEST_DIFF_GETTER.set(lambda app: diff if app == "a" else None)
    try:
        status, detail = _gate_test_regression({"app_id": "a"}, settings=None)
        assert status == "fail"
        assert "1 regression" in detail
    finally:
        ex._TEST_DIFF_GETTER.reset(token)


def test_gate_test_regression_passes_when_clean(monkeypatch):
    import idc.core.execute as ex
    diff = TestDiff(app_id="a", regressions=0,
                    diff=[TestDiffItem(case="x", verdict=TV_PASS)])
    token = ex._TEST_DIFF_GETTER.set(lambda app: diff if app == "a" else None)
    try:
        status, detail = _gate_test_regression({"app_id": "a"}, settings=None)
        assert status == "pass"
    finally:
        ex._TEST_DIFF_GETTER.reset(token)


def test_run_validation_gates_passes_store_to_the_gate(monkeypatch):
    """run_validation_gates(store=...) wires the test_regression gate to the
    store's get_test_diff; a regression makes a must_pass gate fail."""
    import idc.core.execute as ex
    diff = TestDiff(app_id="orders", regressions=1)

    class _Store:
        def get_test_diff(self, app_id):
            return diff if app_id == "orders" else None

    job = MigrationJob(server_id="s", app_id="orders")
    job.validation_gates = [{"name": "test-regression", "kind": "test_regression",
                             "must_pass": True, "app_id": "orders"}]
    from idc.config import Settings
    ok, gates = run_validation_gates(job, Settings(), store=_Store())
    assert ok is False
    assert gates[0]["result"] == "fail"


def test_run_validation_gates_no_store_passes_when_no_diff():
    """No store / no diff → gate PASSES (must not lock out estates that don't
    use automated testing). Regression is the only thing that blocks."""
    job = MigrationJob(server_id="s", app_id="orders")
    job.validation_gates = [{"name": "test-regression", "kind": "test_regression",
                             "must_pass": True, "app_id": "orders"}]
    from idc.config import Settings
    ok, gates = run_validation_gates(job, Settings(), store=None)
    assert ok is True               # no diff → pass → finalize allowed
    assert gates[0]["result"] == "pass"


# -- DB-backed push + retrieve endpoints ------------------------------------
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


def _cleanup(app_id, run_ids=()):
    st = open_store(get_settings().db_url)
    with st.tx() as cur:
        cur.execute(st._x("DELETE FROM test_cases WHERE app_id=?"), (app_id,))
        cur.execute(st._x("DELETE FROM test_diffs WHERE app_id=?"), (app_id,))
        for rid in run_ids:
            cur.execute(st._x("DELETE FROM test_runs WHERE id=?"), (rid,))
    st.close()


def test_put_and_get_test_cases():
    app = "orders-tc"
    try:
        body = {"app_id": app, "cases": [
            {"name": "health", "kind": "functional", "endpoint": "/health",
             "method": "GET", "expected": {"status": 200}},
            {"name": "orders_total", "kind": "regression", "endpoint": "/orders/total",
             "method": "GET", "expected": {"status": 200}},
        ]}
        r = client.put(f"/api/test-cases/{app}", json=body, headers=_bearer())
        assert r.status_code == 200, r.text
        assert set(r.json()["cases"]) == {"health", "orders_total"}
        g = client.get(f"/api/test-cases/{app}").json()
        assert {c["name"] for c in g} == {"health", "orders_total"}
    finally:
        _cleanup(app)


def test_put_test_cases_replaces_on_regen():
    app = "orders-repl"
    try:
        client.put(f"/api/test-cases/{app}",
                   json={"app_id": app, "cases": [{"name": "a", "kind": "functional"}]},
                   headers=_bearer())
        client.put(f"/api/test-cases/{app}",
                   json={"app_id": app, "cases": [{"name": "b", "kind": "integration"}]},
                   headers=_bearer())
        cases = client.get(f"/api/test-cases/{app}").json()
        assert {c["name"] for c in cases} == {"b"}   # old "a" replaced
    finally:
        _cleanup(app)


def test_put_and_get_test_run_and_diff():
    app = "orders-run"
    try:
        # push a pre run
        pre = client.put("/api/test-runs/r-pre", json={
            "id": "r-pre", "app_id": app, "phase": "pre",
            "target": "http://onprem",
            "results": [{"case": "health", "status": "pass", "duration_ms": 30}],
            "passed": 1, "failed": 0, "errors": 0, "scan_id": "sc"}, headers=_bearer())
        assert pre.status_code == 200, pre.text
        g = client.get("/api/test-runs/r-pre").json()
        assert g["phase"] == "pre" and g["results"][0]["status"] == "pass"
        # push a diff with a regression
        d = client.put(f"/api/test-diffs/{app}", json={
            "app_id": app, "pre_run_id": "r-pre", "post_run_id": "r-post",
            "diff": [{"case": "health", "pre_status": "pass", "post_status": "pass",
                      "verdict": "pass"},
                     {"case": "orders_total", "pre_status": "pass",
                      "post_status": "fail", "verdict": "regression",
                      "detail": "500 NPE"}],
            "regressions": 1, "scan_id": "sc", "summary": "1 regression",
        }, headers=_bearer())
        assert d.status_code == 200, d.text
        assert d.json()["regressions"] == 1
        gd = client.get(f"/api/test-diffs/{app}").json()
        assert gd["regressions"] == 1
        assert gd["diff"][1]["verdict"] == "regression"
    finally:
        _cleanup(app, run_ids=("r-pre",))


def test_test_diff_drives_gate_via_store():
    """End-to-end: a stored regression diff makes run_validation_gates fail the
    test_regression must_pass gate (the real cutover gate)."""
    from idc.config import Settings
    from idc.core.execute import run_validation_gates
    app = "orders-gate"
    try:
        client.put(f"/api/test-diffs/{app}", json={
            "app_id": app, "pre_run_id": "r1", "post_run_id": "r2",
            "diff": [{"case": "x", "pre_status": "pass", "post_status": "fail",
                      "verdict": "regression"}], "regressions": 1,
        }, headers=_bearer())
        st = open_store(get_settings().db_url)
        job = MigrationJob(server_id="s", app_id=app)
        job.validation_gates = [{"name": "test-regression",
                                 "kind": "test_regression", "must_pass": True,
                                 "app_id": app}]
        ok, gates = run_validation_gates(job, Settings(), store=st)
        st.close()
        assert ok is False
        assert gates[0]["result"] == "fail"
    finally:
        _cleanup(app)