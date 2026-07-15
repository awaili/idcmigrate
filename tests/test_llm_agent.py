"""Tests for the LLM client and Claude Code agent runner (mocked, no network/subprocess)."""
import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from idc.config import reset_settings, Settings
from idc.core.match import match_server
from idc.core.models import Server, Utilization, Match, Target, Wave, Disk, CodeProfile
from idc.llm.client import LLMClient, UNAVAILABLE, _server_brief, _code_profile_brief
from idc.agent import claude_runner
from idc.agent.claude_runner import AgentEvent, run_agent, build_context


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
def _mk_server():
    return Server(hostname="db-mysql-01", role="db", os="centos", cpu_cores=8,
                  mem_gb=32, business_criticality="high",
                  utilization=Utilization(cpu_p95=61.0, mem_p95=83.0, disk_used_pct=66.0))


def _mk_oracle_server():
    """A host whose disk + mount layout signals Oracle DB (OFA: oradata/redolog)
    + an EOL OS + warranty bucket — the context the drawer's LLM actions must see."""
    return Server(hostname="ora-prod-01", role="db", os="centos", cpu_cores=16,
                  mem_gb=128, business_criticality="high",
                  disks=[Disk(name="/", fs="/", size_gb=50),
                         Disk(name="/opt/app/oracle", fs="/opt/app/oracle", size_gb=20),
                         Disk(name="/oradata1", fs="/oradata1", size_gb=800),
                         Disk(name="/redolog_A", fs="/redolog_A", size_gb=100)],
                  tags=["oracle-db"], app_ids=["APP_ORA"],
                  os_eol_bucket="expired", warranty_bucket="unknown",
                  utilization=Utilization(cpu_p95=40.0, mem_p95=55.0, disk_used_pct=71.0))


def test_server_brief_carries_mounts_eol_network():
    s = _mk_oracle_server()
    b = _server_brief(s)
    # per-partition disks WITH mount points (the OFA layout that classifies Oracle)
    mounts = [d["mount"] for d in b["disks"]]
    assert "/oradata1" in mounts and "/redolog_A" in mounts
    assert b["disk_gb"] == 970
    # EOL + warranty buckets flow through (the silent-rehost signals)
    assert b["os_eol_bucket"] == "expired" and b["warranty_bucket"] == "unknown"
    assert "ips" in b["network"] and "datacenter" in b["network"]


def test_code_profile_brief_none_when_missing():
    assert _code_profile_brief(None) is None
    p = CodeProfile(app_id="APP_ORA", language="java", runtime="tomcat",
                    framework="spring", blockers=["hard license"])
    pb = _code_profile_brief(p)
    assert pb["runtime"] == "tomcat" and "hard license" in pb["blockers"]


def test_explain_match_prompt_includes_mounts_and_profile(monkeypatch):
    s = Settings(llm_enabled=True, llm_model="glm-5.2:cloud")
    c = LLMClient(s)
    captured = {}
    fake_resp = MagicMock(); fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {"message": {"content": "TData fits the OFA layout."}}
    def fake_post(url, json=None, timeout=None):
        captured["messages"] = json["messages"]; return fake_resp
    monkeypatch.setattr("idc.llm.client.httpx.post", fake_post)
    srv = _mk_oracle_server()
    prof = CodeProfile(app_id="APP_ORA", language="sqlpl", runtime="oracle",
                       blockers=["PL/SQL packages"])
    out = c.explain_match(srv, match_server(srv), prof)
    assert out == "TData fits the OFA layout."
    user = next(m["content"] for m in captured["messages"] if m["role"] == "user")
    # the mount layout + EOL bucket + code profile all reach the prompt
    assert "/oradata1" in user and "expired" in user and "oracle" in user


def test_llm_disabled_returns_unavailable(monkeypatch):
    s = Settings(llm_enabled=False)
    c = LLMClient(s)
    out = c.explain_match(_mk_server(), match_server(_mk_server()))
    assert out == UNAVAILABLE


def test_llm_explain_parses_content(monkeypatch):
    s = Settings(llm_enabled=True, llm_model="glm-5.2:cloud")
    c = LLMClient(s)
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {"message": {"content": "Migration to CDB HA is right."}}
    monkeypatch.setattr("idc.llm.client.httpx.post", lambda *a, **k: fake_resp)
    out = c.explain_match(_mk_server(), match_server(_mk_server()))
    assert out == "Migration to CDB HA is right."


