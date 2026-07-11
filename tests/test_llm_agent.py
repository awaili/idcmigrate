"""Tests for the LLM client and Claude Code agent runner (mocked, no network/subprocess)."""
import asyncio
import json
from unittest.mock import MagicMock

import pytest

from idc.config import reset_settings, Settings
from idc.core.match import match_server
from idc.core.models import Server, Utilization, Match, Target, Wave
from idc.llm.client import LLMClient, UNAVAILABLE
from idc.agent import claude_runner
from idc.agent.claude_runner import AgentEvent, run_agent, build_context


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
def _mk_server():
    return Server(hostname="db-mysql-01", role="db", os="centos", cpu_cores=8,
                  mem_gb=32, business_criticality="high",
                  utilization=Utilization(cpu_p95=61.0, mem_p95=83.0, disk_used_pct=66.0))


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
    assert "rule-based result only" in out or "LLM error" in out


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