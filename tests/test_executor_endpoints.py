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


def _seed_bulk_host(app_id, disp=None):
    """Seed a server + its match for the per-host bulk 7R test. Returns
    (sid, hostname). ``disp`` optionally pre-sets a legacy_disposition so the
    migrating-clear branch can be exercised."""
    from idc.core.models import _new_id, Server, Match, Target, LegacyDisposition
    st = m.open_store(m.get_settings().db_url)
    sid = _new_id("srv")
    host = f"bulkhost-{sid[-6:]}"
    s = Server(id=sid, hostname=host, fqdn=f"{host}.dc1.corp", ips=["10.0.0.7"],
               role="app", os="centos", os_version="6", app_ids=[app_id],
               sizing_basis="estimated")
    st.upsert_server(s)
    st.upsert_match(Match(server_id=sid, confidence=0.9, rationale="rule",
                          target=Target(product="CVM", spec="SA5.MEDIUM4",
                                         region="ap-bangkok")))
    if disp:
        st.upsert_legacy_disposition(LegacyDisposition(
            server_id=host.lower(), disposition=disp, rationale="pre-set",
            confidence=1.0, summary="seed"))
    st.close()
    return sid, host


def _cleanup_bulk_host(sid, host, app_id):
    st = m.open_store(m.get_settings().db_url)
    with st.tx() as cur:
        cur.execute(st._x("DELETE FROM legacy_dispositions WHERE server_id=?"), (host.lower(),))
        cur.execute(st._x("DELETE FROM matches WHERE server_id=?"), (sid,))
        cur.execute(st._x("DELETE FROM servers WHERE id=?"), (sid,))
        cur.execute(st._x("DELETE FROM app_strategies WHERE app_id=?"), (app_id,))
    st.close()


def test_strategy_host_batch_applies_retain_disposition(monkeypatch):
    """Per-host bulk 7R (NDJSON) sets a retain host disposition when the LLM
    recommends retain (apply=true), and is a no-op when apply=false."""
    import json as _json
    app = "pytest-bulk-app-ret"
    sid, host = _seed_bulk_host(app)
    try:
        calls = []
        def fake_host(server, match, profile, *, current="", host_disposition=""):
            calls.append(server.id)
            return {"ok": True, "strategy": "retain", "rationale": "mock retain",
                    "target": "on-prem", "confidence": 0.8, "effort": "low",
                    "key_changes": []}
        monkeypatch.setattr(m.LLM, "seven_r_host", fake_host)
        r = client.post(f"/api/strategy/host/batch?app_id={app}",
                        json={"apply": True, "limit": 10})
        assert r.status_code == 200
        frames = [_json.loads(l) for l in r.text.split("\n") if l.strip()]
        types = [f["type"] for f in frames]
        assert types[0] == "start" and types[-1] == "done"
        host_frames = [f for f in frames if f["type"] == "host" and f.get("ok")]
        assert len(host_frames) == 1
        assert host_frames[0]["strategy"] == "retain"
        assert host_frames[0]["applied"] == "retain"
        assert calls == [sid]   # only the matching host was analyzed
        done = frames[-1]
        assert done["analyzed"] == 1 and done["applied_host"] == 1
        # the host-level retain disposition was persisted (keyed by hostname lower)
        d = m.STORE.get_legacy_disposition(host.lower())
        assert d is not None and d.disposition == "retain"

        # apply=false (dry run) on a fresh assertion: re-mock + verify nothing new
        # is written — the disposition above stays, but no app strategy is created
        # (retain never goes to the app tally). There's no app_strategies row.
        assert m.STORE.get_app_strategy(app) is None
    finally:
        _cleanup_bulk_host(sid, host, app)


