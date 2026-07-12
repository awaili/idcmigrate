"""F9 — runtime inventory auto-gather tests.

Covers the pure gatherer (match port + role/os/software inference + stale
profile + port→service guesses), the merge semantics (operator override wins,
gathered fills gaps), and the backend ``GET /api/runtime-inventory/{server_id}``
+ the auto-gather-on-submit behavior of ``POST /api/runtime-containerize``.
"""
import pytest

from idc.core.runtime_inventory import gather_runtime_inventory, merge_inventory
from idc.core.models import Server, Match, Target, CodeProfile


def _srv(role="db", os_="oracle linux", host="db-oracle-01"):
    return Server(id="s1", hostname=host, fqdn=f"{host}.dc1", ips=["10.10.3.21"],
                   role=role, os=os_, app_ids=[])


def _mat(port=1521, product="CVM"):
    return Match(server_id="s1", target=Target(product=product, spec="x",
                 region="ap-shanghai", extras={"port": port} if port else {}),
                 confidence=0.7, method="rule", rationale="x")


# -- pure gatherer (no settings => no telemetry, deterministic) -------------
def test_gather_uses_match_port():
    inv = gather_runtime_inventory(_srv(), _mat(1521))
    assert 1521 in inv["ports"]
    assert inv["basis"]["ports"] == "match"


def test_gather_infers_software_from_role():
    inv = gather_runtime_inventory(_srv(role="db"), _mat(1521))
    assert "oracle" in inv["software"] and "mysql" in inv["software"]


def test_gather_port_service_guesses_strengthen_software():
    # port 6379 + role=app (no software hint) -> redis appears via port guess
    inv = gather_runtime_inventory(_srv(role="app"), _mat(6379))
    assert "redis" in inv["software"]


def test_gather_base_image_from_os():
    inv = gather_runtime_inventory(_srv(os_="ubuntu"), _mat(8080))
    assert inv["base_image"] == "ubuntu:22.04"


def test_gather_uses_stale_profile_for_software_and_process():
    prof = CodeProfile(app_id="a", language="java", runtime="jdk8",
                       framework="spring-boot-2.1")
    inv = gather_runtime_inventory(_srv(), _mat(8080), profile=prof)
    assert "jdk8" in inv["software"] and "spring-boot-2.1" in inv["software"]
    assert inv["process"] == "jdk8"
    assert inv["basis"]["process"] == "profile"


def test_gather_no_port_no_match():
    inv = gather_runtime_inventory(_srv(role="app"), None)
    assert inv["ports"] == []
    assert inv["basis"]["ports"] == "none"


def test_gather_role_with_no_software_hint():
    inv = gather_runtime_inventory(_srv(role="app"), _mat(8080))
    # role=app has no software entry; 8080 -> app-server guess
    assert "app-server" in inv["software"]


# -- merge semantics --------------------------------------------------------
def test_merge_operator_overrides_gathered():
    g = {"process": "", "ports": [1521], "software": ["oracle"], "base_image": "oraclelinux:8-slim"}
    m = merge_inventory(g, {"process": "java -jar app.jar", "ports": [1521],
                             "software": "oracle,openjdk-8"})
    assert m["process"] == "java -jar app.jar"   # operator wins
    assert m["ports"] == [1521]
    assert m["software"] == ["oracle", "openjdk-8"]


def test_merge_gathered_fills_empty_gaps():
    g = {"process": "jdk8", "ports": [1521], "software": ["oracle"], "base_image": "oraclelinux:8-slim"}
    m = merge_inventory(g, {})   # operator provided nothing
    assert m["process"] == "jdk8"
    assert m["ports"] == [1521]
    assert m["software"] == ["oracle"]
    assert m["base_image"] == "oraclelinux:8-slim"


def test_merge_software_string_split():
    g = {"process": "", "ports": [], "software": [], "base_image": ""}
    m = merge_inventory(g, {"software": "nginx, redis ,"})
    assert m["software"] == ["nginx", "redis"]