def test_llm_ask_uses_retrieval(monkeypatch):
    s = Settings(llm_enabled=True)
    c = LLMClient(s)
    captured = {}
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {"message": {"content": "ANS"}}
    def fake_post(url, json=None, timeout=None):
        captured["messages"] = json["messages"]
        return fake_resp
    monkeypatch.setattr("idc.llm.client.httpx.post", fake_post)
    servers = [_mk_server()]
    out = c.ask("which servers run mysql?", servers, matches=[])
    assert out == "ANS"
    # context (retrieved server) is embedded in the user message
    assert "db-mysql-01" in captured["messages"][1]["content"]


def test_llm_error_falls_back(monkeypatch):
    s = Settings(llm_enabled=True)
    c = LLMClient(s)
    def boom(*a, **k): raise RuntimeError("gateway down")
    monkeypatch.setattr("idc.llm.client.httpx.post", boom)
    out = c.explain_match(_mk_server(), match_server(_mk_server()))
    assert "rule-based result only" in out or "MigraQ error" in out


# ---------------------------------------------------------------------------
# match audit (AI second-opinion on the rule target)
# ---------------------------------------------------------------------------
def _audit_resp(verdict="keep", **kw):
    d = {"verdict": verdict, "confidence": 0.85,
         "critique": "rule target fits", "alternative_target": None,
         "alternative_spec": None, "rationale": "managed product fits",
         "risks": ["high utilization"]}
    d.update(kw)
    return json.dumps(d, ensure_ascii=False)


def test_audit_match_keep():
    c = LLMClient(Settings(llm_enabled=True))
    s = _mk_server()
    with patch.object(c, "chat", return_value=_audit_resp("keep")):
        r = c.audit_match(s, match_server(s))
    assert r["ok"] and r["verdict"] == "keep" and r["alternative_target"] is None
    assert r["risks"] == ["high utilization"]


def test_audit_match_change_suggests_alternative():
    c = LLMClient(Settings(llm_enabled=True))
    s = _mk_server()
    with patch.object(c, "chat",
                      return_value=_audit_resp("change", critique="db role should be CDB not CVM",
                                                alternative_target="CDB",
                                                alternative_spec="MySQL HA")):
        r = c.audit_match(s, match_server(s))
    assert r["ok"] and r["verdict"] == "change"
    assert r["alternative_target"] == "CDB" and r["alternative_spec"] == "MySQL HA"


def test_audit_match_rejects_bad_verdict():
    c = LLMClient(Settings(llm_enabled=True))
    with patch.object(c, "chat", return_value=json.dumps({"verdict": "maybe"})):
        r = c.audit_match(_mk_server(), match_server(_mk_server()))
    assert not r["ok"] and "invalid verdict" in r["error"]


def test_audit_match_rejects_non_json():
    c = LLMClient(Settings(llm_enabled=True))
    with patch.object(c, "chat", return_value="I cannot audit this"):
        r = c.audit_match(_mk_server(), match_server(_mk_server()))
    assert not r["ok"] and "not valid JSON" in r["error"]


def test_audit_match_llm_exception_degrades():
    c = LLMClient(Settings(llm_enabled=True))
    with patch.object(c, "chat", side_effect=RuntimeError("gateway down")):
        r = c.audit_match(_mk_server(), match_server(_mk_server()))
    assert not r["ok"] and "MigraQ error" in r["error"]


def test_audit_match_disabled():
    c = LLMClient(Settings(llm_enabled=False))
    r = c.audit_match(_mk_server(), match_server(_mk_server()))
    assert not r["ok"] and r["verdict"] == ""


# ---------------------------------------------------------------------------
# wave risk basis (deterministic) + assess_wave (LLM runbook)
# ---------------------------------------------------------------------------
from idc.core.codeintel import wave_risk_basis
from idc.core.models import Wave


def _wave_estate():
    hi = Server(hostname="web-1", role="web", os="centos", business_criticality="high",
                app_ids=["app-1"], utilization=Utilization(cpu_p95=90, mem_p95=60, disk_used_pct=40))
    lo = Server(hostname="k8s-1", role="k8s", business_criticality="low",
                utilization=Utilization(cpu_p95=20, mem_p95=20, disk_used_pct=20))
    return hi, lo


def test_wave_risk_basis_high_crit_cutover_is_high():
    hi, _ = _wave_estate()
    w = Wave(name="W4 Cutover", stage="4_cutover", server_ids=[hi.id],
             depends_on=["w1", "w2", "w3"])
    b = wave_risk_basis(w, [hi], [], [])
    assert b["level"] == "high" and b["score"] >= 0.7
    assert "high-criticality" in " ".join(b["factors"])
    assert b["signals"]["is_cutover"] is True


