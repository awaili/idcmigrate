"""Codex CLI agent runner (openai/codex, Apache-2.0).

Mirrors ``claude_runner``'s public surface (``AgentEvent`` / ``AgentResult`` /
``build_context`` / ``lz_context`` / ``stream_agent`` / ``run_agent`` /
``run_agent_sync``) so the CLI and ``/api/agent/run`` can switch the agentic
backend via ``IDC_AGENT_RUNNER`` with zero call-site changes.

Spawns ``codex exec`` (the non-interactive, print-equivalent mode) as a
subprocess to perform the same agentic migration tasks Claude Code CLI did:
emit Terraform, draft runbooks, verify security-group equivalence, run
pre-flight checks. The agent receives the same compact inventory context
(``build_context``) so it grounds its work in the actual estate.

Why Codex CLI instead of Claude Code CLI: it is model-agnostic.
``--oss --local-provider ollama`` points Codex at the local Ollama gateway
(127.0.0.1:11434), so the SAME Ollama cloud model that powers MigraQ's
``LLMClient`` (e.g. ``glm-5.2:cloud``) also powers the agentic layer — one
model, one plumbing, no Anthropic / OpenAI model dependency. The ``:cloud``
models are routed via Ollama Cloud, so no local model pull is needed.

Modes (mirror ``claude_runner``):
  * ``plan``    (default) — ``--sandbox read-only``: the agent may read files
    and run read-only shell (``ls``, ``cat``, ``grep``) but CANNOT write or
    mutate. Safe to run anytime. ``codex exec`` defaults to read-only, so this
    is the natural fit (and a harder read-only guarantee than Claude Code's
    ``--permission-mode plan``).
  * ``execute``           — ``--sandbox workspace-write``: the agent may write
    within the workspace + ``--add-dir`` directories and run shell (e.g.
    ``terraform apply``, ``tencentcloud cli``). Opt-in only; the caller must
    confirm.

Output: ``--json`` emits JSONL events to stdout (``thread.started``,
``turn.started``, ``item.started`` / ``item.completed``, ``turn.completed``
with token ``usage``). Each event is mapped to an ``AgentEvent`` so the web
UI / CLI progress display keeps working. ``item.completed`` carries an
``item.type`` — ``agent_message`` (the answer, folded into output),
``reasoning`` (chain-of-thought, surfaced as progress only),
``command_execution`` / file changes (tool events), or ``error`` (benign
warnings like "Model metadata not found" — surfaced, NOT fatal).

The full prompt (preamble + inventory context + task) is fed via stdin
(``codex exec -`` reads instructions from stdin) to avoid ARG_MAX on large
estates — ``build_context`` can serialize up to 200 servers, which can blow
past argv limits if passed as a positional arg.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from ..config import Settings, get_settings
# Agent-agnostic helpers + the event/result dataclasses are shared with the
# claude backend — they carry no Claude-specific coupling, so reuse them
# verbatim instead of duplicating. claude_runner is pure Python at import time
# (it only touches the `claude` binary at stream_agent runtime), so importing
# these symbols here does NOT pull in any Claude dependency.
from .claude_runner import (
    AgentEvent, AgentResult, SYSTEM_PREAMBLE, build_context, lz_context,
    lz_context_all,
)


def _build_cmd(settings: Settings, mode: str, cwd: Optional[str],
               add_dirs: Optional[List[str]]) -> List[str]:
    """Assemble the ``codex exec`` argv. The prompt itself is NOT here — it is
    fed via stdin (the trailing ``-`` arg tells codex to read instructions from
    stdin), so large inventory contexts can't blow past ARG_MAX."""
    sandbox = "workspace-write" if mode == "execute" else "read-only"
    cmd: List[str] = [
        settings.codex_bin, "exec",
        "--oss",
        "--local-provider", settings.codex_provider,
        "-m", settings.codex_model,
        "-s", sandbox,
        # the agent runs server-side against an arbitrary cwd (often a tmp
        # export dir, not necessarily a git repo); skip codex's git-repo
        # guard so it doesn't refuse to start.
        "--skip-git-repo-check",
        # don't load the server operator's personal ~/.codex/config.toml — the
        # model + provider are set explicitly below, so a stray personal
        # config (different default model, OpenAI key, ...) can't leak in and
        # change behavior. Auth still uses CODEX_HOME, but the Ollama local
        # provider needs no auth.
        "--ignore-user-config",
        "--color", "never",
        "--json",
        # the agent runs server-side as one-shot tasks (Terraform emit,
        # runbooks, ...); we never resume a session, so don't persist session
        # rollout files to ~/.codex (avoids disk accumulation under the
        # service's HOME across thousands of agent runs).
        "--ephemeral",
    ]
    if cwd:
        cmd += ["-C", cwd]
    for d in add_dirs or []:
        cmd += ["--add-dir", d]
    # the trailing ``-`` makes codex read the prompt from stdin
    cmd.append("-")
    return cmd