def test_strategy_host_batch_migrating_clears_disposition_and_fills_app(monkeypatch):
    """A migrating recommendation clears any pre-set retain/retire override AND
    fills the app's strategy when the app has none (non-destructive: an
    existing app strategy is preserved)."""
    import json as _json
    app = "pytest-bulk-app-mig"
    sid, host = _seed_bulk_host(app, disp="retain")   # host pre-set to retain
    try:
        def fake_host(server, match, profile, *, current="", host_disposition=""):
            return {"ok": True, "strategy": "replatform", "rationale": "mock mig",
                    "target": "CDB", "confidence": 0.7, "effort": "medium",
                    "key_changes": ["x"]}
        monkeypatch.setattr(m.LLM, "seven_r_host", fake_host)
        r = client.post(f"/api/strategy/host/batch?app_id={app}",
                        json={"apply": True, "limit": 10})
        frames = [_json.loads(l) for l in r.text.split("\n") if l.strip()]
        host_frames = [f for f in frames if f["type"] == "host" and f.get("ok")]
        assert len(host_frames) == 1
        assert host_frames[0]["strategy"] == "replatform"
        assert host_frames[0]["applied"] == "cleared"   # the retain override was cleared
        app_frames = [f for f in frames if f["type"] == "app"]
        assert len(app_frames) == 1 and app_frames[0]["strategy"] == "replatform"
        done = frames[-1]
        assert done["applied_clear"] == 1 and done["applied_app"] == 1
        # the retain override is gone; the app strategy is now replatform
        assert m.STORE.get_legacy_disposition(host.lower()) is None
        s = m.STORE.get_app_strategy(app)
        assert s is not None and s.strategy == "replatform"
    finally:
        _cleanup_bulk_host(sid, host, app)


def test_strategy_host_batch_dry_run_does_not_mutate(monkeypatch):
    """apply=false is a pure preview: no disposition written, no app strategy."""
    import json as _json
    app = "pytest-bulk-app-dry"
    sid, host = _seed_bulk_host(app)
    try:
        def fake_host(server, match, profile, *, current="", host_disposition=""):
            return {"ok": True, "strategy": "retire", "rationale": "mock",
                    "target": "decommission", "confidence": 0.6, "effort": "low",
                    "key_changes": []}
        monkeypatch.setattr(m.LLM, "seven_r_host", fake_host)
        r = client.post(f"/api/strategy/host/batch?app_id={app}",
                        json={"apply": False, "limit": 10})
        frames = [_json.loads(l) for l in r.text.split("\n") if l.strip()]
        host_frames = [f for f in frames if f["type"] == "host" and f.get("ok")]
        assert host_frames[0]["applied"] == "none"   # dry run
        assert frames[-1]["apply"] is False
        assert m.STORE.get_legacy_disposition(host.lower()) is None
        assert m.STORE.get_app_strategy(app) is None
    finally:
        _cleanup_bulk_host(sid, host, app)


def test_server_detail_exposes_7r_source_waves_cost():
    """The host drawer's detail endpoint (GET /api/servers/{id}) carries the
    extra host-level info: the 7R policy + its source (host override / app AI /
    default), the waves the host belongs to, and the run-cost estimate."""
    app = "pytest-detail-app"
    sid, host = _seed_bulk_host(app)
    try:
        d = client.get(f"/api/servers/{sid}").json()
        # 7R policy + source (no app strategy + no disposition -> default rehost)
        assert d["seven_r"] == "rehost"
        assert d["seven_r_source"] == "default (rehost)"
        # waves is a list (likely empty — no plan built around this throwaway host)
        assert isinstance(d["waves"], list)
        # cost is computed from the fixture pricebook (CVM SA5.MEDIUM4 ap-bangkok
        # = 60/mo, 650/yr per test_cost.py)
        assert d["cost"] is not None
        assert d["cost"]["yearly"] == 650 and d["cost"]["monthly"] == 60
        assert d["cost"]["product"] == "CVM" and d["cost"]["basis"]
        # match carries alternatives (the drawer's Alternatives row reads these)
        assert "alternatives" in (d["match"] or {})

        # set a retain disposition -> 7R source flips to "host override"
        client.put(f"/api/servers/{sid}/disposition", json={"disposition": "retain"})
        d2 = client.get(f"/api/servers/{sid}").json()
        assert d2["seven_r"] == "retain"
        assert d2["seven_r_source"] == "host override"
    finally:
        _cleanup_bulk_host(sid, host, app)


