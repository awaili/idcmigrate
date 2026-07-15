"""Pull-dispatch helpers for the external agent executor.

The executor exposes **nothing** to the network — idc-migrate cannot reach it,
so it cannot push triggers to it (the old ``POST {IDC_EXECUTOR_URL}/v1/scan``
direction is gone). Instead idc-migrate **enqueues** a task in the
``executor_tasks`` table and the executor **pulls** it via
``POST /api/executor/tasks/claim``; results come back over the unchanged
``/api/*`` feedback surface (``PUT /api/code-profiles/{app_id}``,
``POST /api/change-jobs``, ...). See ``docs/agent-executor.md``.

This module keeps only what idc-migrate still needs on its side:

* ``ExecutorClient.cb(path)`` — builds the per-task feedback (``callback``) URL
  from ``IDC_PUBLIC_URL`` so the executor knows where to push results back. The
  trigger endpoints bake this into the task payload.
* ``executor_status(settings)`` — a no-probe pull-mode status for ``doctor`` /
  the web indicator (idc-migrate cannot probe an executor that exposes nothing;
  liveness is inferred from claim activity on the queue, reported by the
  ``/api/executor/status`` endpoint, not here).

All request-direction action methods (``scan`` / ``comb`` / ``modify`` / ...)
were removed: there is no outbound call to the executor anymore.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..config import Settings


class ExecutorClient:
    """A thin helper for building the feedback (``callback``) URL baked into each
    task payload. It no longer makes any outbound call — the executor pulls."""

    def __init__(self, settings: Settings):
        # this server's own public base (push-back target). Used to build the
        # per-task `callback` so a remote executor can push results back here
        # without a separate callback-base env.
        self.public_url = (getattr(settings, "public_url", "") or "").rstrip("/")

    @property
    def configured(self) -> bool:
        """A usable callback can be built iff IDC_PUBLIC_URL is set. When unset,
        ``cb()`` returns "" and the executor falls back to its own
        ``IDC_CALLBACK_BASE`` (back-compat)."""
        return bool(self.public_url)

    def cb(self, path: str) -> str:
        """Build the per-task ``callback`` URL for an action's feedback push.

        ``path`` is the idc-migrate feedback route (e.g.
        ``/api/code-profiles/{app_id}``). Returns ``{public_url}{path}`` when
        IDC_PUBLIC_URL is set, else ``""`` (the executor then falls back to its
        own callback-base env — back-compat)."""
        return f"{self.public_url}{path}" if self.public_url else ""


def get_executor_client(settings: Settings) -> ExecutorClient:
    return ExecutorClient(settings)


def executor_status(settings: Settings) -> Dict[str, Any]:
    """Pull-mode executor status for ``doctor`` / the web indicator.

    idc-migrate cannot probe the executor (it exposes nothing), so there is no
    ``/health`` round-trip. ``reachable`` is left ``None`` (unknown from this
    side); real liveness is "an executor is claiming tasks", reported by the
    ``/api/executor/status`` endpoint from the queue, not from a network probe.
    Never raises."""
    base = (settings.executor_url or "").rstrip("/")
    return {
        "configured": bool(base),
        "enabled": settings.executor_enabled,
        "url": base or None,
        "token_set": bool(settings.executor_token),
        "public_url_set": bool(getattr(settings, "public_url", "") or ""),
        # pull mode: no outbound probe possible
        "reachable": None,
        "status_code": None,
        "detail": "pull mode — executor pulls /api/executor/tasks/claim; "
                  "no outbound probe (executor exposes nothing)",
        "version": None,
    }