"""Claude Code CLI agent runner.

Spawns the ``claude`` CLI (``-p`` print mode) as a subprocess to perform
agentic migration tasks: generate Terraform, draft runbooks, verify security
group equivalence, run pre-flight checks, etc. The agent receives a compact
inventory context so it can ground its work in the actual estate.

Modes:
  * ``plan``    (default) — ``--permission-mode plan``: read-only, no side
    effects. Safe to run anytime. Returns analysis/drafts/answers only.
  * ``execute``           — ``--dangerously-skip-permissions``: the agent may
    write files and run shell commands (e.g. ``terraform apply``, ``tencentcloud
    cli``). Opt-in only; the caller must confirm.

Output is streamed as stream-json events so the web UI / CLI can show progress;
the final ``result`` event's text is returned as the task output.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from ..config import Settings, get_settings
from ..core.models import Match, Server, Wave

SYSTEM_PREAMBLE = (
    "You are CloudForge Migration Agent, operating inside an IDC→Tencent-Cloud "
    "migration platform. You receive a compact inventory context. Be concrete "
    "and action-oriented. When asked to generate artifacts (Terraform, scripts, "
    "runbooks), produce production-shaped output with the real Tencent Cloud "
    "resource names from the context. When asked to verify, state PASS/FAIL "
    "with evidence. Prefer Tencent Cloud services: CVM, TKE, CDB, TencentDB "
    "for Redis, EMR, COS, CFS, VPC, Security Group, CLB, CDN."
)


# F8 — Landing Zone archetype context. When the operator asks the agent to
# emit LZ Terraform, this parameterizes it by archetype (corp/online/dmz) so
# the agent emits a per-archetype blueprint (VPC + peering + CAM + SG + tag
# policy + policy-as-code) instead of a one-shot blob. The blueprints are data
# from idc.core.lz; the agent does the actual HCL emit.
def lz_context(archetype: str) -> str:
    """One archetype's LZ blueprint as an agent context block (F8)."""
    from ..core.lz import LZ_BLUEPRINTS, LZ_ARCHETYPES
    if archetype not in LZ_ARCHETYPES:
        return f"(unknown LZ archetype: {archetype})"
    bp = LZ_BLUEPRINTS[archetype]
    return (
        f"LANDING ZONE ARCHETYPE: {archetype}\n"
        f"{bp['summary']}\n"
        f"Blueprint (emit Terraform matching this):\n"
        + json.dumps(bp, ensure_ascii=False, indent=2)
    )


def lz_context_all() -> str:
    """All three archetype blueprints (for a 'generate the full LZ' prompt)."""
    return "\n\n---\n\n".join(lz_context(a) for a in ("corp", "online", "dmz"))


@dataclass
class AgentEvent:
    kind: str            # "text" | "tool" | "status" | "result" | "error"
    text: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "raw": self.raw}


@dataclass
class AgentResult:
    output: str
    status: str          # done | error | timeout
    events: List[AgentEvent] = field(default_factory=list)
    error: str = ""
    cost_usd: Optional[float] = None


def build_context(servers: Optional[List[Server]] = None,
                  matches: Optional[List[Match]] = None,
                  waves: Optional[List[Wave]] = None,
                  focus_server_ids: Optional[List[str]] = None) -> str:
    """Compact, agent-grounding inventory context as a string."""
    from ..core.lz import archetype_for
    parts: List[str] = []
    if servers is not None:
        m_by_id = {m.server_id: m for m in (matches or [])}
        sel = servers
        if focus_server_ids:
            want = set(focus_server_ids)
            sel = [s for s in servers if s.id in want]
        rows = []
        for s in sel[:200]:  # cap context size
            m = m_by_id.get(s.id)
            rows.append({
                "hostname": s.hostname, "role": s.role, "os": s.os,
                "cpu": s.cpu_cores, "mem_gb": s.mem_gb, "disk_gb": s.total_disk_gb,
                "env": s.env, "criticality": s.business_criticality,
                "tags": s.tags, "app_ids": s.app_ids,
                "target": (m.target.product + " " + m.target.spec) if m else None,
                "region": m.target.region if m else None,
                "lz_archetype": archetype_for(s, m),   # F8 placement context
                "utilization": s.utilization.to_dict(),
            })
        parts.append("SERVERS (source estate → proposed Tencent target):\n"
                     + json.dumps(rows, ensure_ascii=False, indent=2))
    if waves is not None:
        ws = [{"name": w.name, "stage": w.stage, "depends_on": w.depends_on,
               "server_count": len(w.server_ids), "rationale": w.rationale}
              for w in waves]
        parts.append("WAVE PLAN:\n" + json.dumps(ws, ensure_ascii=False, indent=2))
    return "\n\n".join(parts) if parts else "(no inventory context attached)"