def test_wave_risk_basis_plain_lz_is_low():
    _, lo = _wave_estate()
    w = Wave(name="LZ", stage="1_landing_zone", server_ids=[lo.id])
    b = wave_risk_basis(w, [lo], [], [])
    assert b["level"] == "low" and b["score"] < 0.4


def test_assess_wave_returns_deterministic_risk_plus_llm_runbook():
    hi, _ = _wave_estate()
    w = Wave(name="W4 Cutover", stage="4_cutover", server_ids=[hi.id],
             depends_on=["w1", "w2", "w3"])
    c = LLMClient(Settings(llm_enabled=True))
    resp = json.dumps({"go_no_go": "hold",
                       "pre_checks": ["drain high-util servers"],
                       "cutover": ["flip CLB"], "rollback": ["repoint DNS"],
                       "summary": "cutover risky"})
    with patch.object(c, "chat", return_value=resp):
        r = c.assess_wave(w, [hi], [], [])
    assert r["ok"] is True
    # deterministic part
    assert r["risk_level"] == "high" and r["risk_score"] >= 0.7
    # LLM part
    assert r["go_no_go"] == "hold"
    assert r["runbook"]["pre_checks"] == ["drain high-util servers"]
    assert r["runbook"]["cutover"] == ["flip CLB"]
    assert r["summary"] == "cutover risky"


def test_assess_wave_llm_disabled_still_returns_risk():
    hi, _ = _wave_estate()
    w = Wave(name="W4 Cutover", stage="4_cutover", server_ids=[hi.id],
             depends_on=["w1", "w2", "w3"])
    c = LLMClient(Settings(llm_enabled=False))
    r = c.assess_wave(w, [hi], [], [])
    assert r["ok"] is False
    # deterministic risk still present
    assert r["risk_level"] == "high" and r["risk_factors"]


def test_assess_wave_rejects_bad_json_keeps_risk():
    hi, _ = _wave_estate()
    w = Wave(name="W4", stage="4_cutover", server_ids=[hi.id])
    c = LLMClient(Settings(llm_enabled=True))
    with patch.object(c, "chat", return_value="not json"):
        r = c.assess_wave(w, [hi], [], [])
    assert r["ok"] is False and "not valid JSON" in r["error"]
    assert r["risk_level"] in ("low", "medium", "high")   # deterministic risk still returned
    assert r["risk_factors"]


# ---------------------------------------------------------------------------
# adversarial plan review (red-team)
# ---------------------------------------------------------------------------
from idc.core.models import Workload


def _plan_estate():
    sp = Server(hostname="a-prod", role="app", env="prod", app_ids=["app-A"],
                 business_criticality="high", utilization=Utilization())
    sd = Server(hostname="a-dev", role="app", env="dev", app_ids=["app-A"],
                utilization=Utilization())
    wls = [Workload(app_id="app-A", name="A", server_ids=[sp.id, sd.id])]
    return sp, sd, wls


def _review_resp(overall="needs-work", findings=None):
    if findings is None:
        findings = [{"severity": "medium", "wave": "W1 Apps",
                     "issue": "prod and dev in same wave",
                     "suggestion": "split dev out"}]
    return json.dumps({"overall": overall, "findings": findings,
                       "summary": "env mixing"}, ensure_ascii=False)


def test_review_plan_parses_findings():
    sp, sd, wls = _plan_estate()
    waves = [Wave(name="W1 Apps", stage="3_application",
                 server_ids=[sp.id, sd.id], depends_on=[])]
    c = LLMClient(Settings(llm_enabled=True))
    with patch.object(c, "chat", return_value=_review_resp("needs-work")):
        r = c.review_plan(waves, [sp, sd], wls, [], [], {})
    assert r["ok"] and r["overall"] == "needs-work"
    assert len(r["findings"]) == 1
    f = r["findings"][0]
    assert f["severity"] == "medium" and f["wave"] == "W1 Apps"
    assert f["issue"] and f["suggestion"]


def test_review_plan_sound_when_no_findings():
    sp, sd, wls = _plan_estate()
    waves = [Wave(name="W1", stage="3_application", server_ids=[sp.id], depends_on=[])]
    c = LLMClient(Settings(llm_enabled=True))
    with patch.object(c, "chat", return_value=_review_resp("sound", findings=[])):
        r = c.review_plan(waves, [sp], wls, [], [], {})
    assert r["ok"] and r["overall"] == "sound" and r["findings"] == []


def test_review_plan_normalizes_bad_overall():
    sp, sd, wls = _plan_estate()
    waves = [Wave(name="W1", stage="3_application", server_ids=[sp.id])]
    c = LLMClient(Settings(llm_enabled=True))
    with patch.object(c, "chat", return_value=json.dumps({"overall": "garbage", "findings": []})):
        r = c.review_plan(waves, [sp], wls, [], [], {})
    assert r["ok"] and r["overall"] == "needs-work"   # normalized