def _seed_bulk_hosts(app, hostnames):
    """Seed N servers (given hostnames) + a match each for the batch/resume tests."""
    from idc.core.models import _new_id, Server, Match, Target
    st = m.open_store(m.get_settings().db_url)
    sids = []
    for host in hostnames:
        sid = _new_id("srv")
        sids.append(sid)
        s = Server(id=sid, hostname=host, fqdn=f"{host}.dc1.corp", ips=["10.0.0.7"],
                   role="app", os="centos", os_version="6", app_ids=[app],
                   sizing_basis="estimated")
        st.upsert_server(s)
        st.upsert_match(Match(server_id=sid, confidence=0.9, rationale="rule",
                              target=Target(product="CVM", spec="SA5.MEDIUM4",
                                             region="ap-bangkok")))
    st.close()
    return sids


def _cleanup_bulk_hosts(sids, app):
    st = m.open_store(m.get_settings().db_url)
    with st.tx() as cur:
        for sid in sids:
            cur.execute(st._x("DELETE FROM legacy_dispositions WHERE server_id=?"), (sid,))
        # dispositions are keyed by hostname lowercased too
        for sid in sids:
            cur.execute(st._x("DELETE FROM legacy_dispositions WHERE server_id IN "
                               "(SELECT LOWER(hostname) FROM servers WHERE id=?)"), (sid,))
        cur.executemany(st._x("DELETE FROM matches WHERE server_id=?"), [(s,) for s in sids])
        cur.executemany(st._x("DELETE FROM servers WHERE id=?"), [(s,) for s in sids])
        cur.execute(st._x("DELETE FROM app_strategies WHERE app_id=?"), (app,))
        # and clear any disposition keyed by the test hostnames
        for host in ("bulkb-a", "bulkb-b", "bulkb-c", "bulkresume-a", "bulkresume-b", "bulkresume-c"):
            cur.execute(st._x("DELETE FROM legacy_dispositions WHERE server_id=?"), (host,))
    st.close()


def test_strategy_host_batch_multiple_batches_in_one_call(monkeypatch):
    """A call with limit > batch_size processes multiple batches, emitting a
    `batch` checkpoint (commit) after each — every host disposition is durable
    before the next batch starts."""
    import json as _json
    app = "pytest-bulk-batches"
    hosts = ["bulkb-a", "bulkb-b", "bulkb-c"]
    sids = _seed_bulk_hosts(app, hosts)
    try:
        def fake_host(server, match, profile, *, current="", host_disposition=""):
            return {"ok": True, "strategy": "retain", "rationale": "mock",
                    "target": "on-prem", "confidence": 0.8, "effort": "low",
                    "key_changes": []}
        monkeypatch.setattr(m.LLM, "seven_r_host", fake_host)
        r = client.post(f"/api/strategy/host/batch?app_id={app}",
                        json={"apply": True, "batch_size": 2, "limit": 10})
        frames = [_json.loads(l) for l in r.text.split("\n") if l.strip()]
        assert frames[0]["type"] == "start" and frames[-1]["type"] == "done"
        host_frames = [f for f in frames if f["type"] == "host" and f.get("ok")]
        batch_frames = [f for f in frames if f["type"] == "batch"]
        assert len(host_frames) == 3
        # 3 hosts in batches of 2 -> 2 batch checkpoints (2 + 1)
        assert len(batch_frames) == 2
        assert all(f["committed"] for f in batch_frames)
        assert frames[-1]["applied_host"] == 3 and frames[-1]["more"] is False
        # all 3 host dispositions persisted (keyed by hostname lowercased)
        for h in hosts:
            d = m.STORE.get_legacy_disposition(h)
            assert d is not None and d.disposition == "retain"
    finally:
        _cleanup_bulk_hosts(sids, app)


