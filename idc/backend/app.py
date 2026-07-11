"""FastAPI backend for idc-migrate.

REST + WebSocket. Reuses ``idc.core`` / ``idc.llm`` / ``idc.agent`` verbatim.
Serves the single-page web UI from ``web/`` at ``/``.

Run: ``python -m idc.backend.app`` or ``idc serve``.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import ROOT, get_settings, load_dotenv
from ..core import open_store, rebuild, run_ingest
from ..core.db import ServerFilter
from ..llm import get_client
from ..agent import build_context, run_agent

load_dotenv()
settings = get_settings()
STORE = open_store(settings.sqlite_path)
LLM = get_client(settings)

# task_id -> asyncio.Queue of agent events (in-memory event bus)
_EVENT_BUS: Dict[str, "asyncio.Queue"] = {}

app = FastAPI(title="idc-migrate", version="0.1.0")


# ---------------------------------------------------------------------------
# request / response schemas
# ---------------------------------------------------------------------------
class IngestReq(BaseModel):
    source: str = "all"


class RebuildReq(BaseModel):
    do_match: bool = True
    do_plan: bool = True


class AskReq(BaseModel):
    question: str
    k: int = 30


class ExplainReq(BaseModel):
    server_id: str


class AgentReq(BaseModel):
    prompt: str
    mode: str = "plan"            # plan | execute
    focus_server_ids: Optional[List[str]] = None
    cwd: Optional[str] = None
    timeout: Optional[int] = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _server_out(s):
    d = s.to_dict()
    m = STORE.get_match(s.id)
    d["match"] = m.to_dict() if m else None
    return d


# ---------------------------------------------------------------------------
# REST: inventory
# ---------------------------------------------------------------------------
@app.get("/api/stats")
def stats():
    return STORE.stats()


@app.get("/api/servers")
def list_servers(role: Optional[str] = None, env: Optional[str] = None,
                 status: Optional[str] = None, source_type: Optional[str] = None,
                 os: Optional[str] = None, criticality: Optional[str] = None,
                 cluster: Optional[str] = None, datacenter: Optional[str] = None,
                 target_product: Optional[str] = None, wave_id: Optional[str] = None,
                 q: Optional[str] = None,
                 util_cpu_min: Optional[float] = None, util_mem_min: Optional[float] = None,
                 util_disk_min: Optional[float] = None,
                 conf_min: Optional[float] = None, conf_max: Optional[float] = None,
                 page: int = 1, page_size: int = 50,
                 order_by: str = "hostname", order_dir: str = "asc"):
    """Paginated, filtered, faceted server query (scales to 15K+)."""
    f = ServerFilter(role=role, env=env, status=status, source_type=source_type,
                     os=os, criticality=criticality, cluster=cluster, datacenter=datacenter,
                     target_product=target_product, wave_id=wave_id, q=q,
                     util_cpu_min=util_cpu_min, util_mem_min=util_mem_min,
                     util_disk_min=util_disk_min, conf_min=conf_min, conf_max=conf_max)
    res = STORE.query_servers(f, page=page, page_size=min(page_size, 500),
                              order_by=order_by, order_dir=order_dir)
    items = [_server_out(s) for s in res["items"]]
    return {"items": items, "total": res["total"], "page": res["page"],
            "page_size": res["page_size"], "facets": res["facets"]}


@app.get("/api/aggregations")
def aggregations():
    """Dashboard tiles + distribution charts over the whole estate."""
    return STORE.aggregations()


@app.get("/api/servers.csv")
def servers_csv(role: Optional[str] = None, env: Optional[str] = None,
                status: Optional[str] = None, source_type: Optional[str] = None,
                os: Optional[str] = None, criticality: Optional[str] = None,
                cluster: Optional[str] = None, target_product: Optional[str] = None,
                wave_id: Optional[str] = None, q: Optional[str] = None,
                util_cpu_min: Optional[float] = None, util_mem_min: Optional[float] = None,
                util_disk_min: Optional[float] = None,
                conf_min: Optional[float] = None, conf_max: Optional[float] = None,
                order_by: str = "hostname"):
    """Stream the filtered server set as CSV (no full-load)."""
    f = ServerFilter(role=role, env=env, status=status, source_type=source_type,
                     os=os, criticality=criticality, cluster=cluster,
                     target_product=target_product, wave_id=wave_id, q=q,
                     util_cpu_min=util_cpu_min, util_mem_min=util_mem_min,
                     util_disk_min=util_disk_min, conf_min=conf_min, conf_max=conf_max)
    return StreamingResponse(STORE.stream_servers_csv(f, order_by=order_by),
                             media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=servers.csv"})


@app.get("/api/servers/{sid}")
def get_server(sid: str):
    s = STORE.get_server(sid)
    if not s:
        raise HTTPException(404, "server not found")
    return _server_out(s)


@app.get("/api/workloads")
def list_workloads():
    return [w.to_dict() for w in STORE.list_workloads()]


@app.get("/api/matches")
def list_matches():
    out = []
    servers = {s.id: s for s in STORE.list_all_servers()}
    for m in STORE.list_matches():
        d = m.to_dict()
        d["server"] = servers[m.server_id].hostname if m.server_id in servers else m.server_id
        out.append(d)
    return out


@app.get("/api/waves")
def list_waves():
    servers = {s.id: s for s in STORE.list_servers(limit=200000)}
    out = []
    for w in STORE.list_waves():
        d = w.to_dict()
        d["members"] = [servers[sid].hostname for sid in w.server_ids if sid in servers]
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# REST: LLM-driven wave planning
# ---------------------------------------------------------------------------
class PlanProposeReq(BaseModel):
    demand: str
    scope: str = "all"                 # "all" | "filter"
    # optional filter (used when scope=="filter") — same fields as /api/servers
    role: Optional[str] = None
    env: Optional[str] = None
    os: Optional[str] = None
    source_type: Optional[str] = None
    criticality: Optional[str] = None
    cluster: Optional[str] = None
    target_product: Optional[str] = None
    q: Optional[str] = None
    util_mem_min: Optional[float] = None
    conf_max: Optional[float] = None


class PlanApplyReq(BaseModel):
    waves: List[dict]


@app.post("/api/plan/propose")
def plan_propose(req: PlanProposeReq):
    """Ask the LLM for a wave policy from the demand, instantiate it (dry-run).
    Does NOT persist — call /api/plan/apply to save."""
    from ..core.llm_plan import build_from_policy, validate_plan
    from ..llm import get_planner
    servers = STORE.list_all_servers()
    wls = STORE.list_workloads()
    matches = STORE.list_matches()
    scope_ids = None
    if req.scope == "filter":
        f = ServerFilter(role=req.role, env=req.env, os=req.os,
                         source_type=req.source_type, criticality=req.criticality,
                         cluster=req.cluster, target_product=req.target_product,
                         q=req.q, util_mem_min=req.util_mem_min, conf_max=req.conf_max)
        scope_ids = set(STORE.query_server_ids(f))
    res = get_planner(settings).propose(req.demand, servers, wls, matches, scope_ids)
    pol = res.get("policy")
    if not pol:
        return {"ok": False, "errors": res.get("errors"), "raw": res.get("raw"),
                "policy": None, "waves": [], "validation": None}
    rep = build_from_policy(pol, servers, wls, matches, scope_ids)
    by_id = {s.id: s for s in servers}
    val = validate_plan(rep.waves, servers)
    return {
        "ok": not res.get("errors"),
        "policy": {"notes": pol.notes,
                   "stages": [{k: v for k, v in vars(s).items() if v not in ([], "", None, True) or k in ("label", "stage", "group_by", "use_app_deps")}
                              for s in pol.stages]},
        "waves": [w.to_dict() for w in rep.waves],
        "wave_hostnames": [{**w.to_dict(), "members": [by_id[sid].hostname for sid in w.server_ids if sid in by_id]}
                           for w in rep.waves[:200]],
        "stats": {"total": rep.total, "assigned": len(rep.assigned),
                  "waves": len(rep.waves), "unassigned": len(rep.unassigned)},
        "validation": val,
        "errors": res.get("errors") + rep.errors,
        "warnings": rep.warnings,
        "raw": res.get("raw"),
    }


@app.post("/api/plan/apply")
def plan_apply(req: PlanApplyReq):
    """Validate and persist a proposed plan (replaces existing waves)."""
    from ..core.llm_plan import validate_plan
    from ..core.models import Wave
    servers = STORE.list_all_servers()
    waves = [Wave.from_dict(w) for w in req.waves]
    val = validate_plan(waves, servers)
    if val["errors"]:
        return {"ok": False, "validation": val, "applied": False}
    from ..core.llm_plan import apply_plan
    apply_plan(STORE, waves)
    return {"ok": True, "applied": True, "validation": val, "waves": len(waves)}


# ---------------------------------------------------------------------------
# REST: pipeline ops
# ---------------------------------------------------------------------------
@app.get("/api/ingest/runs")
def ingest_runs():
    return STORE.list_ingest_runs()


@app.post("/api/ingest")
def ingest(req: IngestReq):
    runs = run_ingest(STORE, settings, req.source)
    return [r.to_dict() for r in runs]


@app.post("/api/rebuild")
def rebuild_derived(req: RebuildReq):
    return rebuild(STORE, settings, do_match=req.do_match, do_plan=req.do_plan)


# ---------------------------------------------------------------------------
# REST: LLM
# ---------------------------------------------------------------------------
@app.post("/api/ask")
async def ask(req: AskReq):
    servers = STORE.list_all_servers()
    answer = LLM.ask(req.question, servers, STORE.list_matches(), k=req.k)
    return {"question": req.question, "answer": answer}


@app.post("/api/explain")
def explain(req: ExplainReq):
    s = STORE.get_server(req.server_id)
    if not s:
        raise HTTPException(404, "server not found")
    m = STORE.get_match(req.server_id)
    if not m:
        raise HTTPException(404, "no match for server")
    return {"server_id": req.server_id, "explanation": LLM.explain_match(s, m),
            "rule_rationale": m.rationale}


@app.post("/api/right-size")
def right_size(req: ExplainReq):
    s = STORE.get_server(req.server_id)
    if not s:
        raise HTTPException(404, "server not found")
    m = STORE.get_match(req.server_id)
    if not m:
        raise HTTPException(404, "no match for server")
    return {"server_id": req.server_id, "advice": LLM.right_size(s, m)}


# ---------------------------------------------------------------------------
# REST + WebSocket: Claude Code agent
# ---------------------------------------------------------------------------
@app.post("/api/agent")
async def start_agent(req: AgentReq):
    """Create an agent task and run it in the background. Stream via WS."""
    from ..core.models import AgentTask, _now
    t = AgentTask(prompt=req.prompt, mode=req.mode, status="pending")
    STORE.create_task(t)
    _EVENT_BUS[t.id] = asyncio.Queue()

    servers = STORE.list_all_servers()
    matches = STORE.list_matches()
    waves = STORE.list_waves()
    context = build_context(servers, matches, waves,
                            focus_server_ids=req.focus_server_ids)

    async def _runner():
        STORE.update_task(t.id, status="running")
        def on_event(ev):
            # ev is AgentEvent; put non-blocking
            try:
                _EVENT_BUS[t.id].put_nowait({"kind": ev.kind, "text": ev.text})
            except Exception:
                pass
        try:
            res = await run_agent(req.prompt, settings=settings, mode=req.mode,
                                  cwd=req.cwd, context=context,
                                  timeout=req.timeout, on_event=on_event)
            STORE.update_task(t.id, status=res.status, output=res.output,
                              error=res.error, finished_at=_now())
        except Exception as e:
            STORE.update_task(t.id, status="error", error=repr(e), finished_at=_now())
        finally:
            await _EVENT_BUS[t.id].put(None)  # sentinel: stream done
            _EVENT_BUS.pop(t.id, None)

    asyncio.create_task(_runner())
    return {"task_id": t.id, "status": "pending", "ws": f"/ws/agent/{t.id}"}


@app.get("/api/tasks")
def list_tasks():
    return STORE.list_tasks()


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    t = STORE.get_task(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    return t


@app.websocket("/ws/agent/{task_id}")
async def ws_agent(ws: WebSocket, task_id: str):
    await ws.accept()
    t = STORE.get_task(task_id)
    if not t:
        await ws.send_json({"kind": "error", "text": "unknown task"})
        await ws.close()
        return
    # if already finished, just send the stored output
    if t["status"] in ("done", "error", "timeout") and task_id not in _EVENT_BUS:
        await ws.send_json({"kind": "result" if t["status"] == "done" else "error",
                            "text": t["output"] or t["error"]})
        await ws.close()
        return
    q = _EVENT_BUS.get(task_id)
    if q is None:
        await ws.send_json({"kind": "error", "text": "task stream not available"})
        await ws.close()
        return
    try:
        while True:
            ev = await q.get()
            if ev is None:
                break
            await ws.send_json(ev)
        # final status
        t = STORE.get_task(task_id)
        await ws.send_json({"kind": "result" if t and t["status"] == "done" else "error",
                            "text": (t["output"] if t else "") or (t["error"] if t else "")})
    except WebSocketDisconnect:
        pass
    await ws.close()


# ---------------------------------------------------------------------------
# static web UI
# Served via explicit HTTP routes (not a catch-all Mount at "/") so that
# WebSocket scopes for /ws/agent/... never reach StaticFiles (which asserts
# scope type == http).
# ---------------------------------------------------------------------------
WEB_DIR = ROOT / "web"
if (WEB_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(WEB_DIR / "assets")), name="assets")


@app.get("/")
def _index():
    idx = WEB_DIR / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    return JSONResponse({"name": "idc-migrate", "web": "web/index.html not built yet",
                         "api": [r.path for r in app.routes if hasattr(r, "path") and r.path.startswith("/api")]})


@app.on_event("shutdown")
def _shutdown():
    STORE.close()


def main():
    import uvicorn
    uvicorn.run("idc.backend.app:app", host="0.0.0.0", port=8010,
                reload=False, log_level="info")


if __name__ == "__main__":
    main()