def test_review_plan_rejects_non_json():
    sp, sd, wls = _plan_estate()
    waves = [Wave(name="W1", stage="3_application", server_ids=[sp.id])]
    c = LLMClient(Settings(llm_enabled=True))
    with patch.object(c, "chat", return_value="I refuse"):
        r = c.review_plan(waves, [sp], wls, [], [], {})
    assert not r["ok"] and "not valid JSON" in r["error"]


def test_review_plan_disabled():
    sp, sd, wls = _plan_estate()
    c = LLMClient(Settings(llm_enabled=False))
    r = c.review_plan([Wave(name="W1", stage="3_application", server_ids=[sp.id])],
                      [sp], wls, [], [], {})
    assert not r["ok"] and r["overall"] is None and r["findings"] == []


# ---------------------------------------------------------------------------
# Agent runner (mocked subprocess)
# ---------------------------------------------------------------------------
async def _fake_stream(prompt, settings=None, mode="plan", cwd=None, add_dirs=None,
                       context=None, timeout=None):
    yield AgentEvent("status", text="init")
    yield AgentEvent("text", text="Hello ")
    yield AgentEvent("text", text="world.")
    yield AgentEvent("tool", text='[tool] Read: {"path":"x"}')
    yield AgentEvent("result", text="Hello world.", raw={"total_cost_usd": 0.01})


def test_run_agent_collects_output(monkeypatch):
    monkeypatch.setattr(claude_runner, "stream_agent", _fake_stream)
    res = asyncio.get_event_loop().run_until_complete(
        run_agent("dummy", settings=Settings(), mode="plan", context="ctx"))
    assert res.status == "done"
    assert "Hello world." in res.output
    assert res.cost_usd == 0.01
    kinds = [e.kind for e in res.events]
    assert "tool" in kinds and "result" in kinds


def test_build_context_caps_and_focuses():
    servers = [Server(hostname=f"h{i}", role="app", cpu_cores=2, mem_gb=4) for i in range(250)]
    matches = [match_server(s) for s in servers]
    ctx = build_context(servers, matches, waves=None, focus_server_ids=[servers[5].id])
    # focus → only that server in context
    assert '"hostname": "h5"' in ctx
    assert '"hostname": "h6"' not in ctx
    # cap of 200 when no focus
    ctx2 = build_context(servers, matches)
    assert ctx2.count('"hostname"') <= 200


def test_build_cmd_plan_vs_execute():
    from idc.agent.claude_runner import _build_cmd
    s = Settings(claude_bin="claude")
    cmd_plan = _build_cmd("p", s, "plan", None, None)
    cmd_exec = _build_cmd("p", s, "execute", None, None)
    assert "--permission-mode" in cmd_plan and "plan" in cmd_plan
    assert "--dangerously-skip-permissions" in cmd_exec
    assert "--output-format" in cmd_plan and "stream-json" in cmd_plan
    assert "--append-system-prompt" in cmd_plan


# ---------------------------------------------------------------------------
# executor connectivity probe (doctor / web status indicator)
# ---------------------------------------------------------------------------
def test_executor_status_not_configured():
    from idc.agent import executor_status
    es = executor_status(Settings())   # executor_url empty
    assert es["configured"] is False and es["reachable"] is False
    assert "not configured" in es["detail"]


def test_executor_status_reachable(monkeypatch):
    from idc.agent import executor_status
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {"ok": True, "service": "idc-executor-mock",
                              "version": "0.1.0"}
    monkeypatch.setattr("httpx.get", lambda *a, **k: fake)
    s = Settings(executor_url="http://localhost:8090", executor_token="t")
    es = executor_status(s)
    assert es["configured"] and es["reachable"] and es["version"] == "0.1.0"
    assert es["token_set"] is True


def test_executor_status_unreachable(monkeypatch):
    from idc.agent import executor_status
    def boom(*a, **k):
        raise ConnectionRefusedError("nope")
    monkeypatch.setattr("httpx.get", boom)
    s = Settings(executor_url="http://localhost:65535")
    es = executor_status(s)
    assert es["configured"] and not es["reachable"]
    assert "unreachable" in es["detail"]


def test_executor_status_non_200(monkeypatch):
    from idc.agent import executor_status
    fake = MagicMock()
    fake.status_code = 404
    monkeypatch.setattr("httpx.get", lambda *a, **k: fake)
    s = Settings(executor_url="http://localhost:8090")
    es = executor_status(s)
    assert not es["reachable"] and es["status_code"] == 404