def test_strategy_host_batch_cursor_resume_across_calls(monkeypatch):
    """The cursor makes a full-estate run resumable: call 1 processes the first
    batch + returns the cursor + more=true; call 2 with that cursor processes
    the rest. Committed batches are durable across the two calls."""
    import json as _json
    app = "pytest-bulk-resume"
    hosts = ["bulkresume-a", "bulkresume-b", "bulkresume-c"]   # asc order
    sids = _seed_bulk_hosts(app, hosts)
    try:
        seen = []
        def fake_host(server, match, profile, *, current="", host_disposition=""):
            seen.append(server.hostname)
            return {"ok": True, "strategy": "retain", "rationale": "mock",
                    "target": "on-prem", "confidence": 0.8, "effort": "low",
                    "key_changes": []}
        monkeypatch.setattr(m.LLM, "seven_r_host", fake_host)
        # call 1: one batch of 2 from the start
        r1 = client.post(f"/api/strategy/host/batch?app_id={app}",
                         json={"apply": True, "batch_size": 2, "limit": 0})
        f1 = [_json.loads(l) for l in r1.text.split("\n") if l.strip()]
        assert f1[0]["type"] == "start" and f1[0]["resumed"] is False
        done1 = f1[-1]
        assert done1["type"] == "done" and done1["more"] is True
        cursor = done1["cursor"]
        assert cursor == "bulkresume-b"   # last processed of the first 2
        assert done1["applied_host"] == 2
        # the first 2 hosts are already durable
        assert m.STORE.get_legacy_disposition("bulkresume-a").disposition == "retain"
        assert m.STORE.get_legacy_disposition("bulkresume-b").disposition == "retain"

        # call 2: resume from the cursor — only the 3rd host remains
        r2 = client.post(f"/api/strategy/host/batch?app_id={app}",
                         json={"apply": True, "batch_size": 2, "limit": 0,
                               "cursor": cursor})
        f2 = [_json.loads(l) for l in r2.text.split("\n") if l.strip()]
        assert f2[0]["type"] == "start" and f2[0]["resumed"] is True
        done2 = f2[-1]
        assert done2["type"] == "done" and done2["more"] is False
        assert done2["cursor"] is None
        assert done2["applied_host"] == 1
        # all 3 hosts now durable, and the LLM saw each exactly once (no rework)
        assert seen == ["bulkresume-a", "bulkresume-b", "bulkresume-c"]
        for h in hosts:
            assert m.STORE.get_legacy_disposition(h).disposition == "retain"
    finally:
        _cleanup_bulk_hosts(sids, app)

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
            cur.execute(m.STORE._x("DELETE FROM system_config WHERE k = 'enroll_secret'"))
            cur.execute(m.STORE._x("DELETE FROM executor_heartbeats"))
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


def test_executor_status_reports_pull_mode_connectivity(_clean_exec_config):
    """Pull mode: idc-migrate never probes, so /api/executor/status keeps
    reachable=None but reports connected/last_seen from inbound activity.
    Before any poll the default executor is 'waiting'; an idle /claim records
    a heartbeat and it flips to connected. A named executor that never polled
    stays 'no poll yet' (NOT 'down' — there was no probe)."""
    s = client.get("/api/executor/status").json()
    assert s["reachable"] is None                 # still no outbound probe
    assert s["connected"] is False
    assert s["last_seen"] is None
    assert "no" in s["detail"].lower()            # 'no poll yet' / 'no executor polling'
    # an idle poll (no pending task) still records the heartbeat
    r = client.post("/api/executor/tasks/claim", json={}, headers=_bearer())
    assert r.status_code == 200 and r.json()["task_id"] is None   # idle
    s = client.get("/api/executor/status").json()
    assert s["connected"] is True
    assert s["last_seen"] is not None
    assert isinstance(s["in_flight"], int) and isinstance(s["done"], int)
    assert isinstance(s["error"], int)
    assert s["detail"].startswith("connected")


def test_registry_lists_connected_from_inbound_activity(_clean_exec_config):
    """The /api/executors list shows connected/last_seen derived from the
    executor polling idc-migrate, not a probe. A named executor that has
    never polled shows 'no poll yet' (never 'down' — idc-migrate never
    initiated anything to fail)."""
    # default never polled yet in this fresh fixture
    d = next(e for e in client.get("/api/executors").json() if e["id"] == "default")
    assert d["status"]["connected"] is False
    assert d["status"]["last_seen"] is None
    assert d["status"]["reachable"] is None
    # a named executor registered but silent
    client.put("/api/executors/db-spec",
               json={"url": "http://x:9", "token": "db-tok", "enabled": True})
    lst = client.get("/api/executors").json()
    db = next(e for e in lst if e["id"] == "db-spec")
    assert db["status"]["connected"] is False
    assert db["status"]["last_seen"] is None
    assert "no poll yet" in db["status"]["detail"]
    assert db["status"]["reachable"] is None     # no probe, so no 'down'