def _map_event(evt: Dict[str, Any]) -> Optional[AgentEvent]:
    """Translate one codex JSONL event into an AgentEvent, or None to skip.

    codex events (from ``codex exec --json``):
      thread.started, turn.started           -> status
      item.started                           -> skip (chatty; item.completed
                                                carries the same info)
      item.completed {item.type=...}:
        agent_message    -> text   (folded into the final output)
        reasoning         -> status (chain-of-thought, progress only — NOT
                                      folded into output, which would pollute
                                      the final answer with the model's thinking)
        command_execution -> tool   (shell command the agent ran)
        file_change / patch / apply_patch -> tool (file mutation)
        error            -> status (benign warnings like "Model metadata not
                                      found" — surfaced but NOT fatal; a real
                                      failure shows up as a non-zero exit)
        <other>          -> tool   (new item types surface as a tool-ish event
                                      so they're visible, not silently dropped)
      turn.completed                        -> result (signals completion;
                                                raw carries token ``usage``)
    """
    etype = evt.get("type")
    if etype == "item.completed":
        it = evt.get("item") or {}
        itype = it.get("type")
        if itype == "agent_message":
            return AgentEvent("text", text=str(it.get("text", "")), raw=evt)
        if itype == "reasoning":
            return AgentEvent("status",
                             text=f"[reasoning] {str(it.get('text', ''))[:200]}",
                             raw=evt)
        if itype == "command_execution":
            return AgentEvent("tool",
                             text=f"[tool] {str(it.get('command', ''))[:300]}",
                             raw=evt)
        if itype in ("file_change", "patch", "apply_patch"):
            return AgentEvent("tool", text=f"[file] {it.get('path', '')}", raw=evt)
        if itype == "error":
            return AgentEvent("status",
                             text=f"[error] {str(it.get('message', ''))[:200]}",
                             raw=evt)
        # an item type codex added later — surface it so it isn't silently lost
        return AgentEvent("tool", text=f"[{itype}] {str(it)[:200]}", raw=evt)
    if etype == "thread.started":
        return AgentEvent("status", text="thread started", raw=evt)
    if etype == "turn.started":
        return AgentEvent("status", text="turn started", raw=evt)
    if etype == "turn.completed":
        # the final answer text was already emitted as item.completed{agent_message};
        # this event just signals the turn is done + carries token usage in raw.
        return AgentEvent("result", text="", raw=evt)
    # item.started / unknown events: skip to keep the stream signal-rich, not noisy
    return None


