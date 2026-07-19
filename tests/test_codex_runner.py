"""Tests for the Codex CLI agent runner (openai/codex, Apache-2.0).

No network, no real codex binary. Three layers:
  * ``_build_cmd``         — argv assembly per mode (plan/execute) + add-dir/cwd.
  * ``_map_event``         — JSONL event -> AgentEvent taxonomy.
  * ``stream_agent``/``run_agent`` via a fake subprocess — verifies stdin feeding
    (the prompt is piped via stdin, not argv), IDC_* env stripping (the
    prompt-injection exfiltration guard), JSONL parsing, and result/cost.

Plus the config-driven dispatcher (IDC_AGENT_RUNNER picks the backend).
"""
import asyncio
import json

import pytest

from idc.agent import codex_runner
from idc.agent import claude_runner
from idc.config import Settings, reset_settings


# ---------------------------------------------------------------------------
# _build_cmd
# ---------------------------------------------------------------------------
def test_build_cmd_plan_read_only():
    s = Settings(codex_bin="codex", codex_model="glm-5.2:cloud",
                 codex_provider="ollama")
    cmd = codex_runner._build_cmd(s, "plan", "/tmp/work", ["/tmp/extra"])
    # model + provider + read-only sandbox (plan mode = safe default)
    assert cmd[:3] == ["codex", "exec", "--oss"]
    assert "--local-provider" in cmd and "ollama" in cmd
    assert "-m" in cmd and "glm-5.2:cloud" in cmd
    assert "-s" in cmd and "read-only" in cmd
    # the trailing ``-`` makes codex read the prompt from stdin (ARG_MAX-safe)
    assert cmd[-1] == "-"
    # cwd -> -C, add_dirs -> --add-dir (repeatable)
    assert "-C" in cmd and "/tmp/work" in cmd
    assert "--add-dir" in cmd and "/tmp/extra" in cmd
    # JSONL streaming output + determinism flags
    assert "--json" in cmd
    assert "--skip-git-repo-check" in cmd
    assert "--ignore-user-config" in cmd
    assert "--color" in cmd and "never" in cmd
    # server-side one-shot runs don't persist session files to ~/.codex
    assert "--ephemeral" in cmd


def test_build_cmd_execute_workspace_write():
    s = Settings(codex_bin="codex")
    cmd = codex_runner._build_cmd(s, "execute", None, None)
    # execute mode escalates to workspace-write (write + shell)
    idx = cmd.index("-s")
    assert cmd[idx + 1] == "workspace-write"
    # no cwd -> no -C; no add_dirs -> no --add-dir
    assert "-C" not in cmd
    assert "--add-dir" not in cmd
    # any unrecognized mode falls back to read-only (safe default)
    cmd2 = codex_runner._build_cmd(s, "weird-mode", None, None)
    assert cmd2[cmd2.index("-s") + 1] == "read-only"


def test_build_cmd_custom_model_and_provider():
    # the operator can pick any Ollama cloud model (kimi-k2:cloud, qwen3.5:cloud)
    # + an alternate local provider (lmstudio)
    s = Settings(codex_model="kimi-k2:cloud", codex_provider="lmstudio")
    cmd = codex_runner._build_cmd(s, "plan", None, None)
    assert "kimi-k2:cloud" in cmd
    assert "lmstudio" in cmd


# ---------------------------------------------------------------------------
# _map_event
# ---------------------------------------------------------------------------
def test_map_event_agent_message_is_text():
    ev = {"type": "item.completed",
          "item": {"id": "item_1", "type": "agent_message", "text": "hello"}}
    m = codex_runner._map_event(ev)
    assert m.kind == "text" and m.text == "hello"


def test_map_event_reasoning_is_status_not_text():
    # chain-of-thought must NOT fold into the final output (would pollute the
    # answer with the model's thinking) — it surfaces as a progress status.
    ev = {"type": "item.completed",
          "item": {"type": "reasoning", "text": "Let me think..."}}
    m = codex_runner._map_event(ev)
    assert m.kind == "status"
    assert "[reasoning]" in m.text