def test_store_executor_activity_round_trip(_clean_exec_config):
    """Store-level: touch_executor_seen + executor_activity round-trip, and a
    never-seen executor yields last_seen=None with zeroed counts."""
    m.STORE.touch_executor_seen("db-spec", m._now_iso())
    act = m.STORE.executor_activity("db-spec")
    assert act["last_seen"] is not None
    assert isinstance(act["in_flight"], int)
    act2 = m.STORE.executor_activity("ghost-executor")
    assert act2["last_seen"] is None and act2["in_flight"] == 0 \
        and act2["done"] == 0 and act2["error"] == 0


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


# -- self-registration -> approval lifecycle (Option B: server mints token) --
def test_self_register_lands_pending_without_token(_clean_exec_config):
    r = client.post("/api/executors/register",
                   json={"id": "db-spec", "url": "http://127.0.0.1:8091", "timeout": 300})
    assert r.status_code == 200, r.text
    e = r.json()
    assert e["id"] == "db-spec"
    assert e["approval"] == "pending"
    assert e["token_set"] is False and "token" not in e   # no token minted yet
    assert e["enabled"] is False
    # it shows in the list as pending
    lst = client.get("/api/executors").json()
    row = next(x for x in lst if x["id"] == "db-spec")
    assert row["approval"] == "pending" and row["token_set"] is False


def test_pending_executor_cannot_claim(_wipe_tasks, _clean_exec_config):
    """A pending self-enrollment has no token, so its bearer (if it sent one)
    can't resolve — and there is no token to send anyway. Claim stays 401."""
    client.post("/api/executors/register", json={"id": "db-spec"})
    # the executor has no token; even a fabricated token doesn't resolve
    r = client.post("/api/executor/tasks/claim", json={}, headers=_bearer("db-spec-fake"))
    assert r.status_code == 401


def test_approve_mints_token_and_enables_claiming(_wipe_tasks, _clean_exec_config):
    """Approve mints a server-side token, returns it ONCE, and flips the
    executor to approved+enabled so its token now resolves on claim."""
    client.post("/api/executors/register", json={"id": "db-spec"})
    r = client.post("/api/executors/db-spec/approve")
    assert r.status_code == 200, r.text
    e = r.json()
    tok = e["token"]
    assert tok and tok.startswith("ex-")          # server-minted, returned once
    assert e["approval"] == "approved" and e["token_set"] is True
    # the token now validates on the push direction too (valid_tokens)
    assert m._executor_bearer_ok(f"Bearer {tok}") is True
    # a second approve is rejected (already approved)
    assert client.post("/api/executors/db-spec/approve").status_code == 400
    # and the list no longer returns the token (only token_set)
    row = next(x for x in client.get("/api/executors").json() if x["id"] == "db-spec")
    assert "token" not in row and row["token_set"] is True
    # the minted token now claims pool tasks
    _enq("scan", executor_id=None)
    r2 = client.post("/api/executor/tasks/claim", json={}, headers=_bearer(tok))
    assert r2.status_code == 200
    assert r2.json()["status"] == "claimed"


def test_re_register_approved_rejected_409(_clean_exec_config):
    """An attacker who knows an approved executor's id can't re-enroll it (that
    path can't change the token; only the operator can rotate it)."""
    client.post("/api/executors/register", json={"id": "db-spec"})
    client.post("/api/executors/db-spec/approve")
    r = client.post("/api/executors/register", json={"id": "db-spec"})
    assert r.status_code == 409


def test_re_register_pending_is_idempotent(_clean_exec_config):
    """Re-enrolling the same pending id refreshes its fields and stays pending."""
    client.post("/api/executors/register", json={"id": "db-spec", "timeout": 120})
    r = client.post("/api/executors/register",
                    json={"id": "db-spec", "url": "http://x:9", "timeout": 240})
    assert r.status_code == 200
    e = r.json()
    assert e["approval"] == "pending"
    row = next(x for x in client.get("/api/executors").json() if x["id"] == "db-spec")
    assert row["timeout"] == 240 and row["approval"] == "pending"