async def stream_agent(
    prompt: str,
    settings: Optional[Settings] = None,
    mode: str = "plan",
    cwd: Optional[str] = None,
    add_dirs: Optional[List[str]] = None,
    context: Optional[str] = None,
    timeout: Optional[int] = None,
) -> AsyncIterator[AgentEvent]:
    """Async generator yielding AgentEvent-s; the final one has kind='result'.

    Mirrors ``claude_runner.stream_agent`` signature-for-signature so callers
    don't change. The preamble + inventory context + task are joined and fed
    via stdin (codex reads instructions from stdin because of the trailing
    ``-`` arg) — avoids ARG_MAX for large estates.
    """
    settings = settings or get_settings()
    full_prompt = (
        SYSTEM_PREAMBLE + "\n\n"
        + (f"{context}\n\n--- TASK ---\n{prompt}" if context else prompt)
    )
    cmd = _build_cmd(settings, mode, cwd, add_dirs)
    timeout = timeout or settings.codex_timeout
    # Total wall-clock deadline for the whole stream: the per-read wait below
    # is derived from the REMAINING budget, so a chatty-but-stuck agent (a line
    # every just-under-timeout seconds) can't run indefinitely — same guard as
    # claude_runner.
    deadline = time.monotonic() + timeout
    try:
        # Pass a minimal environment to the Codex subprocess: keep PATH/HOME/
        # locale + the codex/ollama auth vars the CLI needs, but DROP the
        # project's IDC_* secrets (DB url, executor/zabbix/servicenow/web
        # tokens). The agent's context is fed inventory data that originated
        # from CMDB/executor pushes; a prompt-injection there must not be able
        # to exfiltrate these secrets via the agent's tool use — same guard as
        # claude_runner.
        env = {k: v for k, v in os.environ.items() if not k.startswith("IDC_")}
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
    except FileNotFoundError as e:
        yield AgentEvent("error",
                         text=f"codex binary not found: {settings.codex_bin!r} ({e})")
        return

    # Feed the prompt via stdin in the background so the stdout readline loop
    # below can start draining immediately (codex won't emit anything until it
    # has the full prompt, so close stdin EOF after writing).
    async def _feed_stdin() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(full_prompt.encode("utf-8"))
            await proc.stdin.drain()
        except Exception:
            pass  # a broken-pipe here is fine — codex may have exited early
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
    feed_task = asyncio.ensure_future(_feed_stdin())

    async def _drain_stderr() -> str:
        if proc.stderr is None:
            return ""
        return (await proc.stderr.read()).decode(errors="replace")

    try:
        assert proc.stdout is not None
        while True:
            try:
                remaining = max(0.1, deadline - time.monotonic())
                line = await asyncio.wait_for(proc.stdout.readline(),
                                               timeout=remaining)
            except asyncio.TimeoutError:
                proc.kill()
                yield AgentEvent("error", text=f"agent timed out after {timeout}s")
                return
            if not line:
                break
            line_s = line.decode(errors="replace").strip()
            if not line_s:
                continue
            try:
                evt = json.loads(line_s)
            except json.JSONDecodeError:
                # codex --json should only emit JSONL on stdout; a non-JSON
                # line is unexpected — skip it rather than abort the stream.
                continue
            mapped = _map_event(evt)
            if mapped is not None:
                yield mapped
                # the turn's final answer text was already yielded as a text
                # event (item.completed{agent_message}); result just signals done.
                if mapped.kind == "result":
                    return
    except Exception as e:
        yield AgentEvent("error", text=f"{e!r}")
        return
    finally:
        feed_task.cancel()
    await proc.wait()
    err = await _drain_stderr()
    # a non-zero exit with no result event yet means a real failure (bad model,
    # ollama down, ...). Surface it; if we already yielded result, the caller
    # has the answer and this just adds context.
    if proc.returncode not in (0, None):
        yield AgentEvent("error",
                         text=f"codex exited {proc.returncode}; stderr: {err[:500]}")


async def run_agent(
    prompt: str,
    settings: Optional[Settings] = None,
    mode: str = "plan",
    cwd: Optional[str] = None,
    add_dirs: Optional[List[str]] = None,
    context: Optional[str] = None,
    timeout: Optional[int] = None,
    on_event: Optional[Callable[[AgentEvent], None]] = None,
) -> AgentResult:
    """Run to completion, collecting events. ``on_event`` is called per event.

    Mirrors ``claude_runner.run_agent``. ``cost_usd`` is None for codex — it
    reports token ``usage`` on the turn.completed event (in the result event's
    ``raw``), not a USD figure, and the model runs via Ollama Cloud where USD
    cost is meaningless anyway. Callers that want token counts can read them
    from the result event's raw.
    """
    events: List[AgentEvent] = []
    output_parts: List[str] = []
    status = "done"
    error = ""
    async for ev in stream_agent(prompt, settings, mode, cwd, add_dirs,
                                 context, timeout):
        events.append(ev)
        if on_event:
            on_event(ev)
        if ev.kind == "text":
            output_parts.append(ev.text)
        elif ev.kind == "result":
            # the final answer was the accumulated text events; result carries
            # no extra text for codex. Stop the stream.
            break
        elif ev.kind == "error":
            error = ev.text
            status = "error"
            break
    output = "\n".join(p for p in output_parts if p).strip()
    if not output and error:
        output = error
    return AgentResult(output=output, status=status, events=events,
                       error=error, cost_usd=None)


def run_agent_sync(prompt: str, **kwargs: Any) -> AgentResult:
    """Blocking wrapper for CLI use."""
    return asyncio.run(run_agent(prompt, **kwargs))