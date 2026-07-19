"""Regression: the agent task (Copilot tab ``/api/agent``) read-back path.

The agent task lives in the ``agent_tasks`` table; the executor pull-dispatch
task lives in ``executor_tasks``. Both originally had a ``Store.get_task`` /
``Store.list_tasks`` method, and the executor one (defined later in the class)
SILENTLY shadowed the agent one — so ``/api/tasks/{id}`` and the agent WebSocket
read ``executor_tasks`` instead of ``agent_tasks`` and the agent task was never
found (404 / "unknown task"), even though the row existed. The Copilot tab's
agent had been broken since the executor pull-dispatch layer was added.

Fix: the agent side is ``get_agent_task`` / ``list_agent_tasks`` (distinct
names), and the agent endpoints/WS use those. This test pins the split so a
future name re-collision is caught.
"""
import uuid

import pytest

pytest.importorskip("idc.core.db")
from idc.config import get_settings
from idc.core.db import Store


def _tid() -> str:
    return f"agentreadback-{uuid.uuid4().hex[:8]}"


def test_get_agent_task_reads_the_agent_table():
    """An agent task inserted into agent_tasks is found by get_agent_task (the
    agent surface), NOT by get_task (the executor surface). Pre-fix both methods
    were named get_task and the executor one shadowed the agent one, so the
    agent task read 404'd."""
    s = Store(get_settings().db_url)
    tid = _tid()
    cols = "id,prompt,mode,status,created_at,finished_at,output,error"
    ins = f"INSERT INTO agent_tasks({cols}) VALUES(?,?,?,?,?,?,?,?)"
    try:
        with s.tx() as cur:
            cur.execute(s._x(ins), (tid, "hi", "plan", "done", "t", "", "", ""))
        # the AGENT surface sees it
        got = s.get_agent_task(tid)
        assert got is not None and got["id"] == tid and got["prompt"] == "hi"
        # the EXECUTOR surface does NOT (it reads executor_tasks) — the two are
        # distinct; this asserts the names no longer collide
        assert s.get_task(tid) is None
    finally:
        with s.tx() as cur:
            cur.execute(s._x("DELETE FROM agent_tasks WHERE id=?"), (tid,))


def test_list_agent_tasks_reads_the_agent_table():
    """list_agent_tasks returns agent_tasks rows (the /api/tasks list surface),
    not executor_tasks."""
    s = Store(get_settings().db_url)
    tid = _tid()
    cols = "id,prompt,mode,status,created_at,finished_at,output,error"
    ins = f"INSERT INTO agent_tasks({cols}) VALUES(?,?,?,?,?,?,?,?)"
    try:
        with s.tx() as cur:
            cur.execute(s._x(ins), (tid, "hi", "plan", "done", "t", "", "", ""))
        ids = [t["id"] for t in s.list_agent_tasks(limit=200)]
        assert tid in ids
    finally:
        with s.tx() as cur:
            cur.execute(s._x("DELETE FROM agent_tasks WHERE id=?"), (tid,))