# -- DB-backed: GET endpoint + auto-gather on submit -----------------------
pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient
import idc.backend.app as m
from idc.core.db import open_store
from idc.config import get_settings
from idc.core.models import _new_id

client = TestClient(m.app)


def _seed():
    st = open_store(get_settings().db_url)
    sid = _new_id("srv")
    host = f"rt-inv-{sid[-4:]}"
    s = Server(id=sid, hostname=host, fqdn=f"{host}.dc1", ips=["10.99.0.5"],
               role="db", os="oracle linux", app_ids=[], sizing_basis="estimated")
    st.upsert_server(s)
    st.upsert_match(Match(server_id=sid, target=Target(product="CVM", spec="SA5.SMALL2",
                          region="ap-shanghai", extras={"port": 1521}),
                          confidence=0.7, method="rule", rationale="x"))
    st.close()
    return sid, host


def _cleanup(sid):
    st = open_store(get_settings().db_url)
    with st.tx() as cur:
        cur.execute(st._x("DELETE FROM matches WHERE server_id=?"), (sid,))
        cur.execute(st._x("DELETE FROM servers WHERE id=?"), (sid,))
    st.close()


def test_runtime_inventory_endpoint():
    sid, host = _seed()
    try:
        r = client.get(f"/api/runtime-inventory/{sid}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert 1521 in body["ports"]
        assert "oracle" in body["software"]
        assert body["base_image"] == "oraclelinux:8-slim"
        assert body["basis"]["ports"] == "match"
    finally:
        _cleanup(sid)


def test_runtime_inventory_unknown_server_404():
    assert client.get("/api/runtime-inventory/srv-nope").status_code == 404


def test_containerize_auto_gathers_when_inventory_empty(monkeypatch):
    """POST /api/runtime-containerize with an empty inventory should auto-gather
    (match port + role software) and pass a populated inventory to the executor."""
    sid, host = _seed()
    captured = {}

    from idc.agent.executor_client import ExecutorClient
    def fake_rc(self, app_id, server_id, inventory=None, mode="plan", callback=""):
        captured["inventory"] = inventory
        captured["mode"] = mode
        return {"job_id": "cjob-rt", "status": "pending"}
    monkeypatch.setattr(ExecutorClient, "runtime_containerize", fake_rc)
    # configure executor so the trigger passes the configured-check
    m.settings.executor_enabled = True
    m.settings.executor_url = "http://mock-executor"
    try:
        r = client.post("/api/runtime-containerize", json={
            "app_id": "app-rt", "server_id": sid, "inventory": {}, "mode": "plan"})
        assert r.status_code == 200, r.text
        # the auto-gathered inventory reached the executor
        assert 1521 in captured["inventory"]["ports"]
        assert "oracle" in captured["inventory"]["software"]
    finally:
        m.settings.executor_enabled = False
        m.settings.executor_url = ""
        _cleanup(sid)


def test_containerize_operator_inventory_wins(monkeypatch):
    """Operator-provided inventory fields override the auto-gathered ones."""
    sid, host = _seed()
    captured = {}
    from idc.agent.executor_client import ExecutorClient
    def fake_rc(self, app_id, server_id, inventory=None, mode="plan", callback=""):
        captured["inventory"] = inventory
        return {"job_id": "cjob-rt", "status": "pending"}
    monkeypatch.setattr(ExecutorClient, "runtime_containerize", fake_rc)
    m.settings.executor_enabled = True
    m.settings.executor_url = "http://mock-executor"
    try:
        r = client.post("/api/runtime-containerize", json={
            "app_id": "app-rt", "server_id": sid,
            "inventory": {"process": "oracle_lsnr", "ports": [1521],
                          "software": "oracle,custom"},
            "mode": "plan"})
        assert r.status_code == 200, r.text
        # operator's software string -> list, wins over gathered
        assert captured["inventory"]["process"] == "oracle_lsnr"
        assert captured["inventory"]["software"] == ["oracle", "custom"]
        assert captured["inventory"]["ports"] == [1521]
    finally:
        m.settings.executor_enabled = False
        m.settings.executor_url = ""
        _cleanup(sid)