def _build_cmd(prompt: str, settings: Settings, mode: str,
               cwd: Optional[str], add_dirs: Optional[List[str]]) -> List[str]:
    cmd = [settings.claude_bin, "-p", prompt,
           "--output-format", "stream-json",
           "--verbose",
           "--append-system-prompt", SYSTEM_PREAMBLE]
    if mode == "execute":
        cmd.append("--dangerously-skip-permissions")
    else:
        cmd += ["--permission-mode", "plan"]
    for d in add_dirs or []:
        cmd += ["--add-dir", d]
    return cmd


async def stream_agent(
    prompt: str,
    settings: Optional[Settings] = None,
    mode: str = "plan",
    cwd: Optional[str] = None,
    add_dirs: Optional[List[str]] = None,
    context: Optional[str] = None,
    timeout: Optional[int] = None,
) -> AsyncIterator[AgentEvent]:
    """Async generator yielding AgentEvent-s; final one has kind='result'."""
    settings = settings or get_settings()
    full_prompt = f"{context}\n\n--- TASK ---\n{prompt}" if context else prompt
    cmd = _build_cmd(full_prompt, settings, mode, cwd, add_dirs)
    timeout = timeout or settings.claude_timeout
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=dict(os.environ),
        )
    except FileNotFoundError as e:
        yield AgentEvent("error", text=f"claude binary not found: {settings.claude_bin!r} ({e})")
        return

    async def _drain_stderr() -> str:
        if proc.stderr is None:
            return ""
        return (await proc.stderr.read()).decode(errors="replace")

    try:
        assert proc.stdout is not None
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
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
                continue
            etype = evt.get("type")
            if etype == "assistant":
                for block in (evt.get("message") or {}).get("content", []):
                    if block.get("type") == "text" and block.get("text"):
                        yield AgentEvent("text", text=block["text"], raw=evt)
                    elif block.get("type") == "tool_use":
                        yield AgentEvent("tool", text=f"[tool] {block.get('name')}: "
                                              f"{json.dumps(block.get('input', {}), ensure_ascii=False)[:300]}",
                                         raw=evt)
            elif etype == "result":
                yield AgentEvent("result", text=evt.get("result", ""),
                                 raw=evt)
                return
            elif etype == "system":
                yield AgentEvent("status", text=evt.get("subtype", ""), raw=evt)
    except Exception as e:
        yield AgentEvent("error", text=f"{e!r}")
        return
    await proc.wait()
    err = await _drain_stderr()
    if proc.returncode not in (0, None):
        yield AgentEvent("error", text=f"claude exited {proc.returncode}; stderr: {err[:500]}")


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
    """Run to completion, collecting events. ``on_event`` is called per event."""
    events: List[AgentEvent] = []
    output_parts: List[str] = []
    status = "done"
    error = ""
    cost = None
    async for ev in stream_agent(prompt, settings, mode, cwd, add_dirs,
                                 context, timeout):
        events.append(ev)
        if on_event:
            on_event(ev)
        if ev.kind == "text":
            output_parts.append(ev.text)
        elif ev.kind == "result":
            output_parts.append(ev.text)
            cost = ev.raw.get("total_cost_usd")
            status = "done"
            break
        elif ev.kind == "error":
            error = ev.text
            status = "error" if status == "done" else status
            break
    output = "\n".join(output_parts).strip()
    if not output and error:
        output = error
    return AgentResult(output=output, status=status, events=events,
                       error=error, cost_usd=cost)


def run_agent_sync(prompt: str, **kwargs: Any) -> AgentResult:
    """Blocking wrapper for CLI use."""
    return asyncio.run(run_agent(prompt, **kwargs))