def test_map_event_command_execution_is_tool():
    ev = {"type": "item.completed",
          "item": {"type": "command_execution",
                   "command": "/bin/bash -lc 'ls /tmp'"}}
    m = codex_runner._map_event(ev)
    assert m.kind == "tool"
    assert "[tool]" in m.text and "ls /tmp" in m.text


def test_map_event_file_change_is_tool():
    ev = {"type": "item.completed",
          "item": {"type": "file_change", "path": "/tmp/x.tf"}}
    m = codex_runner._map_event(ev)
    assert m.kind == "tool" and "[file]" in m.text and "/tmp/x.tf" in m.text


def test_map_event_error_is_non_fatal_status():
    # the "Model metadata for X not found" warning is benign — it must surface
    # as a status, NOT an error (which would abort the stream in run_agent).
    ev = {"type": "item.completed",
          "item": {"type": "error",
                   "message": "Model metadata for `glm-5.2:cloud` not found."}}
    m = codex_runner._map_event(ev)
    assert m.kind == "status"
    assert "[error]" in m.text


def test_map_event_lifecycle_events():
    assert codex_runner._map_event({"type": "thread.started"}).kind == "status"
    assert codex_runner._map_event({"type": "turn.started"}).kind == "status"
    res = codex_runner._map_event(
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}})
    assert res.kind == "result"
    # token usage rides on the result event's raw (cost_usd is None for codex;
    # callers wanting token counts read them from here)
    assert res.raw["usage"]["input_tokens"] == 10


def test_map_event_skips_item_started_and_unknown():
    # item.started is chatty (item.completed carries the same info) -> skip
    assert codex_runner._map_event({"type": "item.started"}) is None
    # an unknown future item type still surfaces (not silently dropped)
    m = codex_runner._map_event(
        {"type": "item.completed", "item": {"type": "newtype", "x": 1}})
    assert m.kind == "tool" and "newtype" in m.text


# ---------------------------------------------------------------------------
# fake subprocess — verifies stdin feeding, env stripping, JSONL parse
# ---------------------------------------------------------------------------
class _FakeStdin:
    def __init__(self):
        self.written = b""
        self.closed = False

    def write(self, data):
        self.written += data

    async def drain(self):
        return None

    def close(self):
        self.closed = True


class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


class _FakeStderr:
    async def read(self):
        return b""


class _FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines)
        self.stderr = _FakeStderr()
        self._rc = returncode
        self.killed = False

    def kill(self):
        self.killed = True

    async def wait(self):
        return self._rc

    @property
    def returncode(self):
        return self._rc


def _canned_jsonl():
    """A realistic codex exec --json event stream (same shape as the live
    smoke test against glm-5.2:cloud)."""
    return [
        (json.dumps({"type": "thread.started",
                     "thread_id": "t1"}) + "\n").encode(),
        (json.dumps({"type": "item.completed", "item": {
            "id": "item_0", "type": "error",
            "message": "Model metadata for `glm-5.2:cloud` not found."}}) + "\n").encode(),
        (json.dumps({"type": "turn.started"}) + "\n").encode(),
        (json.dumps({"type": "item.completed", "item": {
            "id": "item_1", "type": "agent_message", "text": "CODEX_OK"}}) + "\n").encode(),
        (json.dumps({"type": "turn.completed",
                     "usage": {"input_tokens": 100, "output_tokens": 3,
                               "reasoning_output_tokens": 0}}) + "\n").encode(),
    ]


