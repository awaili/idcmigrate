"""Agent layer: agentic task runner (claude or codex backend, selected by
``IDC_AGENT_RUNNER``) + the pull-mode executor client + the named-executor
registry.

The runner's public surface (``AgentEvent`` / ``AgentResult`` /
``build_context`` / ``lz_context`` / ``stream_agent`` / ``run_agent`` /
``run_agent_sync``) is identical across backends, so call sites pick the
backend purely via config — no code changes at the CLI or ``/api/agent/run``.
The agent-agnostic helpers + the event/result dataclasses live in
``claude_runner`` and are re-exported here; ``codex_runner`` imports them from
``claude_runner`` too (no duplication). ``stream_agent`` / ``run_agent`` /
``run_agent_sync`` below are thin dispatchers that route to the configured
backend per call (resolving settings, so a config flip takes effect without a
re-import).
"""
from .claude_runner import (
    AgentEvent,
    AgentResult,
    build_context,
    lz_context,
    lz_context_all,
)
from . import claude_runner, codex_runner
from .executor_client import ExecutorClient, get_executor_client, executor_status
from . import executors as registry


def _runner_for(settings):
    """Pick the agentic backend module from ``settings.agent_runner``.

    Resolves settings via ``get_settings()`` when None (so a runtime config
    flip — env or DB overlay — takes effect without re-importing). Falls back
    to the claude backend for any unrecognized value (safe default = prior
    behavior).
    """
    from ..config import get_settings
    s = settings or get_settings()
    name = (getattr(s, "agent_runner", "") or "").strip().lower()
    return codex_runner if name == "codex" else claude_runner


async def stream_agent(prompt, settings=None, **kwargs):
    """Async generator yielding ``AgentEvent``-s; delegates to the configured
    backend (``claude_runner`` or ``codex_runner``). Same signature as each
    backend's ``stream_agent`` — see ``claude_runner.stream_agent`` /
    ``codex_runner.stream_agent``."""
    async for ev in _runner_for(settings).stream_agent(
            prompt, settings=settings, **kwargs):
        yield ev


async def run_agent(prompt, settings=None, **kwargs):
    """Run to completion, collecting events; delegates to the configured
    backend."""
    return await _runner_for(settings).run_agent(
        prompt, settings=settings, **kwargs)


def run_agent_sync(prompt, settings=None, **kwargs):
    """Blocking wrapper for CLI use; delegates to the configured backend."""
    return _runner_for(settings).run_agent_sync(
        prompt, settings=settings, **kwargs)


__all__ = [
    "AgentEvent", "AgentResult", "build_context",
    "lz_context", "lz_context_all",
    "run_agent", "run_agent_sync", "stream_agent",
    "ExecutorClient", "get_executor_client", "executor_status",
    "registry",
]