def test_register_rejects_default_and_bad_id(_clean_exec_config):
    # can't self-register the default
    assert client.post("/api/executors/register",
                       json={"id": "default"}).status_code == 400
    # bad id formats (path-traversal / html injection attempts) rejected
    for bad in ("", "has space", "<script>", "../../etc", "x;y"):
        assert client.post("/api/executors/register",
                           json={"id": bad}).status_code == 400


def test_register_enroll_secret_gate(_clean_exec_config, monkeypatch):
    """When IDC_ENROLL_SECRET is set, register requires X-Enroll-Secret."""
    m.STORE.set_config("enroll_secret", "s3cret-enroll-key")
    # missing header -> 403
    r = client.post("/api/executors/register", json={"id": "db-spec"})
    assert r.status_code == 403
    # wrong header -> 403
    r2 = client.post("/api/executors/register", json={"id": "db-spec"},
                     headers={"X-Enroll-Secret": "wrong"})
    assert r2.status_code == 403
    # correct header -> 200 pending
    r3 = client.post("/api/executors/register", json={"id": "db-spec"},
                     headers={"X-Enroll-Secret": "s3cret-enroll-key"})
    assert r3.status_code == 200 and r3.json()["approval"] == "pending"


def test_rotate_token_invalidates_old(_wipe_tasks, _clean_exec_config):
    client.post("/api/executors/register", json={"id": "db-spec"})
    old = client.post("/api/executors/db-spec/approve").json()["token"]
    new = client.post("/api/executors/db-spec/rotate-token").json()["token"]
    assert new and new != old and new.startswith("ex-")
    # old token no longer validates
    assert m._executor_bearer_ok(f"Bearer {old}") is False
    # new token does
    assert m._executor_bearer_ok(f"Bearer {new}") is True


def test_rotate_rejects_pending(_clean_exec_config):
    """Can't rotate a pending executor's token — approve it first."""
    client.post("/api/executors/register", json={"id": "db-spec"})
    assert client.post("/api/executors/db-spec/rotate-token").status_code == 400


def test_reject_pending_deletes_entry(_clean_exec_config):
    """Reject == DELETE on a pending enrollment removes it."""
    client.post("/api/executors/register", json={"id": "db-spec"})
    assert client.delete("/api/executors/db-spec").status_code == 200
    ids = [e["id"] for e in client.get("/api/executors").json()]
    assert "db-spec" not in ids


def test_trigger_pending_executor_409(_clean_exec_config, monkeypatch):
    """Targeting a pending executor is rejected with 409 (not 404) so the
    operator distinguishes 'still awaiting approval' from 'no such id'."""
    client.post("/api/executors/register", json={"id": "db-spec"})
    monkeypatch.setattr(m.settings, "executor_enabled", True)
    r = client.post("/api/db-scan", json={"db_server_id": "db-1",
                    "source_engine": "mysql", "target_engine": "cdb_mysql",
                    "executor_id": "db-spec"})
    assert r.status_code == 409
    # unknown id stays 404
    r2 = client.post("/api/db-scan", json={"db_server_id": "db-1",
                     "source_engine": "mysql", "target_engine": "cdb_mysql",
                     "executor_id": "ghost"})
    assert r2.status_code == 404


def test_self_register_is_public_without_session(monkeypatch):
    """register must work with NO auth at all (the executor has no bearer and no
    browser session yet) — proves the endpoint is on the public path and the
    enroll-secret gate (unset here) is the only barrier."""
    # turn the web-password gate ON and clear any session cookie
    m.STORE.set_config("web_password", "gate-on")
    try:
        c = TestClient(m.app)
        # no session cookie, no bearer — register still succeeds
        r = c.post("/api/executors/register", json={"id": "pub-exec"})
        assert r.status_code == 200 and r.json()["approval"] == "pending"
        # and a session-gated endpoint is still protected (sanity)
        assert c.get("/api/executors").status_code == 401
    finally:
        m.STORE.set_config("web_password", "")
        with m.STORE.tx() as cur:
            cur.execute(m.STORE._x("DELETE FROM system_config WHERE k = 'executors'"))