def test_stream_agent_feeds_prompt_via_stdin_and_strips_idc_env(monkeypatch):
    """The prompt goes through stdin (not argv) so large inventory contexts
    can't blow past ARG_MAX, and IDC_* secrets are dropped from the child env
    so a prompt-injected agent can't exfiltrate them via tool calls."""
    captured = {}

    proc = _FakeProc(_canned_jsonl())

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        captured["cwd"] = kwargs.get("cwd")
        captured["proc"] = proc
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    # plant an IDC_* secret in the env to prove it's stripped from the child
    monkeypatch.setenv("IDC_DB_URL", "mysql://secret@host/db")
    monkeypatch.setenv("IDC_EXECUTOR_TOKEN", "tok")

    async def _run():
        events = []
        async for ev in codex_runner.stream_agent(
                "do the task", settings=Settings(codex_model="glm-5.2:cloud"),
                mode="plan", context="CTX", timeout=30):
            events.append(ev)
        return events

    events = asyncio.run(_run())
    kinds = [e.kind for e in events]
    # the agent_message folded into a text event; turn.completed -> result
    assert "text" in kinds and events[kinds.index("text")].text == "CODEX_OK"
    assert kinds[-1] == "result"
    # the benign metadata error surfaced as status, NOT error (no abort)
    assert "error" not in kinds
    # the prompt was fed via stdin (preamble + context + task joined), not argv
    written = proc.stdin.written.decode("utf-8", "replace")
    assert "do the task" in written and "CTX" in written
    assert "CloudForge Migration Agent" in written  # SYSTEM_PREAMBLE prepended
    # the trailing ``-`` arg tells codex to read stdin
    assert captured["cmd"][-1] == "-"
    # IDC_* secrets were stripped from the child env (security guard)
    assert captured["env"] is not None
    assert not any(k.startswith("IDC_") for k in captured["env"])
    assert "IDC_DB_URL" not in captured["env"]


def test_stream_agent_nonzero_exit_emits_error(monkeypatch):
    """A real failure (bad model, ollama down) shows up as a non-zero exit;
    the runner surfaces it as an error event so callers aren't left empty."""
    async def fake_exec(*cmd, **kwargs):
        # no result event, non-zero exit -> the post-loop returncode check fires
        return _FakeProc([(json.dumps({"type": "thread.started"}) + "\n").encode()],
                        returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    async def _run():
        return [e async for e in codex_runner.stream_agent(
            "x", settings=Settings(), mode="plan", timeout=10)]
    events = asyncio.run(_run())
    assert any(e.kind == "error" and "codex exited 1" in e.text for e in events)


def test_run_agent_accumulates_output_and_no_cost(monkeypatch):
    """run_agent folds agent_message text events into the output, stops at the
    result event, and reports cost_usd=None (codex gives token usage, not USD)."""
    async def fake_exec(*cmd, **kwargs):
        return _FakeProc(_canned_jsonl())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    res = asyncio.run(codex_runner.run_agent(
        "task", settings=Settings(codex_model="glm-5.2:cloud"),
        mode="plan", timeout=30))
    assert res.status == "done"
    assert res.output == "CODEX_OK"
    assert res.error == ""
    assert res.cost_usd is None  # codex reports tokens, not USD
    kinds = [e.kind for e in res.events]
    assert "text" in kinds and "result" in kinds


# ---------------------------------------------------------------------------
# config-driven dispatcher (IDC_AGENT_RUNNER picks the backend)
# ---------------------------------------------------------------------------
def test_runner_for_default_is_claude(monkeypatch):
    monkeypatch.delenv("IDC_AGENT_RUNNER", raising=False)
    reset_settings()
    from idc.agent import _runner_for
    assert _runner_for(None) is claude_runner


def test_runner_for_codex_when_configured(monkeypatch):
    monkeypatch.setenv("IDC_AGENT_RUNNER", "codex")
    reset_settings()
    from idc.agent import _runner_for
    assert _runner_for(None) is codex_runner


def test_dispatcher_run_agent_routes_to_codex(monkeypatch):
    """The package-level run_agent delegates to the configured backend, so the
    CLI / /api/agent/run call sites don't change when the operator flips
    IDC_AGENT_RUNNER=codex."""
    monkeypatch.setenv("IDC_AGENT_RUNNER", "codex")
    reset_settings()
    called = {}

    async def fake_run_agent(prompt, settings=None, **kw):
        called["prompt"] = prompt
        called["backend"] = "codex"
        from idc.agent import AgentResult
        return AgentResult(output="ok", status="done")

    monkeypatch.setattr(codex_runner, "run_agent", fake_run_agent)
    from idc.agent import run_agent
    res = asyncio.run(run_agent("hello", mode="plan"))
    assert called["backend"] == "codex" and called["prompt"] == "hello"
    assert res.output == "ok"